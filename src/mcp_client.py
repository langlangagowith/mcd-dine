#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 MCP 客户端（Streamable HTTP），只依赖标准库。

来源：复用 https://github.com/langlangagowith/mcd-rogue 的 `scripts/mcp_client.py`
（同一作者），本次新增：
  - `MCD_MCP_URL` 环境变量覆盖（比赛现场可能要求 open.mcd.cn/mcp）
  - `call()` 统一取 structuredContent + success=false 抛 ToolError（原版只打印）
  - 每次调用记入 `client.trace`（工具名 / traceId / 耗时 / 返回码），用于生成"证据链"
  - 429（600 次/分钟限流）与网络抖动退避重试

用法：
    python src/mcp_client.py list-tools
    python src/mcp_client.py call query-meals '{"storeCode":"3330016","orderType":1,"beType":1}'
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("MCD_MCP_URL") or "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "mcd-dine"
CLIENT_VERSION = "0.1.0"


class MCPError(RuntimeError):
    pass


class ToolError(MCPError):
    """工具返回 success=false（业务错误，不是协议错误）。"""

    def __init__(self, tool: str, code, message: str):
        super().__init__(f"{tool} 调用失败 [{code}] {message}")
        self.tool = tool
        self.code = code
        self.message = message


class MCPClient:
    def __init__(self, token: str, url: str = DEFAULT_URL, timeout: int = 60, retries: int = 2):
        if not token:
            raise MCPError("缺少 Token：请设置环境变量 MCD_MCP_TOKEN")
        self.token = token
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.session_id: str | None = None
        self._id = 0
        self.trace: list[dict] = []   # 调用证据链
        self.server_info: dict = {}

    # -- 底层 ---------------------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, expect_response: bool = True) -> list[dict]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read().decode("utf-8", "replace")
                if not expect_response and not body.strip():
                    return []
                return _parse_body(body)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass
                if e.code == 401:
                    raise MCPError(f"401 未授权：Token 无效或已过期。{detail}") from e
                if e.code == 429:
                    last = MCPError(f"429 触发限流（600 次/分钟），请退避重试。{detail}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise MCPError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = MCPError(f"网络错误：{e}")
                time.sleep(0.8 * (attempt + 1))
        raise last or MCPError("未知网络错误")

    # -- 协议 ---------------------------------------------------------------
    def initialize(self) -> dict:
        msgs = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        })
        result = _first_result(msgs)
        self.server_info = result.get("serverInfo") or {}
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_response=False,
        )
        return result

    def list_tools(self) -> list[dict]:
        msgs = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"})
        return _first_result(msgs).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        msgs = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        return _first_result(msgs)

    # -- 业务层便利方法 -----------------------------------------------------
    def call(self, name: str, arguments: dict | None = None) -> dict:
        """调用工具并返回 structuredContent；success=false 抛 ToolError。"""
        t0 = time.time()
        res = self.call_tool(name, arguments or {})
        sc = res.get("structuredContent") or {}
        entry = {
            "tool": name,
            "args": arguments or {},
            "ms": int((time.time() - t0) * 1000),
            "code": sc.get("code"),
            "traceId": sc.get("traceId"),
            "ok": bool(sc.get("success")),
        }
        self.trace.append(entry)
        if res.get("isError") or not sc.get("success"):
            raise ToolError(name, sc.get("code"), sc.get("message") or "MCP 返回 isError")
        return sc


# -- 解析 -------------------------------------------------------------------

def _parse_body(body: str) -> list[dict]:
    """兼容 JSON 与 text/event-stream 两种返回。"""
    text = body.strip()
    if not text:
        return []
    if text.startswith("{"):
        return [json.loads(text)]
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                try:
                    out.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass
    return out


def _first_result(msgs: list[dict]) -> dict:
    for m in msgs:
        if "error" in m:
            raise MCPError(f"JSON-RPC error: {json.dumps(m['error'], ensure_ascii=False)}")
        if "result" in m:
            return m["result"]
    raise MCPError("响应中没有 result 字段")


def load_token() -> str:
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if token:
        return token
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for name in (".env.local", ".env"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("MCD_MCP_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise MCPError("未找到 Token：请设置环境变量 MCD_MCP_TOKEN，或在仓库根目录放置 .env.local")


_load_token = load_token  # 兼容原版调用名


def connect(token: str | None = None) -> MCPClient:
    cli = MCPClient(token or load_token())
    cli.initialize()
    return cli


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cli = connect()
    cmd = argv[1]

    if cmd == "list-tools":
        tools = cli.list_tools()
        si = cli.server_info
        print(f"server: {si.get('name')} {si.get('version')}  工具数: {len(tools)}")
        for t in tools:
            print(f"  - {t['name']}")
        return 0

    if cmd == "call":
        if len(argv) < 3:
            print("用法: python src/mcp_client.py call <tool> '<json>'")
            return 2
        args = json.loads(argv[3]) if len(argv) > 3 else {}
        sc = cli.call(argv[2], args)
        print(json.dumps(sc, ensure_ascii=False, indent=1)[:6000])
        return 0

    print(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
