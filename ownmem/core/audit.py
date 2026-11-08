# -*- coding: utf-8 -*-
"""审计日志：只增不改的事件流水。

这是 OwnMem 可信度的地基——「我没存」不是承诺，是一张可查的账。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime

from . import db

EVENT_LABELS = {
    "recall": "召回注入",
    "recall_blocked": "召回拦截",
    "proxy_inject": "网关注入",
    "write": "写入记忆",
    "write_blocked": "写入拦截",
    "write_triage_reject": "闸门丢弃",
    "write_triage_stage": "闸门转待定",
    "stage": "转入待定",
    "promote": "挑选保存",
    "supersede": "结论取代",
    "recur": "踩坑复发",
    "kind_promote": "账本晋级",
    "delete": "删除记忆",
    "restore": "恢复记忆",
    "purge": "彻底删除",
    "redacted": "审计脱敏",
    "evict": "容量淘汰",
    "wipe": "抹除来源",
    "mode": "模式切换",
    "project": "项目切换",
    "session_end": "会话结束",
    "digest": "会话沉淀",
    "digest_inject": "上次进度注入",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def detail_mode() -> str:
    """审计详情的留存策略：content（默认，存摘录）| hash（只存内容指纹）。"""
    return (os.environ.get("OWNMEM_AUDIT_DETAIL", "content") or "content").strip().lower()


def detail(text: str, limit: int = 120) -> str:
    """审计日志的详情字段：默认截断留摘录；OWNMEM_AUDIT_DETAIL=hash 时只留指纹，
    隐私敏感场景可避免「记忆删了、账本里还有原文残片」。"""
    text = (text or "").strip()
    if not text:
        return ""
    if detail_mode() == "hash":
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text[:limit]


def log(
    conn: sqlite3.Connection,
    event: str,
    *,
    agent: str = "unknown",
    session_id: str = "",
    memory_id: str = "",
    detail: str = "",
    mode: str = "",
) -> None:
    """写一条审计事件。永不修改历史，只追加。"""
    conn.execute(
        "INSERT INTO audit_log(ts, event, agent, session_id, memory_id, detail, mode) "
        "VALUES(?,?,?,?,?,?,?)",
        (now(), event, agent or "unknown", session_id or "", memory_id or "", detail or "", mode or ""),
    )


def _where(
    *,
    session_id: str = "",
    agent: str = "",
    events: list[str] | None = None,
) -> tuple[str, list]:
    """流水与计数共用的筛选片段（两边条件必须字面一致）。"""
    sql = " WHERE 1=1"
    args: list = []
    if session_id:
        sql += " AND session_id=?"
        args.append(session_id)
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    if events:
        sql += f" AND event IN ({','.join('?' * len(events))})"
        args.extend(events)
    return sql, args


def list_events(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    agent: str = "",
    limit: int = 50,
    events: list[str] | None = None,
    offset: int = 0,
) -> list[dict]:
    where, args = _where(session_id=session_id, agent=agent, events=events)
    sql = "SELECT * FROM audit_log" + where + " ORDER BY seq DESC LIMIT ? OFFSET ?"
    args += [max(1, int(limit)), max(0, int(offset))]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count_events(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    agent: str = "",
    events: list[str] | None = None,
) -> int:
    where, args = _where(session_id=session_id, agent=agent, events=events)
    row = conn.execute("SELECT COUNT(*) c FROM audit_log" + where, args).fetchone()
    return row["c"] if row else 0


def receipt(conn: sqlite3.Connection, session_id: str) -> dict:
    """会话回执：读了多少、写了多少、拦了多少。用户可见的「账单」。"""
    rows = conn.execute(
        "SELECT event, COUNT(*) AS n FROM audit_log WHERE session_id=? GROUP BY event",
        (session_id,),
    ).fetchall()
    counts = {r["event"]: r["n"] for r in rows}
    read_ids = {
        r["memory_id"]
        for r in conn.execute(
            "SELECT DISTINCT memory_id FROM audit_log "
            "WHERE session_id=? AND event='recall' AND memory_id<>''",
            (session_id,),
        ).fetchall()
    }
    return {
        "session_id": session_id,
        "recalls": counts.get("recall", 0),
        "recall_blocked": counts.get("recall_blocked", 0),
        "writes": counts.get("write", 0),
        "write_blocked": counts.get("write_blocked", 0),
        "promotes": counts.get("promote", 0),
        "distinct_memories_read": len(read_ids),
        "mode_changes": counts.get("mode", 0),
    }


def receipt_text(r: dict) -> str:
    """把回执渲染成人类可读的一段话。"""
    lines = [
        f"会话 {r['session_id'] or '(未命名)'} 的记忆回执：",
        f"  · 召回注入 {r['recalls']} 次，涉及 {r['distinct_memories_read']} 条记忆"
        + (f"（另有 {r['recall_blocked']} 次被白纸模式拦截）" if r["recall_blocked"] else ""),
        f"  · 写入记忆 {r['writes']} 条"
        + (f"（另有 {r['write_blocked']} 次被隐身模式拦截）" if r["write_blocked"] else ""),
    ]
    if r["promotes"]:
        lines.append(f"  · 挑选保存 {r['promotes']} 条（隐身会话后手动留下）")
    if r["writes"] == 0 and r["promotes"] == 0 and r["write_blocked"] > 0:
        lines.append("  ✅ 本次会话未向长期记忆写入任何内容。")
    elif r["writes"] == 0 and r["promotes"] == 0:
        lines.append("  ✅ 本次会话未向长期记忆写入任何内容。")
    return "\n".join(lines)
