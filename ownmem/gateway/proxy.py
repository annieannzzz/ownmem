# -*- coding: utf-8 -*-
"""OwnMem 模型网关（B 档适配器）：在 LLM 请求层注入记忆。

## 为什么需要它

MCP 工具是「拉」模型——只有模型主动调用才生效，而模型不知道自己缺什么信息，
所以它不会调。真正有效的记忆产品（ChatGPT 记忆、平台内置画像）全是**服务端自动注入**。

Claude Code / OpenCode 这类 harness 提供了 hook，可以借道注入；
但绝大多数 agent（Qoder / Trae / Codex / ZCode / CodeBuddy / 国内全家桶）**没有暴露 hook**。

不过它们有一个共同点：**都要配 base_url**（国内用户更是如此，模型 API 全指向国产服务）。
于是把 base_url 指向本网关，记忆就在模型看不见的地方被塞进上下文——
不需要任何 harness 配合，不需要它支持 hook、插件、rules。

    harness ──▶ 127.0.0.1:8790 (本网关) ──▶ 真实模型 API
                     │
                     ├─ 取出这一轮的 user 输入
                     ├─ 查 OwnMem（recall_context，含模式闸门）
                     ├─ 把命中记忆注入 system / instructions
                     └─ 记审计流水

## 用法

    set OWNMEM_UPSTREAM=https://token.sensenova.cn/v1
    set OWNMEM_AGENT=opencode
    python -m ownmem.gateway.proxy --port 8790

然后把 harness 的 base_url 改成 http://127.0.0.1:8790/v1

## 边界（诚实声明）

- 只对本机能改 base_url 的 client 有效；锁定官方端点的 SaaS 客户端管不了
- 只处理「请求前注入」；模型输出侧不改写——所以**写回路不归它管**：
  只开网关的 agent 记忆库不会增长，写入仍靠 MCP 工具或 CLI
- 密钥只是被转发，不落盘、不写日志
- 默认只监听 127.0.0.1，且校验 Host 头（挡 DNS 重绑定）
- 会话规则：请求头 X-OwnMem-Session > 对话指纹（模型名+首条系统提示+首条
  用户消息的哈希，同对话逐轮稳定、不同对话分开）> proxy:<agent> 兜底
- 本地状态端点 GET /ownmem/health（不转发上游），用于探测网关是否活着

支持的协议格式：OpenAI /chat/completions（messages）与 /responses（instructions）两种线格式。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core import audit, db, sessions
from .recall import recall_context

# 逐跳到跳的头部，不能透传
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

DEFAULT_PORT = 8790
UPSTREAM_TIMEOUT = 600  # 流式响应可能很久

# 复用连接出错时可以安全换新连接重发的情形（都是「旧连接被上游关了」）
_RETRYABLE = (
    http.client.NotConnected,
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    ConnectionResetError,
    BrokenPipeError,
)

# 连接池：ThreadingHTTPServer 多线程，池必须按线程隔离，否则两个请求会串包
_conn_local = threading.local()

# 运行时配置（由 main 写入）
UPSTREAM = ""
PREFIX = "/v1"
AGENT = "unknown"
OWNMEM_DB = ""


# ---------------------------------------------------------------------------
# 请求解析与注入
# ---------------------------------------------------------------------------
def _content_text(content) -> str:
    """message content：字符串直取；多模态分片取各片 text 拼接。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(t for t in parts if t)
    return ""


def extract_query(payload: dict) -> str:
    """从请求体里取出「这一轮用户说了什么」。

    兼容两种线格式：
      - OpenAI chat/completions：messages[].role == user
      - OpenAI Responses API：input（字符串或条目数组）
    """
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            return _content_text(m.get("content"))
        return ""

    inp = payload.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for item in reversed(inp):
            if isinstance(item, dict) and item.get("role") == "user":
                return _content_text(item.get("content"))
    return ""


def conversation_key(payload: dict) -> str:
    """对话指纹：把不同对话分开记账，不需要 harness 配合任何东西。

    同一段对话逐轮重发时，「模型名 + 首条 system/developer + 首条 user」
    三样保持不变；不同对话天然不同。返回 10 位哈希（不泄露内容），
    取不到稳定前缀时返回空串。
    """
    parts = [str(payload.get("model") or "")]
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        sys_txt = ""
        first_user = ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role in ("system", "developer") and not sys_txt:
                sys_txt = _content_text(m.get("content"))
            elif role == "user":
                first_user = _content_text(m.get("content"))
                break
        parts += [sys_txt, first_user]
    else:
        inst = payload.get("instructions")
        parts.append(inst if isinstance(inst, str) else "")
        first_user = ""
        inp = payload.get("input")
        if isinstance(inp, str):
            first_user = inp
        elif isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict) and item.get("role") == "user":
                    first_user = _content_text(item.get("content"))
                    break
        parts.append(first_user)
    if not any(parts[1:]):
        return ""
    key = "\x00".join(p[:500] for p in parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def inject_context(payload: dict, context: str) -> str:
    """把召回内容塞进请求体，返回注入方式（供日志与审计）。

    位置原则：记忆的权威性必须**低于** harness 自己的系统提示——
    追加到开头连续 system/developer 段的最后一条上，绝不插到它们前面
    （否则记忆会盖过 agent 的自身指令，等于把注入点变成了最高权限位）。
    """
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        last_head = -1
        for i, m in enumerate(msgs):
            if (isinstance(m, dict) and m.get("role") in ("system", "developer")
                    and isinstance(m.get("content"), str)):
                last_head = i
            else:
                break
        if last_head >= 0:
            m = msgs[last_head]
            m["content"] = f"{m['content']}\n\n{context}"
            return f"append-to-{m['role']}"
        # 没有 system/developer：新插一条。个别只认 developer 的模型（o1 系）
        # 若 payload 里出现过 developer 角色则跟随之，否则用 system
        role = ("developer" if any(
            isinstance(m, dict) and m.get("role") == "developer" for m in msgs
        ) else "system")
        msgs.insert(0, {"role": role, "content": context})
        return f"prepend-{role}"

    # Responses API 线格式
    inst = payload.get("instructions")
    if isinstance(inst, str) and inst.strip():
        payload["instructions"] = f"{inst}\n\n{context}"
        return "append-to-instructions"
    payload["instructions"] = context
    return "prepend-instructions"


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OwnMemGateway/0.1"

    # 默认的日志会刷屏，改写精简版
    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[gateway] " + (fmt % args) + "\n")

    # -- 工具方法 ---------------------------------------------------------
    def _guard(self) -> bool:
        """Host 校验：挡 DNS 重绑定。本机 harness 连的是 127.0.0.1/localhost，
        Host 必然匹配；浏览器里恶意页面重绑定过来的请求 Host 对不上，直接拒绝。
        （不要求任何专有头——网关要服务 curl/脚本/各种 harness，不能挡它们。）"""
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host and host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            body = b'{"error": "host not allowed"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def _declared_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def _session_id(self, payload: dict) -> str:
        sid = (self.headers.get("X-OwnMem-Session") or "").strip()
        if sid:
            return sid
        # 按对话指纹自动分账：同一对话逐轮归入同一会话，不同对话互不混淆，
        # 且不依赖 harness 配合、不泄露内容。取不到指纹再退化为常驻会话。
        fp = conversation_key(payload)
        if fp:
            return f"proxy:{AGENT}:{fp}"
        return f"proxy:{AGENT}"

    def _target(self, path: str, query: str) -> tuple[str, str, int | None]:
        u = urllib.parse.urlparse(UPSTREAM)
        base = u.path.rstrip("/")
        rest = path
        # 只有上游自带路径（如 .../v1）时才剥掉客户端前缀再拼回去；
        # 上游无路径时必须保留客户端原路径——否则 /v1 会被白白剥掉，上游 404
        if PREFIX and base and path.startswith(PREFIX):
            rest = path[len(PREFIX):] or "/"
        target = (base + rest) or "/"
        if query:
            target = f"{target}?{query}"
        return u.scheme, target, u.port

    def _pool_get(self, scheme: str, host: str, port: int):
        """取本线程到上游的常连接（没有就返回 None）。

        ThreadingHTTPServer 是多线程的，池必须按线程隔离——两个请求抢同一个
        socket 会串包。取不到就新建，由调用方负责放回。
        """
        cache = getattr(_conn_local, "cache", None)
        if cache is None:
            return None
        return cache.get((scheme, host, port))

    def _pool_put(self, key, conn) -> None:
        cache = getattr(_conn_local, "cache", None)
        if cache is None:
            cache = {}
            _conn_local.cache = cache
        cache[key] = conn

    def _pool_drop(self, key) -> None:
        cache = getattr(_conn_local, "cache", None)
        if cache is not None:
            cache.pop(key, None)

    def _forward(self, method: str, body: bytes | None):
        """把请求转发到上游，返回 (connection, response)。

        默认复用本线程到上游的连接，省掉每次 TLS 握手——网关卡在所有 LLM
        请求的必经之路上，握手开销会直接叠加到每条对话上。

        复用有代价：上游随时可能关掉空闲连接。所以只在「用的是旧连接」时
        换新连接重试一次——新建连接都失败的话说明上游真有问题，重复发一次
        POST 可能造成重复计费，那种情况直接把错抛给客户端。
        """
        scheme, target, port = self._target(self.path.split("?")[0], urllib.parse.urlparse(self.path).query)
        u = urllib.parse.urlparse(UPSTREAM)
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_HEADERS
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        key = (scheme, u.hostname, port)

        conn = self._pool_get(*key)
        reused = conn is not None
        for attempt in (0, 1):
            if conn is None:
                conn = cls(u.hostname, port, timeout=UPSTREAM_TIMEOUT)
                self._pool_put(key, conn)
            try:
                conn.request(method, target, body=body, headers=headers)
                return conn, conn.getresponse()
            except _RETRYABLE as exc:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._pool_drop(key)
                conn = None
                # 只有「旧连接断了」才重试；新建连接失败说明上游本身有问题
                if not reused or attempt:
                    raise
                sys.stderr.write(f"[gateway] 复用的上游连接已断开，换新连接重试一次：{exc!r}\n")
        raise RuntimeError("unreachable")

    def _relay(self, resp) -> None:
        """把上游响应原样回给 harness（含 SSE 流式）。"""
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in _HOP_HEADERS or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        # 上游可能是流式（chunked），我们统一改用连接关闭来界定长度
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开，正常现象

    # -- 路由 -------------------------------------------------------------
    def _pass(self, method: str, body: bytes | None) -> None:
        """转发并回给客户端。

        成功后**不关**上游连接，留在本线程的池里复用；只有中继出错时才丢弃，
        因为那时这条连接的状态已经不可信了。
        """
        conn, resp = self._forward(method, body)
        try:
            self._relay(resp)
        except Exception:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool_drop_by_conn(conn)
            raise

    def _pool_drop_by_conn(self, conn) -> None:
        cache = getattr(_conn_local, "cache", None)
        if not cache:
            return
        for key, value in list(cache.items()):
            if value is conn:
                cache.pop(key, None)
                break

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        length = self._declared_length()
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        is_chat = path.endswith("/chat/completions") or path.endswith("/responses")

        if not is_chat or not raw:
            self._pass("POST", raw)
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._pass("POST", raw)
            return

        # ---- 记忆注入（fail-open：记忆侧出任何问题都放行原始请求，
        #      网关卡在所有 LLM 请求的必经之路上，绝不能让它变成单点故障）----
        sid = self._session_id(payload)
        query = extract_query(payload)
        note = "skip:no-query"
        if query.strip():
            try:
                conn = db.connect(OWNMEM_DB)
                try:
                    # 落会话账：让审计台「会话」页能看到网关会话、模式可粘住
                    sessions.touch_session(conn, sid, agent=AGENT)
                    result = recall_context(conn, query, session_id=sid, agent=AGENT)
                    ctx = result.get("context") or ""
                    if ctx:
                        note = inject_context(payload, ctx)
                        audit.log(
                            conn, "proxy_inject", agent=AGENT, session_id=sid,
                            detail=f"[模型网关] {note} · 命中 {result.get('hits')} 条 · 查询 {audit.detail(query, 40)}",
                            mode=result.get("mode", ""),
                        )
                    else:
                        note = f"skip:{result.get('reason') or 'no-hit'}"
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                note = "error:fail-open"
                sys.stderr.write(f"[gateway] 注入失败，已放行原始请求：{exc!r}\n")

        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            body = raw  # 序列化不了就原样转发，不注入也不报错
        self.log_message("POST %s → %s (query=%d字)", path, note, len(query or ""))

        self._pass("POST", body)

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        if self.path.split("?")[0] == "/ownmem/health":
            self._health()
            return
        self._pass("GET", None)

    def _pass_with_body(self, method: str) -> None:
        length = self._declared_length()
        raw = self.rfile.read(length) if length else None
        self._pass(method, raw)

    def do_PUT(self):  # noqa: N802
        if not self._guard():
            return
        self._pass_with_body("PUT")

    def do_PATCH(self):  # noqa: N802
        if not self._guard():
            return
        self._pass_with_body("PATCH")

    def do_HEAD(self):  # noqa: N802
        if not self._guard():
            return
        self._pass("HEAD", None)

    def do_OPTIONS(self):  # noqa: N802
        """网关是代理，OPTIONS 转给上游（审计台才该拒绝跨域，这里不该替上游决定）。"""
        if not self._guard():
            return
        self._pass("OPTIONS", None)

    def do_DELETE(self):  # noqa: N802
        if not self._guard():
            return
        self._pass_with_body("DELETE")

    def _health(self) -> None:
        """本地状态端点（不转发上游）：探测网关活着没有、看基本配置。"""
        info = {"ok": True, "agent": AGENT, "upstream": UPSTREAM,
                "prefix": PREFIX, "db": OWNMEM_DB or str(db.db_path())}
        try:
            conn = db.connect(OWNMEM_DB)
            info["memories_active"] = conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE status='active'"
            ).fetchone()["c"]
            conn.close()
        except Exception as exc:  # noqa: BLE001
            info["db"] = f"error: {exc}"
        body = json.dumps(info, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def serve(port: int = DEFAULT_PORT, upstream: str = "", *, agent: str = "",
          prefix: str = "/v1", db_path: str = "") -> None:
    global UPSTREAM, PREFIX, AGENT, OWNMEM_DB

    UPSTREAM = (upstream or os.environ.get("OWNMEM_UPSTREAM", "")).strip()
    if not UPSTREAM:
        raise SystemExit(
            "缺少上游地址。请设置 OWNMEM_UPSTREAM 或用 --upstream 指定，\n"
            "例如 --upstream https://token.sensenova.cn/v1"
        )
    AGENT = (agent or os.environ.get("OWNMEM_AGENT", "") or "unknown").strip()
    PREFIX = prefix or ""
    OWNMEM_DB = db_path or ""

    httpd = ThreadingHTTPServer(("127.0.0.1", port), GatewayHandler)
    hint = f"http://127.0.0.1:{port}{PREFIX}"
    print(f"OwnMem 模型网关已启动")
    print(f"  agent    : {AGENT}")
    print(f"  上游     : {UPSTREAM}")
    print(f"  harness 配置 base_url → {hint}")
    print(f"  记忆库   : {db_path or db.db_path()}")
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OwnMem 模型网关：在 LLM 请求层注入记忆")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--upstream", default="", help="真实模型 API base，如 https://token.sensenova.cn/v1")
    ap.add_argument("--agent", default="", help="这个网关实例服务的 agent 名（写进审计归属）")
    ap.add_argument("--prefix", default="/v1", help="对外暴露的路径前缀，默认 /v1")
    ap.add_argument("--db", default="", help="OwnMem 数据库路径，默认用全局数据目录")
    args = ap.parse_args(argv)
    serve(args.port, args.upstream, agent=args.agent, prefix=args.prefix, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
