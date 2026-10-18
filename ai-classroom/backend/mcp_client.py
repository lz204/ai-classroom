# -*- coding: utf-8 -*-
"""最小 MCP Client：以子进程方式连接 mcp_server（stdio JSON-RPC 2.0）。

设计要点：
- 默认命令 [sys.executable, "-m", "mcp_server"]，工作目录固定为 backend/（与 REST 服务一致）；
  也可通过构造参数或 config.MCP_SERVER_CMD 覆盖；
- 所有请求带超时（config.MCP_TIMEOUT），响应按 JSON-RPC id 匹配；
- connect() 失败返回 False 并打印 warning（不抛、不阻塞服务），调用方可降级为本地工具；
- 只依赖标准库。
"""
import asyncio
import itertools
import json
import os
import shlex
import sys
from pathlib import Path

import config

BACKEND_DIR = Path(__file__).resolve().parent

PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "ai-classroom", "version": "1.0.0"}

# 子进程退出等待上限（秒）
_PROCESS_CLOSE_TIMEOUT = 3.0


class MCPError(RuntimeError):
    """MCP 协议/连接层错误"""


class MCPTimeoutError(MCPError):
    """MCP 请求超时"""


class MCPClient:
    """MCP stdio 客户端：connect / list_tools / call_tool / aclose。"""

    def __init__(self, command: list[str] | None = None, timeout: float | None = None):
        self._command = self._resolve_command(command)
        self._cwd = str(BACKEND_DIR)
        self._timeout = float(timeout if timeout is not None else config.MCP_TIMEOUT)
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._connected = False
        self._stderr_tail: list[str] = []

    # ---------- 基础属性 ----------
    @property
    def command(self) -> list[str]:
        return list(self._command)

    @property
    def connected(self) -> bool:
        return self._connected and self._proc is not None and self._proc.returncode is None

    @property
    def stderr_tail(self) -> list[str]:
        """MCP server 最近 stderr 日志（排查用，最多保留 50 行）"""
        return list(self._stderr_tail)

    @staticmethod
    def _resolve_command(command: list[str] | None) -> list[str]:
        """未显式指定时：优先 config.MCP_SERVER_CMD，否则 sys.executable -m mcp_server。"""
        if command:
            return [str(part) for part in command]
        configured = (config.MCP_SERVER_CMD or "").strip()
        if configured:
            parts = MCPClient._split_command_string(configured)
            if parts:
                return parts
        return [sys.executable, "-m", "mcp_server"]

    @staticmethod
    def _split_command_string(configured: str) -> list[str]:
        """拆分 MCP_SERVER_CMD 命令字符串。

        注意：Windows 下必须用 posix=False，否则默认 posix 模式会把路径里的反斜杠当转义符
        （E:\\venv\\python.exe 会被拆成 E:venvpython.exe）；同时去掉 token 外层引号。
        """
        try:
            parts = shlex.split(configured, posix=(os.name != "nt"))
        except ValueError:
            parts = configured.split()
        if os.name == "nt":
            cleaned = []
            for part in parts:
                if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
                    part = part[1:-1]
                cleaned.append(part)
            parts = cleaned
        return [part for part in parts if part]

    # ---------- 连接与关闭 ----------
    async def connect(self) -> bool:
        """启动子进程并完成 initialize 握手；失败返回 False（打印 warning，不抛）。"""
        if self.connected:
            return True
        try:
            env = dict(os.environ)
            # 强制子进程 UTF-8：Windows 默认 GBK 会让中文参数/结果乱码
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._read_task = asyncio.create_task(self._read_loop(), name="mcp-client-reader")
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(), name="mcp-client-stderr"
            )
            await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
                timeout=self._timeout,
            )
            await self._notify("notifications/initialized", {})
            self._connected = True
            return True
        except NotImplementedError:
            # Windows 下 uvicorn --reload / 多 worker 会切到 SelectorEventLoop，该 loop 不支持子进程
            print(
                "[MCPClient] 当前事件循环不支持子进程（Windows 下 uvicorn --reload / workers>1 会"
                "使用 SelectorEventLoop）：MCP 降级为本地工具；如需启用 MCP 请去掉 --reload。",
                file=sys.stderr,
                flush=True,
            )
            await self.aclose()
            return False
        except Exception as exc:  # 连接失败不阻塞服务：打印 warning 并降级
            print(
                f"[MCPClient] MCP server 连接失败（将降级为本地工具）：{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            await self.aclose()
            return False

    async def aclose(self):
        """关闭子进程与后台任务（幂等，可重复调用）。"""
        self._connected = False
        proc, self._proc = self._proc, None
        tasks = [task for task in (self._read_task, self._stderr_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        self._read_task = self._stderr_task = None
        # 唤醒所有等待中的请求，避免调用方永久挂起
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(MCPError("MCP 连接已关闭"))
        self._pending.clear()
        if proc is None:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_PROCESS_CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=_PROCESS_CLOSE_TIMEOUT)
                except Exception:
                    pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- 后台读取 ----------
    async def _read_loop(self):
        """持续读取 stdout 的 JSON-RPC 响应，按 id 唤醒对应请求。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # 非协议数据（理论上 stdout 只写协议）忽略，避免读取循环崩溃
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                future = self._pending.pop(request_id, None) if request_id is not None else None
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            # server 退出：让所有挂起请求立即失败，而不是等超时
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(MCPError("MCP server 已退出"))
            self._pending.clear()
            self._connected = False

    async def _drain_stderr(self):
        """持续消费 stderr（防止子进程日志写满管道阻塞），保留末尾日志。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > 50:
                    self._stderr_tail = self._stderr_tail[-50:]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ---------- 请求 ----------
    async def _write_line(self, payload: dict):
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise MCPError("MCP server 未启动或已退出")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            proc.stdin.write(data)
            await proc.stdin.drain()

    async def _notify(self, method: str, params: dict | None = None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(payload)

    async def _request(self, method: str, params: dict | None = None,
                       timeout: float | None = None) -> dict:
        request_id = next(self._ids)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._write_line(payload)
            response = await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self._timeout
            )
        except asyncio.TimeoutError:
            raise MCPTimeoutError(f"MCP 请求超时：{method}（>{timeout or self._timeout}s）")
        finally:
            self._pending.pop(request_id, None)
        if not isinstance(response, dict):
            raise MCPError(f"MCP 响应格式异常：{method}")
        if "error" in response:
            error = response.get("error") or {}
            raise MCPError(
                f"MCP 错误 {error.get('code')}: {error.get('message') or 'unknown error'}"
            )
        return response.get("result") or {}

    # ---------- 对外 API ----------
    async def list_tools(self) -> list[dict]:
        """tools/list：返回 [{name, description, input_schema}]；失败返回 []。"""
        if not self.connected:
            return []
        try:
            result = await self._request("tools/list", {})
        except Exception as exc:
            print(f"[MCPClient] tools/list 失败：{exc}", file=sys.stderr, flush=True)
            return []
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return []
        normalized = []
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            normalized.append(
                {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("inputSchema") or {},
                }
            )
        return normalized

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """tools/call：解析 text content 返回字符串；任何失败返回 "ERROR: ..."（不抛）。"""
        if not self.connected:
            return "ERROR: MCP 未连接"
        payload_args = arguments if isinstance(arguments, dict) else {}
        try:
            result = await self._request(
                "tools/call", {"name": name, "arguments": payload_args}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 超时/协议错误/进程退出统一转 ERROR 文本
            return f"ERROR: MCP 工具 {name} 调用失败：{type(exc).__name__}: {exc}"
        if not isinstance(result, dict):
            return "ERROR: MCP 返回格式异常"
        texts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        text = "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)
        if result.get("isError") and not text.startswith("ERROR:"):
            text = f"ERROR: {text}"
        return text
