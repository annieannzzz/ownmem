# -*- coding: utf-8 -*-
"""会话桥接：让 hook 的召回与 MCP / CLI 的读写落在同一个会话下。

## 为什么需要它

一次对话里通常有两个进程在动记忆：harness 的 hook 子进程负责召回，
MCP server 负责读写。它们必须认成「同一个会话」，否则会话回执会裂成两半——
召回记在 A、写入记在 B，用户拿到的「本次未留下任何记忆」就不可信了。

桥接值统一存 JSON ``{"sid": ..., "ts": ...}``：ts 用来判断新鲜度，
消费方据此拒绝上一段对话的残留。

## 历史包袱

0.3.0 之前，hook 写 JSON 而 CLI 写纯 sid 字符串，同一个 key 混着两种格式；
CLI 按纯字符串读，就把整个 JSON 当成会话 id 用了，副作用是：
  1. 审计台会话页出现一串 JSON 当会话名
  2. 该会话按 default_mode 新建，**而不是用户当前会话的模式**
     ——隐身状态下这条路径能绕过闸门直接写进长期记忆

所以格式必须只有一种，且读写都走这里。read() 对旧格式仍有兼容，
避免用户升级后旧值立刻变成畸形会话。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import audit, db

# 桥接值的新鲜度窗口：hook 每次召回都会刷新 ts，超窗视为上一段对话的残留
DEFAULT_TTL_SECONDS = 12 * 3600


def key(agent: str) -> str:
    """桥接设置的键名。"""
    return f"active_session:{agent or 'unknown'}"


def write(conn: sqlite3.Connection, agent: str, session_id: str) -> None:
    """记下「该 agent 当前活跃的是哪个会话」，供另一侧的进程对齐。"""
    sid = (session_id or "").strip()
    if not sid:
        return
    db.set_setting(
        conn, key(agent),
        json.dumps({"sid": sid, "ts": audit.now()}, ensure_ascii=False),
    )


def read(conn: sqlite3.Connection, agent: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """读出桥接会话；返回空串表示不可用（没有 / 已过期 / 格式坏掉）。

    返回空串时调用方应当新建一个会话，而不是拿一个不知所谓的字符串去用——
    拿错会话的代价比新建会话大得多，那会直接绕过当前会话的模式闸门。
    """
    raw = (db.get_setting(conn, key(agent), "") or "").strip()
    if not raw:
        return ""

    # 新格式：JSON，带时间戳
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            sid = str(data.get("sid", "")).strip()
            ts_raw = str(data.get("ts", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return ""
        if not sid:
            return ""
        if ttl > 0:
            if not ts_raw:
                return ""
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                return ""  # 时间戳坏了，宁可不用，也别拿一个不知年代的会话
            if (datetime.now().astimezone() - ts).total_seconds() > ttl:
                return ""
        return sid

    # 老格式：纯 sid 字符串（没有时间戳，无从判断新鲜度）。
    # 兼容保留——它至少是个合法会话 id，比整个 JSON 串强。
    # 下一次 bridge.write 会把它覆盖成 JSON。
    return raw
