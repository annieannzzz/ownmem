# -*- coding: utf-8 -*-
"""验证模型网关：不改 harness，只在 base_url 上做手脚，记忆能不能自动进场。

测试拓扑（全本地，不打真实 API、不花钱）：

    curl/probe ──▶ 网关 8790 ──▶ 假上游 8791（记录收到的请求体与路径）

断言清单：
  1. normal 模式：上游收到的请求里多出召回内容
  2. blank 模式：不注入（白纸生效）
  3. ghost 模式：不注入
  4. Responses 格式（Codex wire_api）✓
  5. 审计流水里有 proxy_inject / recall_blocked 事件
  6. 注入位置：记忆追加在 harness 系统提示之后（权威性低于 agent 自身指令）
  7. 对话指纹：不带会话头的两段对话自动分成两个会话记账
  8. health 端点：/ownmem/health 本地可达且报活跃记忆数
  9. 上游无路径时：客户端的 /v1 前缀不被剥掉（否则上游 404）
 10. fail-open：记忆库打不开时，请求照常转发到上游（网关不成为单点故障）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
HOME = str(Path(tempfile.gettempdir()) / "ownmem-gwtest")
PY = sys.executable

GW_PORT = 8790
UP_PORT = 8791
SESSION = "gwtest-001"
AGENT = "gateway-test"

RECEIVED: list[dict] = []
RECEIVED_PATHS: list[str] = []


class Upstream(BaseHTTPRequestHandler):
    """假上游：记录收到的请求路径与请求体，回一个最小的 chat completion。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        RECEIVED_PATHS.append(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            RECEIVED.append(json.loads(raw.decode("utf-8")))
        except Exception:
            RECEIVED.append({"_unparsed": raw[:200].decode("utf-8", "replace")})
        out = json.dumps({
            "id": "fake-1", "object": "chat.completion", "model": "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "收到"},
                         "finish_reason": "stop"}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                return True
        except urllib.error.HTTPError:
            return True          # 有响应即算就绪
        except Exception:
            time.sleep(0.4)
    return False


def ask(port: int, text: str) -> None:
    """模拟 harness 发一轮对话请求（带显式会话头）。"""
    body = json.dumps({
        "model": "fake-model",
        "messages": [
            {"role": "system", "content": "你是一个编码助手。"},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "X-OwnMem-Session": SESSION},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


def ask_no_session(port: int, text: str, model: str = "fake-model") -> None:
    """不带会话头——真实 harness 的默认形态，会话靠对话指纹自动分账。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


def injected_text() -> str:
    """把上游最近收到的请求里所有 system 内容拼起来。"""
    if not RECEIVED:
        return ""
    payload = RECEIVED[-1]
    parts = []
    for m in payload.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
    if isinstance(payload.get("instructions"), str):
        parts.append(payload["instructions"])
    return "\n".join(parts)


def main() -> int:
    ok = True
    os.environ["OWNMEM_HOME"] = HOME
    os.environ["PYTHONPATH"] = ROOT
    Path(HOME).mkdir(parents=True, exist_ok=True)
    for f in Path(HOME).glob("ownmem.db*"):
        f.unlink()

    sys.path.insert(0, ROOT)
    from ownmem.core import db, sessions          # noqa: E402
    from ownmem.gateway import guarded_write      # noqa: E402

    # 0. 种一条记忆（真实库路径）
    conn = db.connect(Path(HOME) / "ownmem.db")
    guarded_write(conn, "安宇正在开发 OwnMem 的模型网关：靠改 base_url 在 LLM 请求层注入记忆，"
                        "不依赖任何 harness 的 hook 支持。", session_id=SESSION,
                   agent="seed", importance="high")
    conn.close()
    print("[0] 已种入 1 条记忆")

    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    gw = subprocess.Popen(
        [PY, "-u", "-m", "ownmem.gateway.proxy",
         "--port", str(GW_PORT), "--upstream", f"http://127.0.0.1:{UP_PORT}/v1",
         "--agent", AGENT],
        cwd=ROOT, env={**os.environ}, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    try:
        if not wait_port(GW_PORT):
            print("✗ 网关没起来：", gw.stdout.read()[:400] if gw.stdout else "")
            return 1
        print(f"[1] 网关已就绪 127.0.0.1:{GW_PORT} → 假上游 {UP_PORT}")

        # ---- 用例 1：normal ----
        ask(GW_PORT, "OwnMem 的模型网关做完了吗")
        got = injected_text()
        hit1 = "OwnMem 自动召回" in got and "base_url" in got
        print(f"[2] normal 模式：上游收到 {len(RECEIVED[-1].get('messages', []))} 条消息，"
              f"注入={'有' if hit1 else '无'} {'✓' if hit1 else '✗'}")
        ok &= hit1

        # ---- 用例 2：blank（不读）----
        conn = db.connect(Path(HOME) / "ownmem.db")
        sessions.set_mode(conn, SESSION, "blank", agent=AGENT)
        conn.close()
        ask(GW_PORT, "OwnMem 的模型网关做完了吗")
        got = injected_text()
        hit2 = "OwnMem 自动召回" not in got
        print(f"[3] blank 模式：注入={'无' if hit2 else '有'} {'✓' if hit2 else '✗'}（白纸生效）")
        ok &= hit2

        # ---- 用例 3：ghost（读写全断）----
        conn = db.connect(Path(HOME) / "ownmem.db")
        sessions.set_mode(conn, SESSION, "ghost", agent=AGENT)
        conn.close()
        ask(GW_PORT, "OwnMem 的模型网关做完了吗")
        hit3 = "OwnMem 自动召回" not in injected_text()
        print(f"[4] ghost 模式：注入={'无' if hit3 else '有'} {'✓' if hit3 else '✗'}（双断生效）")
        ok &= hit3

        # ---- 用例 5：Responses API 线格式（Codex 用 wire_api="responses"）----
        conn = db.connect(Path(HOME) / "ownmem.db")
        sessions.set_mode(conn, SESSION, "normal", agent=AGENT)
        conn.close()
        body = json.dumps({"model": "fake", "instructions": "你是一个编码助手。",
                           "input": "OwnMem 的模型网关做完了吗"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{GW_PORT}/v1/responses", data=body,
            headers={"Content-Type": "application/json", "X-OwnMem-Session": SESSION})
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
        instr = RECEIVED[-1].get("instructions", "")
        hit5 = "OwnMem 自动召回" in instr
        print(f"[5] Responses 格式（Codex）：注入={'有' if hit5 else '无'} {'✓' if hit5 else '✗'}")
        ok &= hit5

        # ---- 用例 6：审计流水 ----
        conn = db.connect(Path(HOME) / "ownmem.db")
        sessions.set_mode(conn, SESSION, "normal", agent=AGENT)
        rows = conn.execute(
            "SELECT event, detail FROM audit_log WHERE event IN ('proxy_inject','recall_blocked')"
        ).fetchall()
        conn.close()
        inj = [r for r in rows if r["event"] == "proxy_inject"]
        blk = [r for r in rows if r["event"] == "recall_blocked"]
        print(f"[6] 审计流水：proxy_inject {len(inj)} 条 · recall_blocked {len(blk)} 条 "
              f"{'✓' if inj and blk else '✗'}")
        if inj:
            print(f"    └ {inj[0]['detail'][:80]}")
        ok &= bool(inj) and bool(blk)

        # ---- 用例 7：注入位置——记忆必须排在 harness 系统提示之后 ----
        ask(GW_PORT, "OwnMem 的模型网关做完了吗")
        sys_txt = injected_text()
        pos_ok = (-1 < sys_txt.find("编码助手") < sys_txt.find("OwnMem 自动召回"))
        print(f"[7] 注入位置：记忆在 harness 系统提示之后 {'✓' if pos_ok else '✗'}")
        ok &= pos_ok

        # ---- 用例 8：对话指纹——不带会话头的两段对话自动分成两个会话 ----
        ask_no_session(GW_PORT, "第一段对话的独有开场白甲甲甲")
        ask_no_session(GW_PORT, "第二段对话的独有开场白乙乙乙")
        conn = db.connect(Path(HOME) / "ownmem.db")
        fp_rows = conn.execute(
            "SELECT COUNT(DISTINCT id) c FROM sessions WHERE id LIKE ?",
            ("proxy:" + AGENT + ":%",),
        ).fetchall()
        conn.close()
        fp_ok = fp_rows[0]["c"] >= 2
        print(f"[8] 对话指纹：两段对话分成 {fp_rows[0]['c']} 个独立会话 {'✓' if fp_ok else '✗'}")
        ok &= fp_ok

        # ---- 用例 9：health 端点 ----
        with urllib.request.urlopen(f"http://127.0.0.1:{GW_PORT}/ownmem/health", timeout=10) as r:
            h = json.loads(r.read().decode("utf-8"))
        hp_ok = h.get("ok") is True and h.get("memories_active", 0) >= 1
        print(f"[9] health 端点：ok={h.get('ok')} 活跃记忆={h.get('memories_active')} {'✓' if hp_ok else '✗'}")
        ok &= hp_ok

        # ---- 用例 10：上游无路径时，客户端 /v1 前缀不被剥掉 ----
        gw2 = subprocess.Popen(
            [PY, "-u", "-m", "ownmem.gateway.proxy", "--port", "8792",
             "--upstream", f"http://127.0.0.1:{UP_PORT}", "--agent", AGENT],
            cwd=ROOT, env={**os.environ},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_port(8792)
            ask(8792, "OwnMem 的模型网关做完了吗")
            path_ok = bool(RECEIVED_PATHS) and RECEIVED_PATHS[-1] == "/v1/chat/completions"
            print(f"[10] 上游无路径：转发路径={RECEIVED_PATHS[-1] if RECEIVED_PATHS else '?'} "
                  f"{'✓' if path_ok else '✗'}")
            ok &= path_ok
        finally:
            gw2.terminate()

        # ---- 用例 11：fail-open——记忆库打不开时请求照常到上游 ----
        gw3 = subprocess.Popen(
            [PY, "-u", "-m", "ownmem.gateway.proxy", "--port", "8793",
             "--upstream", f"http://127.0.0.1:{UP_PORT}/v1", "--agent", AGENT,
             "--db", r"C:\ownmem-不存在目录-fail-open-test\ownmem.db"],
            cwd=ROOT, env={**os.environ},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_port(8793)
            body = json.dumps({
                "model": "fake", "messages": [{"role": "user", "content": "网关故障放行测试"}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:8793/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                resp = json.loads(r.read().decode("utf-8"))
            fo_ok = resp.get("object") == "chat.completion"
            print(f"[11] fail-open：库打不开仍返回上游响应 {'✓' if fo_ok else '✗'}")
            ok &= fo_ok
        finally:
            gw3.terminate()

    finally:
        gw.terminate()
        up.shutdown()

    print("\n" + ("全部通过 ✓" if ok else "有失败项 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
