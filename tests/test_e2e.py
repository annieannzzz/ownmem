# -*- coding: utf-8 -*-
"""OwnMem 端到端测试：用临时数据目录跑完整链路，不污染真实记忆。

覆盖：
  1. 正常模式写入 → 召回注入
  2. 隐身模式写入 → 被闸门拦截 + 进入待定区 + 审计留痕
  3. 白纸模式 → 召回被拦截
  4. 挑选保存 → 待定区内容转正
  5. 会话回执 → 「未留下任何记忆」可验证
  6. 判重 → 换措辞的同义重述被拦截
  7. CLI hook 协议 → Claude Code UserPromptSubmit JSON 输出
  8. 默认模式 → 新会话继承（touch 不再硬编码 normal）
  9. 待定区编号不复用；promote 按 sid 移除，不误删同文条目
 10. 中文短查询可召回；容量淘汰留 evict 审计
 11. wipe --hard 物理删除 + 审计内容摘录清空 + purge 事件
 12. hook ↔ MCP 会话桥接；MCP session_mode 只能调紧不能放宽
 13. MCP stdio 在 GBK 管道（无 PYTHONUTF8）下不崩，输出恒为 ASCII
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="ownmem-test-")
os.environ["OWNMEM_HOME"] = TMP

from ownmem.core import audit, db, sessions  # noqa: E402
from ownmem.core.store import MemoryStore  # noqa: E402
from ownmem.gateway import policy, recall  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  → {extra}" if extra and not cond else ""))


def main() -> int:
    conn = db.connect()
    sid = sessions.new_session_id()

    print(f"\n=== OwnMem 端到端测试（数据目录 {TMP}）===\n")

    # 1. 正常模式写入 + 召回
    sessions.set_mode(conn, sid, "normal")
    store = MemoryStore(conn)
    mem, msg = store.add("安宇在做 OwnMem，一个 agent 记忆控制层项目", agent="claude-code", session_id=sid)
    check("正常模式写入成功", mem is not None, msg)

    res = recall.recall_context(conn, "OwnMem 是什么项目", session_id=sid, agent="claude-code")
    check("召回注入命中", res["hits"] >= 1 and "OwnMem" in res["context"], json.dumps(res, ensure_ascii=False)[:200])

    # 2. 隐身模式：写入被拦
    sessions.set_mode(conn, sid, "incognito")
    r = policy.guarded_write(conn, "这是一条不该被记住的敏感信息", session_id=sid, agent="claude-code")
    check("隐身模式拦截写入", r["blocked"] and not r["ok"], str(r))
    # 闸门内部自动转存待定区——不再指望每个调用方自觉去调 stage()
    staged_id = r.get("staged_id", "")
    check("被拦内容自动进入待定区（无需调用方手工 stage）",
          staged_id and len(policy.list_staged(conn, sid)) == 1, f"staged_id={staged_id}")

    row = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE content LIKE '%不该被记住%'"
    ).fetchone()["c"]
    check("敏感内容确实没进记忆库", row == 0, f"count={row}")

    # 3. 隐身读、白纸不读
    res2 = recall.recall_context(conn, "OwnMem 是什么", session_id=sid, agent="claude-code")
    check("隐身模式仍可召回（隐身≠失智）", res2["hits"] >= 1, str(res2))

    sessions.set_mode(conn, sid, "blank")
    res3 = recall.recall_context(conn, "OwnMem 是什么", session_id=sid, agent="claude-code")
    check("白纸模式阻断注入", res3["blocked"] and res3["context"] == "", str(res3))

    # 4. 挑选保存
    sessions.set_mode(conn, sid, "incognito")
    pr = policy.promote(conn, sid, sids=[staged_id], agent="claude-code")
    saved = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE content LIKE '%不该被记住%'"
    ).fetchone()["c"]
    check("挑选保存后进入记忆库", pr["saved"] == 1 and saved == 1, json.dumps(pr, ensure_ascii=False))
    check("待定区被清空", len(policy.list_staged(conn, sid)) == 0)

    # 5. 回执
    s2 = sessions.new_session_id()
    sessions.set_mode(conn, s2, "ghost")
    policy.guarded_write(conn, "幽灵会话里的一句话", session_id=s2, agent="opencode")
    receipt = audit.receipt(conn, s2)
    text = audit.receipt_text(receipt)
    check("ghost 会话回执显示 0 写入", receipt["writes"] == 0 and receipt["write_blocked"] == 1, text)
    check("回执含「未留下任何记忆」结论", "未向长期记忆写入任何内容" in text, text)

    # 6. 判重
    dup, sim = store.find_duplicate("安宇在做一个叫 OwnMem 的 agent 记忆控制层项目")
    check("换措辞的同义重述被判重", dup is not None and sim >= 0.6, f"sim={sim:.2f}")

    # 6b. 容量淘汰顺序（low 先走）
    conn.execute("UPDATE memories SET status='trashed' WHERE status='active'")
    store.add("低重要度临时信息 A", importance="low")
    store.add("这是一条关键红线约定，必须记住", importance="critical")
    import ownmem.core.store as storemod
    original = storemod.MAX_MEMORIES
    storemod.MAX_MEMORIES = 1
    store._enforce_capacity()
    storemod.MAX_MEMORIES = original
    left = store.list_active(limit=10)
    check("容量淘汰优先淘汰 low", len(left) == 1 and left[0]["importance"] == "critical",
          json.dumps([m["importance"] for m in left], ensure_ascii=False))

    # 7. CLI hook 协议（先补一条语料，容量测试把检索语料清空了）
    sessions.set_mode(conn, sid, "normal")
    store.add("安宇在开发 OwnMem，一个 agent 记忆控制层项目", importance="high",
              agent="claude-code", session_id=sid)
    hook_payload = json.dumps({"session_id": sid, "prompt": "OwnMem 这个项目怎么样了", "hook_event_name": "UserPromptSubmit"})
    proc = subprocess.run(
        [sys.executable, "-m", "ownmem.cli", "recall", "--hook", "claude"],
        input=hook_payload, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    ok = False
    detail = proc.stderr[-300:] if proc.returncode else ""
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            ok = (
                payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
                and "OwnMem" in payload["hookSpecificOutput"].get("additionalContext", "")
            )
        except (json.JSONDecodeError, KeyError) as exc:
            detail = f"{exc} | {proc.stdout[:200]}"
    check("Claude Code hook 协议输出正确 JSON", ok, detail)
    bridge_raw = db.get_setting(conn, "active_session:claude-code", "")
    check("hook 落下会话桥接设置（供 MCP 对齐）", sid in bridge_raw, bridge_raw[:120])

    # 8. 白纸模式下的 hook 输出（不应注入）
    subprocess.run(
        [sys.executable, "-m", "ownmem.cli", "mode", "blank", "--session", sid, "--agent", "cli"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    proc2 = subprocess.run(
        [sys.executable, "-m", "ownmem.cli", "recall", "--hook", "claude"],
        input=hook_payload, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    ok2 = False
    if proc2.returncode == 0:
        payload2 = json.loads(proc2.stdout)
        ok2 = "additionalContext" not in payload2.get("hookSpecificOutput", {})
    check("白纸模式下 hook 不注入任何内容", ok2, proc2.stdout[:200])

    # ------------------------------------------------------------------
    # 以下为 0.2.0 修复项的回归测试
    # ------------------------------------------------------------------
    # 8. 默认模式：新会话必须继承，touch_session 不得硬编码 normal
    db.set_setting(conn, "default_mode", "ghost")
    s_def = sessions.new_session_id()
    sessions.touch_session(conn, s_def, agent="probe")
    check("新会话继承默认模式（ghost）", sessions.get_mode(conn, s_def) == "ghost",
          sessions.get_mode(conn, s_def))
    db.set_setting(conn, "default_mode", "normal")

    # 9. 待定区：编号不复用；promote 按 sid 移除
    s_st = sessions.new_session_id()
    pa = policy.stage(conn, s_st, "待定甲")
    pb = policy.stage(conn, s_st, "待定乙")
    pc = policy.stage(conn, s_st, "待定丙")
    policy.discard(conn, s_st, sids=[pb])
    pd = policy.stage(conn, s_st, "待定丁")
    st_sids = [it["sid"] for it in policy.list_staged(conn, s_st)]
    check("丢弃后新待定条目不复用旧编号", len(st_sids) == len(set(st_sids)) and pd not in (pa, pc),
          str(st_sids))
    pr1 = policy.promote(conn, s_st, sids=[pc])
    left1 = policy.list_staged(conn, s_st)
    check("promote 只移除被点名的 sid", pr1["saved"] == 1 and len(left1) == 2,
          json.dumps(pr1, ensure_ascii=False))
    px = policy.stage(conn, s_st, "同文待定")
    py_ = policy.stage(conn, s_st, "同文待定")
    pr2 = policy.promote(conn, s_st, sids=[px])
    left2 = policy.list_staged(conn, s_st)
    check("同文待定条目只删被选中的", pr2["saved"] == 1 and len(left2) == 3,
          json.dumps({"saved": pr2["saved"], "left": len(left2)}))
    st_ev = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='stage'").fetchone()["c"]
    check("待定区落盘留 stage 审计", st_ev >= 5, f"stage events={st_ev}")

    # 10. 中文两字查询可召回；容量淘汰留 evict 审计
    sessions.set_mode(conn, s_def, "normal")
    store.add("用户住在东营区西城", agent="probe", session_id=s_def)
    res_cn = recall.recall_context(conn, "住在哪个区", session_id=s_def, agent="probe")
    check("中文短查询可召回", res_cn["hits"] >= 1, json.dumps(res_cn, ensure_ascii=False)[:150])
    ev_ev = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='evict'").fetchone()["c"]
    check("容量淘汰留 evict 审计", ev_ev >= 1, f"evict events={ev_ev}")

    # 11. wipe --hard：物理删除 + 审计内容摘录清空 + purge 事件
    s_hard = sessions.new_session_id()
    sessions.set_mode(conn, s_hard, "normal")
    policy.guarded_write(conn, "绝密内容甲甲甲，硬删除后不得残留", session_id=s_hard, agent="purge-me")
    policy.guarded_write(conn, "二零二六年十月一日的采购清单，同样要硬删除", session_id=s_hard, agent="purge-me")
    n_hard, msg_hard = store.wipe(agent="purge-me", hard=True)
    left_rows = conn.execute("SELECT COUNT(*) c FROM memories WHERE agent='purge-me'").fetchone()["c"]
    frag = conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE detail LIKE '%甲甲甲%' OR detail LIKE '%采购清单%'"
    ).fetchone()["c"]
    purge_ev = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='purge'").fetchone()["c"]
    check("hard wipe 物理删除全部行", n_hard == 2 and left_rows == 0, f"n={n_hard} rows={left_rows}")
    check("hard wipe 清空审计内容摘录", frag == 0, f"fragments={frag}")
    check("hard wipe 记 purge 事件", purge_ev >= 1)

    # 12. hook ↔ MCP 会话桥接；session_mode 只能调紧
    import ownmem.mcp_server as ms

    old_agent = os.environ.pop("OWNMEM_AGENT", None)
    old_sess = os.environ.pop("OWNMEM_SESSION", None)
    os.environ["OWNMEM_AGENT"] = "claude-code"
    s_bridge = sessions.new_session_id()
    db.set_setting(conn, "active_session:claude-code",
                   json.dumps({"sid": s_bridge, "ts": audit.now()}))
    ms._PROC_SESSION = ""
    check("MCP 会话桥接到 hook 会话", ms._session_id(conn) == s_bridge,
          ms._session_id(conn))
    sessions.set_mode(conn, s_bridge, "incognito", agent="probe")
    res_deny = ms._t_session_mode("blank")
    check("模型不能放宽会话模式（incognito→blank 拒绝）",
          "放宽" in res_deny and sessions.get_mode(conn, s_bridge) == "incognito", res_deny)
    res_tighten = ms._t_session_mode("ghost")
    check("模型仍可把会话调紧（→ghost）", sessions.get_mode(conn, s_bridge) == "ghost", res_tighten)
    sessions.set_mode(conn, s_bridge, "normal", agent="probe")
    if old_agent is not None:
        os.environ["OWNMEM_AGENT"] = old_agent
    if old_sess is not None:
        os.environ["OWNMEM_SESSION"] = old_sess

    # 13. MCP stdio 在 GBK 管道下（无 PYTHONUTF8）不崩，协议输出恒为 ASCII
    gbk_env = {**os.environ, "OWNMEM_AGENT": "gbk-test"}
    gbk_env.pop("PYTHONUTF8", None)
    gbk_env.pop("PYTHONIOENCODING", None)
    gbk_msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "session_mode", "arguments": {"mode": "incognito"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "add_memory", "arguments": {"content": "gbk 管道下的被拦写入"}}},
    ]
    proc_gbk = subprocess.run(
        [sys.executable, "-m", "ownmem.mcp_server"],
        input=("\n".join(json.dumps(m) for m in gbk_msgs) + "\n").encode("utf-8"),
        capture_output=True, cwd=str(ROOT), env=gbk_env, timeout=60,
    )
    gbk_lines = [ln for ln in proc_gbk.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    gbk_ok = proc_gbk.returncode == 0 and len(gbk_lines) == 3
    try:
        for ln in gbk_lines:
            json.loads(ln)
    except json.JSONDecodeError:
        gbk_ok = False
    check("GBK 管道下 stdio 全响应且纯 ASCII",
          gbk_ok and b"\\u26d4" in proc_gbk.stdout and b"Traceback" not in proc_gbk.stderr,
          f"rc={proc_gbk.returncode} lines={len(gbk_lines)} err={proc_gbk.stderr.decode('utf-8', errors='replace')[-200:]}")

    # ------------------------------------------------------------------
    # 以下为 0.3.1 修复项的回归测试
    # ------------------------------------------------------------------
    import time

    from ownmem import cli as cli_mod
    from ownmem.core import bridge, search
    from ownmem.core.text import tokenize

    # 14. 库结构版本：已初始化的库不必每次重跑 DDL
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    check("库已写入 schema 版本号（新进程不再重跑建表）", ver >= 1, f"user_version={ver}")

    # 15. 会话桥接：三种调用方必须认成同一个会话
    s_b = sessions.new_session_id()
    bridge.write(conn, "bridge-agent", s_b)
    check("桥接写入后能读回同一个会话", bridge.read(conn, "bridge-agent") == s_b, s_b)

    class _Args:
        agent = "bridge-agent"
        session = ""

    got_sid = cli_mod._resolve_session(_Args(), conn)
    check("CLI 侧读到的是会话 id，不是整个 JSON 串", got_sid == s_b, got_sid[:80])
    # 升级兼容：0.3.0 之前写下的纯字符串不能变成畸形会话
    db.set_setting(conn, bridge.key("legacy-agent"), "s_legacy_plain")
    check("旧格式桥接值仍可读（升级不破坏已有数据）",
          bridge.read(conn, "legacy-agent") == "s_legacy_plain")
    # 过期桥接必须失效，否则会拿上一段对话的会话来记账
    db.set_setting(conn, bridge.key("stale-agent"),
                   json.dumps({"sid": "s_old", "ts": "2020-01-01T00:00:00+08:00"}))
    check("过期桥接被判为不可用", bridge.read(conn, "stale-agent") == "", "s_old 不该被读出来")

    # 16. 隐身拦截自动进待定区；ghost 连副本都不留
    s_g = sessions.new_session_id()
    sessions.set_mode(conn, s_g, "ghost", agent="probe")
    rg = policy.guarded_write(conn, "ghost 模式下的一句话", session_id=s_g, agent="probe")
    check("ghost 拦截且不留待定区副本",
          rg["blocked"] and not rg.get("staged_id") and len(policy.list_staged(conn, s_g)) == 0,
          str(rg))

    # 17. 抹除某个 agent 时，待定区一并清空
    s_w = sessions.new_session_id()
    sessions.set_mode(conn, s_w, "incognito", agent="wipe-me")
    policy.guarded_write(conn, "待抹除来源的内容", session_id=s_w, agent="wipe-me")
    check("隐身拦截后确实进了待定区", len(policy.list_staged(conn, s_w)) == 1)
    n_st = policy.discard_staged_by_agent(conn, "wipe-me")
    check("抹除 agent 时待定区一并清空",
          n_st == 1 and len(policy.list_staged(conn, s_w)) == 0, f"n={n_st}")

    # 18. 抹除审计详情时必须留下「这里被抹过」的痕迹
    red = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE event='redacted'").fetchone()["c"]
    check("清空审计详情时留下 redacted 痕迹（只增不改要能自证）", red >= 1, f"redacted={red}")

    # 19. 会话「最近活动」会被召回刷新
    s_act = sessions.new_session_id()
    sessions.set_mode(conn, s_act, "normal", agent="probe")
    store.add("刷新活动时间的专用语料，措辞刻意独特以免判重", agent="probe", session_id=s_act)
    before = conn.execute("SELECT updated_at FROM sessions WHERE id=?", (s_act,)).fetchone()["updated_at"]
    time.sleep(1.1)
    recall.recall_context(conn, "刷新活动时间的专用语料", session_id=s_act, agent="probe")
    after = conn.execute("SELECT updated_at FROM sessions WHERE id=?", (s_act,)).fetchone()["updated_at"]
    check("召回刷新会话的最近活动时间", after > before, f"{before} -> {after}")

    # 20. 关键长期事实不因时间褪色
    #     对照设计：两条内容完全相同，只有重要度和"年龄"不同。
    #     修复前 recency 一视同仁（两年前的压到 0.4），于是"两年前的 critical"
    #     会输给"今天的 normal"；修复后 critical 豁免衰减，应当反超。
    conn.execute("UPDATE memories SET status='trashed' WHERE status='active'")
    mc, _ = store.add("用户生日是八月二十九日", importance="critical", agent="age-test",
                      skip_dedup=True)
    mn, _ = store.add("用户生日是八月二十九日", importance="normal", agent="age-test",
                      skip_dedup=True)
    conn.execute("UPDATE memories SET created_at=? WHERE id=?",
                 ("2024-01-01T00:00:00+08:00", mc["id"]))
    ranked = search.rank(store.all_active(), tokenize("用户生日"), limit=10)
    check("两年前的关键记忆不再被时间压到今天的普通记忆之后",
          ranked and ranked[0][0]["id"] == mc["id"],
          str([(m["id"], m["importance"], round(s, 3)) for m, s in ranked]))

    # 21. 待定区超龄残留可被清理（默认只报数，--yes 才删）
    s_old = sessions.new_session_id()
    policy.stage(conn, s_old, "很久以前的待定内容")
    conn.execute("UPDATE settings SET value=? WHERE key=?",
                 (json.dumps([{"sid": "p_001", "content": "很久以前的待定内容",
                               "agent": "probe", "staged_at": "2020-01-01T00:00:00+08:00"}],
                             ensure_ascii=False),
                  f"staged:{s_old}"))
    stats = policy.staged_stats(conn, max_age_days=30)
    check("超龄待定区能被体检发现", stats["stale_items"] >= 1, json.dumps(stats, ensure_ascii=False))
    pruned_items, _ = policy.prune_staged(conn, max_age_days=30)
    check("超龄待定区可被清理", pruned_items >= 1 and len(policy.list_staged(conn, s_old)) == 0,
          f"pruned={pruned_items}")

    print(f"\n=== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 ===")
    if FAIL:
        for f in FAIL:
            print("  失败项：" + f)
    print(f"（测试数据目录 {TMP}，可安全删除）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
