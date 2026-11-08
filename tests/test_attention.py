# -*- coding: utf-8 -*-
"""注意力预算相关行为的回归测试。

起因是第二轮体检的真实数据：同一个记忆库，聊「学促委推文选题」时召回来的
6 条里 4 条是三周前另一个项目的流水，其中一条还写着「明天（10/11）必须问导员」——
那天的「明天」早就过去了。这不是浪费注意力，是过期信息在冒充当前事实。

注意力是这里最稀缺的资源：注入预算总共约 5000 字符，任何一条不该出现的记忆，
都是在挤掉一条该出现的记忆。本文件锁住三件事：
    1. 常驻规则不必每轮都出场（场景门）
    2. 过了保质期的安排要退到后排（时点型衰减）
    3. 长期事实不能被误判成过期安排（比漏判更危险）
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ownmem.core import db, sessions  # noqa: E402
from ownmem.core.search import has_time_anchor  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.gateway import recall  # noqa: E402

TMP = tempfile.mkdtemp(prefix="ownmem-attn-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import db, sessions  # noqa: E402
from ownmem.core.search import has_time_anchor, is_stale  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.gateway import recall  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {name}" + (f"  → {extra}" if extra and not ok else ""))


def main() -> int:
    conn = db.connect()
    sid = sessions.new_session_id()
    sessions.set_mode(conn, sid, "normal")
    store = MemoryStore(conn)

    # ---- 场景门 ----
    _p, _ = store.add(
        "真红线：任何时候都要生效，不挂场景条件。这条必须无条件出场。",
        importance="always", agent="test", session_id=sid,
        tags="红线",
    )
    pinned_plain = _p["id"]
    _g, _ = store.add(
        "场景型规则：只有聊到指定话题时才需要，其他话题出现在场等于白占预算。",
        importance="always", agent="test", session_id=sid,
        tags="when:高数|数学|导数",
    )
    pinned_gated = _g["id"]

    def injected_ids(q: str) -> set[str]:
        r = recall.recall_context(conn, q, session_id=sid, agent="test", log=False)
        return set(r.get("memories", []))

    hot = injected_ids("这道高数题怎么求导，帮忙算一下")
    cold = injected_ids("学校里的栀子花开了，我去拍张照片")
    check("场景命中时常驻规则出场", pinned_gated in hot)
    check("场景未命中时不出场", pinned_gated not in cold)
    check("无条件红线始终出场", pinned_plain in hot and pinned_plain in cold)

    # 前缀误伤：「怎么」不该命中「怎么做」这类更长的关键词
    store.add(
        "方案类约束：涉及工具选型、预算时才需要看的规则内容。",
        importance="always", agent="test", session_id=sid,
        tags="when:怎么做|方案|预算|免费",
    )
    r = recall.recall_context(
        conn, "这台电脑C盘满了怎么清理一下", session_id=sid, agent="test", log=False)
    body = r["context"]
    check("短词不做前缀匹配误触发", "方案类约束" not in body)

    # 未通过场景门的常驻记忆，不得从「相关记忆」段溜回来
    # （那边措辞是「仅供参考」，会让红线被渲染层偷偷降级）
    gated_ids = {pinned_gated}
    related_part = body.split(recall.RELATED_HEADER)[-1] if recall.RELATED_HEADER in body else ""
    check("被拦下的常驻规则未溜进相关段",
          not any(i in related_part for i in gated_ids) or pinned_gated in hot)

    # ---- 时点型 vs 事实型 ----
    check("相对时间词判为时点型", has_time_anchor("明天必须把报名材料交给导员"))
    check("日期+待办判为时点型", has_time_anchor("10月15日回执截止"))
    check("纯事实日期不算时点型", not has_time_anchor("2026-09-18 明确定下的协作惯例"))
    check("实测日期不算时点型", not has_time_anchor("2026-09-30 实测：C盘可用 144 GB"))
    check("规范中的『必须』不算时点型", not has_time_anchor("大方向必须问他，路径必须用正斜杠"))
    check("空内容不崩", not has_time_anchor(""))

    # 时点型衰减应快于事实型：同为 25 天前写入，第一条会退得更远
    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=25)).isoformat()
    conn.execute(
        "INSERT INTO memories(id,content,importance,tags,source,agent,session_id,"
        "created_at,updated_at,hits,status) VALUES(?,?,'high','[]','t','t','',?,?,0,'active')",
        ("ephem", "时点型：下周要去交报名材料，这件事有明确保质期", old, old))
    conn.execute(
        "INSERT INTO memories(id,content,importance,tags,source,agent,session_id,"
        "created_at,updated_at,hits,status) VALUES(?,?,'high','[]','t','t','',?,?,0,'active')",
        ("factual", "事实型：他偏好结构化输出，这是长期稳定的习惯", old, old))
    conn.commit()
    hits = store.search("他习惯怎么输出内容 报名材料", limit=20, count_hits=False)
    sc = {m["id"]: s for m, s in hits}
    if "ephem" in sc and "factual" in sc:
        check("同内容相似时事实型排在时点型之前", sc["factual"] > sc["ephem"],
              f"factual={sc['factual']:.3f} ephem={sc['ephem']:.3f}")
    else:
        check("同内容相似时事实型排在时点型之前", False,
              f"未同时召回: {list(sc)}")

    # ---- 过期标注 ----
    conn.row_factory = sqlite3.Row
    d = conn.execute("select * from memories where id='ephem'").fetchone()
    check("过期时点型被标注", is_stale(dict(d)))
    d2 = conn.execute("select * from memories where id='factual'").fetchone()
    check("长期事实不被标注过期", not is_stale(dict(d2)))

    print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
    for f in FAIL:
        print("  失败项：" + f)
    print(f"（测试数据目录 {TMP}，可安全删除）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
