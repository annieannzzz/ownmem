# -*- coding: utf-8 -*-
"""写入侧闸门测试。

起因是一个真实顾虑：如果做成 hook 自动提炼，闲聊碎片会越堆越多，
最后把模型的注意力稀碎掉。所以写入侧必须有闸门——
不是事后清理，是入口配额。这些用例锁的就是那道门的松紧。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

OWNMEM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, OWNMEM_ROOT)

from ownmem.core import audit, db, sessions  # noqa: E402
from ownmem.core import triage  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.gateway import policy  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK   ' if ok else '  FAIL '}{name}" + (f"  ({extra})" if extra else ""))


def active_n(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]


def staged_n(conn: sqlite3.Connection, sid: str) -> int:
    return len(policy.list_staged(conn, sid))


def main() -> int:
    TMP = tempfile.mkdtemp(prefix="ownmem-gate-")
    os.environ["OWNMEM_HOME"] = TMP
    conn = db.connect()
    sid = sessions.new_session_id()
    sessions.set_mode(conn, sid, "normal")
    store = MemoryStore(conn)

    print("== 一、纯文本裁决（不碰库） ==")
    cases_junk = [
        ("你好", "寒暄"),
        ("好的收到", "应答"),
        ("thanks", "英文致谢"),
        ("这是什么？", "提问"),
        ("我该怎么办呢", "疑问句"),
        ("嗯", "过短"),
    ]
    for text, why in cases_junk:
        v, _r = triage.triage(text)
        check(f"丢弃·{why}", v == triage.REJECT, f"{text!r} -> {v}")

    cases_keep = [
        ("电脑维护红线：他就这一台电脑，系统相关一律不碰，pagefile.sys 不删不改不移",
         "规则"),
        ("医用物理实验必须用滨医官方七段式，误差要平均绝对误差不要不确定度", "口径"),
        ("上次把 cos x² 读成 cos²x 被判 0 分，教训是先确认上标落在谁的右上角", "教训"),
    ]
    for text, why in cases_keep:
        v, _r = triage.triage(text)
        check(f"沉淀·{why}", v == triage.ACCEPT, f"-> {v}")

    cases_stage = [
        ("下周一把改性淀粉的样本交给实验室", "时点安排"),
        ("正在整理这学期的资料", "中间过程"),
        ("报告周五交", "过短但不是废话"),
    ]
    for text, why in cases_stage:
        v, _r = triage.triage(text)
        check(f"待定·{why}", v == triage.STAGE, f"-> {v}")

    print()
    print("== 二、auto 通道真的会拦 ==")
    before = active_n(conn)
    r1 = policy.guarded_write(conn, "你好", session_id=sid,
                              agent="test", channel="auto")
    check("纯寒暄被拦且不写长期库", (not r1["ok"]) and active_n(conn) == before,
          r1["message"][:40])
    check("纯寒暄连待定区都不占", staged_n(conn, sid) == 0)

    r2 = policy.guarded_write(conn, "这个方案到底要不要做呢？", session_id=sid,
                              agent="test", channel="auto")
    check("疑问句不被当结论沉淀", (not r2["ok"]) and active_n(conn) == before,
          r2["message"][:30])
    check("疑问句也不占待定区", staged_n(conn, sid) == 0)

    r3 = policy.guarded_write(conn, "下周一把改性淀粉的样本交给实验室", session_id=sid,
                              agent="test", channel="auto")
    check("时点型落待定区", (not r3["ok"]) and r3.get("staged") and active_n(conn) == before)
    check("待定区确实多了一条", staged_n(conn, sid) == 1)

    r4 = policy.guarded_write(
        conn, "他的电脑只有一台，禁止动 pagefile.sys 和系统还原点",
        session_id=sid, agent="test", importance="critical", channel="auto")
    check("含规则信号的直接沉淀", r4["ok"] and active_n(conn) == before + 1)

    print()
    print("== 三、explicit 通道保持原行为（向后兼容） ==")
    before2 = active_n(conn)
    r5 = policy.guarded_write(conn, "你好呀今天天气不错", session_id=sid,
                              agent="test", channel="explicit")
    check("用户显式指定要记的不过闸门", r5["ok"] and active_n(conn) == before2 + 1)

    print()
    print("== 四、always/critical 不被形态筛掉 ==")
    check("should_bypass: always", triage.should_bypass("", "always"))
    check("should_bypass: critical", triage.should_bypass("", "critical"))
    check("should_bypass: normal 不绕过", not triage.should_bypass("", "normal"))

    r6 = policy.guarded_write(conn, "嗯", session_id=sid, agent="test",
                              importance="always", channel="auto")
    check("always 档即使文本极短也放行", r6["ok"], r6["message"][:30])

    print()
    print("== 五、ghost 模式下连待定区都不留 ==")
    gid = sessions.new_session_id()
    sessions.set_mode(conn, gid, "ghost")
    r7 = policy.guarded_write(conn, "下周把材料交到学工办", session_id=gid,
                              agent="test", channel="auto")
    check("ghost 下 staged 为空", (not r7["ok"]) and not r7.get("staged"))
    check("ghost 下待定区零内容", staged_n(conn, gid) == 0)

    print()
    print("== 六、审计留痕可追溯 ==")
    kinds = {r[0] for r in conn.execute("SELECT event FROM audit_log")}
    check("闸门丢弃有审计", "write_triage_reject" in kinds)
    check("闸门转待定有审计", "write_triage_stage" in kinds)

    _ = store
    conn.commit()
    conn.close()

    total = len(PASS) + len(FAIL)
    print()
    print(f"=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败（共 {total}）===")
    for f in FAIL:
        print(f"  失败：{f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
