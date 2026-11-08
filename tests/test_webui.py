# -*- coding: utf-8 -*-
"""OwnMem Web 审计台测试：接口契约 + 前端页面结构 + 跨站防护。

用临时数据目录 + 临时端口跑真实 HTTP，不碰真实记忆，也不开浏览器。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-webui-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem import __version__  # noqa: E402
from ownmem.core import db, sessions  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.webui import Handler, QuietServer  # noqa: E402

PASS, FAIL = [], []
PORT = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + (f"  → {extra}" if extra and not cond else ""))


def req(method: str, path: str, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = {"X-OwnMem-UI": "1"}
    if headers:
        h.update(headers)
    c.request(method, path, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def jreq(method: str, path: str, headers: dict | None = None):
    s, raw = req(method, path, headers)
    try:
        return s, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return s, None


def main() -> int:
    global PORT
    conn = db.connect()
    store = MemoryStore(conn)
    sid = sessions.new_session_id()
    sessions.set_mode(conn, sid, "normal")
    sessions.set_view(conn, sid, "normal")

    print(f"\n=== OwnMem Web 审计台测试（数据目录 {TMP}）===\n")

    httpd = QuietServer(("127.0.0.1", 0), Handler)
    PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # ---- 页面本体 ----
    s, raw = req("GET", "/")
    html = raw.decode("utf-8")
    check("首页 200 + HTML 文档", s == 200 and "<!doctype html>" in html)
    for marker in ["OwnMem 审计台", "view-overview", "mode-grid", "mem-list",
                   "audit-list", "staged-list", "trash-list", "sess-wrap"]:
        check(f"页面包含锚点 {marker}", marker in html)
    check("页面版本号已注入", f"v{__version__}" in html)
    check("页面不再引用已删除的旧结构", "tab-audit" not in html and "nav button" not in html)

    # ---- 元信息（后端权威文案）----
    s, d = jreq("GET", "/api/meta")
    check("meta 200", s == 200 and isinstance(d, dict))
    check("事件标签来自后端 EVENT_LABELS",
          d and d.get("events", {}).get("recall") == "召回注入"
          and d["events"].get("supersede") == "结论取代")
    check("视角标签来自后端 VIEWS", d and d.get("views", {}).get("blind-review") == "盲审")
    check("账本标签来自后端 KIND_LABELS", d and d.get("kinds", {}).get("pitfall") == "踩坑")

    # ---- 总览 ----
    s, d = jreq("GET", "/api/overview")
    check("overview 200 且含统计字段",
          s == 200 and d and "active" in d["stats"] and "capacity" in d["stats"]
          and "default_mode" in d and "version" in d)

    # ---- 模式 ----
    s, d = jreq("GET", "/api/modes")
    check("modes 返回四种模式", s == 200 and d and len(d["modes"]) == 4)
    check("modes 含读写开关字段",
          d and all("read" in m and "write" in m for m in d["modes"].values()))
    s, d = jreq("POST", "/api/mode?mode=incognito&scope=default")
    check("切换默认模式生效", s == 200 and d and d.get("mode") == "incognito")
    s, d = jreq("GET", "/api/overview")
    check("overview 反映新默认模式", d and d.get("default_mode") == "incognito")
    jreq("POST", "/api/mode?mode=normal&scope=default")

    # ---- 记忆仓库与筛选 ----
    mem, msg = store.add("WebUI 测试用的踩坑记录：截图通道失效时改用无头浏览器",
                         importance="high", kind="pitfall", agent="webui-test",
                         session_id=sid)
    check("准备数据：写入踩坑账", mem is not None, msg)
    s, d = jreq("GET", "/api/memories?kind=pitfall")
    check("按账本筛选记忆", s == 200 and d and len(d["items"]) == 1
          and d["items"][0]["kind"] == "pitfall")
    s, d = jreq("GET", "/api/memories?kind=pitfall&importance=high")
    check("账本 + 重要度组合筛选", s == 200 and d and len(d["items"]) == 1)
    s, d = jreq("GET", "/api/memories?kind=pitfall&importance=low")
    check("组合筛选可返回空集", s == 200 and d and d["items"] == [])
    s, d = jreq("GET", "/api/memories?q=" + urllib.parse.quote("截图通道"))
    check("关键词检索命中", s == 200 and d and len(d["items"]) >= 1)

    # ---- 审计流水与事件筛选 ----
    s, d = jreq("GET", "/api/audit?limit=5")
    check("审计流水可读", s == 200 and d and isinstance(d["items"], list))
    s, d = jreq("GET", "/api/audit?events=write&limit=50")
    check("按事件名筛选审计", s == 200 and d
          and all(e["event"] == "write" for e in d["items"]))
    s, d = jreq("GET", f"/api/audit?session_id={sid}&limit=50")
    check("按会话筛选审计", s == 200 and d
          and all(e["session_id"] == sid for e in d["items"]))

    # ---- 会话视角 ----
    s, d = jreq("POST", f"/api/view?view=blind-review&session={sid}")
    check("切换会话视角生效", s == 200 and d and d.get("view") == "blind-review")
    s, d = jreq("GET", "/api/sessions")
    row = next((x for x in d["items"] if x["id"] == sid), None) if d else None
    check("会话列表反映视角", row is not None and row["view"] == "blind-review")
    s, d = jreq("POST", "/api/view?view=normal")
    check("缺 session 的视角请求被拒", s == 400)

    # ---- 待定区 / 回收站 ----
    s, d = jreq("GET", "/api/staged")
    check("待定区可读", s == 200 and d and isinstance(d["items"], list))
    s, d = jreq("GET", "/api/trash?limit=5")
    check("回收站可读", s == 200 and d and isinstance(d["items"], list))

    # ---- 跨站防护 ----
    s, _ = req("POST", "/api/mode?mode=normal", headers={"X-OwnMem-UI": ""})
    check("缺少专有头的 POST 被拒（CSRF）", s == 403)
    s, _ = req("GET", "/api/overview", headers={"Host": "evil.example"})
    check("非法 Host 被拒（DNS 重绑定）", s == 403)
    s, _ = req("GET", "/api/overview", headers={"Origin": "http://evil.example"})
    check("非法 Origin 被拒", s == 403)
    s, _ = req("GET", "/api/definitely-not-a-route")
    check("未知路由返回 404", s == 404)

    httpd.shutdown()
    httpd.server_close()

    print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
    if FAIL:
        for f in FAIL:
            print("  失败项：" + f)
    print(f"（测试数据目录 {TMP}，可安全删除）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
