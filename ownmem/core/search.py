# -*- coding: utf-8 -*-
"""BM25 检索（移植自 MemLink 内核，零依赖纯本地）。

在 BM25 基础上叠加重要度 / 时近性 / 命中频率三重加权——
记忆不是搜索结果，是「该怎么对待这个人」的线索。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

from .text import tokenize

BM25_K1 = 1.5
BM25_B = 0.75

# 时点型记忆的识别：内容里写着具体日期 / 相对时间锚点的，

# 这类记忆天生带保质期（「明天问导员」「10-15 回执截止」），过期后还按
# 长期事实的重量参与竞争，就会变成冒充当前事实的噪音——这是注意力浪费
# 的主要来源之一。识别出来后用更陡的衰减曲线，让它自然退到后面，而不是删掉。
# 「明天」「10-15 截止」这类才是真的会过期；而「2026-09-18 明确定下的协作惯例」
# 「2026-09-30 实测的磁盘容量」里那个日期只是事实的来源时间，误判成时点型
# 会让长期事实被标注过期。所以分两级：相对时间词单算，裸日期必须搭配待办语义。
_RELATIVE_TIME_RE = re.compile(
    r"明天|后天|下周|本周|截止|deadline|倒计时|之前必须|还剩|到期",
    re.IGNORECASE,
)
_BARE_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/月]\d{1,2}[日号]"
)
# 待办语义：日期 + 这些词 = 这是一条有保质期的安排，不是沉淀下来的事实。
# 刻意不放「必须」——它大量出现在长期规范里（「大方向必须问他」「路径必须用
# 正斜杠」），一旦误判，稳定的协作规则就会被当成过期信息，比漏判更糟。
_TODOISH_RE = re.compile(
    r"截止|要交|交给|提交|提醒|记得|别忘|逾期|ddl|deadline",
    re.IGNORECASE,
)

# 事实型 vs 时点型的衰减周期（天）
FACTUAL_HALF_LIFE = 270.0
EPHEMERAL_HALF_LIFE = 45.0

# 衰减曲线还要按重要度分档（v2 §3.7）：低价值的流水账老得比规则快。
# 只动 low/normal 这两档——critical/always 本来就走 recency=1.0 的豁免分支，
# high 保持 1.0 倍基准。没有这个分档，一条 low 的过程流水会和一条 high 的
# 项目决策以同样速度老化，「账本不烂」就只做了半截。
IMPORTANCE_DECAY = {"high": 1.15, "normal": 1.0, "low": 0.6}

# 踩坑计数器（v2 §3.6）：同一个坑被再次踩中 → 权重上调。
# 幅度刻意温和（每次 +12%，最多算 6 次），因为「复发」证明的是它没被记住，
# 不是它有多重要；真正的重量仍由 importance 决定，这里只负责把它往前挪一格。
PITFALL_BOOST_PER_RECUR = 0.12
PITFALL_BOOST_CAP = 6
CROSS_AGENT_BONUS = 0.10   # 每多一个栽过的 agent，再加 10%

# 时点型记忆超过这个天数，渲染时标注「可能已过期」，提醒模型核实而非盲信
STALE_DAYS = 21.0

IMPORTANCE_WEIGHT = {
    # always = 常驻规则档，见下方 ALWAYS_PIN_SCORE：它不吃这个乘法加成，
    # 因为「× 权重」在 BM25 相似度为 0 时恒等于 0——那正是我们要修的失效点。
    "always": 1.8,
    "critical": 1.6,
    "high": 1.3,
    "normal": 1.0,
    "low": 0.7,
}

# 常驻记忆的稳定置顶分。
# 背景：rank() 原本是 `s = BM25(...)` 然后 `if s <= 0: continue`，
# 重要度只是乘在 s 上的系数。于是「电脑维护红线」这类记忆，在用户聊
# 「这个基金能买吗」时因为词面零重叠被直接过滤——标 critical 也救不回来，
# 因为 1.6 × 0 还是 0。这不是排序问题，是可见性问题。
# 解法：always 档不与相关内容争抢名次，它稳定占据最前排；用户确实提到它时，
# 仍按真实相似度在常驻区内部微调次序。
ALWAYS_PIN_SCORE = 1_000_000.0


def _doc_tokens(mem: dict) -> list[str]:
    tags = mem.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return tokenize(mem.get("content", "") + " " + " ".join(tags))


def prepare(items: list[dict]) -> tuple[int, dict[str, int], float]:
    """语料统计：N 文档数、df 词频、avgdl 平均文档长度。"""
    n = max(len(items), 1)
    df: dict[str, int] = {}
    total_len = 0
    for m in items:
        toks = _doc_tokens(m)
        total_len += max(len(toks), 1)
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    return n, df, max(total_len / n, 1.0)


def score(mem: dict, query_tokens: list[str], n: int, df: dict[str, int], avgdl: float) -> float:
    toks = _doc_tokens(mem)
    dl = max(len(toks), 1)
    tf: dict[str, int] = {}
    for t in toks:
        tf[t] = tf.get(t, 0) + 1
    s = 0.0
    for tok in query_tokens:
        d = df.get(tok, 0)
        if d > 0 and tok in tf:
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            t = tf[tok]
            s += idf * (t * (BM25_K1 + 1)) / (t + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
    return s


def has_time_anchor(content: str) -> bool:
    """内容是否带「会在将来某刻失效」的时间锚点。

    区分两件事：
    - 「10-15 回执截止，明天必须问导员」 → 时点型，过期就该退到后排
    - 「2026-09-18 明确定下的协作惯例」 → 长期事实，日期只是出处，不该被判过期

    后者如果误判，等于让稳定的协作规则随时间褪色，比前者的问题更严重。
    """
    if not content:
        return False
    if _RELATIVE_TIME_RE.search(content):
        return True
    return bool(_BARE_DATE_RE.search(content)) and bool(_TODOISH_RE.search(content))


def is_stale(mem: dict, now: datetime | None = None) -> bool:
    """时点型记忆是否已过保质期，供渲染层做「该核实、别盲信」的标注。

    刻意只标注、不删除，也不把它降权到看不见：带日期的项目流水过期后依然
    是历史事实，「忘了」该由人决定，不该由算法偷偷做。带日期的项目决策会被
    存下来，但模型读到时必须知道它可能是几周前的一张快照。
    """
    content = mem.get("content", "")
    if not has_time_anchor(content):
        return False
    if (mem.get("importance") or "") in ("always", "critical"):
        return False
    now = now or datetime.now().astimezone()
    try:
        created = datetime.fromisoformat(mem["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        return (now - created).total_seconds() / 86400 > STALE_DAYS
    except (KeyError, ValueError, TypeError):
        return False


def _recurred_agents(mem: dict) -> list[str]:
    raw = mem.get("recurred_agents")
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    try:
        val = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def pitfall_boost(mem: dict) -> float:
    """踩坑账的复发加成。非踩坑账恒为 1.0。

    「两家 AI 都栽过的坑」比「一家栽过的」更值得常驻——这正是多 agent
    交叉使用的场景里最该被放大的信号。
    """
    if (mem.get("kind") or "conclusion") != "pitfall":
        return 1.0
    n = min(max(int(mem.get("recurred") or 0), 0), PITFALL_BOOST_CAP)
    agents = {a.strip().lower() for a in _recurred_agents(mem) if a.strip()}
    return 1.0 + PITFALL_BOOST_PER_RECUR * n + CROSS_AGENT_BONUS * max(0, len(agents) - 1)


def rank(
    items: list[dict],
    query_tokens: list[str],
    limit: int = 8,
    *,
    source: str = "",
    agent: str = "",
    project: str = "",
    min_score: float = 0.0,
    include_superseded: bool = False,
) -> list[tuple[dict, float]]:
    """BM25 × 重要度 × 时近性 × 命中频率，返回 (记忆, 总分) 降序。

    include_superseded：默认 False = 已被取代的旧结论不再参与竞争。
    推翻一条结论之后，旧结论若继续被注入，等于让系统同时持两个互相打架的
    版本，模型只能二选一——比不记得更糟。要考古时显式打开。
    """
    if not items or not query_tokens:
        return []
    n, df, avgdl = prepare(items)
    now = datetime.now().astimezone()
    scored: list[tuple[dict, float]] = []
    for m in items:
        if source and m.get("source", "") != source:
            continue
        if agent and m.get("agent", "") != agent:
            continue
        if not include_superseded and (m.get("superseded_by") or ""):
            continue
        # 项目隔离：project 为空 = 全局记忆（红线、身份事实），任何项目都看得见；
        # 非空的只在归属项目里出现，别的事一条都串不过来。
        if project and (m.get("project") or "") not in (project, ""):
            continue
        s = score(m, query_tokens, n, df, avgdl)
        level = m.get("importance", "normal")
        is_always = level == "always"
        # 常驻规则豁免「零相似度即淘汰」：用户没提到相关词的时候，
        # 恰恰是最需要它被看见的时候。其余档位维持原行为，一行没变。
        if s <= 0 and not is_always:
            continue
        importance = IMPORTANCE_WEIGHT.get(level, 1.0)
        age_days = 0.0
        try:
            created = datetime.fromisoformat(m["created_at"])
            # 时间戳有可能不带时区（例如直接 SQL 写入用的 datetime('now','localtime')，
            # 或旧版本/第三方通道写入的数据）。一旦出现这种值，naive 与 aware 相减
            # 会抛 TypeError 并让**整次召回**崩掉——一条脏数据不该拖垮全部记忆。
            # 因此缺时区时按本机时区补齐，而不是让它炸出去。
            if created.tzinfo is None:
                created = created.replace(tzinfo=now.tzinfo)
            age_days = max(0.0, (now - created).total_seconds() / 86400)
        except (KeyError, ValueError, TypeError):
            pass
        # 身份、生日、红线这类东西不该随时间褪色——九个月后统一压到 0.4 倍，
        # 等于让最该记住的先被忘掉。
        if level in ("critical", "always"):
            recency = 1.0
        else:
            # 时点型记忆（内容带「明天」「10/15 截止」这类锚点）用更陡的曲线：
            # 它天生有保质期，过期后按长期事实的重量参与竞争，就会冒充当前事实。
            half = (
                EPHEMERAL_HALF_LIFE
                if has_time_anchor(m.get("content", ""))
                else FACTUAL_HALF_LIFE
            )
            recency = max(0.35, 1.0 - age_days / (half * IMPORTANCE_DECAY.get(level, 1.0)))
        # 命中数是个正反馈环：被召回 → 分更高 → 更容易被召回，久了会让几条
        # 老记忆长期霸榜、新记忆挤不进来。所以这个加成只在最近 90 天内有效，
        # 且幅度减半——老记忆该靠内容取胜，不该靠历史累计。
        popularity = 1 + (min(int(m.get("hits", 0) or 0), 10) * 0.01 if age_days < 90 else 0)
        if is_always:
            # 常驻区稳定置顶；用户真的提到它时，真实相似度决定它在这批里的先后
            total = ALWAYS_PIN_SCORE + max(s, 0.0)
        else:
            total = s * importance * recency * popularity * pitfall_boost(m)
        if total >= min_score:
            scored.append((m, total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(1, min(limit, 30))]
