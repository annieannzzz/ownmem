# -*- coding: utf-8 -*-
"""写入闸门 + 挑选保存。

隐身模式的拦截点在这里：agent 以为写成功了，实际被闸门拦下并留下审计记录。
会话结束后用户可以在「挑选保存」里把想要的部分白名单式留下——
先拦后选，比「默认全存、事后删」的体验和信任都好。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

from ..core import audit, db, sessions
from ..core import triage
from ..core.store import MemoryStore


def guarded_write(
    conn: sqlite3.Connection,
    content: str,
    *,
    session_id: str,
    agent: str = "unknown",
    importance: str = "normal",
    tags: str = "",
    source: str = "",
    kind: str = "conclusion",
    project: str = "",
    supersedes: str = "",
    force: bool = False,
    stage_on_block: bool = True,
    channel: str = "explicit",
) -> dict:
    """带闸门的写入。隐身/双断模式下拦截，并记录 write_blocked 审计。

    拦截时把内容转入待定区（``stage_on_block``，默认开），用户可在会话末
    白名单式挑回来——「先拦后选」要成立，这一步必须在闸门里，
    不能靠每个调用方自觉去调 stage()。以前只有 MCP 入口做了这件事，
    CLI 的 ``ownmem add`` 被拦后就只留一条审计摘录，事后根本捞不回来。

    例外是 ghost 模式：它的承诺是彻底断开，不是「暂缓」，
    所以连待定区都不放——说了不留就得真不留。

    force=True 仅供用户显式操作（CLI / 挑选保存）绕过闸门。

    channel="auto" 表示这条来自自动提炼（hook / agent 顺手记），走质量闸门：
    明确的寒暄、提问、中间过程会被丢掉，存疑的落待定区而不是长期记忆。
    顾虑很简单——召回侧的注意力是定额的，写入侧不设门槛就等于给未来收税。
    """
    mode = sessions.get_mode(conn, session_id)
    store = MemoryStore(conn)

    # 闸门在模式闸门之前跑：上下文之外的东西，不必等到模式判定才处理
    if channel == "auto" and not triage.should_bypass(content, importance):
        verdict, why = triage.triage(content)
        if verdict == triage.REJECT:
            audit.log(
                conn, "write_triage_reject", agent=agent, session_id=session_id,
                detail=f"{why}｜{audit.detail(content, 80)}", mode=mode,
            )
            return {
                "ok": False, "blocked": False, "mode": mode, "id": "",
                "staged": False, "verdict": verdict,
                "message": f"未记录（质量闸门）：{why}。原文：{content[:40]}",
            }
        if verdict == triage.STAGE:
            if mode == "ghost":
                return {
                    "ok": False, "blocked": False, "mode": mode, "id": "",
                    "staged": False, "verdict": verdict,
                    "message": "双断模式连待定区也不留存。",
                }
            staged_id = stage(conn, session_id, content, agent=agent)
            audit.log(
                conn, "write_triage_stage", agent=agent, session_id=session_id,
                detail=f"{why}｜{audit.detail(content, 80)}", mode=mode,
            )
            return {
                "ok": False, "blocked": False, "mode": mode, "id": "",
                "staged": True, "staged_id": staged_id, "verdict": verdict,
                "message": (
                    f"未进长期记忆（质量闸门）：{why}。"
                    f"已转入待定区 {staged_id}，会话末 ownmem staged 可挑选保留，不挑即弃。"
                ),
            }

    if not force and not sessions.can_write(conn, session_id):
        kept_note = (
            "并按当前设置留存内容摘录"
            if audit.detail_mode() == "content" else "仅留存内容指纹（不含原文）"
        )
        staged_id = ""
        if stage_on_block and mode != "ghost" and (content or "").strip():
            staged_id = stage(conn, session_id, content, agent=agent)
        audit.log(
            conn, "write_blocked", agent=agent, session_id=session_id,
            detail=audit.detail(content, 120), mode=mode,
        )
        if staged_id:
            tail = f"内容已转入待定区 {staged_id}，会话末可挑选保存（不挑即弃）。"
        elif mode == "ghost":
            tail = "双断模式连待定区也不留存。"
        else:
            tail = ""
        return {
            "ok": False, "blocked": True, "mode": mode, "id": "",
            "staged_id": staged_id, "staged": bool(staged_id),
            "message": (
                f"已拦截：当前会话处于 {mode} 模式，这条内容不会进入长期记忆。"
                f"{tail}（审计流水留了一条拦截记录，{kept_note}）"
            ),
        }

    # 取代的前置校验必须在写入之前：挂了链才发现旧条早已作废，就白写了一条
    # 平行记忆——而平行记忆正是取代链要消灭的东西。
    if supersedes:
        old = store.get(supersedes)
        if not old:
            return {
                "ok": False, "blocked": False, "mode": mode,
                "id": "", "message": f"找不到要被取代的记忆 {supersedes}。",
            }
        if old.get("superseded_by"):
            return {
                "ok": False, "blocked": False, "mode": mode, "id": "",
                "message": (
                    f"{supersedes} 已经被 {old['superseded_by']} 取代过了。"
                    f"要再改一次，请取代 {old['superseded_by']}（接在最新那版后面）。"
                ),
            }

    mem, msg = store.add(
        content,
        importance=importance,
        tags=tags,
        source=source or agent,
        agent=agent,
        session_id=session_id,
        kind=kind,
        # 未显式指定项目时继承会话归属：写入方不必每句都带项目，
        # 但记忆必须归到正确的那本账里，否则项目隔离形同虚设。
        project=project or sessions.get_project(conn, session_id),
        supersedes=supersedes,
        # 取代必须绕开判重：新结论和旧结论往往只差几个字（「2400 字」→「可配置」），
        # 相似度轻松过 60% 阈值。判重一挡，取代功能就等于不存在——
        # 而用户已经明说「这条取代那条」，重复风险由取代链本身承担。
        skip_dedup=bool(supersedes),
    )
    if mem is None:
        # 撞上已有踩坑账 = 这个坑又被踩了一次。信息已被吸收（复发计数 +1），
        # 所以这不是失败；调用方按 ok=True 处理，否则 agent 会以为没记住而反复重试。
        rec = store.last_recurrence
        if rec:
            return {
                "ok": True, "blocked": False, "mode": mode, "id": rec["id"],
                "recurrence": rec, "message": msg,
            }
        return {"ok": False, "blocked": False, "mode": mode, "id": "", "message": msg}

    if supersedes:
        ok, note = store.link_supersede(supersedes, mem["id"])
        msg = f"{msg}；{note}"

    audit.log(
        conn, "write", agent=agent, session_id=session_id,
        memory_id=mem["id"], detail=audit.detail(content, 120), mode=mode,
    )
    return {"ok": True, "blocked": False, "mode": mode, "id": mem["id"], "message": msg}


# ---------------------------------------------------------------------------
# 挑选保存（Promote）
# ---------------------------------------------------------------------------
def stage(conn: sqlite3.Connection, session_id: str, content: str, *, agent: str = "unknown") -> str:
    """把一条内容放进「待定区」——隐身会话里 agent 想记的东西先存这儿。"""
    items = _load_staged(conn, session_id)
    # 编号取现有最大值 +1：丢弃/挑选后新条目不得复用旧编号，否则无法寻址
    nums = [
        int(m.group(1))
        for it in items
        for m in [re.match(r"p_(\d+)$", str(it.get("sid", "")))]
        if m
    ]
    sid = f"p_{max(nums, default=0) + 1:03d}"
    items.append({
        "sid": sid,
        "content": (content or "").strip(),
        "agent": agent,
        "staged_at": audit.now(),
    })
    db.set_setting(conn, f"staged:{session_id}", json.dumps(items, ensure_ascii=False))
    # 待定区本身也是一处落盘，必须在账本留痕
    audit.log(
        conn, "stage", agent=agent, session_id=session_id,
        detail=audit.detail(content, 120),
    )
    return sid


def _load_staged(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    raw = db.get_setting(conn, f"staged:{session_id}", "[]")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def list_staged(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    return _load_staged(conn, session_id)


def list_all_staged(conn: sqlite3.Connection) -> list[dict]:
    """跨会话一次取全待定区。

    审计台原来逐会话调 list_staged（200 会话 = 200 次查询），
    这里改成对 settings 表一条 LIKE 查询拿全量，条目自带 session_id。
    """
    out: list[dict] = []
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'staged:%'"
    ).fetchall()
    for row in rows:
        try:
            items = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        sid = row["key"].split(":", 1)[1]
        for it in items:
            it = dict(it)
            it["session_id"] = sid
            out.append(it)
    return out


def promote(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    sids: list[str] | None = None,
    contents: list[str] | None = None,
    agent: str = "unknown",
    importance: str = "normal",
) -> dict:
    """把待定区里被勾选的内容真正写入长期记忆（白名单式）。

    两种用法：
      - sids=["p_001"]      挑选待定区里的条目
      - contents=["原文"]   直接保存一段裸文本（不经过待定区）
    """
    store = MemoryStore(conn)
    staged = _load_staged(conn, session_id)
    texts: list[str] = list(contents or [])
    drop_sids: set[str] = set()

    if sids:
        by_sid = {s["sid"]: s for s in staged}
        for sid in sids:
            item = by_sid.get(sid)
            if item:
                texts.append(item["content"])
                drop_sids.add(sid)

    if not texts:
        return {"saved": 0, "kept": len(staged), "message": "没有选中任何内容，待定区保持不变。"}

    saved_ids, skipped = [], []
    for text in texts:
        mem, msg = store.add(text, importance=importance, source=agent, agent=agent, session_id=session_id)
        if mem:
            saved_ids.append(mem["id"])
            audit.log(
                conn, "promote", agent=agent, session_id=session_id,
                memory_id=mem["id"], detail=audit.detail(text, 120),
            )
        else:
            skipped.append(msg)

    # 只移除被点名的 sid——按内容移除会误删同文的其他待定条目
    remaining = [s for s in staged if s["sid"] not in drop_sids]
    db.set_setting(conn, f"staged:{session_id}", json.dumps(remaining, ensure_ascii=False))

    return {
        "saved": len(saved_ids),
        "ids": saved_ids,
        "kept": len(remaining),
        "skipped": skipped,
        "message": f"已保存 {len(saved_ids)} 条，待定区还剩 {len(remaining)} 条。",
    }


def discard(conn: sqlite3.Connection, session_id: str, *, sids: list[str]) -> int:
    """丢弃待定区条目（不留痕，连审计也不记具体内容）。"""
    staged = _load_staged(conn, session_id)
    drop = set(sids or [])
    remaining = [s for s in staged if s["sid"] not in drop]
    db.set_setting(conn, f"staged:{session_id}", json.dumps(remaining, ensure_ascii=False))
    return len(staged) - len(remaining)


# ---------------------------------------------------------------------------
# 待定区的回收
# ---------------------------------------------------------------------------
STAGED_PREFIX = "staged:"
# 待定区是会话级暂存，不是长期存储：超过这个天数还没被处理就清掉
DEFAULT_STAGED_MAX_AGE_DAYS = 30


def _staged_keys(conn: sqlite3.Connection) -> list[str]:
    """所有待定区条目的设置键。"""
    return [r["key"] for r in conn.execute(
        "SELECT key FROM settings WHERE key LIKE ?", (STAGED_PREFIX + "%",)
    ).fetchall()]


def prune_staged(conn: sqlite3.Connection, max_age_days: int = DEFAULT_STAGED_MAX_AGE_DAYS) -> tuple[int, int]:
    """清掉超龄的待定区残留，返回 (清理条目数, 清理会话数)。

    为什么需要它：待定区存的是「用户明确没同意留下的东西」。会话结束了、
    用户也没回来挑，这些内容就会永久躺在本地库里——一个隐私产品不该这样。
    条目级判断：一个会话里新条目和旧条目分开算，不误伤还在有效期内的。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=max(0, int(max_age_days)))
    removed_items = 0
    removed_keys = 0
    for key in _staged_keys(conn):
        items = _load_staged(conn, key[len(STAGED_PREFIX):])
        keep: list[dict] = []
        for it in items:
            raw_ts = str(it.get("staged_at") or "")
            try:
                if raw_ts and datetime.fromisoformat(raw_ts) >= cutoff:
                    keep.append(it)
                    continue
            except ValueError:
                pass  # 时间戳坏掉，按超龄处理
        if len(keep) == len(items):
            continue
        removed_items += len(items) - len(keep)
        if keep:
            db.set_setting(conn, key, json.dumps(keep, ensure_ascii=False))
        else:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            removed_keys += 1
    return removed_items, removed_keys


def staged_stats(conn: sqlite3.Connection, max_age_days: int = DEFAULT_STAGED_MAX_AGE_DAYS) -> dict:
    """待定区概况（只读，给 doctor 用）：多少会话有暂存、多少条已超龄。"""
    cutoff = datetime.now().astimezone() - timedelta(days=max(0, int(max_age_days)))
    sessions_with = 0
    total = 0
    stale = 0
    for key in _staged_keys(conn):
        items = _load_staged(conn, key[len(STAGED_PREFIX):])
        if not items:
            continue
        sessions_with += 1
        total += len(items)
        for it in items:
            raw_ts = str(it.get("staged_at") or "")
            try:
                if not raw_ts or datetime.fromisoformat(raw_ts) < cutoff:
                    stale += 1
            except ValueError:
                stale += 1
    return {"sessions": sessions_with, "items": total, "stale_items": stale, "max_age_days": max_age_days}


def discard_staged_by_agent(conn: sqlite3.Connection, agent: str) -> int:
    """抹除某个 agent 时，把它留在待定区的内容一并清掉。

    wipe 的承诺是「把某段过去删干净」，而待定区也是一处落盘——
    只清 memories 和审计摘录、漏掉待定区，等于承诺没兑现。
    """
    if not (agent or "").strip():
        return 0
    removed = 0
    for key in _staged_keys(conn):
        items = _load_staged(conn, key[len(STAGED_PREFIX):])
        keep = [it for it in items if str(it.get("agent", "") or "") != agent]
        if len(keep) == len(items):
            continue
        removed += len(items) - len(keep)
        if keep:
            db.set_setting(conn, key, json.dumps(keep, ensure_ascii=False))
        else:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
    return removed


def _ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
