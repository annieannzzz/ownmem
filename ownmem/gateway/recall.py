# -*- coding: utf-8 -*-
"""自动召回：记忆主动进场，不靠 agent 自觉。

这是 OwnMem 与「又一个记忆库」的分界线。MCP 工具要靠模型自己决定调用，
而这里是把召回挂在 hook 上——用户每发一条消息，记忆先被检索一遍、
按预算渲染好、直接塞进这次对话的上下文。模型想不看都难。
"""

from __future__ import annotations

import os
import sqlite3

from ..core import audit, db, digest as digest_mod, sessions
from ..core.store import MemoryStore, format_hits
from ..core.text import _STOP_CHARS, tokenize
from ..core.search import ALWAYS_PIN_SCORE, is_stale

# 默认注入预算：条数上限 + 字符上限（防止上下文被记忆挤爆）
DEFAULT_LIMIT = 6
DEFAULT_CHAR_BUDGET = int(os.environ.get("OWNMEM_RECALL_BUDGET", "2400"))

# 常驻层预算：always 档走独立额度，不与检索竞争。
# 否则「常驻规则越多 → 检索越饿」会形成新的失效：红线保住了，话题记忆全没了。
DEFAULT_ALWAYS_BUDGET = int(os.environ.get("OWNMEM_ALWAYS_BUDGET", "2800"))

# 查询词超过这个数时做一次去虚词聚焦——整句塞进 BM25 会被连接词稀释
MAX_QUERY_TOKENS = 30

HEADER = "【OwnMem 自动召回】以下是与此轮对话相关的历史记忆（按相关度排序，供参考，不必逐条回应）："

# 常驻段的措辞是刻意的：必须让模型看懂这两类东西的约束力不同。
# 「常驻规则」= 不管聊什么都生效；「相关记忆」= 仅供参考。混在一起写，
# 模型会一律当成背景资料，红线就形同虚设。
ALWAYS_HEADER = (
    "【OwnMem · 常驻规则】以下为**每次会话都必须遵守**的记忆"
    "（不由本轮话题决定是否出现，必须逐条执行）："
)
RELATED_HEADER = (
    "【OwnMem · 相关记忆】以下是与本轮对话相关的历史记忆"
    "（按相关度排序，供参考，不必逐条回应）："
)

# 上次进度：只在一次会话的**第一轮**注入。每轮都塞，等于让人每次都先复述前情，
# 注意力被历史占满，反而做不动当前的事。
DIGEST_HEADER = (
    "【OwnMem · 上次进度】同一项目上一次会话结束时的沉淀"
    "（供接手参考，说明那时做到哪、留了什么，不必逐条回应）："
)

# ---------------------------------------------------------------------------
# 视角提示词（v2 3.9）。设计约束：对抗提示是**行为指令**，不是角色扮演——
# 角色卡已被研究证伪（EMNLP 2024 / 沃顿 2025.12，见 v2-design.md §6.3），
# 而行为类指令（语气、格式、验证动作）是研究背书有效的部分。
# ---------------------------------------------------------------------------
BLIND_NOTICE = (
    "【OwnMem · 盲审模式】本会话处于盲审视角：此前其他 AI 的结论与思路已全部隐藏，"
    "你只能看到踩坑记录（坑是事实遗产，不属于「影响」）。"
    "请**只依据本轮任务独立作答**。注意：上一个 AI 可能在工作目录里留有文件，"
    "是否参考它们由你自行判断——读了就会带上它的思路。"
)
CROSS_CHECK_NOTICE = (
    "【OwnMem · 交叉验证】以下记忆来自其他 AI，**其中的结论可能是错的**"
    "（幻觉、过时、或当时信息不全）。逐条验证后再采信："
    "与你的判断冲突时明确指出分歧点，不要默认旧记忆正确。"
)
AF_INDEPENDENT_NOTICE = (
    "【OwnMem · 先答后对 · 第 1 阶段（独立作答）】本会话开启了「先答后对」："
    "旧记忆与历史进度已全部屏蔽（常驻规则除外——那是用户的红线，不是别人的答案）。"
    "请**仅凭本轮任务独立给出方案**。作答完成后用户会执行 reveal 进入对照阶段。"
)
AF_COMPARE_NOTICE = (
    "【OwnMem · 先答后对 · 第 2 阶段（对照）】下面先给出你此前**在没看过任何旧记忆"
    "的情况下独立给出的方案**，然后是与本轮任务相关的历史记忆。"
    "请逐点对照：你的方案与旧结论在哪里一致、在哪里分歧、旧结论是否犯了"
    "「踩坑账」里记过的错。分歧点单独列出，供用户裁决。"
)


def _budget(conn: sqlite3.Connection, key: str, default: int) -> int:
    """预算值：settings（用户显式设的）> 环境变量 > 代码默认。

    做成可调是为了「同一个人的不同阶段」：刚上手时需要多注入才信它有用，
    用熟了反而嫌挤——这个数该由用户改，不该由我们拍死。
    """
    raw = db.get_setting(conn, key, "")
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _query_tokens(prompt: str) -> list[str]:
    """查询词：分词为空就跳过召回（中文两三个字的有效词也能查）；
    超长输入先去掉纯停用字 token，避免稀释相关度。"""
    tokens = tokenize(prompt)
    if len(tokens) <= MAX_QUERY_TOKENS:
        return tokens
    focused = [
        t for t in tokens
        if t.isascii() or any(ch not in _STOP_CHARS for ch in t)
    ]
    if len(focused) < 8:      # 聚焦后太少说明全是虚词，退回原样
        focused = tokens
    return focused[:MAX_QUERY_TOKENS]


def _when_keywords(mem: dict) -> list[str]:
    """取出常驻记忆的场景触发条件。

    约定：tag 形如 ``when:高数|作业|求导`` 表示「只有本轮话题命中其中任一词
    时才注入」。这是纯 tags 约定，不动 schema，也不影响 CLI/MCP 的既有行为。

    没有 when tag 的常驻记忆 = 无条件常驻（真红线，必须每轮都在）。
    """
    tags = mem.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.strip("[]").split(",") if t.strip()]
    for t in tags:
        t = str(t).strip()
        if t.startswith("when:"):
            kws = [k.strip() for k in t[5:].split("|") if k.strip()]
            if kws:
                return kws
    return []


def _matches_when(mem: dict, prompt: str, tokens: list[str]) -> bool:
    """常驻记忆本轮是否该出场。没有 when 条件的恒 True。"""
    kws = _when_keywords(mem)
    if not kws:
        return True
    hay = prompt.lower()
    for k in kws:
        if k.lower() in hay:
            return True
    # 兜底：分词可能把长 keyword 切成碎片（中英夹杂、大小写差异等），
    # 这时 token 会落在 keyword 内部。只认这一个方向——反方向（keyword 是 token
    # 的前缀）会把「怎么做」判为被「怎么」命中，语义完全不同，必须排除。
    return any(
        len(t) >= 2 and not k.lower().startswith(t.lower()) and t.lower() in k.lower()
        for k in kws for t in tokens
    )


def _stale_note(mem: dict) -> str:
    """过期时点型记忆的渲染前缀。

    「这条写于 21 天前」比单纯不显示更有价值：模型既拿到了历史决策，
    又知道不能当现状引用。删掉等于让人凭空失忆，静默显示等于纵容过期信息
    冒充当前事实——两头都不能要。
    """
    raw = mem.get("created_at") or ""
    age = ""
    try:
        from datetime import datetime
        created = datetime.fromisoformat(raw)
        now = datetime.now().astimezone()
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        age = f"写于 {int((now - created).total_seconds() // 86400)} 天前"
    except (ValueError, TypeError):
        age = "写入时间不明"
    return f"⚠️ 可能是过期快照（{age}、含时间锚点），引用前先向用户核实：\n"


def _is_first_turn(conn: sqlite3.Connection, session_id: str) -> bool:
    """本会话是否还没注入过任何东西（= 这是第一轮）。

    判据取审计流水而不是内存状态：hook 每轮都是独立进程，进程内标记活不过一轮。
    必须把 digest_inject 也算作「已经开过场」——只注入进度、没命中任何记忆的那轮
    不会产生 recall 事件，只看 recall 会让「上次进度」在后续每轮重复出现。
    """
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE session_id=? AND event IN ('recall','digest_inject') LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is None


def recall_context(
    conn: sqlite3.Connection,
    prompt: str,
    *,
    session_id: str,
    agent: str = "unknown",
    limit: int = DEFAULT_LIMIT,
    char_budget: int | None = None,
    always_budget: int | None = None,
    project: str = "",
    log: bool = True,
) -> dict:
    """检索并渲染可注入的记忆上下文。

    三段式：常驻规则（always，无条件）+ 上次进度（仅第一轮）+ 相关记忆（BM25）。
    返回 {context, hits, blocked, mode, reason}
    - 白纸/双断模式下直接返回空 context，并记一条 recall_blocked 审计
    - 命中不足或超预算时自动截断，永远不会把上下文撑爆
    """
    mode = sessions.get_mode(conn, session_id)
    # 走到这里说明这个会话真的在活动（哪怕随后被模式拦下），刷新它的活动时间
    sessions.mark_active(conn, session_id)

    if not sessions.can_read(conn, session_id):
        if log:
            audit.log(
                conn, "recall_blocked", agent=agent, session_id=session_id,
                detail=f"模式 {mode} 不注入记忆", mode=mode,
            )
        return {
            "context": "", "hits": 0, "blocked": True, "mode": mode,
            "reason": f"当前为 {mode} 模式，已跳过记忆注入",
        }

    store = MemoryStore(conn)
    project = sessions.resolve_project(conn, session_id, project)
    view = sessions.get_view(conn, session_id)
    af = sessions.af_phase(conn, session_id)
    if char_budget is None:
        char_budget = _budget(conn, "recall_budget", DEFAULT_CHAR_BUDGET)
    if always_budget is None:
        always_budget = _budget(conn, "always_budget", DEFAULT_ALWAYS_BUDGET)

    # ---------- 上次进度（仅本会话第一轮） ----------
    # 盲审与独立作答期不注入进度：沉淀里全是上一个 AI 的结论与思路，
    # 正是这两种视角要屏蔽的「影响」——漏了这里，盲审就从门缝漏光了。
    digest_view = ""
    first_turn = _is_first_turn(conn, session_id)
    if first_turn and view != "blind-review" and af != "independent":
        d = digest_mod.latest(conn, project=project, exclude_session=session_id)
        if d:
            digest_view = digest_mod.render(d)
            # 每轮注入都有账：上一场会话的沉淀进了这一场，用户有权知道
            if log:
                audit.log(
                    conn, "digest_inject", agent=agent, session_id=session_id,
                    memory_id=d["id"],
                    detail=f"注入 {d['session_id']} 的沉淀（项目={project or '全局'}）："
                           f"{d['text'][:60]}",
                    mode=mode,
                )
            # 沉淀从「相关记忆」的预算里扣，不挤占常驻规则——红线永远优先
            char_budget = max(800, char_budget - len(digest_view))

    # ---------- 第一段：常驻规则 ----------
    # 「常驻」不等于「每轮都要出现」。真正的红线（隐私/电脑/协作）确实无条件，
    # 但像「高数判分口径」「物理实验七段式」这种场景型规则，在聊体测或基金时
    # 每轮都塞进来，只会稀释注意力——而注意力是这里最稀缺的资源。
    # 所以给常驻层加一道场景门：带 when: 条件的，命中才注入。
    q_tokens = _query_tokens(prompt)
    pinned_ids: set[str] = set()      # 本轮进了常驻段的
    gate_closed: set[str] = set()     # 场景门未命中的，本轮完全跳过
    always_kept: list[tuple[dict, float]] = []
    used_always = 0
    for mem in store.pinned():
        # 项目隔离同样管常驻层：别的项目的常驻规则不该在这里冒充红线
        if project and (mem.get("project") or "") not in (project, ""):
            continue
        if not _matches_when(mem, prompt, q_tokens):
            # 关键：拦下来就必须真拦住。若只从常驻段移除而仍让它在「相关记忆」
            # 里出现，那条记忆会被套上「仅供参考」的措辞——红线的约束力等于
            # 被渲染层偷偷降级了。没通过场景门 = 本轮不该存在。
            gate_closed.add(mem["id"])
            continue
        cost = len(mem.get("content", "")) + 60
        if used_always + cost > always_budget and always_kept:
            break
        always_kept.append((mem, ALWAYS_PIN_SCORE))
        pinned_ids.add(mem["id"])
        used_always += cost

    # ---------- 第二段：相关记忆 ----------
    if not q_tokens:
        top: list[tuple[dict, float]] = []
    else:
        # 命中计数只记真正注入的条目（预算裁剪后的），先不计
        top = store.search(
            prompt, limit=limit + len(pinned_ids), project=project, count_hits=False
        )

    # 常驻记忆在 rank() 里已被置顶，这里再排一次是为了彻底避免重复出现在两个区段
    candidates = [
        (m, s) for m, s in top
        if m["id"] not in pinned_ids and m["id"] not in gate_closed
    ]

    # ---------- 视角过滤（v2 3.9） ----------
    # 在预算裁剪之前过滤：盲审下一条被藏起来的结论不该消耗预算名额。
    # 只认 kind 单一判据——来源（哪个 agent 写的）不参与：同一个 agent 的
    # 旧结论同样会锚定自己，盲审一律藏。踩坑账（pitfall）任何视角都可见——
    # 坑是事实遗产不是观点影响，新 AI 不该把别人踩过的坑再踩一遍（已拍板）。
    if view == "result-only":
        candidates = [(m, s) for m, s in candidates if (m.get("kind") or "conclusion") != "process"]
    elif view == "blind-review":
        candidates = [
            (m, s) for m, s in candidates
            if (m.get("kind") or "conclusion") == "pitfall"
        ]

    kept: list[tuple[dict, float]] = []
    used = 0
    skipped_by_budget = 0
    now_view: dict[str, dict] = {}
    for mem, sc in candidates:
        # 过期标注只改渲染副本，数据库里的原文一个字不动
        if is_stale(mem):
            mem = dict(mem)
            mem["content"] = _stale_note(mem) + mem.get("content", "")
            now_view[mem["id"]] = mem
        cost = len(mem.get("content", "")) + 60
        if used + cost > char_budget:
            # 跳过而不是 break（v2 §3.8）：预算不该按「先到先得」分配。
            # break 会让一条 800 字的长记忆把后面三条 100 字的短结论全堵死，
            # 而后者加起来才占了前者一半的上下文——那是纯粹的浪费。
            if not kept and not always_kept and cost <= char_budget * 2:
                # 兜底：首条就超预算时仍然收下（原行为）。宁可超一点，
                # 也不能因为预算苛死而让本轮一条记忆都没有。
                kept.append((mem, sc))
                used += cost
            else:
                skipped_by_budget += 1
            continue
        kept.append((mem, sc))
        used += cost

    # ---------- 先答后对：独立作答期屏蔽全部相关记忆 ----------
    # notice 本身会出现在上下文里（见下方渲染），所以这里只管把相关段清空。
    # 常驻规则保留：红线是用户的规则，不是别人的答案，屏蔽它等于裸奔。
    if af == "independent":
        kept = []

    injected = always_kept + kept
    # 盲审/交叉验证即使零命中也要把提示词送出去（agent 得知道自己在什么视角下）；
    # result-only 没有提示词，零命中就按原样返回空。
    if not injected and not digest_view and af != "independent" and view in ("normal", "result-only"):
        return {
            "context": "", "hits": 0, "blocked": False, "mode": mode, "reason": "无相关记忆",
            "budget": {"char_budget": char_budget, "used": 0, "always_budget": always_budget,
                       "always_used": used_always, "skipped": 0, "limit": limit},
        }

    store.bump_hits([mem["id"] for mem, _ in injected])

    # ---------- 顶部提示词（视角 / 先答后对） ----------
    notices: list[str] = []
    if af == "independent":
        notices.append(AF_INDEPENDENT_NOTICE)
    elif af == "compare":
        notices.append(AF_COMPARE_NOTICE)
        # 独立作答全文必须原文注入：对照的价值就在「一字未改的你」vs「旧结论」，
        # 截断版会让人对着残缺稿找分歧，找出的是截断的分歧不是方案的分歧。
        ans_id = sessions.af_answer_id(conn, session_id)
        ans = store.get(ans_id) if ans_id else None
        if ans:
            notices.append(f"【你的独立方案（原样）】\n{ans['content']}")
        else:
            notices.append("（独立方案已不在库中，直接对照下方旧记忆即可）")
    if view == "blind-review":
        notices.append(BLIND_NOTICE)
    elif view == "cross-check":
        notices.append(CROSS_CHECK_NOTICE)

    parts = list(notices)
    if always_kept:
        # 常驻规则**不截断**：它是规则不是摘要，剪到 PREVIEW_LEN 会把最要命那半句
        # （例如「他只有这一个主号，任何需要登录他账号的方案一律否决」）切掉。
        # 预算已经统一在上方按成本裁剪过，这里必须完整渲染。
        parts.append(f"{ALWAYS_HEADER}\n{format_hits(always_kept, preview=False)}")
    if digest_view:
        # 放在常驻之后、相关之前：规则最大，历史进度次之，参考资料最后
        parts.append(f"{DIGEST_HEADER}\n{digest_view}")
    if kept:
        parts.append(f"{RELATED_HEADER}\n{format_hits(kept, preview=True)}")
    context = "\n".join(parts)

    if log:
        for mem, _ in injected:
            audit.log(
                conn, "recall", agent=agent, session_id=session_id,
                memory_id=mem["id"], detail=audit.detail(mem["content"], 80), mode=mode,
            )
    return {
        "context": context, "hits": len(injected), "blocked": False,
        "mode": mode, "reason": "", "memories": [m["id"] for m, _ in injected],
        "project": project, "digest": bool(digest_view),
        "view": view, "af_phase": af,
        "budget": {
            "char_budget": char_budget, "used": used,
            "always_budget": always_budget, "always_used": used_always,
            "skipped": skipped_by_budget, "limit": limit,
        },
    }
