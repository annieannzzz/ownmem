# -*- coding: utf-8 -*-
"""OwnMem 命令行：用户侧与 hook 侧的统一入口。

最核心的一条命令是 `ownmem recall --hook claude`：
它被 harness 的 UserPromptSubmit hook 调用，读取用户这轮输入，
把相关记忆渲染成上下文再交还给 harness 注入——记忆因此「主动进场」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .core import audit, bridge, db, digest as digest_mod, divergence, sessions, triage
from .core.store import (
    KIND_LABELS, MAX_CONTENT, PREVIEW_LEN, VALID_IMPORTANCE, VALID_KINDS,
    MemoryStore, format_hits,
)
from .gateway import policy, recall as recall_mod


def _fix_console() -> None:
    """Windows 控制台默认 GBK，中文输出会炸——统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _conn():
    return db.connect()


def _resolve_session(args, conn) -> str:
    """定出本次操作归属哪个会话：显式指定 > 桥接（与 hook/MCP 对齐）> 新建。

    读写一律走 bridge（统一 JSON 格式）。以前这里直接读设置值当字符串用，
    而 hook 写进去的是 JSON——结果整个 JSON 被当成会话 id，那个会话又是按
    default_mode 新建的，于是在隐身会话里也能绕过闸门写进去。
    """
    agent = getattr(args, "agent", "") or "cli"
    sid = getattr(args, "session", "") or os.environ.get("OWNMEM_SESSION", "")
    if not sid:
        # 按 agent 分别记忆「当前会话」，避免不同 harness 之间串会话
        sid = bridge.read(conn, agent)
    if not sid:
        sid = sessions.new_session_id()
    bridge.write(conn, agent, sid)
    sessions.touch_session(conn, sid, agent=agent)
    return sid


# ---------------------------------------------------------------------------
# recall：自动召回（含 Claude Code hook 协议）
# ---------------------------------------------------------------------------
def _read_hook_payload(raw: str) -> dict:
    """Claude Code 的 hook 通过 stdin 传 JSON。解析失败时把原文当 prompt。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"prompt": raw}


def _read_stdin_safe(timeout: float = 1.5) -> str:
    """读 stdin，最多等 timeout 秒。

    手动在终端跑 `ownmem session-end` 时，stdin 可能是一个永不关闭的句柄
    （IDE 终端、被管道接住的父进程），一句 sys.stdin.read() 就把命令挂死。
    hook 场景下 JSON 立刻到达，1.5 秒绰绰有余；超时就当没有 payload，
    后面走桥接会话兜底——宁可少一条 payload，也不能让命令卡住。
    """
    if sys.stdin.isatty():
        return ""
    import threading

    buf: list[str] = []

    def _worker() -> None:
        try:
            buf.append(sys.stdin.read() or "")
        except (OSError, ValueError):
            buf.append("")

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    return buf[0] if buf else ""


def cmd_recall(args) -> int:
    conn = _conn()
    payload = {}

    raw = _read_stdin_safe()
    if raw:
        payload = _read_hook_payload(raw)

    prompt = args.prompt or payload.get("prompt", "") or ""
    session_id = (
        args.session
        or payload.get("session_id", "")
        or os.environ.get("OWNMEM_SESSION", "")
    )
    agent = args.agent or ("claude-code" if args.hook == "claude" else "cli")

    if session_id:
        sessions.touch_session(conn, session_id, agent=agent)
    else:
        session_id = _resolve_session(args, conn)

    limit = args.limit
    if payload.get("session_id"):
        # 落下桥接设置：同一对话里的 MCP 读写（同 agent）会读它对齐到同一会话，
        # 这样 hook 的召回和 MCP 的写入才能合成同一张会话回执
        bridge.write(conn, agent, payload["session_id"])

    res = recall_mod.recall_context(
        conn, prompt, session_id=session_id, agent=agent, limit=limit,
        project=getattr(args, "project", "") or "",
    )

    if args.hook == "claude":
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}
        if res["context"]:
            out["hookSpecificOutput"]["additionalContext"] = res["context"]
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0

    if args.json:
        sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if res["context"]:
        sys.stdout.write(res["context"] + "\n")
    elif args.verbose:
        sys.stderr.write(f"[ownmem] 未注入记忆（{res['reason'] or '无'}，模式={res['mode']}）\n")
    return 0


# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------
def cmd_mode(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    if not args.mode:
        cur = sessions.get_mode(conn, sid)
        for name, info in sessions.MODES.items():
            mark = "← 当前" if name == cur else ""
            sys.stdout.write(
                f"{name:<10} 读={'是' if info['read'] else '否'} 写={'是' if info['write'] else '否'}  {info['desc']} {mark}\n"
            )
        return 0
    new_mode = sessions.set_mode(conn, sid, args.mode, agent=args.agent)
    info = sessions.MODES[new_mode]
    sys.stdout.write(f"[{sid}] 模式已切为 {new_mode}（{info['desc']}）\n")
    return 0


def cmd_view(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    if not args.view:
        cur = sessions.get_view(conn, sid)
        mode = sessions.get_mode(conn, sid)
        for name, info in sessions.VIEWS.items():
            mark = "← 当前" if name == cur else ""
            sys.stdout.write(f"{name:<14} {info['desc']} {mark}\n")
        if mode in ("blank", "ghost"):
            sys.stdout.write(f"（注意：当前模式为 {mode}，整体断读中，视角暂不生效）\n")
        sys.stdout.write("切换：ownmem view <名称>（支持盲审/交叉/结论 等中文别名）\n")
        return 0
    new_view = sessions.set_view(conn, sid, args.view, agent=args.agent)
    sys.stdout.write(f"[{sid}] 视角已切为 {new_view}（{sessions.VIEWS[new_view]['desc']}）\n")
    return 0


def cmd_answer_first(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    action = args.action

    if action == "status" or not action:
        phase = sessions.af_phase(conn, sid)
        if not phase:
            sys.stdout.write(f"[{sid}] 未开启先答后对。流程：begin（盲答）→ submit（存独立方案）→ reveal（对照）\n")
            return 0
        ans = sessions.af_answer_id(conn, sid)
        sys.stdout.write(
            f"[{sid}] 阶段：{phase}（{'独立作答期，旧记忆已屏蔽' if phase == 'independent' else '对照期，注入独立方案+旧记忆'}）"
            f"{'｜独立方案=' + ans if ans else '｜尚未 submit 独立方案'}\n"
        )
        return 0

    if action == "begin":
        sessions.af_set_phase(conn, sid, "independent", agent=args.agent)
        sys.stdout.write(
            f"[{sid}] 先答后对 · 第 1 阶段开始：旧记忆与历史进度已屏蔽（常驻红线除外）。\n"
            f"让 agent 独立作答，然后 ownmem answer-first submit \"独立方案全文\" 存档。\n"
        )
        return 0

    if action == "submit":
        if not args.content:
            sys.stdout.write("用法：ownmem answer-first submit \"独立方案全文\"\n")
            return 1
        res = policy.guarded_write(
            conn, args.content, session_id=sid, agent=args.agent,
            kind="process", tags="answer-first", source="answer-first",
            channel="explicit",
        )
        if not res["ok"]:
            sys.stdout.write(res["message"] + "\n")
            return 1
        sessions.af_set_answer(conn, sid, res["id"])
        sys.stdout.write(f"独立方案已存档（{res['id']}）。准备好后：ownmem answer-first reveal\n")
        return 0

    if action == "reveal":
        if sessions.af_phase(conn, sid) != "independent":
            sys.stdout.write("当前不在独立作答期（先 ownmem answer-first begin）。\n")
            return 1
        if not sessions.af_answer_id(conn, sid):
            sys.stdout.write("还没有 submit 独立方案——没有「你的答案」，对照无从谈起。\n")
            return 1
        sessions.af_set_phase(conn, sid, "compare", agent=args.agent)
        sys.stdout.write(
            f"[{sid}] 第 2 阶段开始：下一轮起注入「你的独立方案 + 相关旧记忆 + 对照指令」。\n"
            f"让 agent 逐点对照并列出分歧点。收尾：ownmem answer-first cancel 清状态。\n"
        )
        return 0

    if action == "cancel":
        sessions.af_clear(conn, sid)
        sys.stdout.write(f"[{sid}] 先答后对状态已清。\n")
        return 0

    sys.stdout.write(f"未知动作：{action}（begin/submit/reveal/status/cancel）\n")
    return 1


def cmd_divergence(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    project = sessions.resolve_project(conn, sid, "") if not args.all_projects else ""
    rep = divergence.report(
        conn, args.query, limit=args.limit, project=project,
        low=args.low, high=args.high,
    )
    sys.stdout.write(divergence.render(rep) + "\n")
    return 0


def cmd_status(args) -> int:
    from .gateway import status as gateway_status

    conn = _conn()
    sid = _resolve_session(args, conn)
    st = gateway_status(conn, sid, agent=args.agent)
    s = st["memories"]
    sys.stdout.write(
        f"会话：{st['session_id']}　模式：{st['mode']}"
        f"（读={'是' if st['read_allowed'] else '否'}，写={'是' if st['write_allowed'] else '否'}）\n"
        f"记忆：{s['active']}/{s['capacity']} 条（回收站 {s['trashed']}）｜来源分布：{s['by_agent']}\n"
        f"待定区：{st['staged_count']} 条\n"
        f"数据目录：{s['home']}\n"
    )
    if st["recent_audit"]:
        sys.stdout.write("最近审计：\n")
        for e in st["recent_audit"][:6]:
            label = audit.EVENT_LABELS.get(e["event"], e["event"])
            sys.stdout.write(f"  · {e['ts'][:19]} [{label}] {e['detail'][:60]}\n")
    return 0


# ---------------------------------------------------------------------------
# 记忆操作
# ---------------------------------------------------------------------------
def cmd_add(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    res = policy.guarded_write(
        conn, args.content, session_id=sid, agent=args.agent,
        importance=args.importance, tags=args.tags or "", source=args.source or "",
        kind=args.kind or "conclusion", project=args.project or "",
        supersedes=getattr(args, "supersedes", "") or "",
        force=args.force,
        channel="auto" if args.auto else "explicit",
    )
    sys.stdout.write(res["message"] + "\n")
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------------------
# 治理（v2 P1）：取代链 / 晋级 / 预算
# ---------------------------------------------------------------------------
def cmd_supersede(args) -> int:
    """推翻一条旧结论：写新结论 + 挂取代链，旧条保留可查。"""
    conn = _conn()
    sid = _resolve_session(args, conn)
    store = MemoryStore(conn)
    old = store.get(args.id)
    if not old:
        sys.stdout.write(f"找不到要被取代的记忆 {args.id}。\n")
        return 1
    if old.get("superseded_by"):
        sys.stdout.write(
            f"{args.id} 已经被 {old['superseded_by']} 取代过了。\n"
            f"要再改一次，请取代 {old['superseded_by']}（沿最新那版往下接）。\n"
        )
        return 1
    res = policy.guarded_write(
        conn, args.content, session_id=sid, agent=args.agent,
        importance=args.importance or old.get("importance", "normal"),
        tags=args.tags or "", source=args.source or "",
        kind=args.kind or old.get("kind", "conclusion"),
        project=args.project or old.get("project", ""),
        supersedes=args.id,
        force=True,
    )
    sys.stdout.write(res["message"] + "\n")
    if not res["ok"]:
        return 1
    sys.stdout.write(f"  旧：{old['content'][:70]}{'…' if len(old['content']) > 70 else ''}\n")
    sys.stdout.write("旧条已作废（不再注入），但保留可查：ownmem chain <id>\n")
    return 0


def cmd_chain(args) -> int:
    conn = _conn()
    ch = MemoryStore(conn).chain(args.id)
    cur = ch["current"]
    if not cur:
        sys.stdout.write(f"未找到 {args.id}\n")
        return 1

    def _line(m: dict, mark: str) -> str:
        return (f"  {mark} [{m['id']}] {m['created_at'][:16]} ({m.get('kind') or 'conclusion'}) "
                f"{m['content'][:70]}{'…' if len(m['content']) > 70 else ''}")

    lines = []
    for m in reversed(ch["older"]):
        lines.append(_line(m, "↑推翻"))
    lines.append(_line(cur, "◆当前"))
    for m in ch["newer"]:
        lines.append(_line(m, "↓被取代"))
    if len(lines) == 1:
        sys.stdout.write(f"{args.id} 没有取代关系（这条既没推翻谁，也没被谁推翻）。\n")
        return 0
    sys.stdout.write("取代链：\n" + "\n".join(lines) + "\n")
    return 0


def cmd_budget(args) -> int:
    """查看 / 设置每轮注入预算（v2 §3.8）。"""
    from .gateway import recall as recall_mod

    conn = _conn()
    if args.recall is None and args.always is None:
        now = recall_mod._budget(conn, "recall_budget", recall_mod.DEFAULT_CHAR_BUDGET)
        alw = recall_mod._budget(conn, "always_budget", recall_mod.DEFAULT_ALWAYS_BUDGET)
        src = "（来自 settings）" if db.get_setting(conn, "recall_budget") else "（默认值）"
        sys.stdout.write(
            f"相关记忆预算：{now} 字{src}\n"
            f"常驻规则预算：{alw} 字\n"
            f"调整：ownmem budget --recall 3000　｜　ownmem budget --always 3500\n"
            f"恢复默认：ownmem budget --reset\n"
        )
        return 0
    if args.reset:
        for k in ("recall_budget", "always_budget"):
            conn.execute("DELETE FROM settings WHERE key=?", (k,))
        sys.stdout.write("已恢复默认预算。\n")
        return 0
    if args.recall is not None:
        if args.recall <= 0:
            sys.stdout.write("预算必须为正数。\n")
            return 1
        db.set_setting(conn, "recall_budget", str(args.recall))
        sys.stdout.write(f"相关记忆预算设为 {args.recall} 字（下一轮生效）。\n")
    if args.always is not None:
        if args.always <= 0:
            sys.stdout.write("预算必须为正数。\n")
            return 1
        db.set_setting(conn, "always_budget", str(args.always))
        sys.stdout.write(f"常驻规则预算设为 {args.always} 字（下一轮生效）。\n")
    return 0


def cmd_promote_kind(args) -> int:
    """把反复被用到的过程账晋级为结论账（v2 §3.7）。"""
    conn = _conn()
    res = MemoryStore(conn).promote_repeated(
        min_hits=args.min_hits, min_sessions=args.min_sessions, dry_run=args.dry_run
    )
    cand = res["candidates"]
    if not cand:
        sys.stdout.write(
            f"没有符合晋级条件的过程账（阈值：召回 ≥{args.min_hits} 次、"
            f"出现在 ≥{args.min_sessions} 个会话）。\n"
        )
        return 0
    head = "可晋级（预演，未改动）：" if args.dry_run else f"已晋级 {res['promoted']} 条："
    sys.stdout.write(head + "\n")
    for c in cand:
        sys.stdout.write(
            f"  [{c['id']}] 召回 {c['hits']} 次 / {c['sessions']} 个会话　{c['content'][:70]}\n"
        )
    if args.dry_run:
        sys.stdout.write("确认执行：ownmem upgrade（去掉 --dry-run）\n")
    return 0


# ---------------------------------------------------------------------------
# 项目（v2 3.2）：记忆按项目分家
# ---------------------------------------------------------------------------
def cmd_project(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    cur = sessions.get_project(conn, sid)
    default = db.get_setting(conn, "default_project", "")

    if args.clear:
        sessions.set_project(conn, sid, "", agent=args.agent)
        if args.default:
            db.set_setting(conn, "default_project", "")
        sys.stdout.write(
            f"已切回全局（项目隔离关闭）：{sid} 的读取范围 = 全部记忆"
            + ("，新会话默认项目也已清空\n" if args.default else "\n")
        )
        return 0

    if not args.project:
        sys.stdout.write(
            f"当前会话 {sid} 的项目：{cur or '（全局：所有项目共享）'}\n"
            f"新会话默认项目：{default or '（未设置，跟随全局）'}\n"
            f"切换：ownmem project <名称>　｜　恢复全局：ownmem project --clear\n"
            f"设为新会话默认：ownmem project <名称> --default\n"
        )
        return 0

    new = sessions.set_project(conn, sid, args.project, agent=args.agent)
    if args.default:
        # 每个新对话都手敲一次 project 太累，隔离就会闲置。
        # --default 把它设成新会话的默认归属（存 settings，跨会话生效）。
        db.set_setting(conn, "default_project", new)
        sys.stdout.write(f"新会话默认项目已设为：{new}\n")
    sys.stdout.write(
        f"会话 {sid} 归属项目：{new}\n"
        f"  写入：默认落到该项目（可用 add --project 临时覆盖）\n"
        f"  召回：只注入「{new} + 全局」的记忆\n"
    )
    return 0


# ---------------------------------------------------------------------------
# 会话沉淀（v2 3.1）：会话结束把「干到哪了」落盘
# ---------------------------------------------------------------------------
def cmd_digest(args) -> int:
    conn = _conn()
    if args.session_end:
        # 顺手结束会话再沉淀：SessionEnd hook 的推荐用法
        sid = args.session or _resolve_session(args, conn)
        sessions.end_session(conn, sid)
    else:
        sid = args.session or ""
    if not sid:
        sid = _resolve_session(args, conn)

    if args.show:
        d = digest_mod.get(conn, args.show)
        if not d:
            sys.stdout.write(f"未找到沉淀 {args.show}\n")
            return 1
    elif args.write or args.note:
        d = digest_mod.save(conn, sid, agent=args.agent, note=args.note or "")
        if d.get("skipped"):
            sys.stdout.write("本次会话没有可沉淀的内容（无写入、无召回），未落盘。\n")
            return 0
        sys.stdout.write(f"已沉淀会话 {sid}（{len(d['text'])} 字，项目={d['project'] or '全局'}）\n\n")
        sys.stdout.write(d["text"] + "\n")
        return 0
    elif not args.list:
        d = digest_mod.latest(conn, project=sessions.resolve_project(conn, sid), exclude_session=sid)
        if not d:
            sys.stdout.write("同项目还没有上一次会话的沉淀。\n")
            return 0
        sys.stdout.write(
            f"同项目最近一次沉淀（{d['session_id']}，{d['created_at'][:16]}）：\n\n{d['text']}\n"
        )
        return 0
    else:
        items = digest_mod.list_recent(conn, limit=args.limit)
        if not items:
            sys.stdout.write("还没有任何会话沉淀。\n")
            return 0
        if args.json:
            sys.stdout.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
            return 0
        for d in items:
            sys.stdout.write(
                f"[{d['id']}] {d['created_at'][:16]} 项目={d['project'] or '全局'} "
                f"agent={d['agent']} 写入={d['writes_n']} 条｜{(d['note'] or d['text'][:60])}\n"
            )
        return 0

    sys.stdout.write(d["text"] + "\n")
    return 0


def cmd_triage(args) -> int:
    """干跑质量闸门：看一段文字会被怎么裁决，不写库。"""
    verdict, why = triage.triage(args.content)
    label = {
        triage.ACCEPT: "直接沉淀",
        triage.STAGE: "进待定区",
        triage.REJECT: "丢弃",
    }[verdict]
    mark = {"accept": "+", "stage": "~", "reject": "-"}[verdict]
    sys.stdout.write(f"[{mark}] {label}：{why}\n")
    sys.stdout.write(f"     原文：{args.content[:80]}\n")
    return 0


def cmd_edit(args) -> int:
    """就地改写一条记忆。

    存在理由：a07 那条规矩写着「结论被推翻就更新原条目，不要新增平行记忆
    让两条互相打架」，但在这之前产品里没有改内容的入口——规矩执行不了。
    改写会留审计，改完前后都能追。
    """
    conn = _conn()
    sid = _resolve_session(args, conn)
    store = MemoryStore(conn)
    mem = store.get(args.id)
    if mem is None:
        sys.stdout.write(f"找不到记忆 {args.id}。\n")
        return 1
    content = (args.content or "").strip()
    if not content:
        sys.stdout.write("新内容为空，未改动。\n")
        return 1
    if len(content) > MAX_CONTENT:
        sys.stdout.write(f"内容过长（{len(content)} 字），上限 {MAX_CONTENT} 字。\n")
        return 1

    fields = []
    if content != mem.get("content"):
        conn.execute("UPDATE memories SET content=? WHERE id=?", (content, args.id))
        fields.append("内容")
    if args.tags:
        conn.execute("UPDATE memories SET tags=? WHERE id=?", (args.tags, args.id))
        fields.append("标签")
    if args.importance:
        if args.importance not in VALID_IMPORTANCE:
            sys.stdout.write(f"重要度只能是 {VALID_IMPORTANCE}。\n")
            return 1
        conn.execute("UPDATE memories SET importance=? WHERE id=?",
                     (args.importance, args.id))
        fields.append("重要度")
    if not fields:
        sys.stdout.write("没有任何改动。\n")
        return 0
    conn.commit()
    audit.log(
        conn, "write", agent=args.agent, session_id=sid, memory_id=args.id,
        detail=audit.detail(f"改写{'/'.join(fields)}｜{content}", 120),
    )
    sys.stdout.write(f"已更新 {args.id}（{'/'.join(fields)}）。\n")
    old_preview = (mem.get("content") or "")[:60]
    sys.stdout.write(f"  旧：{old_preview}{'…' if len((mem.get('content') or '')) > 60 else ''}\n")
    sys.stdout.write(f"  新：{content[:60]}{'…' if len(content) > 60 else ''}\n")
    return 0


def cmd_search(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    if not sessions.can_read(conn, sid) and not args.force:
        sys.stdout.write(f"当前 {sessions.get_mode(conn, sid)} 模式：不读取历史记忆（--force 可强制）。\n")
        return 1
    store = MemoryStore(conn)
    project = args.project or sessions.get_project(conn, sid)
    top = store.search(
        args.query, limit=args.limit, agent=args.agent_filter or "", source=args.source or "",
        project=project, kind=args.kind or "",
        include_superseded=getattr(args, "superseded", False),
    )
    for mem, _ in top:
        audit.log(conn, "recall", agent=args.agent, session_id=sid, memory_id=mem["id"],
                  detail=audit.detail(mem["content"], 80))
    sys.stdout.write(format_hits(top, preview=not args.full) + "\n")
    return 0


def cmd_list(args) -> int:
    conn = _conn()
    store = MemoryStore(conn)
    items = store.list_active(agent=args.agent_filter or "", source=args.source or "",
                              importance=args.importance or "", kind=args.kind or "",
                              project=args.project or "", limit=args.limit)
    if args.json:
        sys.stdout.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        return 0
    if not items:
        sys.stdout.write("没有记忆。\n")
        return 0
    for m in items:
        sys.stdout.write(f"[{m['id']}] {m['created_at'][:16]} ({m['importance']}/{m['agent']}) {m['content'][:90]}\n")
    return 0


def cmd_show(args) -> int:
    conn = _conn()
    store = MemoryStore(conn)
    mem = store.get(args.id)
    if not mem:
        sys.stdout.write(f"未找到 {args.id}\n")
        return 1
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(mem, ensure_ascii=False, indent=2) + "\n")
        return 0
    label = {"conclusion": "结论", "process": "过程", "pitfall": "踩坑"}
    sys.stdout.write(f"[{mem['id']}] {mem['created_at'][:16]}\n")
    sys.stdout.write(
        f"  账本：{label.get(mem.get('kind') or 'conclusion')}　"
        f"重要度：{mem['importance']}　来源：{mem['agent']}"
        f"　项目：{mem.get('project') or '全局'}\n"
    )
    if int(mem.get("recurred") or 0):
        agents = mem.get("recurred_agents") or []
        sys.stdout.write(
            f"  ⚠️ 踩坑复发 {mem['recurred']} 次（栽过：{'/'.join(agents) if isinstance(agents, list) else agents}）"
            f"　最近一次：{(mem.get('last_recurred_at') or '')[:16]}\n"
        )
    if mem.get("superseded_by"):
        sys.stdout.write(f"  ⛔ 已被 {mem['superseded_by']} 取代（作废，仅存档）\n")
    sys.stdout.write(f"\n{mem['content']}\n")
    ch = store.chain(args.id)
    if ch["older"] or ch["newer"]:
        sys.stdout.write("\n取代链：\n")
        for m in reversed(ch["older"]):
            sys.stdout.write(f"  ↑推翻 [{m['id']}] {m['content'][:60]}\n")
        for m in ch["newer"]:
            sys.stdout.write(f"  ↓被取代 [{m['id']}] {m['content'][:60]}\n")
    return 0


def cmd_delete(args) -> int:
    conn = _conn()
    store = MemoryStore(conn)
    for mid in args.id:
        ok, msg = store.delete(mid)
        if ok:
            audit.log(conn, "delete", agent=args.agent, memory_id=mid)
        sys.stdout.write(msg + "\n")
    return 0


def cmd_restore(args) -> int:
    conn = _conn()
    store = MemoryStore(conn)
    for mid in args.id:
        ok, msg = store.restore(mid)
        if ok:
            audit.log(conn, "restore", agent=args.agent, memory_id=mid)
        sys.stdout.write(msg + "\n")
    return 0


def cmd_trash(args) -> int:
    conn = _conn()
    items = MemoryStore(conn).list_trash(limit=args.limit)
    if not items:
        sys.stdout.write("回收站为空。\n")
        return 0
    for m in items:
        sys.stdout.write(f"[{m['id']}] (删于 {m['updated_at'][:16]}, {m['importance']}) {m['content'][:80]}\n")
    return 0


def cmd_wipe(args) -> int:
    conn = _conn()
    if not args.yes:
        sys.stdout.write("这是抹除操作，会清掉该来源的全部记忆。确认请加 --yes。\n")
        return 1
    n, msg = MemoryStore(conn).wipe(agent=args.agent_filter or "", source=args.source or "",
                                    hard=args.hard)
    # 待定区也是一处落盘，抹除时必须一并清掉，否则「删干净」是句空话
    # （待定条目只带 agent 不带 source，所以只有按 agent 抹除时能匹配）
    n_staged = policy.discard_staged_by_agent(conn, args.agent_filter or "") if args.agent_filter else 0
    if args.hard:
        audit.log(conn, "wipe", agent=args.agent,
                  detail=f"hard agent={args.agent_filter} source={args.source} n={n}")
    else:
        audit.log(conn, "wipe", agent=args.agent, detail=f"agent={args.agent_filter} source={args.source} n={n}")
    sys.stdout.write(msg + "\n")
    if n_staged:
        sys.stdout.write(f"待定区一并清空 {n_staged} 条（agent={args.agent_filter}）。\n")
    return 0


# ---------------------------------------------------------------------------
# 待定区 / 挑选保存
# ---------------------------------------------------------------------------
def cmd_staged(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    items = policy.list_staged(conn, sid)
    if not items:
        sys.stdout.write("待定区为空。\n")
        return 0
    sys.stdout.write(f"待定区（{len(items)} 条，均为隐身模式下被拦下的内容）：\n")
    for it in items:
        sys.stdout.write(f"  [{it['sid']}] {it['staged_at'][:16]} {it['content'][:100]}\n")
    sys.stdout.write("\n保留：ownmem promote --ids p_001,p_002　　丢弃：ownmem discard --ids p_001\n")
    return 0


def cmd_promote(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    ids = [s.strip() for s in (args.ids or "").split(",") if s.strip()]
    res = policy.promote(conn, sid, sids=ids, contents=args.content or [], agent=args.agent,
                         importance=args.importance)
    sys.stdout.write(res["message"] + "\n")
    for msg in res.get("skipped", []):
        sys.stdout.write("  跳过：" + msg + "\n")
    return 0


def cmd_discard(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    ids = [s.strip() for s in (args.ids or "").split(",") if s.strip()]
    n = policy.discard(conn, sid, sids=ids)
    sys.stdout.write(f"已丢弃 {n} 条。\n")
    return 0


def cmd_staged_prune(args) -> int:
    """清理超龄的待定区残留。

    待定区存的是用户没同意留下的东西，会话结束又没回来挑，就会永久躺在本地库里。
    默认只报数不删（--yes 才真删）——清理这类数据之前先看清楚是应该的。
    """
    conn = _conn()
    st = policy.staged_stats(conn, args.days)
    if not st["items"]:
        sys.stdout.write("待定区没有残留。\n")
        return 0
    sys.stdout.write(
        f"待定区：{st['items']} 条 / {st['sessions']} 个会话，"
        f"其中超过 {st['max_age_days']} 天未处理的有 {st['stale_items']} 条。\n"
    )
    if not args.yes:
        sys.stdout.write("（这是只读报告，真要清理请加 --yes）\n")
        return 0
    items, keys = policy.prune_staged(conn, args.days)
    sys.stdout.write(f"已清理 {items} 条待定区残留（清空 {keys} 个会话的暂存）。\n")
    return 0


# ---------------------------------------------------------------------------
# 审计 / 回执
# ---------------------------------------------------------------------------
def cmd_audit(args) -> int:
    conn = _conn()
    events = audit.list_events(
        conn,
        session_id=args.session or "",
        agent=args.agent_filter or "",
        limit=args.limit,
    )
    if args.json:
        sys.stdout.write(json.dumps(events, ensure_ascii=False, indent=2) + "\n")
        return 0
    if not events:
        sys.stdout.write("审计日志为空。\n")
        return 0
    for e in events:
        label = audit.EVENT_LABELS.get(e["event"], e["event"])
        mid = f" id={e['memory_id']}" if e["memory_id"] else ""
        sys.stdout.write(
            f"{e['ts'][:19]} [{label}] agent={e['agent']} session={e['session_id'] or '-'}{mid} "
            f"mode={e['mode'] or '-'} {e['detail'][:60]}\n"
        )
    return 0


def cmd_receipt(args) -> int:
    conn = _conn()
    sid = _resolve_session(args, conn)
    r = audit.receipt(conn, sid)
    if args.json:
        sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(audit.receipt_text(r) + "\n")
    return 0


def cmd_sessions(args) -> int:
    conn = _conn()
    rows = sessions.list_sessions(conn, limit=args.limit)
    if not rows:
        sys.stdout.write("还没有会话记录。\n")
        return 0
    for r in rows:
        sys.stdout.write(
            f"{r['id']}  agent={r['agent']:<12} 模式={r['mode']:<10} "
            f"召回={r['recalls']} 写入={r['writes']} 拦截={r['blocked']} 最近={r['updated_at'][:16]}\n"
        )
    return 0


def cmd_session_end(args) -> int:
    """结束会话并出回执。可作为 Claude Code SessionEnd hook 挂载：
    会话结束时自动收到「这次对话留了什么账」的账单。"""
    conn = _conn()
    raw = _read_stdin_safe()
    payload = _read_hook_payload(raw)
    agent = args.agent or "cli"
    session_id = (
        args.session
        or (payload.get("session_id", "") if isinstance(payload, dict) else "")
        or os.environ.get("OWNMEM_SESSION", "")
    )
    if session_id:
        sessions.touch_session(conn, session_id, agent=agent)
    else:
        # 桥接查不到（harness 没传 session_id、或 agent 名对不上）时，
        # 退到「最近活动的会话」——SessionEnd 的语义就是刚才那个会话结束了，
        # 而每次召回都会刷新 updated_at，所以最近活动的那条就是它。
        # 注意顺序：必须先在库里找，再谈新建，否则新建的空会话会把自己选成「最近」。
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row:
            session_id = row["id"]
            sessions.touch_session(conn, session_id, agent=agent)
        else:
            session_id = _resolve_session(args, conn)
    sessions.end_session(conn, session_id)
    r = audit.receipt(conn, session_id)
    # 沉淀是 SessionEnd 的主要产出：会话一结束就把「干到哪了」写下来，
    # 不指望 agent 记得调用——这正是 v1「19 天零沉淀」的病根。
    d = None
    if not args.no_digest:
        d = digest_mod.save(conn, session_id, agent=agent, note=args.note or "")
    # 顺手跑一次账本晋级（v2 §3.7）。选这个时机是因为它是唯一「不需要人记得」
    # 的时刻：会话刚结束，该被记的已经被记了，正好结算哪些过程账够格升级。
    # 任何异常都不能让 SessionEnd 挂掉——hook 崩了等于这场对话白记。
    upgraded = 0
    try:
        upgraded = MemoryStore(conn).promote_repeated()["promoted"]
    except Exception:  # noqa: BLE001
        upgraded = 0
    if args.json:
        payload = {"receipt": r, "digest": d, "upgraded": upgraded}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    sys.stdout.write(audit.receipt_text(r) + "\n")
    if d and d.get("skipped"):
        sys.stdout.write("\n本次会话没有可沉淀的内容（无写入、无召回、也没留要点），未落盘。\n")
    elif d:
        sys.stdout.write(f"\n已沉淀本次会话（项目={d['project'] or '全局'}），下次同项目开局会自动注入：\n")
        sys.stdout.write(d["text"] + "\n")
    if upgraded:
        sys.stdout.write(f"\n账本晋级：{upgraded} 条过程账升为结论账（ownmem upgrade --dry-run 可复查）\n")
    return 0


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
def cmd_mcp(args) -> int:
    from . import mcp_server

    mcp_server.main()
    return 0


def cmd_ui(args) -> int:
    from .webui import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_gateway(args) -> int:
    """模型网关：把 harness 的 base_url 指过来，记忆在请求层自动进场。"""
    from .gateway.proxy import serve as gateway_serve

    gateway_serve(
        port=args.port, upstream=args.upstream,
        agent=args.agent, prefix=args.prefix, db_path=args.db,
    )
    return 0


def cmd_export(args) -> int:
    conn = _conn()
    store = MemoryStore(conn)
    data = {
        "version": __version__,
        "exported_at": audit.now(),
        "memories": store.list_active(limit=100000),
        "trash": store.list_trash(limit=100000),
    }
    if args.json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return 0
    for m in data["memories"]:
        sys.stdout.write(f"[{m['id']}] ({m['importance']}/{m['agent']}) {m['content']}\n")
    return 0


def cmd_unreached(args) -> int:
    """僵尸记忆体检：列出「写进去了但怎么问都召不回来」的记忆。

    这些才是真正被浪费的记忆——不是重复，是**根本没被用过**。原因通常是：
    内容用词太泛，与任何提问都不重叠；或被更长更热的条目长期压在预算之外。
    列出来由人决定是改写、补 tag 还是删除；算法不代做这个决定。
    """
    from .core.search import _doc_tokens, is_stale

    conn = _conn()
    store = MemoryStore(conn)
    active = [
        m for m in store.all_active()
        if (m.get("importance") or "") != "always"
    ]
    if not active:
        sys.stdout.write("没有可检查的记忆。\n")
        return 0

    # 探测词必须是「人会怎么问」，不能用每条记忆自己的词去问它自己——
    # 那样测出来必然 100% 命中，等于自我安慰。这里用一组覆盖常见话题的
    # 真实提问词；用户也可以用 --probe 反复追加自己的高频说法。
    probes = list(DEFAULT_PROBES)
    if getattr(args, "probe", ""):
        probes += [p.strip() for p in args.probe.split(",") if p.strip()]

    hits: set[str] = set()
    for probe in probes:
        for hit, _sc in store.search(probe, limit=12, count_hits=False):
            hits.add(hit["id"])

    dead = [m for m in active if m["id"] not in hits]
    stale = [m for m in active if is_stale(m)]

    sys.stdout.write(f"活跃记忆 {len(active)} 条（不含 always 常驻）\n")
    sys.stdout.write(f"可自检召回 {len(active) - len(dead)} 条\n")
    sys.stdout.write(f"僵尸记忆 {len(dead)} 条（{len(dead)/len(active)*100:.0f}%）\n")
    sys.stdout.write(f"过期快照 {len(stale)} 条（带时间锚点且已超过保质期）\n\n")

    if dead:
        sys.stdout.write("=== 僵尸记忆：怎么问都召不回来 ===\n")
        for m in dead:
            tags = ",".join(m.get("tags") or []) or "-"
            sys.stdout.write(f"  {m['id']}  [{m['importance']}] hits={m.get('hits',0)}  tags={tags}\n")
            sys.stdout.write(f"      {m['content'][:88]}\n")
    if stale:
        sys.stdout.write("\n=== 过期快照：引用前建议核实 ===\n")
        for m in stale:
            sys.stdout.write(f"  {m['id']}  [{m['importance']}] {m['content'][:66]}\n")
    if not dead and not stale:
        sys.stdout.write("干净，没有问题条目。\n")
    return 0


# 体检用的探测句：覆盖「一个 agent 日常会问到的方向」，而不是记忆里已有的词。
# 目的不是命中某条具体记忆，而是看这些方向里有没有全都召不回来的死胡同。
DEFAULT_PROBES = [
    "这个项目是干什么的", "技术栈选了什么", "目录结构是怎样的",
    "怎么跑起来 部署", "依赖装不上包", "报错了看哪里",
    "接口怎么调用", "配置放在哪个文件", "环境变量怎么设",
    "上次讨论的结论是什么", "这个方案定了吗", "为什么不用另一种做法",
    "之前踩过什么坑", "这个 bug 修过没有", "测试怎么跑",
    "代码规范是什么", "提交信息怎么写", "分支怎么管理",
    "接下来做什么", "截止时间是什么时候", "这个要不要做",
    "数据库结构在哪", "日志在哪个目录", "出问题怎么回滚",
]


def _selftest_queries(mem: dict) -> list[str]:
    """用记忆自身的显著词构造探测 query，检验它是否可检索。"""
    from .core.search import _doc_tokens

    toks = _doc_tokens(mem)
    if not toks:
        return []
    # 取最长的几个词（信息量最大），拼 2~3 个探测句
    toks = sorted(set(toks), key=len, reverse=True)[:6]
    return [" ".join(toks[:3]), " ".join(toks[3:6]) or " ".join(toks[:3])]


def cmd_doctor(args) -> int:
    """自检：数据目录、SQLite、MCP stdio 服务是否到位。"""
    conn = _conn()
    home = db.home_dir()
    sys.stdout.write(f"OwnMem v{__version__}\n")
    sys.stdout.write(f"数据目录：{home}\n")
    sys.stdout.write(f"数据库：{db.db_path()}（存在={db.db_path().exists()}）\n")
    s = MemoryStore(conn).stats()
    sys.stdout.write(f"记忆：{s['active']} 条，容量 {s['capacity']}，回收站 {s['trashed']}\n")
    sid = _resolve_session(args, conn) if getattr(args, "session", "") else ""
    st = policy.staged_stats(conn)
    staged_desc = (
        f"{st['items']} 条 / {st['sessions']} 个会话"
        f"（超 {st['max_age_days']} 天未处理 {st['stale_items']} 条，"
        f"可用 ownmem staged-prune --yes 清理）"
        if st["items"] else "空"
    )
    sys.stdout.write(f"待定区：{staged_desc}\n")

    # ---- 注意力健康：召回预算是定额的，堆得多了自然会稀释 ----
    rows = conn.execute(
        "SELECT importance, status, content FROM memories WHERE status='active'"
    ).fetchall()
    total_chars = 0
    by_imp: dict[str, list[int]] = {}
    longies: list[tuple[int, str, str]] = []
    for row in rows:
        imp = row["importance"] or "normal"
        content = row["content"] or ""
        n = len(content)
        total_chars += n
        by_imp.setdefault(imp or "normal", []).append(n)
        if n > PREVIEW_LEN:
            longies.append((n, imp or "normal", content or ""))
    longies.sort(reverse=True)

    # 预算必须读「实际生效值」：用户在 settings 里调过之后，体检还报默认值
    # 就等于在骗自己——这行数字的唯一用途就是让人判断要不要调。
    always_budget = recall_mod._budget(conn, "always_budget", recall_mod.DEFAULT_ALWAYS_BUDGET)
    related_budget = recall_mod._budget(conn, "recall_budget", recall_mod.DEFAULT_CHAR_BUDGET)
    sys.stdout.write(
        f"注意力：常驻预算 {always_budget} 字 + 相关预算 {related_budget} 字 "
        f"= 每轮上限 {always_budget + related_budget} 字\n"
    )
    sys.stdout.write(
        f"         库里 active {len(rows)} 条 / {total_chars} 字"
        f"（够塞满 {round(total_chars / max(1, always_budget + related_budget), 1)} 轮预算）\n"
    )
    for imp in ("always", "critical", "high", "normal", "low"):
        if imp in by_imp:
            sys.stdout.write(
                f"           {imp:9} {len(by_imp[imp]):>3} 条 / {sum(by_imp[imp]):>6} 字\n"
            )
    if longies:
        sys.stdout.write(
            f"         超 {PREVIEW_LEN} 字的 {len(longies)} 条"
            f"（会被预览截断，最要命的半句可能在尾巴上）：\n"
        )
        for n, imp, c in longies[:3]:
            sys.stdout.write(f"           [{imp}] {n} 字 {c[:36]}…\n")
        sys.stdout.write("         建议：ownmem unreached 查僵尸记忆，ownmem edit <id> 改写或压缩。\n")

    # ---- 治理健康（v2 P1）：账本会不会烂，看这四个数 ----
    st = MemoryStore(conn).stats()
    kinds = st.get("by_kind") or {}
    sys.stdout.write(
        f"治理：结论 {kinds.get('conclusion', 0)} / 过程 {kinds.get('process', 0)} / "
        f"踩坑 {kinds.get('pitfall', 0)} 条｜作废（已被取代）{st.get('superseded', 0)} 条"
    )
    if st.get("recurring_pitfalls"):
        sys.stdout.write(
            f"｜复发中的坑 {st['recurring_pitfalls']} 个（累计 {st['recurrence_total']} 次）\n"
        )
    else:
        sys.stdout.write("｜暂无复发坑\n")
    faded = MemoryStore(conn).decay_report(floor=0.4)["faded"]
    if faded:
        sys.stdout.write(
            f"       快淡出的 {len(faded)} 条（分数已到地板 0.35～0.45）："
            f"最重要的那条是 [{faded[0]['importance']}] {faded[0]['content'][:32]}…\n"
        )
    sys.stdout.write("       账本晋级：ownmem upgrade --dry-run　｜　注入预算：ownmem budget\n")

    sys.stdout.write(f"Python：{sys.version.split()[0]}\n")
    sys.stdout.write("MCP stdio 自检：")
    try:
        from .mcp_server import TOOLS
        sys.stdout.write(f"OK（自带零依赖实现，{len(TOOLS)} 个工具，无需第三方 mcp 包）\n")
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(f"失败：{exc}\n")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ownmem", description="OwnMem — 你的 agent 的记忆，归你。")
    p.add_argument("--version", action="version", version=f"ownmem {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, session=True):
        sp.add_argument("--agent", default="cli", help="调用方标识，写入审计用")
        if session:
            sp.add_argument("--session", default="", help="会话 ID")

    sp = sub.add_parser("recall", help="自动召回并输出可注入上下文")
    add_common(sp)
    sp.set_defaults(agent="")  # 留空：hook 模式下默认 claude-code，否则 cli
    sp.add_argument("--prompt", default="", help="用户输入（不传则从 stdin 读）")
    sp.add_argument("--hook", choices=["none", "claude"], default="none",
                    help="claude：按 Claude Code UserPromptSubmit hook 协议输出 JSON")
    sp.add_argument("--limit", type=int, default=6)
    sp.add_argument("--project", default="", help="按项目过滤（默认取会话归属）")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_recall)

    sp = sub.add_parser("mode", help="查看/切换记忆模式（normal/incognito/blank/ghost）")
    add_common(sp)
    sp.add_argument("mode", nargs="?", default="")
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser("view", help="查看/切换会话视角（normal/result-only/blind-review/cross-check）")
    add_common(sp)
    sp.add_argument("view", nargs="?", default="")
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("answer-first", help="先答后对：begin → submit → reveal → cancel")
    add_common(sp)
    sp.add_argument("action", nargs="?", default="", choices=["", "begin", "submit", "reveal", "status", "cancel"])
    sp.add_argument("content", nargs="?", default="", help="submit 时传入独立方案全文")
    sp.set_defaults(func=cmd_answer_first)

    sp = sub.add_parser("divergence", help="分歧报告：找出不同 AI 对同一件事的不同说法")
    add_common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--low", type=float, default=divergence.LOW_SIM)
    sp.add_argument("--high", type=float, default=divergence.HIGH_SIM)
    sp.add_argument("--all-projects", action="store_true", dest="all_projects",
                    help="跨项目查（默认只看当前项目+全局）")
    sp.set_defaults(func=cmd_divergence)

    sp = sub.add_parser("status", help="当前状态")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("add", help="写入一条记忆（过闸门）")
    add_common(sp)
    sp.add_argument("content")
    sp.add_argument("--importance", default="normal", choices=["always", "critical", "high", "normal", "low"])
    sp.add_argument("--tags", default="")
    sp.add_argument("--source", default="")
    sp.add_argument("--kind", default="conclusion", choices=list(VALID_KINDS),
                    help="三本账：conclusion=结论 / process=过程 / pitfall=踩坑")
    sp.add_argument("--project", default="", help="归属项目（默认取会话归属，空=全局）")
    sp.add_argument("--supersedes", default="", metavar="ID",
                    help="这条新记忆推翻了哪条旧记忆（自动挂取代链，旧条作废但保留）")
    sp.add_argument("--force", action="store_true", help="绕过隐身闸门（用户显式操作）")
    sp.add_argument("--auto", action="store_true",
                    help="声明来自自动提炼：过质量闸门，存疑的落待定区而不是长期记忆")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("triage", help="干跑质量闸门：看看这段文字会被怎么裁决")
    sp.add_argument("content")
    sp.set_defaults(func=cmd_triage)

    sp = sub.add_parser("edit", help="就地改写一条记忆（不留平行副本）")
    add_common(sp)
    sp.add_argument("id")
    sp.add_argument("content")
    sp.add_argument("--tags", default="")
    sp.add_argument("--importance", default="", choices=["always", "critical", "high", "normal", "low"])
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("search", help="检索记忆")
    add_common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--source", default="")
    sp.add_argument("--agent-filter", default="", dest="agent_filter")
    sp.add_argument("--project", default="", help="只搜该项目（含全局记忆）")
    sp.add_argument("--kind", default="", choices=[""] + list(VALID_KINDS),
                    help="只搜某本账")
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--superseded", action="store_true",
                    help="连已被取代的旧结论一起搜（考古用，默认不出现）")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("list", help="列出记忆")
    add_common(sp, session=False)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--source", default="")
    sp.add_argument("--agent-filter", default="", dest="agent_filter")
    sp.add_argument("--importance", default="")
    sp.add_argument("--kind", default="", choices=[""] + list(VALID_KINDS))
    sp.add_argument("--project", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="查看单条（含取代链与复发记录）")
    add_common(sp, session=False)
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("supersede", help="推翻一条旧结论：写新条 + 挂取代链，旧条保留可查")
    add_common(sp)
    sp.add_argument("id", help="被推翻的旧记忆 id")
    sp.add_argument("content", help="新的结论")
    sp.add_argument("--importance", default="", choices=[""] + list(VALID_IMPORTANCE),
                    help="默认沿用旧条的重要度")
    sp.add_argument("--kind", default="", choices=[""] + list(VALID_KINDS))
    sp.add_argument("--tags", default="")
    sp.add_argument("--source", default="")
    sp.add_argument("--project", default="", help="默认沿用旧条的项目归属")
    sp.set_defaults(func=cmd_supersede)

    sp = sub.add_parser("chain", help="看一条记忆的取代链（推翻了谁 / 又被谁推翻）")
    add_common(sp, session=False)
    sp.add_argument("id")
    sp.set_defaults(func=cmd_chain)

    sp = sub.add_parser("upgrade", help="把反复被用到的过程账晋级为结论账")
    add_common(sp, session=False)
    sp.add_argument("--min-hits", type=int, default=5, dest="min_hits")
    sp.add_argument("--min-sessions", type=int, default=3, dest="min_sessions")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run", help="只看不改")
    sp.set_defaults(func=cmd_promote_kind)

    sp = sub.add_parser("budget", help="查看/调整每轮注入预算（字数）")
    add_common(sp, session=False)
    sp.add_argument("--recall", type=int, default=None, help="相关记忆的字符预算")
    sp.add_argument("--always", type=int, default=None, help="常驻规则的字符预算")
    sp.add_argument("--reset", action="store_true", help="恢复默认")
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("delete", help="删除（进回收站）")
    add_common(sp, session=False)
    sp.add_argument("id", nargs="+")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("restore", help="从回收站恢复")
    add_common(sp, session=False)
    sp.add_argument("id", nargs="+")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("trash", help="查看回收站")
    add_common(sp, session=False)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("wipe", help="抹除某来源的全部记忆")
    add_common(sp, session=False)
    sp.add_argument("--agent-filter", default="", dest="agent_filter")
    sp.add_argument("--source", default="")
    sp.add_argument("--hard", action="store_true",
                    help="彻底删除：物理删除并收缩数据库，同时清空相关审计内容摘录（不可恢复）")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_wipe)

    sp = sub.add_parser("staged", help="查看待定区")
    add_common(sp)
    sp.set_defaults(func=cmd_staged)

    sp = sub.add_parser("promote", help="把待定区条目写入长期记忆")
    add_common(sp)
    sp.add_argument("--ids", default="")
    sp.add_argument("--content", action="append", default=[])
    sp.add_argument("--importance", default="normal")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("discard", help="丢弃待定区条目")
    add_common(sp)
    sp.add_argument("--ids", default="")
    sp.set_defaults(func=cmd_discard)

    sp = sub.add_parser("staged-prune",
                        help="清理超龄的待定区残留（默认只报数，--yes 才真删）")
    add_common(sp, session=False)
    sp.add_argument("--days", type=int, default=policy.DEFAULT_STAGED_MAX_AGE_DAYS,
                    help=f"超过多少天未处理算超龄，默认 {policy.DEFAULT_STAGED_MAX_AGE_DAYS} 天")
    sp.add_argument("--yes", action="store_true", help="真的执行清理")
    sp.set_defaults(func=cmd_staged_prune)

    sp = sub.add_parser("audit", help="审计流水")
    add_common(sp)
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--agent-filter", default="", dest="agent_filter")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("receipt", help="会话回执")
    add_common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_receipt)

    sp = sub.add_parser("sessions", help="会话列表")
    add_common(sp, session=False)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("session-end", help="结束会话并打印回执（可作 SessionEnd hook，默认自动沉淀）")
    add_common(sp)
    sp.add_argument("--note", default="", help="本次会话的一句话要点（会写进沉淀最前面）")
    sp.add_argument("--no-digest", action="store_true", help="只出回执，不沉淀")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_session_end)

    sp = sub.add_parser("project", help="查看/切换当前会话归属的项目（记忆按项目分家）")
    add_common(sp)
    sp.add_argument("project", nargs="?", default="", help="项目名；不传则显示当前")
    sp.add_argument("--clear", action="store_true", help="切回全局（不做项目隔离）")
    sp.add_argument("--default", action="store_true", help="同时设为新会话的默认项目")
    sp.set_defaults(func=cmd_project)

    sp = sub.add_parser("digest", help="会话沉淀：查看/生成「上次干到哪了」")
    add_common(sp)
    sp.add_argument("--list", action="store_true", help="列出最近的沉淀")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--write", action="store_true", help="立即为本会话生成沉淀")
    sp.add_argument("--note", default="", help="沉淀里的一句话要点")
    sp.add_argument("--show", default="", help="按 id 查看某条沉淀")
    sp.add_argument("--session-end", action="store_true", dest="session_end",
                    help="先结束会话再沉淀（SessionEnd hook 用法）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("mcp", help="启动 MCP server（stdio）")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("ui", help="启动 Web 审计台")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser(
        "gateway",
        help="启动模型网关：在 LLM 请求层注入记忆，适配所有能改 base_url 的 agent",
    )
    sp.add_argument("--port", type=int, default=8790)
    sp.add_argument("--upstream", default="",
                    help="真实模型 API base，如 https://token.sensenova.cn/v1（或设 OWNMEM_UPSTREAM）")
    sp.add_argument("--agent", default="", help="本网关实例服务的 agent 名（写进审计归属）")
    sp.add_argument("--prefix", default="/v1", help="对外暴露的路径前缀，默认 /v1")
    sp.add_argument("--db", default="", help="OwnMem 数据库路径，默认用全局数据目录")
    sp.set_defaults(func=cmd_gateway)

    sp = sub.add_parser("export", help="导出全部记忆")
    add_common(sp, session=False)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("unreached", help="僵尸记忆体检：列出怎么问都召不回来的记忆")
    sp.add_argument("--probe", default="", help="追加探测词，逗号分隔（会用\n这些词模拟真实提问来测召回）")
    add_common(sp, session=False)
    sp.set_defaults(func=cmd_unreached)

    sp = sub.add_parser("doctor", help="自检")
    add_common(sp, session=False)
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
