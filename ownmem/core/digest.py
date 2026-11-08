# -*- coding: utf-8 -*-
"""会话沉淀：会话结束把「这次干到哪了」落成一行，下次开局自动注入。

为什么不用 LLM 总结：OwnMem 的卖点是零依赖、本地、可审计。一次开场注入的
成本里不该包含一次模型调用——既贵又不可复现，摘要质量还取决于当时调的哪个模型。
这里走确定性路径：从审计流水与本次写入里把「已发生的事实」抽出来拼成一段，
宁可朴素，但可查、可复现、不花钱。

它解决的正是「跨对话延续感」这个真痛点：上个会话聊到哪、留了什么，
下个会话第一句话就能看到，不必由人复述一遍。
"""

from __future__ import annotations

import json
import random
import sqlite3
import string
from datetime import datetime

from . import audit, sessions
from .store import KIND_LABELS

MAX_WRITES = 8
MAX_TOPICS = 6
PREVIEW = 90          # 每条写入记忆在沉淀里的摘录长度
TOPIC_PREVIEW = 26    # 话题标签的截断长度
DEFAULT_RENDER_BUDGET = 700   # 注入时的字符上限


def _new_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return "d_" + datetime.now().strftime("%Y%m%d") + "_" + suffix


def _session_row(conn: sqlite3.Connection, session_id: str):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def _writes(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """本次会话真正落盘的记忆（含挑选保存的）。

    只认 write / promote：被闸门拦下的、进待定区的都不算——沉淀必须与
    「记忆库里确实多了什么」严格对齐，否则沉淀会吹牛。
    """
    rows = conn.execute(
        "SELECT m.id, m.content, m.importance, m.kind, MIN(a.seq) AS first_seq "
        "FROM audit_log a JOIN memories m ON m.id = a.memory_id "
        "WHERE a.session_id=? AND a.event IN ('write','promote') AND a.memory_id<>'' "
        "GROUP BY m.id ORDER BY first_seq",
        (session_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "content": r["content"] or "",
            "importance": r["importance"] or "normal",
            "kind": r["kind"] or "conclusion",
        }
        for r in rows
    ]


def _topics(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """本次会话被召回最多的记忆，用作「聊过什么」的近似标签。

    刻意不存用户的原始提问：那是另一类数据（对话内容），存进来会让
    「我只看得到记忆」变成「我还留着你每句话」。这里只记「哪几条记忆被反复用到」。
    """
    rows = conn.execute(
        "SELECT m.content, COUNT(*) AS n "
        "FROM audit_log a JOIN memories m ON m.id = a.memory_id "
        "WHERE a.session_id=? AND a.event='recall' AND a.memory_id<>'' "
        "GROUP BY m.id ORDER BY n DESC LIMIT ?",
        (session_id, MAX_TOPICS),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        text = (r["content"] or "").strip().replace("\n", " ")
        if not text:
            continue
        label = text[:TOPIC_PREVIEW] + ("…" if len(text) > TOPIC_PREVIEW else "")
        if label not in out:
            out.append(label)
    return out


def _span(started: str, ended: str) -> str:
    def fmt(raw: str) -> str:
        try:
            return datetime.fromisoformat(raw).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            return raw[:16] if raw else "?"

    return f"{fmt(started)} → {fmt(ended)}"


def build(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    agent: str = "",
    note: str = "",
) -> dict:
    """生成沉淀内容（不写库）。"""
    row = _session_row(conn, session_id)
    project = (row["project"] if row else "") or sessions.get_project(conn, session_id)
    agent = agent or (row["agent"] if row else "unknown") or "unknown"
    mode = sessions.get_mode(conn, session_id)
    started = (row["started_at"] if row else "") or ""
    ended = audit.now()
    rec = audit.receipt(conn, session_id)
    writes = _writes(conn, session_id)
    topics = _topics(conn, session_id)

    head = (
        f"会话 {session_id[:10]}｜agent={agent}｜项目={project or '全局'}｜"
        f"{_span(started, ended)}｜模式={mode}"
    )
    lines = [head]
    if (note or "").strip():
        # 人给的一句话优先——它是唯一带「意图」的信息，比任何统计都值钱
        lines.append(f"要点：{note.strip()}")
    if writes:
        shown = writes[:MAX_WRITES]
        parts = []
        for w in shown:
            label = KIND_LABELS.get(w["kind"], w["kind"])
            body = w["content"][:PREVIEW] + ("…" if len(w["content"]) > PREVIEW else "")
            parts.append(f"[{label}]{body}")
        more = f"（另有 {len(writes) - len(shown)} 条）" if len(writes) > len(shown) else ""
        lines.append(f"本次写入 {len(writes)} 条：{'；'.join(parts)}{more}")
    if topics:
        lines.append("涉及话题：" + "；".join(topics))
    lines.append(
        f"账目：召回 {rec['recalls']} 次 / 写入 {rec['writes']} 条 / 拦截 {rec['write_blocked']} 次"
    )
    text = "\n".join(lines)
    return {
        "id": _new_id(),
        "session_id": session_id,
        "agent": agent,
        "project": project,
        "mode": mode,
        "started_at": started,
        "ended_at": ended,
        "note": (note or "").strip(),
        "text": text,
        "writes": writes,
        "topics": topics,
        "recalls": rec["recalls"],
        "writes_n": rec["writes"],
        "blocked_n": rec["write_blocked"],
        "created_at": audit.now(),
    }


def save(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    agent: str = "",
    note: str = "",
) -> dict:
    """生成并落库。同一会话重复调用会覆盖旧沉淀（幂等，不堆平行副本）。

    空会话不落盘：既没写入也没召回、人也没留要点的那种会话，留下的一行
    「召回 0 次」对下一个接手的人没有任何价值，只会变成噪音。
    """
    d = build(conn, session_id, agent=agent, note=note)
    if not d["note"] and not d["writes"] and d["recalls"] == 0:
        d["skipped"] = True
        return d
    existing = conn.execute(
        "SELECT id FROM session_digests WHERE session_id=?", (session_id,)
    ).fetchone()
    payload = {
        "id": existing["id"] if existing else d["id"],
        "session_id": session_id,
        "agent": d["agent"],
        "project": d["project"],
        "mode": d["mode"],
        "started_at": d["started_at"],
        "ended_at": d["ended_at"],
        "note": d["note"],
        "text": d["text"],
        "writes": json.dumps(d["writes"], ensure_ascii=False),
        "topics": json.dumps(d["topics"], ensure_ascii=False),
        "recalls": d["recalls"],
        "writes_n": d["writes_n"],
        "blocked_n": d["blocked_n"],
        "created_at": d["created_at"],
    }
    if existing:
        conn.execute(
            "UPDATE session_digests SET agent=:agent, project=:project, mode=:mode, "
            "ended_at=:ended_at, note=:note, text=:text, writes=:writes, topics=:topics, "
            "recalls=:recalls, writes_n=:writes_n, blocked_n=:blocked_n, created_at=:created_at "
            "WHERE session_id=:session_id",
            payload,
        )
    else:
        conn.execute(
            "INSERT INTO session_digests(id, session_id, agent, project, mode, started_at, "
            "ended_at, note, text, writes, topics, recalls, writes_n, blocked_n, created_at) "
            "VALUES(:id,:session_id,:agent,:project,:mode,:started_at,:ended_at,:note,:text,"
            ":writes,:topics,:recalls,:writes_n,:blocked_n,:created_at)",
            payload,
        )
    audit.log(
        conn, "digest", agent=d["agent"], session_id=session_id,
        detail=f"会话沉淀 {len(d['text'])} 字（写入 {d['writes_n']} 条）", mode=d["mode"],
    )
    d["id"] = payload["id"]
    return d


def _row_to_digest(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("writes", "topics"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except (json.JSONDecodeError, TypeError):
            d[key] = []
    return d


def latest(
    conn: sqlite3.Connection,
    *,
    project: str = "",
    exclude_session: str = "",
) -> dict | None:
    """同项目最近一次沉淀。

    跨 agent 也认：上个会话是 WorkBuddy、这个会话是 Claude Code，
    恰恰是最需要交接的场景——沉淀是「给下一个接手的人看」的，不是 agent 私产。
    """
    rows = conn.execute(
        "SELECT * FROM session_digests WHERE project=? AND session_id<>? "
        "ORDER BY created_at DESC LIMIT 1",
        ((project or "").strip(), exclude_session or ""),
    ).fetchall()
    return _row_to_digest(rows[0]) if rows else None


def list_recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM session_digests ORDER BY created_at DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [_row_to_digest(r) for r in rows]


def get(conn: sqlite3.Connection, digest_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM session_digests WHERE id=?", (digest_id,)).fetchone()
    return _row_to_digest(row) if row else None


def render(d: dict, budget: int = DEFAULT_RENDER_BUDGET) -> str:
    """渲染成可注入文本。超预算就截断——开场那点预算不该被上一场会话吃光。"""
    text = (d.get("text") or "").strip()
    if len(text) > budget:
        text = text[:budget] + "…（已截断，完整版用 ownmem digest 查看）"
    return text
