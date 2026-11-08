# -*- coding: utf-8 -*-
"""OwnMem v2 · P2 视角层测试：四档视角 / 先答后对 / 分歧报告。

锁住六件事：
  1. 视角与模式正交：blank/ghost 仍整体断读，view 只在可读时有意义
  2. result-only 隐藏过程账，结论与踩坑照常
  3. blind-review 隐藏结论+过程+上次进度，踩坑与常驻规则照常，提示词在场
  4. cross-check 全量可见 + 对抗提示词在场
  5. 先答后对：independent 屏蔽相关记忆（红线保留）→ submit 存档 → reveal 注入方案原文
  6. 分歧报告：不同 agent 的相似结论配对；同 agent 不配、复述不配、已取代不上桌

跑法：python tests/test_v2_p2.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-v2p2-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import db, digest, divergence, sessions  # noqa: E402
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


def sess(agent: str, project: str = "p2test") -> str:
    sid = sessions.new_session_id()
    sessions.touch_session(conn, sid, agent=agent)
    sessions.set_project(conn, sid, project, agent=agent)
    return sid


def w(content, agent, kind="conclusion", importance="normal", project="p2test"):
    sid = sess(agent, project)
    r = policy.guarded_write(
        conn, content, session_id=sid, agent=agent, kind=kind,
        importance=importance, project=project, channel="explicit",
    )
    sessions.end_session(conn, sid)
    return r


# ============================== 1. 视角基础 ==============================
print("\n--- 1. 视角基础 ---")
check("normalize_view 中文别名", sessions.normalize_view("盲审") == "blind-review"
      and sessions.normalize_view("交叉") == "cross-check"
      and sessions.normalize_view("只看结论") == "result-only")
check("normalize_view 未知值回退 normal", sessions.normalize_view("nonsense") == "normal")

s1 = sess("t")
check("默认视角 normal", sessions.get_view(conn, s1) == "normal")
check("set_view 持久化", sessions.set_view(conn, s1, "blind-review", agent="t") == "blind-review"
      and sessions.get_view(conn, s1) == "blind-review")
sessions.end_session(conn, s1)

# v3 旧库升级：手搓一个没有 view 列的库，确认补列幂等
import sqlite3 as _sq  # noqa: E402
oldp = Path(TMP) / "old_v3.db"
oc = _sq.connect(str(oldp))
oc.executescript("""
CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL,
    importance TEXT NOT NULL DEFAULT 'normal', tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'unknown', agent TEXT NOT NULL DEFAULT 'unknown',
    session_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'conclusion',
    project TEXT NOT NULL DEFAULT '', superseded_by TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT '', recurred INTEGER NOT NULL DEFAULT 0,
    recurred_agents TEXT NOT NULL DEFAULT '[]', last_recurred_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT NOT NULL DEFAULT 'unknown',
    mode TEXT NOT NULL DEFAULT 'normal', project TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
CREATE TABLE session_digests (id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
    agent TEXT NOT NULL DEFAULT 'unknown', project TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
    writes TEXT NOT NULL DEFAULT '[]', topics TEXT NOT NULL DEFAULT '[]',
    recalls INTEGER NOT NULL DEFAULT 0, writes_n INTEGER NOT NULL DEFAULT 0,
    blocked_n INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    event TEXT NOT NULL, agent TEXT NOT NULL DEFAULT 'unknown',
    session_id TEXT NOT NULL DEFAULT '', memory_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT '');
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO memories(id, content, created_at) VALUES('m_old1', '旧库内容', '2026-01-01T00:00:00+08:00');
INSERT INTO sessions(id, agent, started_at, updated_at) VALUES('s_old', 't', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00');
""")
oc.commit()
oc.close()
oc = db.connect(oldp)
check("v3 旧库升级补 view 列", oc.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
      and "view" in {r[1] for r in oc.execute("PRAGMA table_info(sessions)")})
check("旧会话视角默认 normal", sessions.get_view(oc, "s_old") == "normal")
oc.close()

# ============================== 2. 视角过滤 ==============================
print("\n--- 2. 视角过滤 ---")
# 准备三本账各一条 + 一条 always 常驻 + 一个沉淀
r_con = w("结论：推文发布窗口定在 19:00 到 21:00", "claude", kind="conclusion", importance="high")
r_pro = w("过程：当时对比了三个时段的数据才定的窗口", "claude", kind="process")
r_pit = w("踩坑：小红书封面用纯文字截图会被判低质不给推荐", "claude", kind="pitfall", importance="high")
r_alw = w("常驻：任何需要登录他账号的方案一律否决", "user", kind="conclusion", importance="always", project="")
r_pit2 = w("踩坑：小红书封面用纯文字截图会被判低质不给推荐流量，要配实拍图", "codex", kind="pitfall", importance="high")

# 沉淀一条（含结论）用于验证盲审屏蔽
sid_d = sess("claude")
policy.guarded_write(conn, "上次会话结论：栏目定为人间观察", session_id=sid_d, agent="claude", project="p2test")
sessions.end_session(conn, sid_d)
digest.save(conn, sid_d, agent="claude")

PROMPT = "小红书 推文 发布窗口 封面 怎么定"

def recall_as(view, sid=None, prompt=PROMPT):
    sid = sid or sess("gemini")
    if view:
        sessions.set_view(conn, sid, view, agent="t")
    r = recall.recall_context(conn, prompt, session_id=sid, agent="gemini")
    sessions.end_session(conn, sid)
    return r

r_n = recall_as("normal")
check("normal 全量：结论可见", r_con["id"] in r_n["context"])
check("normal 全量：过程可见", r_pro["id"] in r_n["context"])
check("normal 全量：无视角提示", "盲审" not in r_n["context"] and "交叉验证" not in r_n["context"])

r_ro = recall_as("result-only")
check("result-only 隐藏过程账", r_pro["id"] not in r_ro["context"])
check("result-only 保留结论账", r_con["id"] in r_ro["context"])
check("result-only 保留踩坑账", r_pit2["id"] in r_ro["context"])

r_bl = recall_as("blind-review")
check("blind-review 隐藏结论账", r_con["id"] not in r_bl["context"])
check("blind-review 隐藏过程账", r_pro["id"] not in r_bl["context"])
check("blind-review 保留踩坑账", r_pit2["id"] in r_bl["context"])
check("blind-review 保留常驻规则", "登录他账号" in r_bl["context"])
check("blind-review 注入提示词", "盲审模式" in r_bl["context"])
check("blind-review 屏蔽上次进度", "上次进度" not in r_bl["context"])
check("blind-review 来源不豁免：自己 agent 的旧结论也藏",
      all("人间观察" not in p for p in [r_bl["context"]]))

r_cc = recall_as("cross-check")
check("cross-check 全量可见", r_con["id"] in r_cc["context"] and r_pro["id"] in r_cc["context"])
check("cross-check 对抗提示在场", "可能是错的" in r_cc["context"] and "验证" in r_cc["context"])

# 模式优先：blank 下视角不生效（整体断读）
s_b = sess("gemini")
sessions.set_view(conn, s_b, "cross-check", agent="t")
sessions.set_mode(conn, s_b, "blank", agent="t")
r_blk = recall.recall_context(conn, PROMPT, session_id=s_b, agent="gemini")
check("blank 模式整体断读，视角不生效", r_blk["blocked"] and not r_blk["context"])
sessions.end_session(conn, s_b)

# ============================== 3. 先答后对 ==============================
print("\n--- 3. 先答后对 ---")
s_af = sess("gemini")
sessions.af_set_phase(conn, s_af, "independent", agent="t")
r_ind = recall.recall_context(conn, PROMPT, session_id=s_af, agent="gemini")
check("independent 屏蔽相关记忆", r_con["id"] not in r_ind["context"] and r_pit2["id"] not in r_ind["context"])
check("independent 保留常驻红线", "登录他账号" in r_ind["context"])
check("independent 注入第 1 阶段提示", "先答后对" in r_ind["context"] and "独立" in r_ind["context"])
check("independent 屏蔽上次进度", "上次进度" not in r_ind["context"])

# submit 独立方案
res_sub = policy.guarded_write(
    conn, "我的独立方案：窗口定在 21:00 之后，学生晚自习结束才有空刷手机",
    session_id=s_af, agent="gemini", kind="process", tags="answer-first",
    source="answer-first", channel="explicit",
)
check("submit 写入成功", res_sub["ok"])
sessions.af_set_answer(conn, s_af, res_sub["id"])
sessions.af_set_phase(conn, s_af, "compare", agent="t")
r_cmp = recall.recall_context(conn, PROMPT, session_id=s_af, agent="gemini")
check("compare 注入独立方案原文", "21:00" in r_cmp["context"] and "我的独立方案" in r_cmp["context"])
check("compare 旧结论恢复可见", r_con["id"] in r_cmp["context"])
check("compare 注入对照指令", "分歧" in r_cmp["context"])
sessions.af_clear(conn, s_af)
sessions.end_session(conn, s_af)

# ============================== 4. 分歧报告 ==============================
print("\n--- 4. 分歧报告 ---")
# 注意：三句必须措辞差异够大，否则被判重通道拦下（同义改写进不了库）——
# 这本身就是正确行为：库里不该有措辞相近的平行条目，分歧报告配的是
# 「同话题下说法不同的」条目。
r_f1 = w("推文栏目更新频率定为每周三更，周二周四周六各发一条", "claude", importance="high")
r_f2 = w("栏目改成每周两更，先把单篇质量做上去再考虑提频", "codex", importance="high")
r_f3 = w("栏目改成日更，每天发一条蹭热点词涨流量", "gemini", importance="high")
w("今天中午吃食堂二楼", "claude")  # 无关内容（BM25 也不会召回它）
# 高频复述（相似度 >0.90）：正常写入会被判重拦截，这里直接绕过判重模拟
# 「库中已存在的平行复述」——分歧报告的上限应当把它滤掉。
r_copy = store.add(
    "推文栏目更新频率定为每周三更，周二、周四、周六各发一条",
    agent="davinci", kind="conclusion", importance="high", project="p2test",
    skip_dedup=True,
)[0]

rep = divergence.report(conn, "栏目 更新 频率", project="p2test")
check("分歧配对非空", len(rep["pairs"]) >= 2, f"pairs={len(rep['pairs'])}")
check("同 agent 不配对", all(p["a"]["agent"] != p["b"]["agent"] for p in rep["pairs"]))
check("复述×原文这对（sim≈1.0）不进窗口",
      all({r_copy["id"], r_f1["id"]} != {p["a"]["id"], p["b"]["id"]} for p in rep["pairs"]),
      f"copy={r_copy['id']}, f1={r_f1['id']}, "
      f"pairs={[(p['a']['id'], p['b']['id'], p['sim']) for p in rep['pairs']]}")
check("无关内容不进报告", all("食堂" not in p["a"]["content"] + p["b"]["content"] for p in rep["pairs"]))
check("三个 agent 的频率说法两两成对",
      {r_f1["id"], r_f2["id"], r_f3["id"]} <=
      {p["a"]["id"] for p in rep["pairs"]} | {p["b"]["id"] for p in rep["pairs"]},
      f"found={[(p['a']['agent'], p['b']['agent']) for p in rep['pairs']]}")
check("报告可渲染", "疑似分歧" in divergence.render(rep))

# 已取代的不上桌：codex 的说法被推翻后，它不应再出现在新报告里
r_sup = policy.guarded_write(
    conn, "栏目节奏最终拍板：每周两更，质量优先", session_id=sess("user"),
    agent="user", importance="high", project="p2test", kind="conclusion",
    supersedes=r_f2["id"],
)
check("取代写入成功", r_sup["ok"], r_sup.get("message", ""))
rep2 = divergence.report(conn, "栏目 更新 频率", project="p2test")
ids_in = {p["a"]["id"] for p in rep2["pairs"]} | {p["b"]["id"] for p in rep2["pairs"]}
check("已被取代的结论不再上桌", r_f2["id"] not in ids_in,
      f"superseded={r_f2['id']}, in={r_f2['id'] in ids_in}")
text = divergence.render(rep2)
check("报告可渲染（含取代后）", "疑似分歧" in text and "supersede" in text)

# ============================== 汇总 ==============================
print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
if FAIL:
    for f in FAIL:
        print("  失败项:", f)
    sys.exit(1)
