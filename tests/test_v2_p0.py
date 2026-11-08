# -*- coding: utf-8 -*-
"""OwnMem v2 · P0 底座测试：三本账 / 项目隔离 / 会话沉淀。

锁住四件事：
  1. 旧库（v1）升级到 v2 是无损的：老记忆自动落成「结论账 + 全局」
  2. 三本账字段能写、能按账过滤，旧数据默认结论账
  3. 项目隔离只挡别的事，不挡红线——全局记忆（project=''）永远可见
  4. 会话沉淀：会话结束必落盘；同项目新会话第一轮注入、第二轮不再重复注入

跑法：python tests/test_v2_p0.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-v2-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import db, digest, sessions  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.gateway import policy, recall  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"✅ {name}")
    else:
        FAIL.append(f"{name}｜{detail}")
        print(f"❌ {name}｜{detail}")


def make_v1_db(path: Path) -> None:
    """手搓一个 v1 老库：没有 kind / project，也没有 session_digests。"""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE memories (
        id TEXT PRIMARY KEY, content TEXT NOT NULL,
        importance TEXT NOT NULL DEFAULT 'normal', tags TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL DEFAULT 'unknown', agent TEXT NOT NULL DEFAULT 'unknown',
        session_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active');
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, agent TEXT NOT NULL DEFAULT 'unknown',
        mode TEXT NOT NULL DEFAULT 'normal', started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, ended_at TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '');
    CREATE TABLE audit_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT 'unknown', session_id TEXT NOT NULL DEFAULT '',
        memory_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '');
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO memories(id, content, importance, created_at, status)
        VALUES('m_old', '老记忆：电脑维护红线', 'always', '2026-10-01T10:00:00+08:00', 'active');
    PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()


def main() -> int:
    # ---------- 1. 旧库升级 ----------
    old_path = Path(TMP) / "old-v1.db"
    make_v1_db(old_path)
    conn_old = db.connect(old_path)
    cols = {r[1] for r in conn_old.execute("PRAGMA table_info(memories)").fetchall()}
    check("v1 旧库升级后补上 kind 列", "kind" in cols, str(sorted(cols)))
    check("v1 旧库升级后补上 project 列", "project" in cols, str(sorted(cols)))
    check("v1 旧库升级后建出 session_digests 表",
          conn_old.execute(
              "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='session_digests'"
          ).fetchone()[0] == 1)
    row = conn_old.execute("SELECT kind, project FROM memories WHERE id='m_old'").fetchone()
    check("老记忆默认落成结论账 + 全局", (row["kind"], row["project"]) == ("conclusion", ""),
          f"{row['kind']}/{row['project']}")
    check("升级后 user_version 升到当前版本",
          conn_old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION)

    # ---------- 2. 三本账 ----------
    conn = db.connect()
    store = MemoryStore(conn)
    sid = sessions.new_session_id()
    sessions.touch_session(conn, sid, agent="t")

    m_c, _ = store.add("v2 定稿：九件功能清单", agent="t", kind="conclusion", skip_dedup=True)
    m_p, _ = store.add("v2 讨论过程：先想抄角色卡，后来砍了", agent="t",
                       kind="process", skip_dedup=True)
    m_k, _ = store.add("踩坑：角色卡不提升准确率，别再做", agent="t",
                       kind="pitfall", skip_dedup=True)
    check("三本账都能写入", all([m_c, m_p, m_k]))
    check("kind 落库正确",
          (m_c["kind"], m_p["kind"], m_k["kind"]) == ("conclusion", "process", "pitfall"))
    check("非法 kind 回落为结论账",
          store.add("随便一条", agent="t", kind="瞎写", skip_dedup=True)[0]["kind"]
          == "conclusion")
    only_pit = store.search("角色卡", limit=5, kind="pitfall")
    check("可按账过滤检索", only_pit and all(m["kind"] == "pitfall" for m, _ in only_pit),
          str([(m["kind"], round(s, 2)) for m, s in only_pit]))

    # ---------- 3. 项目隔离 ----------
    store.add("A 项目：科研读文献的进度", agent="t", project="research", skip_dedup=True)
    store.add("B 项目：小红书推文选题", agent="t", project="xhs", skip_dedup=True)
    store.add("全局：隐私红线 别用我账号", agent="t", importance="always", skip_dedup=True)

    hits_a = store.search("项目进度 选题 红线", limit=10, project="research")
    got_projects = {m.get("project") or "" for m, _ in hits_a}
    check("A 项目只看得到 A + 全局", got_projects <= {"research", ""}, str(sorted(got_projects)))
    check("A 项目看不到 B 项目的记忆", "xhs" not in got_projects, str(sorted(got_projects)))
    check("全局记忆在任何项目都能召回", "" in got_projects, str(sorted(got_projects)))

    hits_b = store.search("项目进度 选题", limit=10, project="xhs")
    check("B 项目看不到 A 项目的记忆",
          {m.get("project") or "" for m, _ in hits_b} <= {"xhs", ""},
          str(sorted({m.get("project") or "" for m, _ in hits_b})))

    # 常驻层也要守项目隔离
    res_research = recall.recall_context(conn, "今天聊点啥", session_id=sid, agent="t",
                                        project="research", log=False)
    check("常驻层在项目隔离下仍然注入全局红线",
          "隐私红线" in res_research["context"], res_research["context"][:120])

    # ---------- 4. 会话沉淀 ----------
    sid1 = sessions.new_session_id()
    sessions.touch_session(conn, sid1, agent="t")
    sessions.set_project(conn, sid1, "ownmem", agent="t")
    policy.guarded_write(conn, "OwnMem v2 动了 P0：三账本+项目隔离+沉淀", session_id=sid1,
                         agent="t", kind="conclusion", project="ownmem")
    recall.recall_context(conn, "v2 方案", session_id=sid1, agent="t", log=False)
    sessions.end_session(conn, sid1)
    d1 = digest.save(conn, sid1, agent="t", note="今天把 P0 底座做完了")
    check("会话结束能生成沉淀", bool(d1.get("text")) and d1["project"] == "ownmem")
    check("沉淀里带上了人给的一句话要点", "今天把 P0 底座做完了" in d1["text"])
    check("沉淀里带上了本次写入的内容", "三账本" in d1["text"], d1["text"][:200])
    check("沉淀带账目（召回/写入/拦截）", "账目：" in d1["text"])

    # 幂等用另一个会话验证：同一会话的二次 save 是覆盖（后一次 note 生效），
    # 拿它来测会顺手改掉 sid1 的沉淀正文，让后面的注入断言失真。
    sid_dup = sessions.new_session_id()
    sessions.touch_session(conn, sid_dup, agent="t")
    digest.save(conn, sid_dup, agent="t", note="第一次")
    digest.save(conn, sid_dup, agent="t", note="第二次")
    n = conn.execute("SELECT COUNT(*) FROM session_digests WHERE session_id=?",
                     (sid_dup,)).fetchone()[0]
    check("同一会话重复沉淀不堆平行副本", n == 1, f"rows={n}")

    latest = digest.latest(conn, project="ownmem", exclude_session="")
    check("能取到同项目最近一次沉淀", latest and latest["session_id"] == sid1)

    # 第一轮注入：新会话同项目
    sid2 = sessions.new_session_id()
    sessions.touch_session(conn, sid2, agent="t")
    sessions.set_project(conn, sid2, "ownmem", agent="t")
    first = recall.recall_context(conn, "继续做 v2", session_id=sid2, agent="t")
    check("新会话第一轮自动注入上次进度", "上次进度" in first["context"], first["context"][:200])
    check("注入的正是上个会话的要点", "P0 底座" in first["context"])
    second = recall.recall_context(conn, "还是继续做 v2", session_id=sid2, agent="t")
    check("第二轮不再重复注入上次进度", "上次进度" not in second["context"])

    # 只注入进度、一条记忆都没命中的那种轮次，也不能让进度反复出现
    sid_q = sessions.new_session_id()
    sessions.touch_session(conn, sid_q, agent="t")
    sessions.set_project(conn, sid_q, "ownmem", agent="t")
    q1 = recall.recall_context(conn, "zzz", session_id=sid_q, agent="t")
    q2 = recall.recall_context(conn, "zzz", session_id=sid_q, agent="t")
    check("零命中的首轮仍注入进度", "上次进度" in q1["context"])
    check("零命中的次轮不再注入进度", "上次进度" not in q2["context"], q2["context"][:80])

    # 别的项目看不到这个沉淀
    sid3 = sessions.new_session_id()
    sessions.touch_session(conn, sid3, agent="t")
    sessions.set_project(conn, sid3, "xhs", agent="t")
    other = recall.recall_context(conn, "推文怎么写", session_id=sid3, agent="t")
    check("别的项目不会串到这份沉淀", "P0 底座" not in other["context"])

    # 白纸模式：既不给记忆也不给进度
    sessions.set_mode(conn, sid3, "blank", agent="t")
    blank = recall.recall_context(conn, "推文怎么写", session_id=sid3, agent="t")
    check("白纸模式连沉淀也不注入", blank["blocked"] and not blank["context"])

    # 空会话不落盘：什么也没发生的一次会话，沉淀只是噪音
    sid_empty = sessions.new_session_id()
    sessions.touch_session(conn, sid_empty, agent="t")
    sessions.end_session(conn, sid_empty)
    d_empty = digest.save(conn, sid_empty, agent="t")
    check("空会话不生成沉淀", d_empty.get("skipped") is True)
    check("空会话确实没写进表",
          conn.execute("SELECT COUNT(*) FROM session_digests WHERE session_id=?",
                       (sid_empty,)).fetchone()[0] == 0)

    print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
    for f in FAIL:
        print("  失败项：" + f)
    print(f"（测试数据目录 {TMP}，可安全删除）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
