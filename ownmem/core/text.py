# -*- coding: utf-8 -*-
"""文本处理与相似度判重（移植自 MemLink 内核）。

设计原则：零依赖、纯本地、中英混排可用。
"""

from __future__ import annotations

import re

# 常见虚词/停用字：显著性判重时过滤，避免"然后/主要"这类连接词拉高相似度
_STOP_CHARS = frozenset(
    "的了是在也和就都把吧吗呢啊个中上下于与及或其等或再您你他她它我们你们他们"
    "这那有没不很更越最要会能可以应该自己用户用来做制作通知得到作为对于关于从到向对加拉把它被让仍还却才只还有已经正在左右前后两侧十分非常之一二三四五"
    "开始结束全部部分主要首先然后最后例如比如其中此外同时之后之前这里那里一个一种一些一天一次一时间一眼一遍"
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def tokenize(text: str) -> list[str]:
    """英文按单词、中文按相邻双字切分，兼顾两种语言。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9_]{2,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk) >= 2:
        return words + [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return words + cjk


def bigrams(text: str) -> set[str]:
    """字符双字 gram 集合（英文单词整体作为一个 gram）。"""
    t = normalize(text)
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    words = re.findall(r"[a-z0-9_]{2,}", t)
    grams = set(words)
    if len(cjk) >= 2:
        grams.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return grams


def sig_tokens(text: str) -> set[str]:
    """显著性 token：英文/数字词原样保留，中文双字去掉含停用字的。

    对"换措辞的同义重述"更鲁棒——连接词被滤掉后剩下的实体词更稳定。
    """
    t = normalize(text)
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    words = re.findall(r"[a-z0-9_]{2,}", t)
    out = set(words)
    for i in range(len(cjk) - 1):
        tok = cjk[i] + cjk[i + 1]
        if not any(ch in _STOP_CHARS for ch in tok):
            out.add(tok)
    return out


def overlap_ratio(a: set[str], b: set[str]) -> float:
    """重叠率 = |A∩B| / min(|A|,|B|)。短文本共享部分语义时也能检出。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def dice_coef(a: set[str], b: set[str]) -> float:
    """Dice 系数 = 2|A∩B| / (|A|+|B|)。对长文本换措辞的重述更不敏感。"""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def similarity(content_a: str, content_b: str) -> float:
    """两条记忆的相似度：bigram 重叠率与显著性 Dice 取最大值。

    - bigram 重叠率：捕捉措辞几乎一样的情况
    - 显著性 Dice：捕捉换措辞但实体一致的重述
    """
    ratio = overlap_ratio(bigrams(content_a), bigrams(content_b))
    dice = dice_coef(sig_tokens(content_a), sig_tokens(content_b))
    return max(ratio, dice)
