# -*- coding: utf-8 -*-
"""OwnMem 咽喉层：所有记忆读写都必须过这道闸。

三件套（共用同一个入口）：
  - recall  自动召回——记忆主动进场，不靠 agent 自觉
  - policy  写入闸门 + 挑选保存——隐身在这里生效
  - audit   审计日志——「没存」是可查的账，不是承诺
"""

from __future__ import annotations

import sqlite3

from ..core import audit, db, sessions
from .policy import discard, guarded_write, list_staged, promote, stage
from .recall import recall_context

__all__ = [
    "recall_context", "guarded_write", "stage", "promote", "discard",
    "list_staged", "status", "session_receipt", "set_mode", "demo_payload",
]


def set_mode(conn: sqlite3.Connection, session_id: str, mode: str, *, agent: str = "unknown") -> str:
    return sessions.set_mode(conn, session_id, mode, agent=agent)


def status(conn: sqlite3.Connection, session_id: str = "", *, agent: str = "unknown") -> dict:
    """OwnMem 当前状态：模式、记忆规模、待定区、最近审计。"""
    from ..core.store import MemoryStore

    sid = session_id or ""
    mode = sessions.get_mode(conn, sid) if sid else sessions.normalize_mode(
        db.get_setting(conn, "default_mode", "normal")
    )
    store = MemoryStore(conn)
    stats = store.stats()
    staged = list_staged(conn, sid) if sid else []
    recent = audit.list_events(conn, session_id=sid, limit=8) if sid else audit.list_events(conn, limit=8)
    return {
        "session_id": sid,
        "mode": mode,
        "read_allowed": sessions.MODES[mode]["read"],
        "write_allowed": sessions.MODES[mode]["write"],
        "memories": stats,
        "staged_count": len(staged),
        "staged": staged,
        "recent_audit": recent,
    }


def session_receipt(conn: sqlite3.Connection, session_id: str) -> dict:
    return audit.receipt(conn, session_id)


def demo_payload(conn: sqlite3.Connection) -> dict:
    """给 WebUI 首页用的轻量概览。"""
    from ..core.store import MemoryStore

    store = MemoryStore(conn)
    return {
        "stats": store.stats(),
        "sessions": sessions.list_sessions(conn, limit=10),
        "audit": audit.list_events(conn, limit=20),
    }
