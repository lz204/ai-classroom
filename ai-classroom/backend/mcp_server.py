# -*- coding: utf-8 -*-
"""最小 MCP Server（Model Context Protocol）：stdio 上换行分隔的 JSON-RPC 2.0。

启动（backend 目录下）：
    python -m mcp_server

协议要点：
- stdout 只写协议数据（每行一条 JSON），日志一律写 stderr；
- 支持 initialize / notifications/initialized / tools/list / tools/call / ping；
- 暴露 3 个工具：calculate（ast 安全求值）、get_current_time、text_stats；
- 协议/参数错误返回 JSON-RPC error，工具执行失败返回带 isError 的 result（不让进程崩溃）。

只使用 Python 标准库，不引入任何新依赖。
"""
import ast
import json
import math
import re
import sys
import traceback
from datetime import datetime, timezone

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ai-classroom-mcp", "version": "1.0.0"}

# 表达式求值限制（防止超长表达式 / 巨大幂运算拖垮进程）
_MAX_EXPRESSION_LEN = 200
_MAX_POW_EXPONENT = 100

_ALLOWED_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARY_OPS = (ast.UAdd, ast.USub)

# 中文/全角符号归一化，方便用户直接输入 "3×（2+1）"
_EXPRESSION_TRANSLATION = str.maketrans(
    {"×": "*", "÷": "/", "（": "(", "）": ")", "－": "-", "＋": "+", "^": "**"}
)

# 词数统计：CJK 单字计一个词，连续英文/数字串计一个词
_RE_WORD = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")


class JsonRpcError(Exception):
    """JSON-RPC 协议层错误（返回 error 对象，不写 stdout 日志）"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ExpressionError(ValueError):
    """计算表达式非法（只允许数字/四则/幂/括号）"""


# ============ 安全求值（AST 白名单，不使用 eval 执行任意代码） ============
def evaluate_expression(expression: str) -> int | float:
    """安全求值四则/幂/括号表达式；非法输入抛 ExpressionError，除零抛 ZeroDivisionError。"""
    if not isinstance(expression, str):
        raise ExpressionError("表达式必须是字符串")
    expression = expression.strip().translate(_EXPRESSION_TRANSLATION)
    if not expression:
        raise ExpressionError("表达式为空")
    if len(expression) > _MAX_EXPRESSION_LEN:
        raise ExpressionError(f"表达式过长（>{_MAX_EXPRESSION_LEN} 字符）")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ExpressionError("表达式语法错误") from None

    def _eval(node) -> int | float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            # bool 是 int 子类，必须显式拒绝；只接受数字常量
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExpressionError("只允许数字常量")
            return node.value
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BIN_OPS):
                raise ExpressionError("只允许四则运算与幂运算")
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Pow):
                # 限制指数规模，避免 9**9**9 之类的资源耗尽
                if abs(right) > _MAX_POW_EXPONENT:
                    raise ExpressionError(f"幂指数过大（|指数|>{_MAX_POW_EXPONENT}）")
                return left**right
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            raise ExpressionError("不支持的运算符")
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARY_OPS):
                raise ExpressionError("只允许正负号")
            value = _eval(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        # 其余节点（Name/Call/Attribute/Subscript/...）一律拒绝，杜绝任意代码执行
        raise ExpressionError("表达式包含不允许的语法")

    value = _eval(tree)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExpressionError("计算结果不是有限数")
        if value.is_integer() and abs(value) < 1e15:
            return int(value)  # 3.0 -> 3，输出更友好
    return value


def format_number(value: int | float) -> str:
    """数字转展示文本：整数直出，浮点去掉多余尾零。"""
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


def calculate_text(expression: str) -> str:
    """calculate 工具的文本结果；非法表达式返回 "ERROR: ..." 文本（不抛出）。"""
    try:
        return format_number(evaluate_expression(expression))
    except ExpressionError as exc:
        return f"ERROR: 表达式非法：{exc}"
    except ZeroDivisionError:
        return "ERROR: 除数不能为 0"
    except (OverflowError, ValueError, TypeError):
        return "ERROR: 计算失败（数值超出范围或类型非法）"


def text_stats_text(text: str) -> str:
    """text_stats 工具的文本结果：字符数/词数/行数。"""
    chars = len(text)
    words = len(_RE_WORD.findall(text))
    lines = len(text.splitlines())
    return f"字符数：{chars}；词数：{words}；行数：{lines}"


# ============ 工具定义（inputSchema 必须准确） ============
_TOOLS: dict[str, dict] = {
    "calculate": {
        "description": "安全计算数学表达式，支持数字、四则运算、幂（** 或 ^）与括号。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "待计算的表达式，例如 12*3+5 或 (1+2)**3",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        "handler": lambda args: calculate_text(args.get("expression", "")),
    },
    "get_current_time": {
        "description": "获取当前本地时间（ISO 8601 格式，含时区偏移）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": lambda args: datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        ),
    },
    "text_stats": {
        "description": "统计一段文本的字符数、词数与行数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待统计的文本"}
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": lambda args: text_stats_text(str(args.get("text", ""))),
    },
}


def list_tools() -> list[dict]:
    """MCP tools/list 结果：name/description/inputSchema。"""
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for name, spec in _TOOLS.items()
    ]


def call_tool(name: str, arguments: dict) -> dict:
    """执行工具并返回 MCP tools/call 的 result；未知工具抛 JsonRpcError。

    工具参数按 inputSchema 白名单取值（防注入），执行异常统一转成 isError 文本结果。
    """
    spec = _TOOLS.get(name)
    if spec is None:
        raise JsonRpcError(-32602, f"未知工具: {name}")
    if not isinstance(arguments, dict):
        arguments = {}
    schema = spec["inputSchema"]
    properties = schema.get("properties") or {}
    # 必填参数校验（按 inputSchema.required）
    for field in schema.get("required") or []:
        if field not in arguments:
            raise JsonRpcError(-32602, f"工具 {name} 缺少必填参数: {field}")
    # 只保留 schema 声明的字段，并做基本类型收敛（多余字段丢弃，防注入）
    clean: dict = {}
    for field, field_schema in properties.items():
        if field not in arguments:
            continue
        value = arguments[field]
        expected = (field_schema or {}).get("type")
        if expected == "string" and not isinstance(value, str):
            value = str(value)
        clean[field] = value
    try:
        text = spec["handler"](clean)
        is_error = isinstance(text, str) and text.startswith("ERROR:")
    except Exception:  # 工具内部异常不允许打崩 server（错误细节写 stderr）
        traceback.print_exc(file=sys.stderr)
        text, is_error = "ERROR: 工具执行异常", True
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# ============ JSON-RPC 处理 ============
def _write_message(message: dict, stream=None):
    """向 stdout 写一条 JSON-RPC 消息（stdout 只允许协议数据）"""
    out = stream if stream is not None else sys.stdout
    out.write(json.dumps(message, ensure_ascii=False) + "\n")
    out.flush()


def _error_response(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_request(request) -> dict | None:
    """处理单条 JSON-RPC 请求/通知；通知返回 None（不写响应）。"""
    if not isinstance(request, dict):
        return _error_response(None, -32600, "Invalid Request")
    request_id = request.get("id")
    is_notification = "id" not in request
    method = request.get("method")
    params = request.get("params")
    if not isinstance(params, dict):
        params = {}

    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None  # 通知不需要响应
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list_tools()}
        elif method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                raise JsonRpcError(-32602, "tools/call 缺少 name 参数")
            result = call_tool(tool_name, params.get("arguments"))
        else:
            raise JsonRpcError(-32601, f"Method not found: {method}")
    except JsonRpcError as exc:
        return None if is_notification else _error_response(request_id, exc.code, exc.message)
    except Exception as exc:  # 兜底：内部错误不暴露堆栈给客户端
        traceback.print_exc(file=sys.stderr)
        return (
            None
            if is_notification
            else _error_response(request_id, -32603, f"Internal error: {type(exc).__name__}")
        )

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(stdin=None, stdout=None) -> int:
    """stdio 主循环：逐行读取 JSON-RPC、写响应，直到 EOF。"""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            _write_message(_error_response(None, -32700, "Parse error"), out_stream)
            continue
        # 兼容 JSON-RPC batch（数组）；通知不产生响应
        if isinstance(payload, list):
            responses = []
            for item in payload:
                response = _handle_request(item)
                if response is not None:
                    responses.append(response)
            if responses:
                _write_message(responses, out_stream)
            continue
        response = _handle_request(payload)
        if response is not None:
            _write_message(response, out_stream)
    return 0


def main() -> int:
    # 统一 stdio 编码；reconfigure 在 Python 3.7+ 可用，失败则保持原样
    for stream, kwargs in (
        (sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
        (sys.stdout, {"encoding": "utf-8"}),
        (sys.stderr, {"encoding": "utf-8", "errors": "replace"}),
    ):
        try:
            stream.reconfigure(**kwargs)
        except Exception:
            pass
    print(f"[mcp_server] {SERVER_INFO['name']} v{SERVER_INFO['version']} started", file=sys.stderr, flush=True)
    try:
        return serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
