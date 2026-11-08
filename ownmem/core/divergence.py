# -*- coding: utf-8 -*-
"""分歧报告（v2 3.11）：把不同 AI 对同一件事的不同说法摆到一张桌上。

原理：人只能判断分歧，判断不了趋同。各家 AI 给的都是磨好的终稿，
「哪篇读着顺」没有判断价值；「A 说 X、B 说 Y」才有。所以本模块
不生成任何新观点，只做三件事：找相关（BM25）、配对（相似度）、并排展示。

零依赖实现：BM25 收敛候选 → bigram+Dice 两两配对（复用 text.similarity）。

两层过滤，各管一件事：
  · **同话题**由 BM25 负责——能同时进候选，说明两条都在回应同一个查询；
  · **相似度窗口** [0.10, 0.90) 只做两件事：上限滤掉复述（>0.90 是判重通道
    的领地），下限滤掉仅靠查询词偶然沾边的弱重叠。

下限取 0.10 而不是更高，是实测出来的：中文措辞不同的两个结论
（「每周三更，周二周四周六发」vs「改成每周两更，质量优先」）bigram
相似度只有 0.1–0.2——**分歧越大、用词越不重合**。窗口定高了，漏掉的
恰恰是最值得上桌的那种分歧。代价是同话题不同侧面的条目也可能进窗
（比如「发布窗口」和「更新频率」），所以报告措辞是「疑似分歧」并排
展示原文，判断权留给用户——这正是本功能的设计立场。

已知局限（如实记录）：BM25 候选收敛意味着「措辞完全不同的两种说法」
可能只有一边进候选，漏配对。宁缺毋滥——漏报的分歧用户下次换个问法
还能查到，误报的噪音却会消耗「只判断分歧点」这件事本身的信任。
"""

from __future__ import annotations

import sqlite3

from .store import MemoryStore
from .text import similarity

# 相似度窗口：可用参数调，两端的理由写在模块 docstring
LOW_SIM = 0.10
HIGH_SIM = 0.90


def report(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    project: str = "",
    kind: str = "conclusion",
    low: float = LOW_SIM,
    high: float = HIGH_SIM,
) -> dict:
    """在候选结论里找「不同 agent 的相似配对」。

    只配不同 agent：同一家 AI 的前后矛盾通常会被取代链/判重处理，
    跨 agent 的分歧才是交叉使用的核心场景，也是报告要服务的人。
    已被取代的旧结论自动排除——它已经有裁决了，不该再上桌。
    """
    store = MemoryStore(conn)
    top = store.search(query, limit=limit, project=project, kind=kind, count_hits=False)
    items = [m for m, _ in top if not (m.get("superseded_by") or "")]
    pairs: list[dict] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if (a.get("agent") or "").lower() == (b.get("agent") or "").lower():
                continue
            s = similarity(a.get("content", ""), b.get("content", ""))
            if low <= s < high:
                pairs.append({"a": a, "b": b, "sim": round(s, 4)})
    pairs.sort(key=lambda p: -p["sim"])
    return {"query": query, "pairs": pairs, "scanned": len(items), "low": low, "high": high}


def render(rep: dict) -> str:
    low, high, scanned = rep["low"], rep["high"], rep["scanned"]
    if not rep["pairs"]:
        return (
            f"未发现分歧：{scanned} 条候选结论里没有来自不同 agent 的相似配对"
            f"（窗口 {low:.2f}–{high:.2f}）。"
        )
    lines = [
        f"发现 {len(rep['pairs'])} 组疑似分歧（候选 {scanned} 条结论，"
        f"相似度窗口 {low:.2f}–{high:.2f}）——只对分歧点做判断，不对整篇做判断："
    ]
    for idx, p in enumerate(rep["pairs"], 1):
        a, b = p["a"], p["b"]
        lines.append("")
        lines.append(f"{idx}. 相似度 {p['sim']:.0%} ｜ {a.get('agent')} vs {b.get('agent')}")
        lines.append(f"   [{a['id']}] ({a.get('created_at', '?')[:10]}) {a['content']}")
        lines.append(f"   [{b['id']}] ({b.get('created_at', '?')[:10]}) {b['content']}")
    lines.append("")
    lines.append("裁决方式：ownmem supersede <旧id> \"新结论\"（旧条作废挂链，不再注入）。")
    return "\n".join(lines)
