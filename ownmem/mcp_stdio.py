# -*- coding: utf-8 -*-
"""零依赖 MCP stdio 服务实现（标准库 only）。

为什么不用官方 mcp 包：
  1. 实测本机 mcp 2.0.0 的 stdio 传输对任何请求都不响应（最小样例同样无输出），
     而 stdio 恰恰是 Claude Code / OpenCode / Cline 接入 MCP 的唯一方式
  2. 官方包拖进 pydantic / anyio / starlette 一串重依赖，且 mcp 2.0.0 与
     pydantic ≥2.9 存在构造期冲突
  3. 我们只需要 MCP 的一小部分：initialize / tools/list / tools/call

协议依据：JSON-RPC 2.0 over stdio，每条消息一行 JSON（换行分隔）。
诊断信息一律走 stderr——stdout 是协议通道，多打一个字符都会让宿主解析失败。
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LATEST_PROTOCOL = SUPPORTED_PROTOCOL_VERSIONS[0]


@dataclass
class Tool:
    """一个 MCP 工具的完整描述。schema 用 JSON Schema 手写，避免依赖反射。"""

    name: str
    description: str
    schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    handler: Callable[..., str] = lambda **_: ""

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


class StdioMCPServer:
    """极小 MCP 服务端：读一行、派发、写一行。"""

    def __init__(
        self,
        name: str,
        version: str,
        instructions: str = "",
        tools: list[Tool] | None = None,
    ):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    # ---------------- 协议 ----------------
    def _initialize(self, params: dict) -> dict:
        requested = (params or {}).get("protocolVersion", "")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, params: dict) -> dict:
        name = (params or {}).get("name", "")
        args = (params or {}).get("arguments") or {}
        tool = self.tools.get(name)
        if tool is None:
            return {
                "content": [{"type": "text", "text": f"未知工具：{name}"}],
                "isError": True,
            }
        try:
            text = tool.handler(**args)
        except TypeError as exc:
            return {
                "content": [{"type": "text", "text": f"参数错误：{exc}"}],
                "isError": True,
            }
        except Exception as exc:  # 工具内部异常不该拖垮服务
            tb = traceback.format_exc().splitlines()[-3:]
            self._log(f"tool {name} failed: {exc}\n" + "\n".join(tb))
            return {
                "content": [{"type": "text", "text": f"执行出错：{exc}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": text or ""}], "isError": False}

    def handle(self, msg: dict) -> dict | None:
        """处理一条消息；通知类（无 id）返回 None。"""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:      # 通知：不需要回复
            return None

        if method == "initialize":
            return self._ok(msg_id, self._initialize(params))
        if method == "tools/list":
            return self._ok(msg_id, {"tools": [t.descriptor() for t in self.tools.values()]})
        if method == "tools/call":
            return self._ok(msg_id, self._call_tool(params))
        if method == "ping":
            return self._ok(msg_id, {})
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return self._ok(msg_id, {key: []})
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    @staticmethod
    def _ok(msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _log(text: str) -> None:
        sys.stderr.write(f"[ownmem-mcp] {text}\n")
        sys.stderr.flush()

    # ---------------- 传输 ----------------
    def serve_forever(self) -> None:
        # 协议通道必须是确定性的编码：Windows 宿主不设 PYTHONUTF8 时管道默认 GBK，
        # 工具返回文本里的任何生僻字符都会让整个服务崩掉。输出统一 ASCII 转义。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        self._log(f"ready: {self.name} {self.version}，工具 {len(self.tools)} 个（stdio）")
        stdout = sys.stdout
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._log(f"忽略非 JSON 输入：{line[:120]}")
                continue
            try:
                response = self.handle(msg)
            except Exception as exc:  # 兜底：绝不让一行坏输入杀死服务
                self._log(f"handle failed: {exc}")
                if isinstance(msg, dict) and msg.get("id") is not None:
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32603, "message": str(exc)},
                    }
                else:
                    response = None
            if response is not None:
                # ensure_ascii=True：协议输出永远是纯 ASCII，任何 locale 下都安全
                stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
                stdout.flush()


def serve(name: str, version: str, instructions: str, tools: list[Tool]) -> None:
    StdioMCPServer(name, version, instructions, tools).serve_forever()
