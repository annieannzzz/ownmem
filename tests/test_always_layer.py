# -*- coding: utf-8 -*-
"""常驻层（importance='always'）端到端测试。

起因是一次真实事故：重要度在 rank() 里被当作 BM25 分数的**乘数**
（`total = s * importance * ...`），而在此之前有 `if s <= 0: continue`。
结果是——用户聊「这个基金能买吗」时，「电脑维护红线」因为词面零重叠被直接
过滤掉，标 critical 也救不回来，因为 1.6 × 0 依然是 0。

常驻层要解决的是「可见性」，不是「排名」：
    always 档 = 每次会话都必须在场的规则（红线 / 口径 / 身份）
    其余档位 = 与当前话题相关的线索，仍按 BM25 竞争

本文件锁住这个语义不被后续改动悄悄破坏。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-always-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import db, sessions  # noqa: E402
from ownmem.core.search import ALWAYS_PIN_SCORE  # noqa: E402
from ownmem.core.store import PREVIEW_LEN, MemoryStore  # noqa: E402
from ownmem.gateway import recall  # noqa: E402
from ownmem.gateway.recall import ALWAYS_HEADER, RELATED_HEADER  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  → {extra}" if extra and not cond else ""))


# 五句真实口吻的提问，全部**不出现**红线里的任何关键词（这是测试的关键）
QUERIES = [
    "帮我看看这段代码为什么报错",
    "给我推荐个周末能去的地方",
    "我导让我下周交个东西，怎么安排",
    "这个基金现在能买吗",
    "帮我写个小红书标题",
]

PINNED = [
    # 刻意保持「超过 PREVIEW_LEN(200)」的长度：真实红线就是这样，
    # 且恰恰是最容易被截掉的尾巴上挂着最该执行的那半句。
    ("电脑维护红线：他就这一台电脑，系统相关一律不碰——pagefile.sys / hiberfil.sys / "
     "System32 / 注册表 / 驱动 / 启动项，不删不改不移。原话「你可别给我把系统整坏了」。"
     "清理只允许动他自己产生的文件和可自动重建的缓存/日志，优先搬到 D 盘而非删除；"
     "确实要删的先列路径给他看并留可逆手段。平台机制坑：所谓「删除」实际是移到回收站，"
     "删大件后磁盘不立刻变小，必须配套清空回收站才真正释放。"
     "⚠️ 最后这条最容易被截掉：单回合累计删超 50 个文件会触发弹窗确认，无法靠分批绕过。"),
    "隐私红线：不得使用他的小红书主号或任何第三方账号凭据去抓取数据；抓取一律用小号，"
    "且跑完立刻删除浏览器登录态 profile。他只有这一个主号、没有小号，绝不冒封号风险。",
    "医用高等数学在线作业判分口径：凡趋于正负无穷一律判「不存在」，不认 ∞ 记号——"
    "严格按课本「极限=趋近常数A」的定义作答。",
    "协作方式：大方向必须问他（方向选择、路线取舍、要不要花钱）；具体执行少问多干。",
]


def main() -> int:
    conn = db.connect()
    sid = sessions.new_session_id()
    sessions.set_mode(conn, sid, "normal")
    store = MemoryStore(conn)

    print(f"\n=== OwnMem 常驻层测试（数据目录 {TMP}）===\n")

    # 1. always 档可写入
    for content in PINNED:
        mem, msg = store.add(content, importance="always", agent="test", session_id=sid)
        check("常驻记忆写入成功", mem is not None, msg)

    # 2. 话题记忆：用来验证两段之间没有互相挤占
    topic_mem, _ = store.add(
        "安宇在推进校园推文制作，方向是国家奖学金年度盘点",
        importance="normal", agent="test", session_id=sid,
    )
    check("话题记忆写入成功", topic_mem is not None)

    # 一条与第 4 节测试 query 真正相关的话题记忆（否则相关段本就该为空，
    # 那验证的就不是「两段共存」而是「相关段缺席」了）
    xhs_mem, _ = store.add(
        "小红书标题偏好「处境—反转」结构，王阳明/认知觉醒方向曾跑到 400 浏览 30 赞",
        importance="high", agent="test", session_id=sid,
    )
    check("小红书话题记忆写入成功", xhs_mem is not None)

    # 再塞一条会触发付费/预算话题的记忆，覆盖另一种常驻红线场景
    store.add(
        "他对预算敏感，付费服务须先提醒成本；优先免费或本地方案",
        importance="normal", agent="test", session_id=sid,
    )

    check("pinned() 能取到全部常驻记忆", len(store.pinned()) == len(PINNED),
          f"pinned={len(store.pinned())}")

    # 3. 核心命题：任何话题下，常驻规则都必须在场
    print("\n--- 逐 query 验证常驻规则在场率 ---")
    all_in = True
    for q in QUERIES:
        res = recall.recall_context(conn, q, session_id=sid, agent="test")
        ctx = res["context"]
        missing = [p[:12] for p in PINNED if p[:12] not in ctx]
        if missing:
            all_in = False
        check(f"常驻规则全在场：{q}", not missing, f"缺 {missing}")
    check("任意话题下常驻规则 100% 在场", all_in)

    # 4. 两个区段必须分开标注，否则模型会一律当作背景资料
    res = recall.recall_context(conn, "帮我写个小红书标题", session_id=sid, agent="test")
    check("上下文含常驻段标题", ALWAYS_HEADER in res["context"])
    check("上下文含相关段标题", RELATED_HEADER in res["context"])
    check("常驻段排在相关段之前",
          ALWAYS_HEADER in res["context"] and RELATED_HEADER in res["context"]
          and res["context"].index(ALWAYS_HEADER) < res["context"].index(RELATED_HEADER))

    parts_by_header = res["context"].split(RELATED_HEADER)
    always_part = parts_by_header[0]
    related_part = parts_by_header[1] if len(parts_by_header) > 1 else ""
    check("常驻记忆未污染相关段",
          all(p[:12] not in related_part for p in PINNED if p[:12] in always_part))

    # 6. 话题记忆不会因为常驻层存在而消失
    check("相关记忆仍能进入相关段", "小红书标题偏好" in related_part, related_part[:160])

    # 7. 预算仍受控，不会把上下文撑爆
    check("总注入不超预算上限",
          len(res["context"]) <= recall.DEFAULT_ALWAYS_BUDGET + recall.DEFAULT_CHAR_BUDGET + 400,
          f"ctx={len(res['context'])}")

    # 8. 隐身/白纸模式优先于常驻层——用户的隐私开关必须压过一切
    sessions.set_mode(conn, sid, "blank")
    res_blank = recall.recall_context(conn, PINNED[0], session_id=sid, agent="test")
    check("白纸模式下常驻层同样被拦截", res_blank["blocked"] and res_blank["context"] == "",
          json.dumps(res_blank, ensure_ascii=False)[:160])
    sessions.set_mode(conn, sid, "normal")

    # 9. 容量淘汰不能动常驻记忆
    always_ids = {m["id"] for m in store.pinned()}
    from ownmem.core.store import MAX_MEMORIES
    filler = 0
    while filler <= MAX_MEMORIES + 20:
        active = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
        if active > MAX_MEMORIES:
            break
        store.add(f"填充记忆第 {filler} 号，用于把容量顶到上限触发淘汰路径",
                  importance="low", agent="test", session_id=sid, skip_dedup=True)
        filler += 1
    check("确实触发了容量淘汰路径",
          conn.execute("SELECT COUNT(*) c FROM memories WHERE status='trashed'").fetchone()["c"] > 0)
    still_pin = {m["id"] for m in store.pinned()}
    check("容量淘汰后常驻记忆一条未丢", always_ids <= still_pin,
          f"丢 {always_ids - still_pin}")

    # 10. always 档在 rank() 里确实拿到置顶分
    hits = store.search("完全不相干的查询词阿克琉斯之踵", limit=20, count_hits=False)
    pinned_scores = [s for m, s in hits if m["importance"] == "always"]
    check("常驻记忆检索即置顶", bool(pinned_scores) and min(pinned_scores) >= ALWAYS_PIN_SCORE,
          f"scores={pinned_scores[:3]}")

    # 11. 常驻规则必须**完整**渲染，不接受 PREVIEW_LEN 截断
    #     截断会把红线最要命的后半句切掉（例如「他只有这一个主号、任何需要登录他账号的
    #     方案一律否决」），那等于没注入。
    long_pins = [p for p in PINNED if len(p) > PREVIEW_LEN]
    check("存在长于 PREVIEW_LEN 的常驻记忆用于校验截断", bool(long_pins))
    res_full = recall.recall_context(conn, "随便聊点别的", session_id=sid, agent="test")
    always_seg = res_full["context"].split(RELATED_HEADER)[0]
    cut = [p for p in long_pins if p not in always_seg]
    check("常驻规则完整渲染未被截断", not cut, f"被截断 {len(cut)} 条")
    check("常驻段不含预览省略号", "…（共" not in always_seg, always_seg[-120:])

    # 12. 脏时间戳不得让整次召回崩溃
    #     直接 SQL 写入不带时区的时间（第三方通道/早期数据就是这样），
    #     naive 与 aware 相减曾抛 TypeError，导致**所有记忆都注入不了**。
    conn.execute(
        "INSERT INTO memories(id,content,importance,tags,source,agent,session_id,"
        "created_at,updated_at,hits,status) VALUES(?,?,'high','[]','probe','probe','',"
        "'2026-10-30 12:00:00','',0,'active')",
        ("naive_ts_test", "这条记忆的时间戳故意不带时区，用来验证召回不会整体崩溃"),
    )
    conn.commit()
    ok = True
    err = ""
    try:
        r2 = recall.recall_context(conn, "帮我查一下上次那条不带时区的测试记忆",
                                   session_id=sid, agent="test")
        ok = r2["hits"] >= 1
    except Exception as exc:  # noqa: BLE001
        ok, err = False, f"{type(exc).__name__}: {exc}"
    check("naive 时间戳不拖垮召回", ok, err)

    print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
    if FAIL:
        for f in FAIL:
            print("  失败项：" + f)
    print(f"（测试数据目录 {TMP}，可安全删除）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
