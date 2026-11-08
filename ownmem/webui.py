# -*- coding: utf-8 -*-
"""OwnMem Web 审计台（标准库 http.server，零依赖）。

页面本体在 webui_page.PAGE（纯字符串模块），本文件只管服务端逻辑。

页面回答三个问题：
  1. 我的 agent 到底记了什么？（记忆仓库）
  2. 谁在什么时候动了记忆？（审计流水）
  3. 有没有背着我存东西？（拦截记录 + 待定区）
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .core import audit, db, proc, sessions
from .core.store import KIND_LABELS, MemoryStore
from .gateway import policy, status as gateway_status

from .webui_page import PAGE  # noqa: E402  (页面本体，纯字符串)


class QuietServer(ThreadingHTTPServer):
    """浏览器中途关页面/断连时别刷 traceback，安静处理。"""

    def handle_error(self, request, client_address):
        exc = sys.exception()
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


_SERVER = None  # 当前 HTTPServer 实例，供 /api/shutdown 优雅停机


def _int_qs(qs: dict, key: str, default: int) -> int:
    """query 里的整数参数：非法值回落默认，不让一个坏参数炸掉整个请求。"""
    try:
        return max(1, int(qs.get(key, [default])[0]))
    except (TypeError, ValueError):
        return default


def _offset_qs(qs: dict, key: str = "offset") -> int:
    try:
        return max(0, int(qs.get(key, ["0"])[0]))
    except (TypeError, ValueError):
        return 0


ACTIVITY_DAYS = 14
_WRITE_EVENTS = ("write", "promote", "supersede", "recur", "kind_promote")
_BLOCK_EVENTS = (
    "write_blocked", "recall_blocked", "write_triage_reject", "write_triage_stage",
)


def _activity(conn, days: int = ACTIVITY_DAYS) -> list[dict]:
    """最近 N 天的读写/拦截强度，按天补齐空档。

    总览上那条「动静」刻度条靠它——审计流水 90% 以上是召回注入，
    只看总量会把「其实这几天什么都没干」误读成「很活跃」，所以分层：
    n=当天全部事件，w=写入类，b=拦截类。
    """
    rows = conn.execute(
        "SELECT substr(ts,1,10) d, COUNT(*) n,"
        " SUM(CASE WHEN event IN (%s) THEN 1 ELSE 0 END) w,"
        " SUM(CASE WHEN event IN (%s) THEN 1 ELSE 0 END) b"
        " FROM audit_log GROUP BY d"
        % (",".join("?" * len(_WRITE_EVENTS)), ",".join("?" * len(_BLOCK_EVENTS))),
        (*_WRITE_EVENTS, *_BLOCK_EVENTS),
    ).fetchall()
    by_day = {r["d"]: r for r in rows}
    today = datetime.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        day = (today - datetime.timedelta(days=i)).isoformat()
        r = by_day.get(day)
        out.append({
            "day": day,
            "n": r["n"] if r else 0,
            "w": r["w"] if r else 0,
            "b": r["b"] if r else 0,
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"OwnMem/{__version__}"

    def log_message(self, fmt, *args):  # 静默
        pass

    def _guard(self, need_ui_header: bool) -> bool:
        """本机服务的基本防护（审计台存的是隐私，值得这三道闸）：
          1. Host 校验——挡 DNS 重绑定（攻击页把自己的域名解析到 127.0.0.1）
          2. Origin 校验——挡其它网页发起的跨站请求
          3. POST 额外要求 X-OwnMem-UI 头——自定义头会触发 CORS 预检，
             恶意页面根本发不进来（页面前端自己会带上这个头）
        无 Host/Origin 的客户端（curl、CLI）不受影响。"""
        port = self.server.server_address[1]
        allowed_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        allowed_origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        host = self.headers.get("Host", "")
        if host and host not in allowed_hosts:
            self._json({"error": "host not allowed"}, 403)
            return False
        origin = self.headers.get("Origin", "")
        if origin and origin not in allowed_origins:
            self._json({"error": "origin not allowed"}, 403)
            return False
        if need_ui_header and self.headers.get("X-OwnMem-UI", "") != "1":
            self._json({"error": "missing X-OwnMem-UI header"}, 403)
            return False
        return True

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        # 审计台不提供任何跨域能力：预检一律拒绝
        self._json({"error": "cross-origin not allowed"}, 403)

    def do_GET(self):
        if not self._guard(need_ui_header=False):
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__VERSION__", __version__).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        conn = db.connect()
        try:
            store = MemoryStore(conn)

            if path == "/api/overview":
                stats = store.stats()
                blocked = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE event='write_blocked'"
                ).fetchone()["c"]
                sessions_total = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
                audit_total = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
                self._json({
                    "stats": stats,
                    "blocked_total": blocked,
                    "sessions_total": sessions_total,
                    "audit_total": audit_total,
                    "activity": _activity(conn),
                    "default_mode": db.get_setting(conn, "default_mode", "normal"),
                    "version": __version__,
                    "now": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                })
            elif path == "/api/audit":
                events = [e for e in qs.get("events", [""])[0].split(",") if e]
                sid = qs.get("session_id", [""])[0] or ""
                agent = qs.get("agent", [""])[0] or ""
                limit = _int_qs(qs, "limit", 100)
                offset = _offset_qs(qs)
                total = audit.count_events(conn, session_id=sid, agent=agent,
                                           events=events or None)
                self._json({
                    "items": audit.list_events(
                        conn, session_id=sid, agent=agent,
                        events=events or None, limit=limit, offset=offset,
                    ),
                    "total": total,
                    "offset": offset,
                    "has_more": offset + limit < total,
                })
            elif path == "/api/memories":
                q = qs.get("q", [""])[0].strip()
                agent = qs.get("agent", [""])[0].strip()
                kind = qs.get("kind", [""])[0].strip()
                importance = qs.get("importance", [""])[0].strip()
                project = qs.get("project", [""])[0].strip()
                limit = _int_qs(qs, "limit", 100)
                offset = _offset_qs(qs)
                if q:
                    # 检索走相关度排序，分页只能在排序结果上切片：数据在本地、
                    # 量级是几百条，取够再切比给 BM25 加 OFFSET 语义更实在。
                    top = store.search(
                        q, limit=max(limit + offset, 500), agent=agent, count_hits=False,
                    )
                    items = [m for m, _ in top]
                    if kind:
                        items = [m for m in items if m.get("kind") == kind]
                    if importance:
                        items = [m for m in items if m.get("importance") == importance]
                    if project:
                        items = [m for m in items if m.get("project") == project]
                    total = len(items)
                    self._json({
                        "items": items[offset:offset + limit],
                        "total": total,
                        "offset": offset,
                        "has_more": offset + limit < total,
                        "ranked": True,
                    })
                else:
                    filters = dict(agent=agent, kind=kind, importance=importance, project=project)
                    total = store.count_active(**filters)
                    self._json({
                        "items": store.list_active(limit=limit, offset=offset, **filters),
                        "total": total,
                        "offset": offset,
                        "has_more": offset + limit < total,
                    })
            elif path == "/api/memory":
                # 单条读取：抽屉/深链要用，且取代链要能一跳跳到另一条
                mid = qs.get("id", [""])[0].strip()
                m = store.get(mid) if mid else None
                if not m:
                    self._json({"error": "memory not found"}, 404)
                else:
                    self._json({"item": m})
            elif path == "/api/export":
                bundle = {
                    "app": "OwnMem",
                    "version": __version__,
                    "exported_at": audit.now(),
                    "note": "整本账带走：记忆、回收站、审计流水全在里面，不含任何服务端状态。",
                    "memories": store.list_active(limit=1000000),
                    "trash": store.list_trash(limit=1000000),
                    "audit": audit.list_events(conn, limit=1000000),
                }
                body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
                name = f"ownmem-{datetime.date.today().isoformat()}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/trash":
                self._json({"items": store.list_trash(limit=_int_qs(qs, "limit", 200))})
            elif path == "/api/staged":
                # 一次查询拿全量（原来逐会话查询，200 会话 = 200 次 SQL）
                self._json({"items": policy.list_all_staged(conn)})
            elif path == "/api/modes":
                self._json({
                    "modes": {
                        k: {"label": v.get("label", k), "desc": v["desc"],
                            "read": v["read"], "write": v["write"]}
                        for k, v in sessions.MODES.items()
                    },
                    "default_mode": sessions.normalize_mode(
                        db.get_setting(conn, "default_mode", "normal")
                    ),
                })
            elif path == "/api/meta":
                # 事件/视角/账本的权威文案在后端，前端拉一份，避免两处标签漂移
                self._json({
                    "events": audit.EVENT_LABELS,
                    "views": {k: v.get("label", k) for k, v in sessions.VIEWS.items()},
                    "kinds": dict(KIND_LABELS),
                })
            elif path == "/api/sessions":
                self._json({"items": sessions.list_sessions(conn, limit=50)})
            elif path == "/api/status":
                sid = qs.get("session", [""])[0]
                self._json(gateway_status(conn, sid))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # 分支兜底：不让一个坏请求裸奔 traceback
            self._json({"error": "internal error: " + exc.__class__.__name__}, 500)
        finally:
            conn.close()


    def do_POST(self):
        if not self._guard(need_ui_header=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        conn = db.connect()
        try:
            store = MemoryStore(conn)

            if parsed.path == "/api/mode":
                mode = qs.get("mode", ["normal"])[0]
                scope = qs.get("scope", ["default"])[0]
                sid = qs.get("session", [""])[0]
                if scope == "session" and sid:
                    applied = sessions.set_mode(conn, sid, mode, agent="webui")
                    where = f"会话 {sid}"
                else:
                    applied = sessions.normalize_mode(mode)
                    db.set_setting(conn, "default_mode", applied)
                    audit.log(conn, "mode", agent="webui",
                              detail=f"默认模式切换为 {applied}", mode=applied)
                    where = "默认（所有新会话）"
                conn.commit()
                self._json({
                    "ok": True, "mode": applied, "scope": scope,
                    "session_id": sid, "label": sessions.MODES[applied].get("label", applied),
                    "message": f"{where} 已切换为「{sessions.MODES[applied].get('label', applied)}」",
                })
            elif parsed.path == "/api/view":
                view = qs.get("view", ["normal"])[0]
                sid = qs.get("session", [""])[0]
                if not sid:
                    self._json({"ok": False, "message": "缺少 session 参数"}, 400)
                else:
                    applied = sessions.set_view(conn, sid, view, agent="webui")
                    conn.commit()
                    label = sessions.VIEWS[applied].get("label", applied)
                    self._json({
                        "ok": True, "view": applied, "label": label,
                        "message": f"会话 {sid} 视角已切换为「{label}」",
                    })
            elif parsed.path == "/api/shutdown":
                audit.log(conn, "shutdown", agent="webui", detail="WebUI 请求停止服务")
                self._json({"ok": True, "message": "服务正在停止"})
                if _SERVER is not None:
                    threading.Timer(0.3, _SERVER.shutdown).start()
            elif parsed.path == "/api/memory/delete":
                mid = qs.get("id", [""])[0]
                ok, msg = store.delete(mid)
                if ok:
                    audit.log(conn, "delete", agent="webui", memory_id=mid)
                self._json({"ok": ok, "message": msg})
            elif parsed.path == "/api/memory/restore":
                mid = qs.get("id", [""])[0]
                ok, msg = store.restore(mid)
                if ok:
                    audit.log(conn, "restore", agent="webui", memory_id=mid)
                self._json({"ok": ok, "message": msg})
            elif parsed.path == "/api/memory/purge":
                mid = qs.get("id", [""])[0]
                ok, msg = store.purge(mid)
                self._json({"ok": ok, "message": msg})
            elif parsed.path == "/api/discard":
                sid = qs.get("session", [""])[0]
                sids = [s for s in qs.get("ids", [""])[0].split(",") if s]
                n = policy.discard(conn, sid, sids=sids)
                self._json({"ok": True, "discarded": n, "message": f"已丢弃 {n} 条。"})
            elif parsed.path == "/api/promote":
                sid = qs.get("session", [""])[0]
                sids = [s for s in qs.get("ids", [""])[0].split(",") if s]
                self._json(policy.promote(conn, sid, sids=sids, agent="webui"))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # 分支兜底：不让一个坏请求裸奔 traceback
            self._json({"error": "internal error: " + exc.__class__.__name__}, 500)
        finally:
            conn.close()



def _pid_alive(pid: int) -> bool:
    """这个 pid 还在跑吗（纯 ctypes，和 core.proc 一个做派，不引第三方依赖）。"""
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = wintypes.DWORD()
        ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
        k.CloseHandle(handle)
        # 259 = STILL_ACTIVE：拿到句柄不代表活着，得看退出码
        return bool(ok) and code.value == 259
    except Exception:  # noqa: BLE001
        return False


def _acquire_instance_lock(port: int) -> str:
    """抢「本端口只能有一个实例」的锁，拿到返回锁文件路径，拿不到返回空串。

    为什么需要它：Windows 的 SO_REUSEADDR 允许两个进程绑定同一端口，
    第二个既不报错也收不到连接——它静静挂着，用户以为服务起来了其实是死的。
    双击两次启动器就会撞上（探测 HTTP 有窗口期，那时第一个还没开始应答）。

    O_EXCL 创建是原子的：谁先创建谁赢。进程崩了会留锁，靠 pid 存活检测兜底。
    """
    lock = db.home_dir() / f"webui-{port}.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return str(lock)
    except FileExistsError:
        pass
    except OSError:
        return ""
    try:
        pid = int((lock.read_text(encoding="utf-8") or "0").strip() or 0)
    except (ValueError, OSError):
        pid = 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return ""  # 真有实例在跑，别起第二个
    try:  # 陈旧锁（上一个进程没来得及删）：抢过来
        lock.unlink()
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return str(lock)
    except OSError:
        return ""


def _release_instance_lock(lock_path: str) -> None:
    if not lock_path:
        return
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def serve(port: int = 8787, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}/"
    # Windows 上 SO_REUSEADDR 允许重复绑定同一端口（第二个实例收不到连接），
    # 所以不能用「bind 失败」判断是否已在运行，必须直接探测 HTTP。
    try:
        with urllib.request.urlopen(url + "api/overview", timeout=1.5):
            print(f"OwnMem 审计台已在运行：{url}")
            if open_browser:
                webbrowser.open(url)
            return
    except OSError:
        pass
    # 探测之外再加一道锁：挡住「两个进程同时启动、那时谁都还没开始应答」的窗口
    lock_path = _acquire_instance_lock(port)
    if not lock_path:
        print(f"端口 {port} 上已有 OwnMem 审计台实例在运行，不重复启动。")
        if open_browser:
            webbrowser.open(url)
        return
    try:
        httpd = QuietServer(("127.0.0.1", port), Handler)
    except OSError:
        _release_instance_lock(lock_path)
        print(f"端口 {port} 被其它程序占用，换个端口：ownmem ui --port 8788")
        return
    global _SERVER
    _SERVER = httpd
    print(f"OwnMem 审计台已启动：{url}")
    # 记下「谁拉起我」——中间层进程（wscript/cmd）启动完就会退出，事后从进程表上看不出关系，
    # 只有启动这一刻的父链能说明它会不会随 agent 会话一起被回收。
    lineage = proc.chain(os.getpid())
    who = proc.host(lineage)
    verdict = {"shell": "由桌面 shell 托管，会话结束不受影响",
               "agent": "由 agent 会话拉起，会话结束会被回收",
               "unknown": "托管者不明"}[who]
    print(f"  进程 pid={os.getpid()}｜托管者={who}（{verdict}）")
    print(f"  父链 {' <- '.join(lineage)}")
    print("（Ctrl+C 停止）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        _release_instance_lock(lock_path)


if __name__ == "__main__":
    serve()
