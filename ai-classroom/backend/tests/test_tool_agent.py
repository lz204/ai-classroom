# -*- coding: utf-8 -*-
"""Tool-Use Agent / MCP 测试：不需要真实 API Key、不需要向量索引、不需要 Redis。

运行方式（backend 目录下）：
    ..\\.venv\\Scripts\\python.exe -X utf8 tests\\test_tool_agent.py

覆盖：
1. calculate 安全求值（AST 白名单）与 3 个本地工具；
2. Mock LLM 触发 tool_call -> ReAct 循环产出最终答案 + trace（计算 / 知识库检索两类任务）；
3. max_steps 生效、连续重复动作检测；
4. 工具参数非法 / 工具异常 / 检索器异常不崩溃、不暴露堆栈；
5. MCP server 独立进程 roundtrip（connect/list_tools/call_tool/未知工具/非法表达式）；
6. MCP 不可用时 ToolAgent 自动降级本地工具（坏命令 / connect 抛异常 / ENABLE_MCP=False）。
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 测试隔离：不加载真实向量索引、不连 Redis；MCP 默认启用（连不上会自动 skip/降级）
os.environ["VECTOR_INDEX_PATH"] = str(BACKEND_DIR / "_no_index_for_tests")
os.environ["REDIS_URL"] = ""
os.environ["ENABLE_MCP"] = "true"

import config  # noqa: E402
import llm_client  # noqa: E402

# 强制 Mock（若 .env 配了真实 Key，测试也不消耗额度）
llm_client.LLM_API_KEY = ""

from llm_client import MockLLM  # noqa: E402
from mcp_client import MCPClient  # noqa: E402
from mcp_server import ExpressionError, evaluate_expression  # noqa: E402
from tool_agent import ToolAgent  # noqa: E402


# ---------- 测试替身 ----------
class FakeDoc:
    def __init__(self, content: str, chapter: str = ""):
        self.page_content = content
        self.metadata = {"chapter": chapter}


class FakeRetriever:
    """固定返回一段带章节引用的检索结果，避免加载真实向量索引。"""

    def retrieve(self, query: str, top_k: int = 4) -> list:
        return [FakeDoc("极限的定义：函数在某点极限存在与函数在该点是否有定义无关。", "第2章")][:top_k]

    @staticmethod
    def format_context(docs: list) -> str:
        return "\n".join(f"[{doc.metadata.get('chapter', '')}] {doc.page_content}" for doc in docs)


class BrokenRetriever:
    def retrieve(self, query: str, top_k: int = 4) -> list:
        raise RuntimeError("索引损坏（测试模拟）")

    @staticmethod
    def format_context(docs: list) -> str:
        return ""


class FakeUnavailableMCP:
    """模拟 MCP 不可用（不真正启动子进程，用于纯本地工具测试）。"""

    async def connect(self) -> bool:
        return False

    async def list_tools(self) -> list:
        return []

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        return "ERROR: MCP 未连接"

    async def aclose(self):
        return None


class ExplodingMCP:
    """connect 直接抛异常：验证 ToolAgent 的异常降级路径。"""

    async def connect(self) -> bool:
        raise RuntimeError("模拟 MCP 进程启动失败")

    async def list_tools(self) -> list:
        return []

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        return "ERROR: MCP 未连接"

    async def aclose(self):
        return None


def _make_agent(llm=None, retriever=None, mcp_client=None) -> ToolAgent:
    return ToolAgent(
        llm=llm or MockLLM(),
        retriever=retriever if retriever is not None else FakeRetriever(),
        mcp_client=mcp_client if mcp_client is not None else FakeUnavailableMCP(),
    )


# ---------- 1. 安全求值 ----------
def test_evaluate_expression_safe():
    async def _run():
        assert evaluate_expression("12*3+5") == 41
        assert evaluate_expression("(1+2)*3") == 9
        assert evaluate_expression("2**10") == 1024
        assert evaluate_expression("2^10") == 1024  # ^ 归一化为幂
        assert evaluate_expression("3×（2+1）") == 9  # 全角符号归一化
        assert evaluate_expression("10/4") == 2.5
        for bad in ("__import__('os')", "1+", "a+b", "().__class__", "9**9**9", ""):
            try:
                evaluate_expression(bad)
            except ExpressionError:
                pass
            else:
                raise AssertionError(f"非法表达式应被拒绝: {bad!r}")
        try:
            evaluate_expression("1/0")
        except ZeroDivisionError:
            pass
        else:
            raise AssertionError("除零应抛 ZeroDivisionError，由调用方转 ERROR 文本")

    asyncio.run(_run())
    print("  OK calculate 安全求值（四则/幂/括号，拒绝代码注入与超大幂）")


# ---------- 2. 本地工具 ----------
def test_local_tools():
    async def _run():
        agent = _make_agent()

        tools = agent.list_tools()
        names = {tool["function"]["name"] for tool in tools}
        assert {"calculate", "search_knowledge_base", "text_stats"} <= names
        for tool in tools:
            assert tool["type"] == "function"
            assert tool["function"]["parameters"]["type"] == "object"
        assert agent.mcp_connected is False

        assert await agent._execute_tool("calculate", {"expression": "12*3+5"}) == "41"
        assert await agent._execute_tool("calculate", {"expression": "2**10"}) == "1024"
        bad = await agent._execute_tool("calculate", {"expression": "__import__('os')"})
        assert bad.startswith("ERROR"), bad
        assert "Traceback" not in bad, "错误信息不能暴露内部堆栈"

        stats = await agent._execute_tool("text_stats", {"text": "ab cd\n中文"})
        assert "字符数：8" in stats and "词数：4" in stats and "行数：2" in stats, stats

        context = await agent._execute_tool("search_knowledge_base", {"query": "极限", "top_k": 2})
        assert "极限" in context and "第2章" in context, context

        assert (await agent._execute_tool("no_such_tool", {})).startswith("ERROR: 未知工具")
        # 参数白名单：schema 之外的字段必须被丢弃（防注入）
        cleaned = agent._sanitize_arguments("calculate", {"expression": "1+1", "evil": "rm -rf /"})
        assert cleaned == {"expression": "1+1"}, cleaned
        # 类型不合法的字段被丢弃
        assert agent._sanitize_arguments("calculate", {"expression": 123}) == {}

    asyncio.run(_run())
    print("  OK 本地工具 calculate / text_stats / search_knowledge_base + 参数白名单")


# ---------- 3. Mock LLM 触发 tool_call -> 最终回答 + trace ----------
def test_mock_tool_call_flow():
    async def _run():
        agent = _make_agent()

        calc = await agent.ask("帮我计算 12*3+5", use_mcp=False)
        print("  [计算任务] answer =", calc["answer"].replace("\n", " / "))
        print("  [计算任务] trace  =", json.dumps(calc["trace"], ensure_ascii=False))
        assert "41" in calc["answer"], calc
        assert calc["degraded"] is False and calc["steps"] == 2, calc
        assert len(calc["trace"]) == 1
        entry = calc["trace"][0]
        assert entry["step"] == 1 and entry["tool"] == "calculate"
        assert entry["args"] == {"expression": "12*3+5"}, entry["args"]
        assert entry["observation"] == "41"
        assert entry["elapsed_ms"] >= 0
        assert calc["mcp_connected"] is False
        assert calc["tools_used"] == ["calculate"]

        kb = await agent.ask("请解释极限与连续的关系", use_mcp=False)
        print("  [检索任务] answer =", kb["answer"].replace("\n", " / "))
        print("  [检索任务] trace  =", json.dumps(kb["trace"], ensure_ascii=False))
        assert kb["degraded"] is False and kb["steps"] == 2
        assert len(kb["trace"]) == 1 and kb["trace"][0]["tool"] == "search_knowledge_base"
        assert "极限的定义" in kb["answer"] and "第2章" in kb["answer"], kb

        direct = await agent.ask("你好，请简单介绍一下你自己", use_mcp=False)
        assert direct["trace"] == [] and direct["steps"] == 1 and direct["degraded"] is False

    asyncio.run(_run())
    print("  OK Mock LLM 触发 tool_call -> ReAct 最终答案 + trace（计算/检索两类任务）")


# ---------- 4. max_steps 生效 ----------
def test_max_steps():
    class AlwaysToolLLM:
        """每一步都返回不同的工具调用（避免触发重复检测），验证 max_steps 截断。"""

        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, messages, tools=None):
            self.calls += 1
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{self.calls}",
                        "name": "calculate",
                        "arguments": json.dumps({"expression": f"{self.calls}+1"}),
                    }
                ],
            }

    async def _run():
        agent = _make_agent(llm=AlwaysToolLLM())
        result = await agent.ask("随便算点东西", max_steps=2, use_mcp=False)
        assert result["degraded"] is True, result
        assert result["steps"] == 2, result
        assert len(result["trace"]) == 2, result
        assert "最大工具调用步数" in result["answer"], result["answer"]

    asyncio.run(_run())
    print("  OK max_steps=2 生效（降级返回已有信息）")


# ---------- 5. 连续重复动作检测 ----------
def test_repeat_detection():
    class SameToolLLM:
        async def chat_with_tools(self, messages, tools=None):
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_same",
                        "name": "calculate",
                        "arguments": json.dumps({"expression": "1+1"}),
                    }
                ],
            }

    async def _run():
        agent = _make_agent(llm=SameToolLLM())
        result = await agent.ask("随便", max_steps=6, use_mcp=False)
        assert result["degraded"] is True, result
        assert len(result["trace"]) == 2, f"第 3 次重复应在执行前被拦截: {result['trace']}"
        assert "重复调用" in result["answer"], result["answer"]

    asyncio.run(_run())
    print("  OK 同一 (tool,args) 连续重复 3 次 -> 停止并降级")


# ---------- 6. 工具异常 / 参数非法不崩溃 ----------
def test_tool_errors_no_crash():
    class BadArgsLLM:
        """第一次返回坏 JSON 参数，第二次（已带 tool 结果）返回最终答案。"""

        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_bad", "name": "calculate", "arguments": "{not-json"}
                    ],
                }
            return {"content": "最终答案：已根据错误信息继续处理。", "tool_calls": []}

    class UnknownToolLLM:
        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_x", "name": "delete_everything", "arguments": "{}"}
                    ],
                }
            return {"content": "最终答案：不会执行未注册的工具。", "tool_calls": []}

    async def _run():
        # a) 数学错误（除零）：observation = ERROR，流程继续并给出最终答案
        agent = _make_agent()
        result = await agent.ask("请计算 1/0", use_mcp=False)
        assert result["trace"] and result["trace"][0]["observation"].startswith("ERROR"), result
        assert result["degraded"] is False
        assert "除数不能为 0" in result["trace"][0]["observation"]

        # b) 工具参数不是合法 JSON
        agent = _make_agent(llm=BadArgsLLM())
        result = await agent.ask("计算 1+1", use_mcp=False)
        assert result["degraded"] is False and result["steps"] == 2
        assert result["trace"][0]["observation"].startswith("ERROR: 工具参数不是合法 JSON")

        # c) 未知工具
        agent = _make_agent(llm=UnknownToolLLM())
        result = await agent.ask("随便", use_mcp=False)
        assert result["trace"][0]["observation"].startswith("ERROR: 未知工具")
        assert "delete_everything" in result["trace"][0]["observation"]

        # d) 检索器抛异常 -> ERROR 观察，不暴露堆栈
        broken = _make_agent(retriever=BrokenRetriever())
        result = await broken.ask("请解释极限", use_mcp=False)
        observation = result["trace"][0]["observation"]
        assert observation.startswith("ERROR: 知识库检索失败：RuntimeError"), observation
        assert "Traceback" not in observation

    asyncio.run(_run())
    print("  OK 工具异常/参数非法/检索器异常均不崩溃（只回传 ERROR 观察，无堆栈）")


# ---------- 7. MCP server 独立进程 roundtrip ----------
def test_mcp_roundtrip():
    async def _run():
        client = MCPClient()
        print("  MCP command =", client.command)
        connected = await client.connect()
        if not connected:
            print("  [SKIP] MCP server 无法启动（当前环境不支持子进程），跳过 roundtrip")
            if client.stderr_tail:
                print("  MCP stderr:", client.stderr_tail[-3:])
            await client.aclose()
            return
        try:
            tools = await client.list_tools()
            names = {tool["name"] for tool in tools}
            print("  MCP tools =", sorted(names))
            assert {"calculate", "get_current_time", "text_stats"} <= names, names
            calculate_schema = next(t for t in tools if t["name"] == "calculate")["input_schema"]
            assert calculate_schema["properties"]["expression"]["type"] == "string"
            assert "expression" in calculate_schema.get("required", [])

            assert await client.call_tool("calculate", {"expression": "12*3+5"}) == "41"
            assert await client.call_tool("calculate", {"expression": "2**10"}) == "1024"
            stats = await client.call_tool("text_stats", {"text": "ab cd\n中文"})
            assert "字符数：8" in stats, stats
            current_time = await client.call_tool("get_current_time", {})
            assert re.match(r"\d{4}-\d{2}-\d{2}T", current_time), current_time
            bad = await client.call_tool("calculate", {"expression": "__import__('os')"})
            assert bad.startswith("ERROR") or "非法" in bad, bad
            missing_arg = await client.call_tool("calculate", {})
            assert missing_arg.startswith("ERROR"), missing_arg
            unknown = await client.call_tool("no_such_tool", {})
            assert unknown.startswith("ERROR"), unknown
        finally:
            await client.aclose()
        print("  MCP roundtrip OK（initialize/list_tools/call_tool/非法表达式/未知工具）")

        # ToolAgent 连接 MCP 后应动态注册 mcp__ 前缀工具并可直接调用
        agent = ToolAgent(llm=MockLLM(), retriever=FakeRetriever(), mcp_client=MCPClient())
        try:
            connected = await agent.ensure_mcp(True)
            assert connected is True, "ToolAgent 应能连上 MCP server"
            tool_names = {tool["function"]["name"] for tool in agent.list_tools()}
            print("  ToolAgent MCP 工具 =", sorted(n for n in tool_names if n.startswith("mcp__")))
            assert "mcp__calculate" in tool_names
            assert "mcp__get_current_time" in tool_names
            assert await agent._execute_tool("mcp__calculate", {"expression": "2**10"}) == "1024"
            # MCP 已连接时 ask 仍可正常走 ReAct（Mock 选择本地 calculate，mcp_connected=True）
            result = await agent.ask("帮我计算 12*3+5", use_mcp=True)
            assert result["mcp_connected"] is True, result
            assert "41" in result["answer"], result
        finally:
            await agent.aclose()

    asyncio.run(_run())
    print("  OK MCP 独立进程 roundtrip + ToolAgent 动态注册 mcp__ 工具")


# ---------- 8. MCP 不可用时自动降级 ----------
def test_mcp_unavailable_degrade():
    async def _run():
        # a) 子进程启动后立即退出 -> connect 返回 False
        bad_command = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('boom\\n'); sys.exit(2)",
        ]
        agent = ToolAgent(
            llm=MockLLM(),
            retriever=FakeRetriever(),
            mcp_client=MCPClient(command=bad_command),
        )
        try:
            result = await agent.ask("帮我计算 2+3", use_mcp=True)
            assert result["mcp_connected"] is False, result
            assert "5" in result["answer"], result
            assert len(result["trace"]) == 1 and result["trace"][0]["tool"] == "calculate"
        finally:
            await agent.aclose()

        # b) connect 抛异常 -> 同样降级
        agent = _make_agent(mcp_client=ExplodingMCP())
        try:
            result = await agent.ask("帮我计算 7+8", use_mcp=True)
            assert result["mcp_connected"] is False
            assert "15" in result["answer"], result
        finally:
            await agent.aclose()

        # c) config.ENABLE_MCP=False 时即使 use_mcp=True 也不启动 MCP
        original_enable = config.ENABLE_MCP
        config.ENABLE_MCP = False
        agent = _make_agent(mcp_client=MCPClient())
        try:
            result = await agent.ask("帮我计算 3+4", use_mcp=True)
            assert result["mcp_connected"] is False
            assert "7" in result["answer"], result
        finally:
            config.ENABLE_MCP = original_enable
            await agent.aclose()

        # d) config.MCP_SERVER_CMD 覆盖默认命令；Windows 路径反斜杠/引号必须保留正确
        original_cmd = config.MCP_SERVER_CMD
        try:
            config.MCP_SERVER_CMD = f'"{sys.executable}" -m mcp_server'
            resolved = MCPClient().command
            assert resolved == [sys.executable, "-m", "mcp_server"], resolved
            config.MCP_SERVER_CMD = f"{sys.executable} -m mcp_server"
            resolved = MCPClient().command
            assert resolved == [sys.executable, "-m", "mcp_server"], resolved
        finally:
            config.MCP_SERVER_CMD = original_cmd
        # 未设置时默认 [sys.executable, -m, mcp_server]
        config.MCP_SERVER_CMD = ""
        try:
            assert MCPClient().command == [sys.executable, "-m", "mcp_server"]
        finally:
            config.MCP_SERVER_CMD = original_cmd

    asyncio.run(_run())
    print("  OK MCP 不可用/异常/ENABLE_MCP=False 时自动降级本地工具")


# ---------- 9. 默认构造（retriever 默认 HybridRetriever）与空索引兜底 ----------
def test_default_constructor_and_empty_kb():
    async def _run():
        # 不显式传 retriever：应默认构造 HybridRetriever；测试环境无索引 -> 空检索兜底（不崩、不下载模型）
        agent = ToolAgent(llm=MockLLM())
        try:
            assert agent.retriever is not None, "retriever 默认应为 HybridRetriever"
            result = await agent.ask("请解释极限与连续的关系", use_mcp=False)
            assert len(result["trace"]) == 1
            observation = result["trace"][0]["observation"]
            assert "未检索到相关内容" in observation, observation
            assert result["degraded"] is False
        finally:
            await agent.aclose()

        # 显式 retriever=None 走默认值；显式传入自定义 retriever 时原样保留
        custom = FakeRetriever()
        assert ToolAgent(llm=MockLLM(), retriever=custom).retriever is custom

    asyncio.run(_run())
    print("  OK 默认构造 HybridRetriever + 空索引检索兜底（当前环境不可用不崩溃）")


# ---------- 入口 ----------
_TESTS = [
    test_evaluate_expression_safe,
    test_local_tools,
    test_mock_tool_call_flow,
    test_max_steps,
    test_repeat_detection,
    test_tool_errors_no_crash,
    test_mcp_roundtrip,
    test_mcp_unavailable_degrade,
    test_default_constructor_and_empty_kb,
]


def _run_all():
    total = len(_TESTS)
    for index, test in enumerate(_TESTS, 1):
        print(f"[{index}/{total}] {test.__name__}")
        test()
    print(f"\nALL TOOL AGENT TESTS PASSED ({total}/{total})")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError:
        raise
