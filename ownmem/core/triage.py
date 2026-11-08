# -*- coding: utf-8 -*-
"""写入侧质量闸门：决定一条内容该「直接沉淀 / 进待定区 / 直接丢弃」。

存在的理由
-----------
召回侧的注意力是定额的（条数上限 + 字符预算 + 常驻层占位）。写入侧如果不设
门槛，任何一次「顺手记一下」都会变成一道永久性的注意力税——今天看着无害，
半年后几十条低信息密度的条目挤在一起，真正重要的东西就被稀释掉了。

所以这里做的是**入口配额**，不是事后清理：
  - 明显不是事实的（寒暄、提问、中间过程）→ 直接丢弃，连待定区都不占
  - 不确定值不值得留的 → 进待定区，等人挑；不挑就自动过期
  - 明确的规则 / 结论 / 教训 → 直接沉淀

判断依据是**文本自身的形态**，不调模型、不联网、纯本地。
"""

from __future__ import annotations

import re

from .text import sig_tokens

# 三种裁决
ACCEPT = "accept"   # 值得长期记住
STAGE = "stage"     # 存疑，进待定区等人确认
REJECT = "reject"   # 不是事实，丢弃

MIN_LEN = 10

# 纯寒暄 / 应答：这些东西每天出现几十次，一条都不该沉淀
_CHITCHAT_RE = re.compile(
    r"^(你好|您好|嗨|hi|hello|哈喽|谢谢|感谢|thanks|thx|好的|收到|明白|了解|ok|okay|"
    r"嗯+|哦+|啊+|行|可以|没问题|不用了|算了|辛苦了|早啊|早安|晚安|在吗|在的)\s*[!！。.~\s]*$",
    re.IGNORECASE,
)

# 结论性信号：出现这些词说明这条是「被确定下来的东西」，不是随口一提。
#
# 「不要」加了左向否定断言，因为「到底要不要做」这类疑问句里也含这两个字——
# 把它当成结论放行，等于把提问当事实沉淀，是这层最典型的误判。
_CONCLUSION_RE = re.compile(
    r"红线|禁止|(?<!要)不要|别再|不许|一律|必须|切记|规定了?|明确要求|已确认|决定了?|定下来|"
    r"结论|原因是|教训|坑|注意以后|默认|改为|不再|从此|每当|凡是|规则|口径|"
    r"因为所以|导致|所以以后|确认过|当面重申",
)

# 中间过程：正在做的事本身不是事实，做完才有沉淀价值
_PROCESS_RE = re.compile(
    r"^(现在|正在|刚|刚才|接下来|下一步|稍等|等一下|我先|我们再|让我|然后再)",
)

# 纯时点安排：有保质期，优先落待定区而不是长期记忆
_SCHEDULE_RE = re.compile(
    r"(明天|后天|大后天|下周|本周|这周|今晚|中午之前|\d+月\d+[日号])"
    r"(之前|前|必须|要|得|截止|到期|交|提交|问|做|给)?"
)

_QUESTION_END = ("?", "？", "吗", "呢", "么")


def triage(content: str) -> tuple[str, str]:
    """裁决一条内容该去哪。返回 (裁决, 理由)。

    顺序很重要：先剔除根本不是事实的，再识别明确值得留的，剩下的都存疑。
    """
    text = (content or "").strip()
    if not text:
        return REJECT, "空内容"
    if len(text) < MIN_LEN:
        # 过短不等于没价值（"周五交报告" 只有 5 个字但确实要做），
        # 所以这里不急着丢弃，交给下面的形态判断；真没内容的会被实体词检查挡掉。
        pass

    low = text.lower()
    if _CHITCHAT_RE.match(low):
        return REJECT, "纯寒暄/应答，没有可沉淀的信息"

    sig = sig_tokens(text)
    if not sig:
        return REJECT, "去除停用词后没有可检索的实体词"

    # 纯提问：问题是待解决的事，不是已确立的快照。
    # 例外是「问句形式的规则」（"这种情况下该先问他吗？——该"），靠结论词放行。
    if low.endswith(_QUESTION_END) and not _CONCLUSION_RE.search(text):
        return REJECT, "提问不是事实，答完了才有沉淀价值"

    if _CONCLUSION_RE.search(text):
        return ACCEPT, "含结论/规则信号，值得长期记住"

    if len(text) < MIN_LEN:
        return STAGE, f"过短（{len(text)} 字），存疑，等人确认"

    if _PROCESS_RE.match(text) or len(sig) <= 2:
        return STAGE, "疑似中间过程或信息密度过低，等人确认"

    if _SCHEDULE_RE.search(text):
        return STAGE, "时点型安排有保质期，优先落待定区而不是长期记忆"

    return STAGE, "无明显结论信号，默认存疑，等人确认"


def should_bypass(content: str, importance: str) -> bool:
    """重要度已经是人判断过的：always / critical 不进闸门。

    一条被人类显式标成「红线」的内容不该被文本形态筛掉——
    筛掉它的代价，比放过一百条寒暄大得多。
    """
    return importance in ("always", "critical")
