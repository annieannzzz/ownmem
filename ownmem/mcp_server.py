# -*- coding: utf-8 -*-
"""OwnMem MCP Server：把记忆控制权交给用户，而不是交给模型。

与普通记忆库的区别：
  1. 所有写入都过闸门——隐身会话里 agent 说「已保存」，实际被拦下并进了待定区
  2. 所有读写都进审计——用户可以随时查「谁在什么时候动了我的记忆」
  3. 工具描述里显式告诉模型：不要假装记得，先查再答

传输：零依赖 stdio 实现（见 mcp_stdio.py），不依赖官方 mcp 包。

启动：
  ownmem mcp              # stdio（Claude Code / OpenCode / Cline 用这个）
"""

from __future__ import annotations

import os
import sqlite3

from . import __version__
from .core import audit, bridge, db, digest as digest_mod, divergence, sessions
from .core.store import MemoryStore, format_hits
from .gateway import policy, recall
from .mcp_stdio import Tool, serve

INSTRUCTIONS = """OwnMem 是这个环境里用户唯一可控的长期记忆层。它有四种模式：normal / incognito（不写入）/ blank（不读取）/ ghost（既不读也不写）。

使用守则：
1. 回答涉及「用户以前说过/做过/偏好」的问题前，先调用 recall 或 search_memory——不要凭印象猜，也不要声称「我记得」。
2. 遇到值得长期记住的信息（身份、偏好、项目进展、重要决定），调用 add_memory。批量/自动提炼时用 channel="auto"，闸门会把没价值的内容挡在外面。若当前处于 incognito/ghost 模式，写入会被拦截并转存到「待定区」，由用户决定是否保留——这是设计如此，请如实告知用户「已进入待定区，需你确认」，不要谎称已保存。
3. 用户问「你记了什么/删掉了什么」时，调用 audit_log 给出真实流水，不要编造。
4. 用户要求「这次别记」「当没听到」时，调用 session_mode 切到 incognito；要求「别用旧印象回答我」时切到 blank。注意：会话模式只能调向更私密（如 normal→incognito→ghost），放宽需要用户自己在 CLI 或审计台操作。
5. 记忆有容量上限，低重要度的旧记忆会被自动淘汰进回收站（可 restore_memory 找回）。
6. 记忆分三本账（kind）：conclusion=结论（定下来的事实/决定）、process=过程（当时怎么想的）、pitfall=踩坑（哪条路走不通）。踩坑账默认对所有会话可见；过程账只在需要追溯时查。
7. 记忆按项目分家（project）：全局记忆（project 为空）到处都是红线与身份事实；项目记忆只在归属项目里出现。写入不确定归属时留空，让它继承会话。
8. 会话视角（session_view）与模式正交：盲审（blind-review）下你看到的上下文已被剥离其他 AI 的结论与思路——这是用户刻意的安排（让你独立判断），不要试图绕过，也不必向用户确认「为什么看不到旧结论」；交叉验证（cross-check）下注入的记忆带有「可能是错的」提示，逐条验证后再采信。
9. 上下文里若出现「先答后对」提示：第 1 阶段请真的只凭任务本身独立作答（这是整套机制的价值所在，偷看旧记忆等于作废）；第 2 阶段逐点对照并单独列出分歧点，供用户裁决。
"""


# ---------------------------------------------------------------------------
# 会话身份：hook 与 MCP 同属一个对话，靠 hook 落下的桥接设置对齐
# ---------------------------------------------------------------------------
_PROC_SESSION = ""  # 桥接不可用时的兜底：一个 MCP 进程 = 一个会话

# 桥接会话的新鲜度窗口：hook 每次召回都会刷新，超窗视为上一段对话的残留
_BRIDGE_TTL_SECONDS = 12 * 3600


def _agent() -> str:
    return os.environ.get("OWNMEM_AGENT", "mcp").strip() or "mcp"


def _bridged_session(conn: sqlite3.Connection) -> str:
    """读 hook 落下的桥接设置（active_session:{agent}），让 MCP 的读写
    与同一对话里 hook 的召回记在同一个会话下，回执才能对上账。

    解析统一交给 core.bridge——hook 与 CLI 都走同一份实现，格式不会再分叉。
    """
    return bridge.read(conn, _agent(), _BRIDGE_TTL_SECONDS)


def _session_id(conn: sqlite3.Connection) -> str:
    sid = os.environ.get("OWNMEM_SESSION", "").strip()
    if sid:
        return sid
    bridged = _bridged_session(conn)
    if bridged:
        return bridged
    global _PROC_SESSION
    if not _PROC_SESSION:
        _PROC_SESSION = sessions.new_session_id()
        sessions.touch_session(conn, _PROC_SESSION, agent=_agent())
    return _PROC_SESSION


def _conn() -> sqlite3.Connection:
    return db.connect()


# ---------------------------------------------------------------------------
# 工具实现（handler 只关心业务，schema 在下面显式声明）
# ---------------------------------------------------------------------------
def _t_recall(query: str = "", limit: int = 6) -> str:
    conn = _conn()
    sid = _session_id(conn)
    if not (query or "").strip():
        return "请传入 query（要检索的话题）。"
    res = recall.recall_context(conn, query, session_id=sid, agent=_agent(), limit=int(limit))
    if res["blocked"]:
        return f"（{res['reason']}：本次不注入任何历史记忆，请以全新状态回答）"
    if not res["context"]:
        return "没有找到相关记忆。"
    return res["context"]


def _t_search_memory(query: str = "", limit: int = 8, source: str = "", agent: str = "",
                     kind: str = "", project: str = "") -> str:
    conn = _conn()
    sid = _session_id(conn)
    if not sessions.can_read(conn, sid):
        return f"（当前 {sessions.get_mode(conn, sid)} 模式：不读取任何历史记忆）"
    if not (query or "").strip():
        return "请传入 query。"
    store = MemoryStore(conn)
    top = store.search(
        query, limit=int(limit), source=source, agent=agent,
        kind=kind, project=project or sessions.get_project(conn, sid),
    )
    for mem, _ in top:
        audit.log(conn, "recall", agent=_agent(), session_id=sid, memory_id=mem["id"],
                  detail=audit.detail(mem["content"], 80), mode=sessions.get_mode(conn, sid))
    return format_hits(top)


def _t_add_memory(content: str = "", importance: str = "normal", tags: str = "",
                  channel: str = "explicit", kind: str = "conclusion",
                  project: str = "", supersedes: str = "") -> str:
    """channel="auto" 用于自动提炼（hook / 批量收割）：走质量闸门，
    存疑的落待定区而不是直接进长期记忆——召回预算是定额的，写入侧不设门槛
    等于给未来的注意力收税。默认 explicit，保持原有行为不变。

    kind: conclusion=结论账 / process=过程账 / pitfall=踩坑账（三本账，v2）。
    project 留空则继承会话归属。
    supersedes: 这条新结论推翻了哪条旧记忆——旧条作废但不删，保留可追。"""
    conn = _conn()
    sid = _session_id(conn)
    res = policy.guarded_write(
        conn, content, session_id=sid, agent=_agent(), importance=importance,
        tags=tags, channel=channel or "explicit", kind=kind or "conclusion",
        project=project or "", supersedes=(supersedes or "").strip(),
    )
    if res.get("verdict") in ("reject", "stage"):
        # 闸门拦下的：如实转述，不许谎称已记住
        return f"⛔ {res['message']}\n请如实告知用户这条没有被长期记住。"
    if res["blocked"]:
        # 闸门内部已把内容转入待定区（ghost 除外），这里只负责如实转述
        if res.get("staged_id"):
            return (
                f"⛔ 未写入长期记忆（当前 {res['mode']} 模式）。内容已转入待定区 "
                f"{res['staged_id']}，用户可在会话结束时决定是否保留。"
                f"请如实告知用户，不要声称已记住。"
            )
        return (
            f"⛔ 未写入长期记忆（当前 {res['mode']} 模式），且未留存任何副本。"
            f"请如实告知用户，不要声称已记住。"
        )
    return res["message"]


def _t_session_mode(mode: str = "") -> str:
    conn = _conn()
    sid = _session_id(conn)
    cur = sessions.get_mode(conn, sid)
    if not (mode or "").strip():
        info = sessions.MODES[cur]
        return (
            f"当前会话 {sid} 模式：{cur}（{info['desc']}）｜"
            f"读记忆={'是' if info['read'] else '否'}，写记忆={'是' if info['write'] else '否'}"
        )
    target = sessions.normalize_mode(mode)
    # 模型只能把会话调向更私密；放宽必须由用户在 CLI / 审计台操作
    cur_flags = sessions.MODES[cur]
    tgt_flags = sessions.MODES[target]
    if tgt_flags["read"] > cur_flags["read"] or tgt_flags["write"] > cur_flags["write"]:
        return (
            f"⛔ 模型不能把会话从 {cur} 放宽为 {target}。"
            f"放宽记忆模式需要用户自己在 CLI（ownmem mode）或审计台操作。"
        )
    new_mode = sessions.set_mode(conn, sid, target, agent=_agent())
    info = sessions.MODES[new_mode]
    return f"已切换为 {new_mode}（{info['desc']}）。后续记忆读写按此模式执行。"


def _t_list_staged() -> str:
    conn = _conn()
    sid = _session_id(conn)
    items = policy.list_staged(conn, sid)
    if not items:
        return "待定区为空。"
    lines = [f"待定区共 {len(items)} 条（仅存于本地待确认区，未进入长期记忆）："]
    for it in items:
        lines.append(f"- [{it['sid']}] ({it['staged_at'][:16]}) {it['content'][:100]}")
    return "\n".join(lines)


def _t_promote(sids: str = "") -> str:
    conn = _conn()
    sid = _session_id(conn)
    id_list = [s.strip() for s in (sids or "").split(",") if s.strip()]
    if not id_list:
        return "请传入要保留的条目编号（如 p_001,p_002）。"
    res = policy.promote(conn, sid, sids=id_list, agent=_agent())
    return res["message"]


def _t_discard(sids: str = "") -> str:
    conn = _conn()
    sid = _session_id(conn)
    id_list = [s.strip() for s in (sids or "").split(",") if s.strip()]
    if not id_list:
        return "请传入要丢弃的条目编号（如 p_001,p_002）。"
    n = policy.discard(conn, sid, sids=id_list)
    return f"已丢弃 {n} 条待定区条目（不留痕，也不记入审计详情）。"


def _t_audit_log(limit: int = 30, session_id: str = "") -> str:
    conn = _conn()
    sid = session_id or _session_id(conn)
    events = audit.list_events(conn, session_id=sid if session_id else "", limit=int(limit))
    if not events:
        return "审计日志为空（本会话暂无记忆读写）。"
    lines = ["审计流水（最新在前）："]
    for e in events:
        label = audit.EVENT_LABELS.get(e["event"], e["event"])
        mid = f" id={e['memory_id']}" if e["memory_id"] else ""
        lines.append(
            f"- {e['ts'][:19]} [{label}] agent={e['agent']} session={e['session_id'] or '-'}"
            f"{mid} mode={e['mode'] or '-'} {e['detail'][:60]}"
        )
    return "\n".join(lines)


def _t_session_receipt(session_id: str = "") -> str:
    conn = _conn()
    sid = session_id or _session_id(conn)
    return audit.receipt_text(audit.receipt(conn, sid))


def _t_last_progress() -> str:
    """上次进度：同一项目最近一次会话的沉淀——接手前先看它，别让用户复述前情。"""
    conn = _conn()
    sid = _session_id(conn)
    if not sessions.can_read(conn, sid):
        return f"（当前 {sessions.get_mode(conn, sid)} 模式：不读取历史记忆）"
    project = sessions.get_project(conn, sid)
    d = digest_mod.latest(conn, project=project, exclude_session=sid)
    if not d:
        return "同项目还没有上一次会话的沉淀（会话结束时用 ownmem session-end 生成）。"
    audit.log(
        conn, "digest_inject", agent=_agent(), session_id=sid, memory_id=d["id"],
        detail=f"手动拉取 {d['session_id']} 的沉淀", mode=sessions.get_mode(conn, sid),
    )
    return (
        f"上一次会话（{d['session_id']}，{d['created_at'][:16]}，"
        f"项目={d['project'] or '全局'}，agent={d['agent']}）留下的沉淀：\n\n{d['text']}"
    )


def _t_get_memory(memory_id: str = "") -> str:
    conn = _conn()
    store = MemoryStore(conn)
    mem = store.get(memory_id)
    if not mem or mem.get("status") != "active":
        return f"未找到 id={memory_id} 的活跃记忆。"
    label = {"conclusion": "结论", "process": "过程", "pitfall": "踩坑"}
    lines = [
        f"[{mem['id']}] 创建于 {mem['created_at'][:16]}｜"
        f"{label.get(mem.get('kind') or 'conclusion')}账｜重要度={mem['importance']}｜"
        f"来源={mem['agent']}｜命中={mem['hits']} 次"
    ]
    if mem.get("project"):
        lines.append(f"项目={mem['project']}")
    if int(mem.get("recurred") or 0):
        agents = mem.get("recurred_agents") or []
        lines.append(f"⚠️ 已复发 {mem['recurred']} 次，栽过：{'/'.join(agents) if isinstance(agents, list) else agents}")
    if mem.get("superseded_by"):
        lines.append(f"⛔ 已被 {mem['superseded_by']} 取代（作废，仅供参考历史）")
    lines.append("")
    lines.append(mem["content"])
    ch = store.chain(memory_id)
    if ch["older"] or ch["newer"]:
        lines.append("")
        lines.append("取代链：")
        for m in reversed(ch["older"]):
            lines.append(f"  ↑推翻 [{m['id']}] {m['content'][:60]}")
        for m in ch["newer"]:
            lines.append(f"  ↓被取代 [{m['id']}] {m['content'][:60]}")
    return "\n".join(lines)


def _t_delete_memory(memory_id: str = "") -> str:
    conn = _conn()
    ok, msg = MemoryStore(conn).delete(memory_id)
    if ok:
        audit.log(conn, "delete", agent=_agent(), session_id=_session_id(conn), memory_id=memory_id)
    return msg


def _t_restore_memory(memory_id: str = "") -> str:
    conn = _conn()
    ok, msg = MemoryStore(conn).restore(memory_id)
    if ok:
        audit.log(conn, "restore", agent=_agent(), session_id=_session_id(conn), memory_id=memory_id)
    return msg


def _t_wipe_source(agent: str = "", source: str = "") -> str:
    conn = _conn()
    n, msg = MemoryStore(conn).wipe(agent=agent, source=source)
    # 待定区也是一处落盘，抹除时一并清掉（待定条目只带 agent，故仅按 agent 时匹配）
    n_staged = policy.discard_staged_by_agent(conn, agent)
    audit.log(conn, "wipe", agent=_agent(), session_id=_session_id(conn),
              detail=f"agent={agent} source={source} 共 {n} 条，另清待定区 {n_staged} 条")
    if n_staged:
        return msg + f"\n待定区一并清空 {n_staged} 条。"
    return msg


def _t_memory_stats() -> str:
    s = MemoryStore(_conn()).stats()
    return (
        f"记忆总数：{s['active']}/{s['capacity']}（回收站 {s['trashed']} 条）\n"
        f"重要度分布：{s['by_importance']}\n"
        f"来源分布：{s['by_agent']}\n"
        f"数据目录：{s['home']}\n"
        f"Web 审计台：ownmem ui → http://127.0.0.1:8787/"
    )


def _t_session_view(view: str = "") -> str:
    """查询/切换会话视角。视角管「读的时候看到什么」，与 mode 正交。"""
    conn = _conn()
    sid = _session_id(conn)
    if not (view or "").strip():
        cur = sessions.get_view(conn, sid)
        mode = sessions.get_mode(conn, sid)
        extra = f"（注意：当前模式 {mode} 整体断读，视角暂不生效）" if mode in ("blank", "ghost") else ""
        return f"当前视角：{cur}（{sessions.VIEWS[cur]['desc']}）{extra}"
    new_view = sessions.set_view(conn, sid, view, agent=_agent())
    return f"已切换视角为 {new_view}（{sessions.VIEWS[new_view]['desc']}）。"


def _t_answer_first(action: str = "status", content: str = "") -> str:
    """先答后对两阶段：independent（屏蔽旧记忆独立作答）→ compare（对照）。"""
    conn = _conn()
    sid = _session_id(conn)
    action = (action or "status").strip().lower()
    phase = sessions.af_phase(conn, sid)

    if action == "status":
        if not phase:
            return "未开启先答后对。流程：begin（盲答）→ submit（存独立方案）→ reveal（对照）。"
        ans = sessions.af_answer_id(conn, sid)
        return (
            f"阶段：{phase}{'｜独立方案=' + ans if ans else '｜尚未 submit 独立方案'}"
        )
    if action == "begin":
        sessions.af_set_phase(conn, sid, "independent", agent=_agent())
        return (
            "第 1 阶段（独立作答）已开始：旧记忆与历史进度已屏蔽（常驻红线除外）。"
            "请仅凭当前任务独立给出方案，完成后用 answer_first(action=\"submit\", content=方案全文) 存档。"
        )
    if action == "submit":
        if not (content or "").strip():
            return "请传入 content（独立方案全文）。"
        res = policy.guarded_write(
            conn, content, session_id=sid, agent=_agent(),
            kind="process", tags="answer-first", source="answer-first", channel="explicit",
        )
        if not res.get("ok"):
            return f"⛔ {res['message']}\n请如实告知用户方案没有被存档。"
        sessions.af_set_answer(conn, sid, res["id"])
        return f"独立方案已存档（{res['id']}）。用户确认后 answer_first(action=\"reveal\") 进入对照。"
    if action == "reveal":
        if phase != "independent":
            return "当前不在独立作答期（先 begin）。"
        if not sessions.af_answer_id(conn, sid):
            return "还没有 submit 独立方案，对照无从谈起。"
        sessions.af_set_phase(conn, sid, "compare", agent=_agent())
        return (
            "第 2 阶段（对照）已开始：下一轮起将注入你的独立方案原文 + 相关旧记忆 + 对照指令。"
            "请逐点对照并列出分歧点，分歧点单独列出供用户裁决。"
        )
    if action == "cancel":
        sessions.af_clear(conn, sid)
        return "先答后对状态已清。"
    return f"未知 action：{action}（begin/submit/reveal/status/cancel）"


def _t_divergence_report(query: str = "", limit: int = 20) -> str:
    """分歧报告：不同 agent 对同一件事的不同说法，配对并排展示。"""
    conn = _conn()
    sid = _session_id(conn)
    if not (query or "").strip():
        return "请传入 query（要查分歧的话题）。"
    if not sessions.can_read(conn, sid):
        return f"（当前 {sessions.get_mode(conn, sid)} 模式：不读取历史记忆）"
    rep = divergence.report(
        conn, query, limit=int(limit), project=sessions.get_project(conn, sid),
    )
    return divergence.render(rep)


# ---------------------------------------------------------------------------
# 工具清单（名字 / 描述 / JSON Schema / handler）
# ---------------------------------------------------------------------------
TOOLS: list[Tool] = [
    Tool(
        "recall",
        "召回与此轮对话相关的记忆（自动召回的手动版）。涉及用户过去的事情、项目、偏好时先调用它，不要凭印象回答。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的话题或用户当前这句话"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 6"},
            },
            "required": ["query"],
        },
        _t_recall,
    ),
    Tool(
        "search_memory",
        "检索记忆仓库（BM25）。可传 source/agent/kind/project 过滤，返回带 id 的条目；需要全文用 get_memory。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "source": {"type": "string"},
                "agent": {"type": "string"},
                "kind": {"type": "string",
                         "enum": ["conclusion", "process", "pitfall"],
                         "description": "只搜某本账：结论/过程/踩坑"},
                "project": {"type": "string", "description": "项目名，默认取当前会话归属"},
            },
            "required": ["query"],
        },
        _t_search_memory,
    ),
    Tool(
        "add_memory",
        "存入一条长期记忆。incognito/ghost 模式下会被闸门拦截并进入待定区（由用户决定是否保留），返回值会明确说明是否真的保存成功。importance: always/critical/high/normal/low。kind: conclusion=结论账（定下来的事实/决定）/ process=过程账（当时怎么想的）/ pitfall=踩坑账（哪条路走不通；同一个坑再次被写入不会新增条目，只累加复发次数）。channel=auto 表示这条来自自动提炼，会先过质量闸门：寒暄/提问被丢弃，存疑的进待定区而不是长期记忆。supersedes 用于推翻旧结论（旧条作废但保留可查，不要用新条目去和旧条目并存）。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要长期记住的内容"},
                "importance": {"type": "string", "enum": ["always", "critical", "high", "normal", "low"]},
                "tags": {"type": "string", "description": "逗号分隔的标签"},
                "channel": {"type": "string", "enum": ["explicit", "auto"],
                            "description": "explicit（默认，用户或你明确指定要记）；auto 用于自动提炼，走质量闸门"},
                "kind": {"type": "string", "enum": ["conclusion", "process", "pitfall"],
                         "description": "三本账：结论 / 过程 / 踩坑，默认结论"},
                "project": {"type": "string", "description": "归属项目，默认继承会话"},
                "supersedes": {"type": "string",
                               "description": "这条新结论推翻了哪条旧记忆的 id（旧条自动作废但保留，可追溯）"},
            },
            "required": ["content"],
        },
        _t_add_memory,
    ),
    Tool(
        "session_mode",
        "查询或切换当前会话的记忆模式。mode: normal（可读可写）/ incognito（可读不写）/ blank（不读旧记忆，产出可写）/ ghost（既不读也不写）。不传 mode 只查询。注意：只能调向更私密（normal→incognito/blank→ghost），放宽会被拒绝，需用户在 CLI 或审计台操作。",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["normal", "incognito", "blank", "ghost"]},
            },
        },
        _t_session_mode,
    ),
    Tool(
        "session_view",
        "查询或切换当前会话的**视角**（与 mode 正交：mode 管能不能读，view 管读的时候看到什么）。view: normal（全量）/ result-only（只看结论与踩坑，隐藏过程）/ blind-review（盲审：隐藏其他 AI 的结论与思路，踩坑照常，用于挑毛病或发散）/ cross-check（全量+对抗提示：旧结论可能是错的，逐条验证）。用户说「你来看看上一个 AI 做得对不对」时切 blind-review；说「帮我交叉验证一下这些结论」时切 cross-check。",
        {
            "type": "object",
            "properties": {
                "view": {"type": "string",
                         "enum": ["normal", "result-only", "blind-review", "cross-check"]},
            },
        },
        _t_session_view,
    ),
    Tool(
        "answer_first",
        "先答后对（两阶段，压上下文锚定最有效的一招）：begin 让旧记忆全部屏蔽、你独立作答；submit 把你的独立方案存档；reveal 恢复注入并给出你的方案原文+旧记忆，让你逐点对照列分歧。用户要求「先别看以前的记录自己答，然后再对照」时用它。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["begin", "submit", "reveal", "status", "cancel"]},
                "content": {"type": "string", "description": "action=submit 时的独立方案全文"},
            },
        },
        _t_answer_first,
    ),
    Tool(
        "divergence_report",
        "分歧报告：检索某话题下的结论，找出**来自不同 agent 的相似配对**并排展示（人只判断分歧点，不判断整篇）。发现旧结论之间互相矛盾、或用户想知道「几个 AI 说的不一样」时用它。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "候选条数上限，默认 20"},
            },
            "required": ["query"],
        },
        _t_divergence_report,
    ),
    Tool(
        "list_staged",
        "查看待定区（隐身模式下被拦下、等待用户确认的内容）。",
        {"type": "object", "properties": {}},
        _t_list_staged,
    ),
    Tool(
        "promote",
        "把待定区里被用户选中的条目真正写入长期记忆（白名单式）。传 sids，如 p_001,p_002。",
        {"type": "object", "properties": {"sids": {"type": "string"}}, "required": ["sids"]},
        _t_promote,
    ),
    Tool(
        "discard",
        "丢弃待定区条目（用户明确说不要时用）。传 sids，如 p_001。丢弃不留痕。",
        {"type": "object", "properties": {"sids": {"type": "string"}}, "required": ["sids"]},
        _t_discard,
    ),
    Tool(
        "audit_log",
        "查看审计日志：谁在什么时候读了/写了/被拦下了哪些记忆。用户问「你记了什么」「删掉了吗」时用它给出真实流水，不要编造。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "session_id": {"type": "string", "description": "可选，指定会话"},
            },
        },
        _t_audit_log,
    ),
    Tool(
        "session_receipt",
        "会话回执：本次会话召回几次、写入几条、被拦下几次。隐身会话结束时用它给用户一句「本次未留下任何记忆」的可验证结论。",
        {"type": "object", "properties": {"session_id": {"type": "string"}}},
        _t_session_receipt,
    ),
    Tool(
        "last_progress",
        "上次进度：同一项目上一次会话结束时的沉淀（做到哪、写了什么、涉及什么话题）。接手别人的活、或用户说「接着上次的」时先看它，不要让对方复述前情。",
        {"type": "object", "properties": {}},
        _t_last_progress,
    ),
    Tool(
        "get_memory",
        "按 ID 获取记忆全文。",
        {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]},
        _t_get_memory,
    ),
    Tool(
        "delete_memory",
        "删除一条记忆（进回收站，可用 restore_memory 找回）。",
        {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]},
        _t_delete_memory,
    ),
    Tool(
        "restore_memory",
        "从回收站恢复一条记忆。",
        {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]},
        _t_restore_memory,
    ),
    Tool(
        "wipe_source",
        "抹除某个 agent 或来源的全部记忆（一次性清干净，进回收站可恢复）。agent/source 至少传一个。",
        {"type": "object", "properties": {"agent": {"type": "string"}, "source": {"type": "string"}}},
        _t_wipe_source,
    ),
    Tool(
        "memory_stats",
        "OwnMem 运行状态：记忆总数、容量、来源分布、数据目录。",
        {"type": "object", "properties": {}},
        _t_memory_stats,
    ),
]


def run_stdio() -> None:
    serve(name="ownmem", version=__version__, instructions=INSTRUCTIONS, tools=TOOLS)


def main() -> None:
    import sys

    if "--http" in sys.argv:
        # 曾经的 --http 实际启动的是 Web 审计台而非 MCP 端点，误导接入方，已移除
        sys.stderr.write("ownmem mcp: --http 已移除。stdio 是唯一支持的传输方式。\n")
        sys.exit(2)
    run_stdio()


if __name__ == "__main__":
    main()
