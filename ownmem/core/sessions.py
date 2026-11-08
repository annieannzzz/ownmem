# -*- coding: utf-8 -*-
"""会话与模式：OwnMem 的「无痕开关」在这里落成状态。

两种能力拆成两个独立开关，模式只是预设组合：

    模式       读记忆(recall)   写记忆(write)   适用场景
    normal         ✅              ✅          日常
    incognito      ✅              ❌          记录但不留痕（浏览器无痕）
    blank          ❌              ✅          零偏见回答，产出可沉淀
    ghost          ❌              ❌          全断开：既不看也不记

关键原则：隐身 ≠ 失智。会话内的临时上下文照常工作，分界线只在「落盘」那一刻。
"""

from __future__ import annotations

import os
import sqlite3
import uuid

from . import audit, db

MODES = {
    "normal": {"read": True, "write": True, "label": "正常", "desc": "常规：可读可写"},
    "incognito": {"read": True, "write": False, "label": "隐身", "desc": "可读当地素材，但不写入长期记忆"},
    "blank": {"read": False, "write": True, "label": "白纸", "desc": "不注入任何旧记忆，产出可沉淀"},
    "ghost": {"read": False, "write": False, "label": "双断", "desc": "既不读也不写，彻底断开"},
}

MODE_ALIASES = {
    "off": "normal",
    "on": "incognito",
    "private": "incognito",
    "白纸": "blank",
    "隐身": "incognito",
    "正常": "normal",
    "双断": "ghost",
}


# ---------------------------------------------------------------------------
# 会话视角（v2 3.9）：mode 管「能不能读」，view 管「读的时候看到什么」。
# 两个维度正交——blank/ghost 整体断读，view 只在可读时有意义。
# ---------------------------------------------------------------------------
VIEWS = {
    "normal": {"label": "全量", "desc": "所有账本都可见（接着上一家干）"},
    "result-only": {"label": "只看结论", "desc": "隐藏过程账；结论与踩坑照常（接手但保留判断）"},
    "blind-review": {"label": "盲审", "desc": "隐藏结论+过程+上次进度，踩坑账与常驻规则照常（挑毛病/发散用）"},
    "cross-check": {"label": "交叉验证", "desc": "全量可见，但注入对抗提示：旧结论可能是错的，逐条验证再采信"},
}

VIEW_ALIASES = {
    "blind": "blind-review",
    "review": "blind-review",
    "盲审": "blind-review",
    "cross": "cross-check",
    "check": "cross-check",
    "交叉": "cross-check",
    "results": "result-only",
    "only": "result-only",
    "结论": "result-only",
    "只看结论": "result-only",
    "全量": "normal",
}


def normalize_view(view: str) -> str:
    v = (view or "").strip().lower().replace("_", "-")
    return VIEW_ALIASES.get(v, v if v in VIEWS else "normal")


def get_view(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute('SELECT view FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row and row["view"]:
        return normalize_view(row["view"])
    return "normal"


def set_view(
    conn: sqlite3.Connection,
    session_id: str,
    view: str,
    *,
    agent: str = "unknown",
) -> str:
    view = normalize_view(view)
    touch_session(conn, session_id, agent=agent)
    conn.execute(
        'UPDATE sessions SET view=?, updated_at=? WHERE id=?',
        (view, audit.now(), session_id),
    )
    audit.log(
        conn, "view", agent=agent, session_id=session_id,
        detail=f"视角切换为 {view}（{VIEWS[view]['label']}）", mode=get_mode(conn, session_id),
    )
    return view


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    return MODE_ALIASES.get(m, m if m in MODES else "normal")


def new_session_id() -> str:
    return "s_" + uuid.uuid4().hex[:12]


def touch_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    agent: str = "unknown",
    create: bool = True,
) -> dict:
    """取会话；不存在则创建。返回会话 dict。"""
    session_id = (session_id or "").strip() or new_session_id()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        if not create:
            return {
                "id": session_id,
                "agent": agent or "unknown",
                "mode": normalize_mode(db.get_setting(conn, "default_mode", "normal")),
                "project": db.get_setting(conn, "default_project", ""),
                "started_at": audit.now(),
                "updated_at": audit.now(),
                "ended_at": "",
                "note": "",
            }
        # 新会话继承默认模式——否则审计台上设的「默认模式」永远轮不到生效
        initial_mode = normalize_mode(db.get_setting(conn, "default_mode", "normal"))
        initial_project = db.get_setting(conn, "default_project", "")
        conn.execute(
            "INSERT INTO sessions(id, agent, mode, project, started_at, updated_at) VALUES(?,?,?,?,?,?)",
            (session_id, agent or "unknown", initial_mode, initial_project, audit.now(), audit.now()),
        )
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    elif agent and agent != "unknown" and row["agent"] in ("", "unknown"):
        conn.execute("UPDATE sessions SET agent=? WHERE id=?", (agent, session_id))
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row)


def mark_active(conn: sqlite3.Connection, session_id: str) -> None:
    """记一次真实活动：刷新 updated_at，让审计台「最近活动」说的是真话。

    touch_session 只在创建会话和改 agent 归属时写 updated_at，而召回才是
    会话里最频繁的动作——以前会话页显示的「最近活动」其实等于创建时间，
    一个今天还在疯狂召回的会话会显示成几天前。
    """
    if not session_id:
        return
    conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (audit.now(), session_id))


def get_mode(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute("SELECT mode FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row:
        return normalize_mode(row["mode"])
    return normalize_mode(db.get_setting(conn, "default_mode", "normal"))


def set_mode(
    conn: sqlite3.Connection,
    session_id: str,
    mode: str,
    *,
    agent: str = "unknown",
) -> str:
    mode = normalize_mode(mode)
    touch_session(conn, session_id, agent=agent)
    conn.execute("UPDATE sessions SET mode=?, updated_at=? WHERE id=?", (mode, audit.now(), session_id))
    audit.log(conn, "mode", agent=agent, session_id=session_id, detail=f"切换为 {mode}", mode=mode)
    return mode


def can_read(conn: sqlite3.Connection, session_id: str) -> bool:
    return MODES[get_mode(conn, session_id)]["read"]


def can_write(conn: sqlite3.Connection, session_id: str) -> bool:
    return MODES[get_mode(conn, session_id)]["write"]


# ---------------------------------------------------------------------------
# 项目（v2 3.2）：记忆按项目分家，全局记忆（project=''）所有项目通用
# ---------------------------------------------------------------------------
def get_project(conn: sqlite3.Connection, session_id: str) -> str:
    """本会话归属的项目；'' = 全局（不隔离，所有记忆都能召回）。"""
    row = conn.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row:
        return (row["project"] or "").strip()
    return (db.get_setting(conn, "default_project", "") or "").strip()


def set_project(
    conn: sqlite3.Connection,
    session_id: str,
    project: str,
    *,
    agent: str = "unknown",
) -> str:
    project = (project or "").strip()
    touch_session(conn, session_id, agent=agent)
    conn.execute(
        "UPDATE sessions SET project=?, updated_at=? WHERE id=?",
        (project, audit.now(), session_id),
    )
    audit.log(
        conn, "project", agent=agent, session_id=session_id,
        detail=f"项目切换为 {project or '全局'}",
    )
    return project


def resolve_project(
    conn: sqlite3.Connection,
    session_id: str,
    override: str = "",
) -> str:
    """定出本轮该用哪个项目：显式参数 > OWNMEM_PROJECT 环境变量 > 会话设置。

    刻意不从工作目录猜：WorkBuddy 每次会话的工作区是时间戳目录
    （2026-11-06-10-21-45），拿它当项目名等于每条会话一个项目，隔离就成了噪音。
    项目归谁由用户用 `ownmem project` 明确指定，或由接入方通过环境变量约定。
    """
    for candidate in (override, os.environ.get("OWNMEM_PROJECT", "")):
        if (candidate or "").strip():
            return candidate.strip()
    return get_project(conn, session_id)


def end_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET ended_at=?, updated_at=? WHERE id=?", (audit.now(), audit.now(), session_id))
    audit.log(conn, "session_end", session_id=session_id, mode=get_mode(conn, session_id))


# ---------------------------------------------------------------------------
# 先答后对（v2 3.10）：两阶段状态放在 settings（KV 表），不加 schema——
# 它是会话级的临时状态，会话结束就该清掉，不值得动表结构。
#   af:{sid}        = independent | compare
#   af_answer:{sid} = 独立作答那条记忆的 id
# ---------------------------------------------------------------------------
def af_phase(conn: sqlite3.Connection, session_id: str) -> str:
    """'' = 未开启；independent = 独立作答期（屏蔽旧记忆）；compare = 对照期。"""
    return db.get_setting(conn, f"af:{session_id}", "")


def af_set_phase(conn: sqlite3.Connection, session_id: str, phase: str, *, agent: str = "unknown") -> None:
    if phase:
        db.set_setting(conn, f"af:{session_id}", phase)
    else:
        conn.execute("DELETE FROM settings WHERE key=?", (f"af:{session_id}",))
    if phase:
        audit.log(conn, "answer_first", agent=agent, session_id=session_id, detail=f"阶段 → {phase}")


def af_answer_id(conn: sqlite3.Connection, session_id: str) -> str:
    return db.get_setting(conn, f"af_answer:{session_id}", "")


def af_set_answer(conn: sqlite3.Connection, session_id: str, memory_id: str) -> None:
    db.set_setting(conn, f"af_answer:{session_id}", memory_id)


def af_clear(conn: sqlite3.Connection, session_id: str) -> None:
    for key in (f"af:{session_id}", f"af_answer:{session_id}"):
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def list_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM audit_log a WHERE a.session_id=s.id AND a.event='recall') AS recalls, "
        "(SELECT COUNT(*) FROM audit_log a WHERE a.session_id=s.id AND a.event='write') AS writes, "
        "(SELECT COUNT(*) FROM audit_log a WHERE a.session_id=s.id AND a.event='write_blocked') AS blocked "
        "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(r) for r in rows]
