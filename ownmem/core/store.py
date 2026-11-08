# -*- coding: utf-8 -*-
"""记忆仓库：增删改查、判重、容量淘汰、回收站、抹除。

这一层是纯能力层，不做策略判断——「该不该写」由 gateway 决定。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import string
from datetime import datetime

from . import audit, db, search
from .text import normalize, similarity, tokenize

MAX_MEMORIES = int(os.environ.get("OWNMEM_MAX_MEMORIES", "500"))
DUP_THRESHOLD = float(os.environ.get("OWNMEM_DUP_THRESHOLD", "0.6"))
MAX_CONTENT = int(os.environ.get("OWNMEM_MAX_CONTENT", "5000"))
PREVIEW_LEN = 200

# 判重规模阈值：库里超过这么多条时，模糊相似度只对 BM25 召回的候选做，
# 不再全表扫。全表扫是 O(N)，写入因此变成 O(N²)（实测灌 500 条要 12.6s）。
DEDUP_SCAN_LIMIT = int(os.environ.get("OWNMEM_DEDUP_SCAN_LIMIT", "60"))
DEDUP_CANDIDATES = int(os.environ.get("OWNMEM_DEDUP_CANDIDATES", "30"))

VALID_IMPORTANCE = ("always", "critical", "high", "normal", "low")

# 三本账（v2 §2）：结论 / 过程 / 踩坑。
# 默认 conclusion 是对旧数据的兜底——历史记忆绝大多数是定下来的规则与事实，
# 把它们默认当结论账，升级前后召回结果完全一致。
VALID_KINDS = ("conclusion", "process", "pitfall")
KIND_LABELS = {"conclusion": "结论", "process": "过程", "pitfall": "踩坑"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return "m_" + datetime.now().strftime("%Y%m%d") + "_" + suffix


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key, fallback in (("tags", []), ("recurred_agents", [])):
        try:
            val = json.loads(d.get(key) or json.dumps(fallback))
            d[key] = val if isinstance(val, list) else fallback
        except (json.JSONDecodeError, TypeError):
            d[key] = fallback
    return d


class MemoryStore:
    """记忆仓库。所有方法都通过同一个 sqlite 连接操作。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # 上一次 add() 若命中「已有踩坑账」，不新增条目而是累加复发计数；
        # 这里留下这次复发的明细，供调用方渲染（ok=False 但信息其实已被吸收）
        self.last_recurrence: dict | None = None

    # ---------------- 读 ----------------
    def all_active(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status='active' ORDER BY created_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def pinned(self) -> list[dict]:
        """常驻记忆（importance='always'）。

        不走 BM25：它的语义是「每次会话都必须在场」，不是「与当前话题相关」。
        用检索去捞常驻规则，等于让红线取决于用户这句话碰巧用了什么词。
        """
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status='active' AND importance='always' "
            "ORDER BY created_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get(self, memory_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        source: str = "",
        agent: str = "",
        project: str = "",
        kind: str = "",
        count_hits: bool = True,
        include_superseded: bool = False,
    ) -> list[tuple[dict, float]]:
        items = self.all_active()
        if kind:
            items = [m for m in items if (m.get("kind") or "conclusion") == kind]
        tokens = tokenize(query)
        if not tokens:
            return []
        top = search.rank(
            items, tokens, limit,
            source=source, agent=agent, project=project,
            include_superseded=include_superseded,
        )
        if count_hits and top:
            self.bump_hits([mem["id"] for mem, _ in top])
        return top

    def bump_hits(self, memory_ids: list[str]) -> None:
        """命中计数。调用方应在确认条目真正被使用后再计数，避免虚高。"""
        for mid in memory_ids:
            self.conn.execute("UPDATE memories SET hits=hits+1 WHERE id=?", (mid,))

    def _active_where(
        self,
        *,
        agent: str = "",
        source: str = "",
        importance: str = "",
        kind: str = "",
        project: str = "",
        include_global: bool = True,
        include_superseded: bool = True,
    ) -> tuple[str, list]:
        """列表与计数共用的筛选片段——两边条件必须字面一致，
        否则「显示 20 条 / 共 3 条」这种自相矛盾的数字迟早出现。"""
        sql = " WHERE status='active'"
        args: list = []
        if not include_superseded:
            # 列表默认能看到作废条目（它是有用的历史），只有「当作现状来用」的
            # 场景（召回、按相关度检索）才需要把它们挡掉。
            sql += " AND superseded_by=''"
        if agent:
            sql += " AND agent=?"
            args.append(agent)
        if source:
            sql += " AND source=?"
            args.append(source)
        if importance:
            sql += " AND importance=?"
            args.append(importance)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if project:
            if include_global:
                sql += " AND (project=? OR project='')"
            else:
                sql += " AND project=?"
            args.append(project)
        return sql, args

    def list_active(
        self,
        *,
        agent: str = "",
        source: str = "",
        importance: str = "",
        kind: str = "",
        project: str = "",
        include_global: bool = True,
        include_superseded: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        where, args = self._active_where(
            agent=agent, source=source, importance=importance, kind=kind,
            project=project, include_global=include_global,
            include_superseded=include_superseded,
        )
        sql = "SELECT * FROM memories" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [max(1, int(limit)), max(0, int(offset))]
        return [_row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_active(
        self,
        *,
        agent: str = "",
        source: str = "",
        importance: str = "",
        kind: str = "",
        project: str = "",
        include_global: bool = True,
        include_superseded: bool = True,
    ) -> int:
        where, args = self._active_where(
            agent=agent, source=source, importance=importance, kind=kind,
            project=project, include_global=include_global,
            include_superseded=include_superseded,
        )
        row = self.conn.execute("SELECT COUNT(*) c FROM memories" + where, args).fetchone()
        return row["c"] if row else 0

    def list_trash(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status='trashed' ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------------- 写 ----------------
    def find_duplicate(self, content: str, *, exclude_id: str = "") -> tuple[dict | None, float]:
        """返回最相似的一条及其相似度。

        两条路径：
          · 归一化后全等 —— 便宜（字符串比较），全表扫一遍也不心疼
          · 模糊相似度   —— 贵（bigram + Dice），库大了先让 BM25 收敛候选集，
                            否则每次写入都是一次全表扫描（实测 500 条约 25ms/条）
        """
        items = self.all_active()
        if exclude_id:
            items = [it for it in items if it["id"] != exclude_id]
        if not items:
            return None, 0.0

        norm = normalize(content)
        for it in items:
            if normalize(it.get("content", "")) == norm:
                return it, 1.0

        candidates = items
        if len(items) > DEDUP_SCAN_LIMIT:
            tokens = tokenize(content)
            ranked = search.rank(items, tokens, limit=DEDUP_CANDIDATES)
            candidates = [m for m, _ in ranked]
            if not candidates:
                return None, 0.0
            # 补齐：全等检查已过，这里只需保证最高相似度的那条在候选里。
            # BM25 用词频倒排召回，对「换措辞的同义重述」通常能进候选——
            # 它共享实体词；纯虚词构成的短句例外，那种内容判不判重都不重要。

        best, best_sim = None, 0.0
        for it in candidates:
            s = similarity(content, it.get("content", ""))
            if s > best_sim:
                best, best_sim = it, s
        return best, best_sim

    def add(
        self,
        content: str,
        *,
        importance: str = "normal",
        tags: str = "",
        source: str = "unknown",
        agent: str = "unknown",
        session_id: str = "",
        kind: str = "conclusion",
        project: str = "",
        skip_dedup: bool = False,
        supersedes: str = "",
    ) -> tuple[dict | None, str]:
        """返回 (记忆, 说明)。判重命中时记忆为 None。

        kind: 三本账（conclusion/process/pitfall），默认结论账。
        project: 项目归属，''=全局（任何项目都能召回）。
        supersedes: 这条新记忆推翻了哪条旧记忆（写入后自动挂取代链）。
        """
        self.last_recurrence = None
        content = (content or "").strip()
        if not content:
            return None, "内容为空，未保存。"
        if len(content) > MAX_CONTENT:
            return None, f"内容过长（{len(content)} 字），上限 {MAX_CONTENT} 字。"
        if importance not in VALID_IMPORTANCE:
            importance = "normal"
        if kind not in VALID_KINDS:
            kind = "conclusion"
        if not skip_dedup:
            dup, sim = self.find_duplicate(content)
            if dup is not None and sim >= DUP_THRESHOLD:
                # 撞上的是一条**踩坑账**：这不是「重复写入」，是「这个坑又被踩了」。
                # 新增一条平行记录毫无价值——两条一模一样的坑只会稀释注意力，
                # 真正该记录的是复发次数和「谁栽过」。
                if (dup.get("kind") or "conclusion") == "pitfall":
                    n, agents = self.record_recurrence(dup, agent=agent, session_id=session_id)
                    self.last_recurrence = {"id": dup["id"], "recurred": n, "agents": agents}
                    return None, (
                        f"这个坑已经记过了（id={dup['id']}），本次计为第 {n} 次复发"
                        f"（栽过的：{'/'.join(agents) or agent}）。未新增重复条目。"
                        f"原记录：{dup['content'][:60]}{'…' if len(dup['content']) > 60 else ''}"
                    )
                note = "（该条已被取代，你可能在推翻自己后来的结论）" if dup.get("superseded_by") else ""
                return None, (
                    f"已存在高度相似记忆（id={dup['id']}，相似度 {sim:.0%}）{note}："
                    f"{dup['content'][:60]}{'…' if len(dup['content']) > 60 else ''}"
                )

        entry = {
            "id": _new_id(),
            "content": content,
            "importance": importance,
            "tags": json.dumps(
                [t.strip() for t in (tags or "").split(",") if t.strip()], ensure_ascii=False
            ),
            "source": (source or "unknown").strip() or "unknown",
            "agent": (agent or "unknown").strip() or "unknown",
            "session_id": session_id or "",
            "kind": kind,
            "project": (project or "").strip(),
            "supersedes": (supersedes or "").strip(),
            "created_at": _now(),
            "updated_at": "",
        }
        self.conn.execute(
            "INSERT INTO memories(id, content, importance, tags, source, agent, session_id, "
            "kind, project, supersedes, created_at, updated_at, hits, status) "
            "VALUES(:id,:content,:importance,:tags,:source,:agent,:session_id,"
            ":kind,:project,:supersedes,:created_at,:updated_at,0,'active')",
            entry,
        )
        self._enforce_capacity()
        saved = self.get(entry["id"])
        return saved, f"已保存记忆 {entry['id']}（importance={importance}, kind={kind}）"

    def record_recurrence(
        self, mem: dict, *, agent: str = "unknown", session_id: str = ""
    ) -> tuple[int, list[str]]:
        """记一次「同一个坑又踩了」。返回 (累计复发数, 栽过的 agent 列表)。

        只对踩坑账有意义，别的账本重复写入交给判重去挡。
        """
        agents = [a for a in (mem.get("recurred_agents") or []) if str(a).strip()]
        # 原始作者也算「栽过」——第一版是谁写的，本身就是这条坑最重要的归属信息。
        # 只在第一次复发时补上，之后靠下面的去重逻辑保持名单干净。
        for who in (mem.get("agent") or "", agent or ""):
            who = str(who).strip()
            if who and who.lower() not in {a.lower() for a in agents}:
                agents.append(who)
        who = (agent or "unknown").strip() or "unknown"
        n = int(mem.get("recurred") or 0) + 1
        self.conn.execute(
            "UPDATE memories SET recurred=?, recurred_agents=?, last_recurred_at=? WHERE id=?",
            (n, json.dumps(agents, ensure_ascii=False), _now(), mem["id"]),
        )
        audit.log(
            self.conn, "recur",
            agent=who, session_id=session_id, memory_id=mem["id"],
            detail=f"第 {n} 次复发（栽过：{'/'.join(agents)}）｜{audit.detail(mem.get('content', ''), 80)}",
        )
        return n, agents

    def update(
        self, memory_id: str, *, content: str = "", importance: str = "", tags: str = ""
    ) -> tuple[bool, str]:
        target = self.get(memory_id)
        if not target:
            return False, f"未找到 id={memory_id} 的记忆。"
        sets, args = [], []
        if content:
            dup, sim = self.find_duplicate(content, exclude_id=memory_id)
            if dup is not None and sim >= DUP_THRESHOLD:
                return False, f"与其他记忆高度相似（id={dup['id']}），未更新。"
            sets.append("content=?"); args.append(content.strip())
        if importance:
            if importance not in VALID_IMPORTANCE:
                return False, f"无效重要度 '{importance}'。"
            sets.append("importance=?"); args.append(importance)
        if tags:
            sets.append("tags=?")
            args.append(json.dumps([t.strip() for t in tags.split(",") if t.strip()], ensure_ascii=False))
        if not sets:
            return False, "未提供任何更新字段。"
        sets.append("updated_at=?"); args.append(_now())
        args.append(memory_id)
        self.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", args)
        return True, f"已更新记忆 {memory_id}。"

    def link_supersede(self, old_id: str, new_id: str) -> tuple[bool, str]:
        """把 old 标记成「已被 new 取代」。

        刻意不删、不撤下旧条：结论被推翻的证据链本身就是记忆的一部分——
        「曾经以为是 A，后来改成 B，因为……」。真删了，下次还会有人（或
        另一个 agent）重新提出 A，而你没有任何依据说它已经被否过。
        召回侧默认过滤 superseded_by 非空的条目，所以作废的结论不会
        继续冒充现状；要考古时用 search --superseded。
        """
        if old_id == new_id:
            return False, "不能用自己取代自己。"
        old = self.get(old_id)
        new = self.get(new_id)
        if not old:
            return False, f"未找到被取代的记忆 {old_id}。"
        if not new:
            return False, f"未找到新记忆 {new_id}。"
        if old.get("superseded_by"):
            return False, (
                f"{old_id} 已经被 {old['superseded_by']} 取代过了；"
                f"若要再改一次，请取代 {old['superseded_by']} 这一版。"
            )
        self.conn.execute(
            "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
            (new_id, _now(), old_id),
        )
        self.conn.execute("UPDATE memories SET supersedes=? WHERE id=?", (old_id, new_id))
        audit.log(
            self.conn, "supersede", memory_id=old_id,
            detail=f"{old_id} 被 {new_id} 取代｜旧：{audit.detail(old.get('content', ''), 60)}"
                   f"｜新：{audit.detail(new.get('content', ''), 60)}",
        )
        return True, f"已把 {old_id} 标记为被 {new_id} 取代（旧条保留可查，不再注入）。"

    def chain(self, memory_id: str, *, max_hops: int = 20) -> dict:
        """沿取代链往两头追。返回 {"current": 记忆, "older": [...], "newer": [...]}。

        older 是「这条推翻了谁」，newer 是「这条又被谁推翻了」。
        带跳数上限：链是被写坏的（手工改库、循环取代）也不能把进程拖死。
        """
        cur = self.get(memory_id)
        if not cur:
            return {"current": None, "older": [], "newer": []}
        older: list[dict] = []
        seen = {memory_id}
        nxt = cur.get("supersedes") or ""
        while nxt and nxt not in seen and len(older) < max_hops:
            seen.add(nxt)
            item = self.get(nxt)
            if not item:
                break
            older.append(item)
            nxt = item.get("supersedes") or ""
        newer: list[dict] = []
        nxt = cur.get("superseded_by") or ""
        while nxt and nxt not in seen and len(newer) < max_hops:
            seen.add(nxt)
            item = self.get(nxt)
            if not item:
                break
            newer.append(item)
            nxt = item.get("superseded_by") or ""
        return {"current": cur, "older": older, "newer": newer}

    def delete(self, memory_id: str, *, reason: str = "manual") -> tuple[bool, str]:
        if not self.get(memory_id):
            return False, f"未找到 id={memory_id} 的记忆。"
        self.conn.execute(
            "UPDATE memories SET status='trashed', updated_at=? WHERE id=?", (_now(), memory_id)
        )
        return True, f"已删除 {memory_id}（进入回收站，可用 restore 找回）。"

    def restore(self, memory_id: str) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id=? AND status='trashed'", (memory_id,)
        ).fetchone()
        if not row:
            return False, f"回收站中未找到 id={memory_id}。"
        self.conn.execute("UPDATE memories SET status='active' WHERE id=?", (memory_id,))
        self._enforce_capacity()
        return True, f"已恢复记忆 {memory_id}。"

    def wipe(self, *, agent: str = "", source: str = "", hard: bool = False) -> tuple[int, str]:
        """抹除某个来源/agent 的全部记忆——「把某段过去删干净」。

        hard=False 进回收站（可恢复）；hard=True 物理删除并收缩数据库，
        同时清空这些记忆在审计日志里留下的内容摘录。
        """
        if not agent and not source:
            return 0, "必须指定 agent 或 source。"
        where = "WHERE status='active'"
        args: list = []
        if agent:
            where += " AND agent=?"
            args.append(agent)
        if source:
            where += " AND source=?"
            args.append(source)
        ids = [r["id"] for r in self.conn.execute(f"SELECT id FROM memories {where}", args).fetchall()]
        if not ids:
            return 0, "没有匹配的活跃记忆。"
        if hard:
            marks = ",".join("?" * len(ids))
            self.conn.execute(f"DELETE FROM memories WHERE id IN ({marks})", ids)
            cur = self.conn.execute(
                f"UPDATE audit_log SET detail='' WHERE memory_id IN ({marks})", ids
            )
            cleared = cur.rowcount if cur is not None and cur.rowcount > 0 else 0
            audit.log(
                self.conn, "purge",
                detail=f"hard wipe agent={agent} source={source} n={len(ids)}，"
                       f"这些记忆的审计内容摘录已一并清空",
            )
            if cleared:
                # 审计台账本该只增不改，这里破例改了一次就必须记一笔——
                # 否则事后分不清「从没记录过」和「记录了又被清掉」
                audit.log(
                    self.conn, "redacted",
                    detail=f"hard wipe 清空了 {cleared} 条审计详情的内容摘录"
                           f"（agent={agent} source={source}），不可恢复",
                )
            self.conn.execute("VACUUM")
            return len(ids), f"已彻底删除 {len(ids)} 条记忆（数据库已收缩，不可恢复）。"
        self.conn.execute(
            f"UPDATE memories SET status='trashed', updated_at=? {where}", [_now()] + args
        )
        return len(ids), f"已抹除 {len(ids)} 条记忆（可恢复）。"

    def purge(self, memory_id: str) -> tuple[bool, str]:
        """彻底删除单条记忆：物理删除 + 清空其审计内容摘录，不可恢复。"""
        if not self.get(memory_id):
            return False, f"未找到 id={memory_id} 的记忆。"
        self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        cur = self.conn.execute("UPDATE audit_log SET detail='' WHERE memory_id=?", (memory_id,))
        cleared = cur.rowcount if cur is not None and cur.rowcount > 0 else 0
        self.conn.execute("VACUUM")
        audit.log(self.conn, "purge", memory_id=memory_id, detail="彻底删除单条并清空其审计详情")
        if cleared:
            # 同上：改了历史就得留痕，否则「没记录」和「清掉了」无法区分
            audit.log(
                self.conn, "redacted", memory_id=memory_id,
                detail=f"彻底删除记忆时清空了 {cleared} 条审计详情的内容摘录，不可恢复",
            )
        return True, f"已彻底删除 {memory_id}（不可恢复）。"

    def _enforce_capacity(self) -> int:
        """超容量时淘汰：先 low，再按 重要度→命中数→新旧。进回收站不销毁。"""
        total = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active'"
        ).fetchone()["c"]
        if total <= MAX_MEMORIES:
            return 0
        overflow = total - MAX_MEMORIES
        evicted = 0
        evicted_ids: list[str] = []
        order = {
            # always 是常驻规则（红线/口径/身份），容量淘汰永远轮不到它——
            # 挤掉一条「别动系统文件」比丢失任何一条项目流水都严重。
            "always": -1,
            "critical": 0, "high": 1, "normal": 2, "low": 3,
        }
        rows = self.conn.execute(
            "SELECT id, importance, hits, created_at FROM memories WHERE status='active'"
        ).fetchall()
        ranked = sorted(
            # -rank 使 low(3) 排最前、critical(0) 排最后：先淘汰 low，再少命中，最后旧的
            rows,
            key=lambda r: (-order.get(r["importance"], 9), r["hits"], r["created_at"]),
        )
        for r in ranked:
            if evicted >= overflow:
                break
            self.conn.execute(
                "UPDATE memories SET status='trashed', updated_at=? WHERE id=?", (_now(), r["id"])
            )
            evicted += 1
            evicted_ids.append(r["id"])
        if evicted_ids:
            # 容量淘汰是「系统自己动了记忆」，必须留账
            audit.log(
                self.conn, "evict",
                detail=f"容量淘汰 {evicted} 条: {' '.join(evicted_ids)}"[:200],
            )
        return evicted

    def promote_repeated(self, *, min_hits: int = 5, min_sessions: int = 3, dry_run: bool = False) -> dict:
        """晋级：反复被用到的「过程账」升格为「结论账」（v2 §3.7）。

        为什么是过程账先晋级：过程账记的是「当时怎么想的」，本来只在特定场合
        有用；但如果它被三个以上不同会话反复命中，说明它已经不是过程，而是
        这个人稳定依赖的结论——只是当初写的时候还不知道。

        阈值刻意保守（≥5 次召回 + ≥3 个不同会话）。放宽到 2 次就会把偶然
        重提的中间稿当成定论，那比不晋级更糟：错误的东西一旦挂上「结论」的
        名义，后面所有 agent 都会照着它做。

        只升不降、不删除——降级和遗忘交给时间衰减，改 kind 是「承认它的地位
        变了」，不是清理动作。
        """
        rows = self.conn.execute(
            "SELECT m.id, m.content, m.hits FROM memories m "
            "WHERE m.status='active' AND m.kind='process' AND m.superseded_by='' "
            "  AND m.hits >= ?",
            (max(1, int(min_hits)),),
        ).fetchall()
        promoted: list[dict] = []
        for r in rows:
            n_sessions = self.conn.execute(
                "SELECT COUNT(DISTINCT session_id) c FROM audit_log "
                "WHERE memory_id=? AND event='recall' AND session_id<>''",
                (r["id"],),
            ).fetchone()["c"]
            if n_sessions < min_sessions:
                continue
            promoted.append({"id": r["id"], "hits": r["hits"], "sessions": n_sessions,
                             "content": r["content"]})
            if dry_run:
                continue
            self.conn.execute(
                "UPDATE memories SET kind='conclusion', updated_at=? WHERE id=?",
                (_now(), r["id"]),
            )
            audit.log(
                self.conn, "kind_promote", memory_id=r["id"],
                detail=f"过程账晋级为结论账（召回 {r['hits']} 次 / {n_sessions} 个会话）"
                       f"｜{audit.detail(r['content'], 80)}",
            )
        return {"candidates": promoted, "promoted": 0 if dry_run else len(promoted)}

    def decay_report(self, *, floor: float = 0.4) -> dict:
        """衰减体检：哪些记忆已经掉到分数地板附近（= 事实上快被忘了）。

        只报告不处理。遗忘该不该发生要人拍板——算法偷偷决定「这条不重要」，
        和它偷偷删掉一样危险。
        """
        from . import search as search_mod

        now = datetime.now().astimezone()
        faded: list[dict] = []
        for m in self.all_active():
            try:
                created = datetime.fromisoformat(m["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=now.tzinfo)
                age = max(0.0, (now - created).total_seconds() / 86400)
            except (KeyError, ValueError, TypeError):
                continue
            level = m.get("importance", "normal")
            if level in ("always", "critical"):
                continue
            half = (
                search_mod.EPHEMERAL_HALF_LIFE
                if search_mod.has_time_anchor(m.get("content", ""))
                else search_mod.FACTUAL_HALF_LIFE
            ) * search_mod.IMPORTANCE_DECAY.get(level, 1.0)
            recency = max(0.35, 1.0 - age / half)
            if recency <= floor + 0.05:
                faded.append({
                    "id": m["id"], "recency": round(recency, 2),
                    "age_days": int(age), "importance": level,
                    "kind": m.get("kind") or "conclusion",
                    "content": m.get("content", ""),
                })
        faded.sort(key=lambda x: x["recency"])
        return {"floor": floor, "faded": faded}

    # ---------------- 统计 ----------------
    def stats(self) -> dict:
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active'"
        ).fetchone()["c"]
        trashed = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='trashed'"
        ).fetchone()["c"]
        by_imp = {
            r["importance"]: r["n"]
            for r in self.conn.execute(
                "SELECT importance, COUNT(*) n FROM memories WHERE status='active' GROUP BY importance"
            ).fetchall()
        }
        by_agent = {
            r["agent"]: r["n"]
            for r in self.conn.execute(
                "SELECT agent, COUNT(*) n FROM memories WHERE status='active' GROUP BY agent"
            ).fetchall()
        }
        by_kind = {
            r["kind"]: r["n"]
            for r in self.conn.execute(
                "SELECT kind, COUNT(*) n FROM memories WHERE status='active' GROUP BY kind"
            ).fetchall()
        }
        superseded = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' AND superseded_by<>''"
        ).fetchone()["c"]
        recurred = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(recurred), 0) s FROM memories "
            "WHERE status='active' AND kind='pitfall' AND recurred>0"
        ).fetchone()
        return {
            "active": active,
            "capacity": MAX_MEMORIES,
            "trashed": trashed,
            "by_importance": by_imp,
            "by_agent": by_agent,
            "by_kind": by_kind,
            "superseded": superseded,
            "recurring_pitfalls": recurred["c"],
            "recurrence_total": recurred["s"],
            "home": str(db.home_dir()),
        }


def open_store(conn: sqlite3.Connection | None = None) -> tuple[sqlite3.Connection, MemoryStore]:
    c = conn or db.connect()
    return c, MemoryStore(c)


def format_hits(top: list[tuple[dict, float]], preview: bool = True) -> str:
    """把检索结果渲染成给 agent 看的文本。"""
    if not top:
        return "没有找到相关记忆。"
    lines = [f"找到 {len(top)} 条相关记忆："]
    for mem, sc in top:
        text = mem["content"]
        if preview and len(text) > PREVIEW_LEN:
            text = text[:PREVIEW_LEN] + f"…（共 {len(text)} 字）"
        # 类型只在非默认值时显式标注：结论账占绝大多数，人人都标一遍等于噪音
        kind = mem.get("kind") or "conclusion"
        kind_note = f"类型={KIND_LABELS.get(kind, kind)}, " if kind != "conclusion" else ""
        extra = ""
        if kind == "pitfall" and int(mem.get("recurred") or 0) > 0:
            agents = mem.get("recurred_agents") or []
            who = "/".join(agents) if isinstance(agents, list) else str(agents)
            extra = f"⚠️ 已复发 {mem['recurred']} 次（栽过：{who}）, "
        if mem.get("superseded_by"):
            extra += f"⛔ 已被 {mem['superseded_by']} 取代（仅存档）, "
        lines.append(
            f"- [{mem['id']}] ({mem.get('created_at', '?')[:10]}, "
            f"{kind_note}{extra}"
            f"重要度={mem.get('importance', 'normal')}, "
            f"来自={mem.get('agent', 'unknown')}, "
            f"相关分={sc:.2f}) {text}"
        )
    return "\n".join(lines)
