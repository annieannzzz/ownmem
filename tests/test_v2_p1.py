# -*- coding: utf-8 -*-
"""OwnMem v2 · P1 治理层测试：取代链 / 踩坑计数器 / 衰减晋级 / 注入预算。

锁住四件事：
  1. 取代链：旧结论作废但不删，召回里不再出现，chain 能两头追
  2. 踩坑计数器：同一个坑再踩不新增条目，只累加复发次数与「谁栽过」
  3. 衰减与晋级：低重要度老得更快；过程账被反复用够次数才升级
  4. 注入预算：超预算的长条被跳过，后面的短条仍能进来（不是一刀 break）

跑法：python tests/test_v2_p1.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-p1-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import db, sessions  # noqa: E402
from ownmem.core.search import pitfall_boost  # noqa: E402
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


conn = db.connect()
store = MemoryStore(conn)


def new_session(project: str = "", agent: str = "t") -> str:
    sid = sessions.new_session_id()
    sessions.touch_session(conn, sid, agent=agent)
    if project:
        sessions.set_project(conn, sid, project, agent=agent)
    return sid


# ===========================================================================
print("\n== 一、取代链：推翻不删 ==")
# ===========================================================================
sid = new_session()
old, _ = store.add("OwnMem 的召回预算固定在 2400 字", session_id=sid, agent="claude",
                   kind="conclusion", importance="high")
res = policy.guarded_write(
    conn, "OwnMem 的召回预算改为可配置，默认 2400 字", session_id=sid, agent="codex",
    kind="conclusion", importance="high", supersedes=old["id"],
)
new_id = res["id"]
check("取代后新条写入成功", res["ok"] and bool(new_id), res["message"])
old_row = store.get(old["id"])
check("旧条没被删除，只是标记作废",
      bool(new_id) and old_row is not None and old_row["superseded_by"] == new_id,
      f"superseded_by={old_row.get('superseded_by') if old_row else 'None'}")
check("旧条 status 仍是 active（可查、可追溯）", old_row["status"] == "active")
new_row = store.get(new_id)
check("新条记着自己推翻了谁", new_row.get("supersedes") == old["id"])

top = store.search("召回预算 2400 字", limit=10, count_hits=False)
ids = [m["id"] for m, _ in top]
check("召回里不再出现被取代的旧条", old["id"] not in ids, f"ids={ids}")
check("召回里能看到新条", new_id in ids, f"ids={ids}")

top_all = store.search("召回预算 2400 字", limit=10, count_hits=False, include_superseded=True)
check("考古模式能看到旧条", old["id"] in [m["id"] for m, _ in top_all])

ch = store.chain(old["id"])
check("chain 从旧条能追到新条", [m["id"] for m in ch["newer"]] == [new_id])
ch2 = store.chain(new_id)
check("chain 从新条能追回旧条", [m["id"] for m in ch2["older"]] == [old["id"]])

again = policy.guarded_write(
    conn, "再改一次预算，改成 3000 字", session_id=sid, agent="gemini",
    kind="conclusion", supersedes=old["id"],
)
check("已被取代过的旧条不能再被取代一次", not again["ok"], again["message"])

# 链式：第二次推翻应当接在最新那版上
res2 = policy.guarded_write(
    conn, "OwnMem 的召回预算默认值定为 3000 字", session_id=sid, agent="gemini",
    kind="conclusion", supersedes=new_id,
)
check("链式取代成功（接在最新那版上）", res2["ok"], res2["message"])
ch3 = store.chain(new_id)
check("中间那版两头都有链", ch3["older"] and ch3["newer"], f"{len(ch3['older'])}/{len(ch3['newer'])}")
check("整条链共 3 条", len(store.chain(res2["id"])["older"]) == 2)


# ===========================================================================
print("\n== 二、踩坑计数器：同坑再踩只记账不堆条 ==")
# ===========================================================================
sid = new_session()
p1, _ = store.add("踩坑：在 Windows 上用 rm -rf 清构建产物会把路径吃掉",
                  session_id=sid, agent="claude", kind="pitfall", importance="normal")
before = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
sid_b = new_session()
res = policy.guarded_write(
    conn, "踩坑：在 Windows 上用 rm -rf 清构建产物会把路径吃掉，改用 rimraf",
    session_id=sid_b, agent="codex", kind="pitfall",
)
after = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
check("复发不新增条目", before == after, f"{before} → {after}")
check("复发被当作成功吸收（ok=True）", res["ok"], res["message"])
check("复发计数 = 1", res.get("recurrence", {}).get("recurred") == 1, str(res.get("recurrence")))
row = store.get(p1["id"])
check("两个 agent 都记在「栽过」名单里",
      set(row["recurred_agents"]) == {"claude", "codex"}, str(row["recurred_agents"]))
check("复发行有审计留痕",
      conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='recur' AND memory_id=?",
                   (p1["id"],)).fetchone()["c"] >= 1)

# 同一个 agent 再踩不重复记名，但计数仍加
store.add("踩坑：在 Windows 上用 rm -rf 清构建产物会把路径吃掉", session_id=sid_b,
          agent="codex", kind="pitfall")
row = store.get(p1["id"])
check("同 agent 复发计数继续累加", row["recurred"] == 2, f"recurred={row['recurred']}")
check("「栽过」名单不重复记同一个 agent", row["recurred_agents"].count("codex") == 1,
      str(row["recurred_agents"]))

boost_single = pitfall_boost({"kind": "pitfall", "recurred": 1, "recurred_agents": ["claude"]})
boost_cross = pitfall_boost({"kind": "pitfall", "recurred": 1,
                             "recurred_agents": ["claude", "codex"]})
check("复发会抬高踩坑账权重", boost_single > 1.0, f"{boost_single}")
check("跨 agent 的坑权重更高", boost_cross > boost_single, f"{boost_single} vs {boost_cross}")
check("非踩坑账不吃这个加成", pitfall_boost({"kind": "conclusion", "recurred": 9}) == 1.0)


# ===========================================================================
print("\n== 三、衰减：重要度低的老得更快 ==")
# ===========================================================================
old_time = (datetime.now().astimezone() - timedelta(days=150)).isoformat(timespec="seconds")
low, _ = store.add("低价值的临时流水一条", importance="low", session_id=new_session(), agent="t")
high, _ = store.add("高价值的关键决策一条", importance="high", session_id=new_session(), agent="t")
for mid in (low["id"], high["id"]):
    conn.execute("UPDATE memories SET created_at=? WHERE id=?", (old_time, mid))
low_sc = store.search("低价值的临时流水一条", limit=10, count_hits=False)
high_sc = store.search("高价值的关键决策一条", limit=10, count_hits=False)
low_score = next((s for m, s in low_sc if m["id"] == low["id"]), 0)
high_score = next((s for m, s in high_sc if m["id"] == high["id"]), 0)
# 两句话长度接近，分数差异主要来自重要度与衰减曲线
ratio = (high_score / low_score) if low_score else 0
check("150 天后 high 的分明显高于 low", ratio > 2.0, f"high={high_score:.2f} low={low_score:.2f} 比={ratio:.1f}")

# 衰减地板：极老的 low 也不会掉到 0（还能被找到，只是靠后）
ancient = (datetime.now().astimezone() - timedelta(days=3000)).isoformat(timespec="seconds")
conn.execute("UPDATE memories SET created_at=? WHERE id=?", (ancient, low["id"]))
still = store.search("低价值的临时流水一条", limit=10, count_hits=False)
check("极老记忆不会被衰减到彻底消失", low["id"] in [m["id"] for m, _ in still])

rep = store.decay_report(floor=0.4)
check("衰减体检能列出快被忘掉的条目",
      low["id"] in [x["id"] for x in rep["faded"]], f"{len(rep['faded'])} 条")


# ===========================================================================
print("\n== 四、晋级：过程账用够次数才升结论 ==")
# ===========================================================================
proc, _ = store.add("讨论备忘：v2 先做底座再谈视角", kind="process",
                    session_id=new_session(), agent="claude")
conn.execute("UPDATE memories SET hits=8 WHERE id=?", (proc["id"],))
for i in range(3):
    conn.execute(
        "INSERT INTO audit_log(ts, event, agent, session_id, memory_id, detail, mode) "
        "VALUES(?, 'recall', 'claude', ?, ?, '', 'normal')",
        (datetime.now().astimezone().isoformat(timespec="seconds"), f"s_fake_{i}", proc["id"]),
    )
dry = store.promote_repeated(dry_run=True)
check("预演能挑出够格的过程账", proc["id"] in [c["id"] for c in dry["candidates"]])
check("预演不改库", store.get(proc["id"])["kind"] == "process")

weak, _ = store.add("讨论备忘：还没被用够次数的想法", kind="process",
                    session_id=new_session(), agent="claude")
conn.execute("UPDATE memories SET hits=2 WHERE id=?", (weak["id"],))
done = store.promote_repeated()
check("正式执行后晋级 1 条", done["promoted"] == 1, str(done["promoted"]))
check("够格的那条变成结论账", store.get(proc["id"])["kind"] == "conclusion")
check("不够格的仍是过程账", store.get(weak["id"])["kind"] == "process")
check("晋级有审计留痕",
      conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='kind_promote' AND memory_id=?",
                   (proc["id"],)).fetchone()["c"] == 1)
check("结论账不会被反向降级", store.promote_repeated()["promoted"] == 0)


# ===========================================================================
print("\n== 五、注入预算：跳过长的，别堵死短的 ==")
# ===========================================================================
sid = new_session()
long_text = "预算挤压测试长条目：" + ("啰嗦的细节说明" * 220)   # ~1500 字
store.add(long_text, importance="high", session_id=sid, agent="t")
for i in range(3):
    store.add(f"预算挤压测试短条目{i}：结论是 A", importance="high", session_id=sid, agent="t")

res = recall.recall_context(conn, "预算挤压测试", session_id=sid, agent="t", char_budget=1000)
hit_ids = res["memories"]
long_id = [m["id"] for m, _ in store.search("预算挤压测试长条目", limit=20, count_hits=False)
           if len(m["content"]) > 1000][0]
check("超预算的长条被跳过", long_id not in hit_ids, str(hit_ids))
check("长条之后的短条仍然进得来", len(hit_ids) >= 2, f"命中 {len(hit_ids)} 条")
check("返回里带了预算报告", "budget" in res and res["budget"]["used"] > 0)
check("实际用量没超过预算太多", res["budget"]["used"] <= 1200, str(res["budget"]))

db.set_setting(conn, "recall_budget", "800")
res2 = recall.recall_context(conn, "预算挤压测试", session_id=sid, agent="t")
check("settings 里的预算覆盖默认值", res2["budget"]["char_budget"] == 800, str(res2["budget"]))
db.set_setting(conn, "recall_budget", "2400")

alw = recall.DEFAULT_ALWAYS_BUDGET
db.set_setting(conn, "always_budget", "3000")
res3 = recall.recall_context(conn, "预算挤压测试", session_id=new_session(), agent="t")
check("常驻预算也能从 settings 调", res3["budget"]["always_budget"] == 3000, str(res3["budget"]))
db.set_setting(conn, "always_budget", str(alw))


# ===========================================================================
print("\n== 六、旧库升级到 v3 是无损的 ==")
# ===========================================================================
old_path = Path(TMP) / "old_v2.db"
c = sqlite3.connect(str(old_path))
c.executescript(
    """
    CREATE TABLE memories (
        id TEXT PRIMARY KEY, content TEXT NOT NULL,
        importance TEXT NOT NULL DEFAULT 'normal', tags TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL DEFAULT 'unknown', agent TEXT NOT NULL DEFAULT 'unknown',
        session_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'conclusion',
        project TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active');
    INSERT INTO memories(id, content, created_at) VALUES('m_old1', '老库里的结论', '2026-10-01T10:00:00+08:00');
    PRAGMA user_version = 2;
    """
)
c.commit()
c.close()
c2 = db.connect(old_path)
row = c2.execute("SELECT * FROM memories WHERE id='m_old1'").fetchone()
check("旧条升级后 superseded_by 为空（没被误判作废）", row["superseded_by"] == "")
check("旧条升级后 recurred = 0", row["recurred"] == 0)
check("旧条升级后 recurred_agents 是空列表", row["recurred_agents"] == "[]")
check("旧库版本号被抬到当前版本", c2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION)
old_store = MemoryStore(c2)
top = old_store.search("老库里的结论", limit=5, count_hits=False)
check("旧数据仍能被召回", [m["id"] for m, _ in top] == ["m_old1"])


print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
if FAIL:
    print("  失败项：" + "\n           ".join(FAIL))
print(f"（测试数据目录 {TMP}，可安全删除）")
sys.exit(1 if FAIL else 0)
