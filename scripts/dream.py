#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 做梦：聚类巩固、梦境应用与报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from core import *

import memory as _mem   # load_all / save_all / retention / 归档等（模块级延迟引用避免循环）

# ---------------------------------------------------------------- 做梦 / 巩固

# 聚类池上限：记忆多了之后 O(n²) 两两比对会拖垮每晚的巩固，
# 超过就按重要度取前 N 条，剩下的留到下轮（反正 consolidate 是按批推进的）。
_MAX_CLUSTER_POOL = 400

# 孤儿梦任务时效：pending 生成后超过这么久还没被 apply，视为孤儿任务。
# 未到期前重复触发不覆盖（沿用旧任务），到期后归档再开新任务。
_DREAM_ORPHAN_TTL = 48 * 3600

# 重放池上限：上次做梦之后的新记忆 + 未完成工作记忆，太多就取最近的
_REPLAY_MAX = 30

# 交接摘要目录：做梦时把重放线索压成结构化交接（借鉴 dsh-handoff-compaction：
# 老上下文→结构化摘要，最近细节原样保留在记忆库里，可 recall 追溯，不破坏原始数据）
# 保留最近 10 份，旧的自动清理。

# 分模式压缩策略（联动面板 AI/工程/拟人/自动模式）：
#   work（工程模式）——骨架压缩：少淡化、少淡忘，代码/报错/路径类细节保真；
#   human（拟人模式）——摘要压缩：细节可放心淡化，人设与情绪要点保住即可；
#   auto 按当前话题判定落到上面两档。
_COMPACTION_POLICY = {
    "work": {
        "detail_floor": 0.55,        # detail 淡化下限：骨架信息不打太薄
        "dust_retention_shift": 0.10,  # 淡忘判据加严：R 要再低 0.10 才进淡忘区
        "desc": "工程模式：骨架压缩——代码/报错/路径类细节保真，淡忘加严",
    },
    "human": {
        "detail_floor": 0.0,
        "dust_retention_shift": 0.0,
        "desc": "拟人模式：摘要压缩——细节可淡化，要点与人设保住即可",
    },
}


def _num(v, default: float = 0.0) -> float:
    """把 None/字符串/坏值安全转成 float（与 memory._num 同语义，避免环形 import）。"""
    if v is None or isinstance(v, bool):
        return float(default)
    try:
        f = float(v)
        return default if (f != f) else f   # NaN 检查
    except (TypeError, ValueError):
        return float(default)


def compaction_policy(mode: str = "") -> dict:
    """按会话模式取压缩策略（auto 时按话题判定，拿不到就当 human）。"""
    try:
        if not mode:
            mode = resolve_mode("")
    except Exception:
        mode = "human"
    pol = _COMPACTION_POLICY.get(mode)
    if not pol:
        mode = "human"
        pol = _COMPACTION_POLICY["human"]
    out = dict(pol)
    out["mode"] = mode
    return out


def replay_recent(limit: int = _REPLAY_MAX) -> list:
    """重放：把「上次做梦之后」的经历线索按时间倒序拼出来（做梦前的快速眼动）。

    来源：新写入的记忆（hippo/cortex，跳过 dust）+ 工作记忆里还没做完的事。
    只读不改库——重放是做梦的输入，不是对记忆的修改。
    """
    last = _num(load_dream_state().get("last_dream"), 0)
    now = now_ts()
    items = []
    for m in load_all():
        if not m.get("id") or m.get("layer") == "dust":
            continue
        if last and _num(m.get("created"), 0) < last:
            continue
        items.append({"id": m["id"], "kind": m.get("kind", ""),
                      "text": (m.get("text") or "")[:200],
                      "ts": _num(m.get("created"), 0), "from": "memory"})
    try:
        for r in read_jsonl(WORKING_FILE):
            if not r.get("open", True):
                continue
            ts = _num(r.get("ts"), 0)
            if now - ts > 12 * HOUR:
                continue
            items.append({"id": "", "kind": "working",
                          "text": (r.get("text") or "")[:200],
                          "ts": ts, "from": "working"})
    except Exception:
        pass
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:max(1, int(limit))]


def build_handoff(items: list, mode: str = "") -> dict:
    """压缩：重放线索 → 结构化交接摘要（状态 / 要点 / 未决 / 下一步）。

    只生成摘要、不动原始数据：原文仍在记忆库里，下个会话可以 recall 追溯。
    工程模式保「未决/下一步」优先（干活导向），拟人模式保「要点」优先（陪伴导向）。
    """
    pol = compaction_policy(mode)
    memories = [x for x in items if x.get("from") == "memory"]
    workings = [x for x in items if x.get("from") == "working"]
    # 要点：按 kind 分组取代表，工程模式优先 fact/insight/mistake，拟人模式优先 emotion/relationship
    priority = ("fact", "insight", "mistake", "preference") if pol["mode"] == "work" \
        else ("emotion", "relationship", "preference", "insight")
    ranked = sorted(memories, key=lambda x: (priority.index(x["kind"]) if x["kind"] in priority else 99,
                                             -x["ts"]))
    points = [x["text"] for x in ranked[:6]]
    todo = [x["text"] for x in workings[:5]]
    summary = {
        "at": now_ts(),
        "mode": pol["mode"],
        "replayed": len(items),
        "状态": f"上次做梦后新增 {len(memories)} 条记忆、{len(workings)} 件未完成的事",
        "要点": points,
        "未决": todo,
        "下一步": "下个会话开始时先处理「未决」事项；细节用 recall <关键词> 追原文",
    }
    return summary


def save_handoff(summary: dict) -> Path:
    """交接摘要落盘（JSON，可检索），只保留最近 10 份。"""
    d = HOME / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"handoff_{int(summary.get('at', now_ts()))}.json"
    atomic_json(path, summary)
    olds = sorted(d.glob("handoff_*.json"))
    for old in olds[:-10]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def latest_handoff() -> dict | None:
    """最近一份交接摘要（注入用）。"""
    d = HOME / "handoffs"
    files = sorted(d.glob("handoff_*.json")) if d.exists() else []
    if not files:
        return None
    try:
        h = _slurp_json(files[-1])
        return h if isinstance(h, dict) and h.get("要点") is not None else None
    except Exception:
        return None


def cluster_memories(records: list, max_clusters: int = 6, include_ids: set | None = None) -> list:
    include_ids = include_ids or set()
    # 无 id 的畸形记录没法做并查集/parent 映射，直接跳过。
    # 这些记录会原样保留在库，不会被 save_all 丢掉。
    # #41：correction 改口记录也不进聚类——合并成摘要=改口语境丢失，教训提炼读不到证据。
    pool = [m for m in records if m.get("id") and m.get("layer") != "dust"
            and not m.get("correction")
            and (not m.get("consolidated") or m["id"] in include_ids)]
    if not pool:
        return []
    if len(pool) > _MAX_CLUSTER_POOL:
        pool = sorted(pool, key=lambda m: _num(m.get("importance"), 0), reverse=True)[:_MAX_CLUSTER_POOL]
    parent = {m["id"]: m["id"] for m in pool}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    term_map = {m["id"]: terms(m.get("text", "")) for m in pool}
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            shared = (set(a.get("entities", [])) & set(b.get("entities", [])) |
                      set(a.get("tags", [])) & set(b.get("tags", [])))
            if shared or jaccard(term_map[a["id"]], term_map[b["id"]]) > 0.34:
                union(a["id"], b["id"])

    groups = {}
    for m in pool:
        groups.setdefault(find(m["id"]), []).append(m)

    clusters = []
    for gid, members in groups.items():
        if len(members) < 2 and max(_num(m.get("importance")) for m in members) < 0.7:
            continue
        clusters.append(members)
    clusters.sort(key=lambda g: (len(g) * max(_num(m.get("importance")) for m in g)), reverse=True)

    out = []
    now = now_ts()
    for g in clusters[:max_clusters]:
        out.append({
            "ids": [m["id"] for m in g],
            "texts": [{"id": m["id"], "kind": m.get("kind", "fact"),
                       "text": m.get("text", ""), "importance": m.get("importance", 0.5),
                       "age_days": round((now - _num(m.get("created"), now)) / DAY, 1)} for m in g],
            "tags": _dedupe([t for m in g for t in m.get("tags", [])]),
            "entities": _dedupe([e for m in g for e in m.get("entities", [])]),
        })
    return out


def _dream_snapshot() -> str:
    """做梦改库前的自动快照（借鉴 sage-mem：整理前强制建回滚点）。

    只打包记忆相关的核心文件（hippocampus/assoc/archive 等），比全库 backup
    轻量得多；存在 ~/.brain-memory/dream-snapshots/ 下，保留最近 10 份。
    失败返回 "" 并记事件日志；调用方（dream）fail-closed：拿不到快照就拒绝整理。
    """
    snap_dir = HOME / "dream-snapshots"
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
        base = f"dream-snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        path = snap_dir / base
        k = 1
        while path.exists():
            k += 1
            path = snap_dir / f"dream-snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{k}.zip"
        core_files = ["hippocampus.jsonl", "archive.jsonl", "assoc.jsonl",
                      "knowledge.jsonl", "persona.json", "dream_state.json"]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in core_files:
                f = HOME / name
                if f.is_file():
                    zf.write(f, name)
        # 保留最近 10 份，旧的删掉
        snaps = sorted(snap_dir.glob("dream-snap_*.zip"))
        for p in snaps[:-10]:
            try:
                p.unlink()
            except OSError:
                pass
        return str(path)
    except Exception as exc:
        try:
            log_event("dream", f"做梦前自动快照失败：{exc}")
        except Exception:
            pass
        return ""


def _mine_lessons(records: list, max_lessons: int = 3) -> list:
    """教训提炼（#36）：扫描失败闭环证据源 → 生成待审教训候选。

    证据源（按优先级）：
      1. correction=true 改口记录——用户亲手纠正，证据最强；
      2. 被 superseded_by 的旧记忆——结论错过一次被新事实取代；
      3. detect_conflicts 冲突对——两条记忆互相打架，其中带改口信号的当教训素材。
    每条教训 status="pending"（与 knowledge/persona 同一审核闸），
    evidence 存证据记忆 id，confidence 起步 0.4（梦提炼的先验：像，但没验证过）。
    返回本轮新建的教训（已入库，供梦报告引用）。
    """
    import memory as mem_mod
    created = []
    # 已有教训按词面近似去重：同一坑不反复提炼（confidence/文本近似即认为已记下）
    existing = mem_mod.load_lessons()

    def _dup(text: str) -> bool:
        t = terms(text)
        for l in existing + created:
            if jaccard(t, terms(l.get("text", ""))) >= 0.55:
                return True
        return False

    # 1) correction 改口记录：教训文本直接从改口原文提炼
    corrections = [m for m in records
                   if m.get("correction") and m.get("id") and m.get("text", "").strip()]
    for m in corrections[:max_lessons]:
        text = f"{m['text'].strip()}"
        if _dup(text):
            continue
        created.append(mem_mod.add_lesson(
            text, evidence=[m["id"]], confidence=0.4, status="pending"))
        if len(created) >= max_lessons:
            return created

    # 2) 被 supersede 的旧记忆：旧结论被新事实取代 → 教训=新结论可信、旧的不可信
    by_id = {m.get("id"): m for m in records if m.get("id")}
    for m in records:
        if len(created) >= max_lessons:
            break
        new_id = m.get("superseded_by")
        if not new_id or m.get("layer") == "dust":
            continue
        newer = by_id.get(new_id)
        if not newer or not str(newer.get("text", "")).strip():
            continue
        text = f"「{str(newer.get('text', '')).strip()[:120]}」已取代旧结论——别再按旧的说。"
        if _dup(text):
            continue
        created.append(mem_mod.add_lesson(
            text, evidence=[m["id"], new_id], confidence=0.4, status="pending"))

    # 3) 冲突对：带改口信号的那一侧是更新的事实，提炼成教训
    try:
        conflicts = mem_mod.detect_conflicts(limit=10)
    except Exception as e:
        log_event("dream", f"冲突扫描失败（跳过冲突类教训）：{e}", level="warn")
        conflicts = []
    for c in conflicts:
        if len(created) >= max_lessons:
            break
        # 时间新的那侧通常代表最新状态
        pair = sorted((c.get("a") or {}, c.get("b") or {}),
                      key=lambda x: _num(x.get("ts"), 0), reverse=True)
        newest = pair[0]
        newest_rec = by_id.get(newest.get("id"))
        text_src = (newest_rec or newest).get("text", "")
        if not str(text_src).strip():
            continue
        text = f"关于矛盾信息：以最新的说法为准——{str(text_src).strip()[:120]}"
        if _dup(text):
            continue
        ev = [x.get("id") for x in (c.get("a"), c.get("b")) if x.get("id")]
        created.append(mem_mod.add_lesson(
            text, evidence=ev, confidence=0.35, status="pending"))
    return created


def dream_priority() -> dict:
    """#47 按需做梦（SSGM 3.5）：不用固定闹钟，而是每次做梦前先量一遍"记忆库健康度"，
    决定今晚要重点做什么（巩固/淡忘/蒸馏/都不用做）。

    指标（全部只读，缺啥降啥）：
      - 未巩固新记忆数：hippo 里 consolidated 为假的 → 该巩固；
      - 待整理梦任务是否已悬着 → 该先消费旧任务而不是生成新的；
      - 工作记忆未完成件 → 该重放交接；
      - 召回使用率（#17 指标）：rate 明显偏低（<0.15 且 recalled≥20）说明召回
        塞了一堆没用的 → 该蒸馏/清理而不是继续囤；
      - 距上次做梦时长 → 太久强制至少走一遍淡忘/归档。
    返回 {"score": 0-1 健康分, "actions": [按优先级排序的行动], "reasons": {...}}
    """
    actions: list[str] = []
    reasons: dict = {}
    score = 1.0

    # 1) 未巩固的新记忆
    recs = _mem.load_all()
    now = now_ts()
    uncons = [m for m in recs if m.get("layer") == "hippo" and not m.get("consolidated")
              and not m.get("correction")]
    reasons["unconsolidated"] = len(uncons)
    if len(uncons) >= 5:
        actions.append("consolidate")
        score -= min(0.4, 0.05 * len(uncons))

    # 2) 旧梦任务悬置
    pending = load_dream_pending()
    if pending and pending.get("task_id"):
        age_h = (now - _num(pending.get("created"), now)) / 3600
        reasons["pending_task_hours"] = round(age_h, 1)
        if age_h >= 6:
            actions.append("consume_pending")
            score -= 0.2

    # 3) 工作记忆堆积
    try:
        ws = _mem.work_status()
        reasons["working_open"] = ws["open_count"]
        if ws["open_count"] >= 3:
            actions.append("replay_handoff")
            score -= 0.15
    except Exception:
        pass

    # 4) 召回使用率偏低 → 该蒸馏而非继续囤
    try:
        um = _mem.usage_metrics()
        reasons["recall_rate"] = um.get("rate", 0.0)
        if um.get("recalled", 0) >= 20 and um.get("rate", 1.0) < 0.15:
            actions.append("distill")
            score -= 0.25
    except Exception:
        pass

    # 5) 距上次做梦太久——淡忘/归档例行公事不能停
    last = _num(load_dream_state().get("last_dream"), 0)
    gap_h = (now - last) / 3600 if last else 9999
    reasons["hours_since_dream"] = round(gap_h, 1) if last else None
    if last and gap_h >= 24 * 3:
        actions.append("routine_forget")
        score -= 0.15

    # 6) 健康分还很高、也没攒够活 → 今晚可以不做
    if not actions and score >= 0.9:
        actions.append("skip")
        reasons["verdict"] = "记忆库健康，无需做梦"

    return {"score": round(max(0.0, min(1.0, score)), 2),
            "actions": actions or ["consolidate"], "reasons": reasons}


def _bounded_growth(records: list, now: int) -> list:
    """#40 记忆有界增长：非 dust 非归档条目超软上限（默认 3000）时，做梦流程里
    把超额部分按"低保留度 → 低重要度 → 越旧越先"排序雪藏进 archive 层
    （原文保真、永不删，可 archive restore 回热）——总量近似恒定，只压缩不删。

    软上限可用 config `dream.capacity_cap` 覆盖。返回本次被归档的记录列表。
    保护名单与 _is_archivable 一致：pinned / verbatim / identity / correction / pending。
    """
    try:
        cap = int(_num(load_config().get("dream.capacity_cap"), 3000))
    except Exception:
        cap = 3000
    cap = max(cap, 100)  # 防呆：设得太小也不至于清空热层
    hot = [m for m in records if m.get("layer") in ("hippo", "cortex")]
    over = len(hot) - cap
    if over <= 0:
        return []
    # 排序依据：保留度低（快忘了）> 重要度低 > 更旧。protection 复用 _is_archivable 的名单。
    def _rank(m):
        return (retention(m, now),
                -_num(m.get("importance"), 0.5),
                -_num(m.get("created"), 0))
    protected = {m.get("id") for m in hot
                 if m.get("pinned") or m.get("verbatim") or m.get("kind") == "identity"
                 or m.get("correction") or m.get("review") == "pending"}
    cands = sorted((m for m in hot if m.get("id") not in protected), key=_rank)
    picked = []
    for m in cands:
        if over <= 0:
            break
        if m.get("layer") == ARCHIVE_LAYER:
            continue
        picked.append(m)
        over -= 1
    if not picked:
        return []
    picked_ids = {m.get("id") for m in picked}
    for m in picked:
        m["layer"] = ARCHIVE_LAYER
        m["archived_at"] = now
    log_event("dream", f"#40 有界增长：热层超软上限，归档 {len(picked)} 条（cap={cap}）")
    return [m for m in records if m.get("id") in picked_ids]


def dream(trigger: str = "manual", dry_run: bool = False) -> dict:
    ensure_home()
    now = now_ts()
    report = {"trigger": trigger, "at": now, "forgotten": [], "promoted": [], "faded": 0,
              "archived": [], "clusters": []}

    # #47 按需做梦：先量健康度。分数高且没活可干时自动做梦触发直接跳过
    # （省 nightly 空转），手动触发照跑（人点了就要给个交代）。
    try:
        prio = dream_priority()
    except Exception as e:
        prio = None
        log_event("dream", f"健康度评估失败（照常做梦）：{e}", level="warn")
    if prio:
        report["priority"] = prio
        if trigger == "auto" and prio["actions"] == ["skip"]:
            report["skipped"] = True
            report["skip_reason"] = prio["reasons"].get("verdict", "无需做梦")
            log_event("dream", f"按需做梦跳过：{report['skip_reason']}")
            return report

    # 读-改-写整体加锁：dream 用 save_all 整库快照覆写，
    # 不加锁会把锁外并发写入的新记忆用旧快照整库覆盖丢。
    # dry_run 纯预览不写盘，就别去抢锁——只读记忆库（HOME 不可写）下抢锁会直接报错。
    lock_ctx = (nullcontext() if dry_run else _locked(HOME / "hippocampus.jsonl"))
    with lock_ctx:
        # 改库前先打快照（回滚点）：梦错了可以用 restore 还原，做梦本身不背"压错找不回"的锅。
        # fail-closed：快照失败（磁盘满/权限等）拿不到回滚点，宁可拒绝整理也不裸奔。
        if not dry_run:
            snap = _dream_snapshot()
            if not snap:
                raise RuntimeError(
                    "做梦前自动快照失败（没有回滚点），已中止整理；"
                    "请检查 dream-snapshots 目录可写、磁盘未满后再试")
            report["snapshot"] = snap
        records = load_all()

        # 重放→压缩：先快速眼动重放上次做梦后的经历，压成交接摘要落盘
        # （借鉴 dsh-handoff-compaction：结构化摘要 + 原文可 recall 追溯 + 不动原始数据）。
        # 失败不挡巩固——交接只是加分项，聚类/淡忘/晋升照常走。
        try:
            replay = replay_recent()
            report["replayed"] = len(replay)
            pol = compaction_policy()
            report["policy"] = pol["mode"]
            detail_floor = _num(pol.get("detail_floor"), 0.0)
            dust_shift = _num(pol.get("dust_retention_shift"), 0.0)
            if not dry_run and replay:
                hp = save_handoff(build_handoff(replay))
                report["handoff"] = hp.name
        except Exception as e:
            log_event("dream", f"重放/交接失败（不影响巩固）：{e}", level="warn")
            detail_floor, dust_shift = 0.0, 0.0

        archived_recs = []

        for m in records:
            if not m.get("id"):          # 缺 id 的畸形记录不参与巩固/遗忘（但仍原样保留在库）
                continue
            if m.get("layer") == "dust" or m.get("pinned"):
                continue
            # #41 correction 语义：改口记录永不自动淡忘/归档——它是教训提炼的第一手证据
            if m.get("correction"):
                continue
            text = m.get("text", "") or ""
            # 封存的原文不做细节淡化、不进淡忘区：它的价值就是"一个字都没丢"
            verbatim = bool(m.get("verbatim"))
            R = retention(m, now)
            # 冷热归档：久到想不起的沉眠记忆优先雪藏（原文保真、永不删、可 archive restore 回热），
            # 而不是直接模糊淡忘。放在淡忘判断之前，避免低重要沉眠记忆先被 dust 吃掉。
            if not verbatim and _is_archivable(m, now):
                m["layer"] = ARCHIVE_LAYER
                m["archived_at"] = now
                archived_recs.append(m)
                report["archived"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
                continue
            if (not verbatim and m.get("layer") == "hippo" and R < RETENTION_FLOOR - dust_shift
                    and _num(m.get("importance"), 0.5) < 0.6 and now - _num(m.get("created"), now) > 5 * DAY):
                # 淡忘判据曾经是"2 天 + 重要度<0.75"：做梦触发得勤了之后（daily 自动
                # 做梦、面板一键做梦），同一批记忆第一次被巩固处理就会大片进淡忘区，
                # 看起来像"新版算得严了"。放宽到"满 5 天 + 重要度<0.6"——普通闲聊
                # 碎片多留几天被想起的机会，真沉眠的才淡忘；想抓回来的仍有轮廓在 dust。
                m["layer"] = "dust"
                m["forgotten_at"] = now
                m["detail"] = round(max(0.12, _num(m.get("detail"), 1) * 0.4), 2)
                report["forgotten"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
                continue
            if (m.get("layer") == "hippo" and R >= 0.45
                    and (_num(m.get("access_count"), 0) >= 2 or _num(m.get("importance"), 0) >= 0.78)):
                m["layer"] = "cortex"
                m["consolidated"] = True
                report["promoted"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
            if verbatim:
                m["detail"] = 1.0  # 封存件永不模糊
            else:
                target = round(0.35 + 0.65 * R, 2)
                # 工程模式骨架压缩：detail 有下限，代码/报错类细节不被淡忘打得太薄
                target = max(target, detail_floor)
                if _num(m.get("detail"), 1) > target:
                    m["detail"] = target
                    report["faded"] += 1
            if not m.get("gist"):
                m["gist"] = heuristic_gist(text)

        # 冷热归档：长期没被想起、保留度极低的沉眠记忆雪藏进 archive 层
        # （原文保真、永不删，只是移出 recall 的热遍历集合）。顺带归档几乎零额外成本。
        if archived_recs:
            records = [m for m in records if m.get("layer") != ARCHIVE_LAYER]
            if not dry_run:
                _append_archive(archived_recs)

        # #43 唤醒值遗忘：对 hippo/cortex 里 activation 低于阈值的记忆做软处理
        # （降层 dust + 切关联 + 清 embedding；pinned/correction/identity 等保护名单
        # 在 activation_forget 内部把关）。失败不挡巩固——软遗忘是加分工序。
        try:
            act_forgotten = _mem.activation_forget(records, now)
            if act_forgotten:
                report["activation_forgotten"] = [
                    {"id": x["id"], "activation": round(_num(x.get("activation"), 0.0), 3),
                     "gist": x.get("gist") or heuristic_gist(x.get("text", "") or "")}
                    for x in act_forgotten]
        except Exception as e:
            log_event("dream", f"唤醒值遗忘失败（不影响巩固）：{e}", level="warn")

        # #40 有界增长：热层超软上限时把超额沉眠记忆雪藏进 archive（只压缩不删）。
        # 放在常规归档之后，用最新的层分布算超额；与 archived_recs 合并一次落盘。
        try:
            bounded = _bounded_growth(records, now)
        except Exception as e:
            bounded = []
            log_event("dream", f"#40 有界增长失败（不影响巩固）：{e}", level="warn")
        if bounded:
            report["bounded_archived"] = [
                {"id": m["id"], "gist": m.get("gist") or heuristic_gist(m.get("text", "") or "")}
                for m in bounded]
            if not dry_run:
                _append_archive(bounded)
            records = [m for m in records if m.get("layer") != ARCHIVE_LAYER]

        clusters = cluster_memories(records, include_ids={x["id"] for x in report["promoted"]})
        if not dry_run:
            save_all(records)

    report["clusters"] = clusters
    task_id = f"dream_{now}"
    report["task_id"] = task_id

    # —— 教训提炼（#36）：从失败闭环的证据源里挖教训候选 ——
    # 证据源三类：被 supersede 的旧记忆（说法变了=旧结论错过一次）、conflict 冲突记录
    # （detect_conflicts 启发式发现的矛盾对）、correction 改口记录（用户亲手纠正）。
    # 生成的教训全部 status="pending" 待审（与 knowledge/persona 同一审核闸），
    # review confirm 后转 active 才注入；绝不直接生效——梦是建议，不是判决。
    if not dry_run:
        try:
            mined = _mine_lessons(records)
            report["lessons_mined"] = len(mined)
        except Exception as e:
            log_event("dream", f"教训提炼失败（不影响巩固）：{e}", level="warn")
            mined = []

    # —— #44 月度漂移自检：做梦时顺手自省——人格基准冻结满 30 天就对比一次。
    # 失败不挡做梦（自检是加分工序）。
    if not dry_run:
        try:
            import persona as _persona
            ps = _persona.load_persona()
            last_check = _num(ps.get("drift_selfcheck", [{}])[-1].get("at"), 0) \
                if ps.get("drift_selfcheck") else 0
            if now - last_check >= 30 * 86400:
                report["drift_selfcheck"] = _persona.monthly_drift_selfcheck()
        except Exception as e:
            log_event("dream", f"月度漂移自检失败（不影响做梦）：{e}", level="warn")

    if not dry_run:
        if clusters:
            new_pending = {
                "task_id": task_id,
                "created": now,
                "trigger": trigger,
                "instructions": "把 clusters 里的碎片压缩成更抽象、更持久的长期记忆；提取人格变化；输出 apply.json 后执行 --apply。",
                "clusters": clusters,
                "report": {k: report[k] for k in ("forgotten", "promoted", "faded", "archived")},
                "apply_schema": {
                    "task_id": task_id,
                    "memories": [{"kind": "fact|preference|insight|habit|emotion",
                                  "text": "抽象后的长期记忆", "gist": "一句话要点",
                                  "importance": "0-1 或 0-10", "tags": [], "entities": [],
                                  "source_ids": ["m_..."]}],
                    "persona_updates": {"mood": {"value": "这次聊完的情绪", "reason": "..."},
                                        "traits": {"幽默": 2},
                                        "habits": [{"name": "深夜更容易说真话", "evidence": "..."}],
                                        "likes": [], "dislikes": [], "insights": []},
                    "forget": ["m_..."],
                    "narrative": "这次梦的叙述（可选）",
                },
            }
            # 幂等抢占：已有未消费的 pending 时不覆盖——旧任务的 task_id 已发给
            # AI（面板/注入都读 pending.json），静默顶掉会让旧 apply 全部对不上号，
            # 新任务也可能被晾着（借鉴 Mnemosyne 幂等键防重复情节、租约验证）。
            # 未超时 → 沿用旧任务；超时太久（孤儿）→ 归档后接新任务。
            old = load_dream_pending()
            if old and old.get("task_id"):
                age = now - _num(old.get("created"), now)
                if age < _DREAM_ORPHAN_TTL:
                    report["superseded_task_id"] = task_id
                    task_id = old["task_id"]
                    report["task_id"] = task_id
                    report["pending_kept"] = True
                    log_event("dream", f"已有未整理的梦（task_id={old['task_id']}，"
                              f"{age / 3600:.1f}h 前触发），本次不重复生成新任务", level="warn")
                else:
                    orphan_path = HOME / "dreams" / f"orphaned_{old['task_id']}.json"
                    atomic_json(orphan_path, old)
                    pending_path = HOME / "dreams" / "pending.json"
                    if pending_path.exists():
                        pending_path.unlink()
                    log_event("dream", f"过期梦任务已归档（task_id={old['task_id']}，"
                              f"悬置 {age / 3600:.1f}h > {_DREAM_ORPHAN_TTL / 3600:.0f}h），"
                              "生成新任务", level="warn")
                    report["orphaned_task_id"] = old["task_id"]
            atomic_json(HOME / "dreams" / "pending.json", new_pending)
        # 没有可整理的碎片就不写 pending.json，免得让 AI 对着一份空任务空转
        write_dream_report(report)
        st = load_dream_state()
        st["last_dream"] = now
        st["last_task"] = task_id if clusters else None
        atomic_json(HOME / "dream_state.json", st)

    return report


def write_dream_report(report: dict) -> Path:
    now = report["at"]
    base = f"{datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M%S')}"
    path = HOME / "dreams" / f"{base}.md"
    n = 1
    while path.exists():  # 同秒多次做梦时不要覆盖上一份
        n += 1
        path = HOME / "dreams" / f"{base}_{n}.md"
    lines = [f"# 梦境整理 · {fmt_time(now)}", "", f"触发：{report['trigger']}", ""]
    lines.append(f"- 重放片段：{sum(len(c['texts']) for c in report['clusters'])} 条 / {len(report['clusters'])} 组")
    lines.append(f"- 巩固入长期：{len(report['promoted'])} 条")
    lines.append(f"- 淡忘归档：{len(report['forgotten'])} 条")
    lines.append(f"- 雪藏冷档：{len(report.get('archived', []))} 条（原文保留，可 archive search / restore）")
    lines.append(f"- 模糊化细节：{report['faded']} 条")
    mined = report.get("lessons_mined", 0)
    if mined:
        lines.append(f"- 教训提炼：{mined} 条新教训候选（pending 待审，review 里确认后生效）")
    if report.get("snapshot"):
        lines.append(f"- 回滚点：{report['snapshot']}（梦错了可用 restore 还原）")
    if report["clusters"]:
        lines += ["", "## 重放的主题"]
        for c in report["clusters"]:
            tags = "、".join(c["tags"][:4]) or "、".join(c["entities"][:4]) or "无标签"
            lines.append(f"- {tags}（{len(c['texts'])} 条）")
    if report["forgotten"]:
        lines += ["", "## 淡忘的轮廓"]
        for f in report["forgotten"][:10]:
            lines.append(f"- {f['gist']}")
    write_text_atomic(path, "\n".join(lines) + "\n")
    return path


def load_dream_pending() -> dict | None:
    """当前待整理的梦任务（pending.json）；不存在/损坏返回 None。"""
    return _slurp_json(HOME / "dreams" / "pending.json", None)


def dream_draft_path() -> Path:
    return HOME / "dreams" / "apply_draft.json"


def load_dream_draft() -> dict | None:
    """上次在面板里编辑、还没应用的 apply.json 草稿。"""
    return _slurp_json(dream_draft_path(), None)


def save_dream_draft(obj) -> dict:
    """把 apply.json 草稿存盘（面板网页编辑→保存，避免模型合并出错时只能命令行改文件）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法保存草稿。")
    ensure_home()
    if not isinstance(obj, dict):
        raise ValueError("草稿必须是 JSON 对象")
    atomic_json(dream_draft_path(), obj)
    return {"ok": True, "path": str(dream_draft_path())}


def clear_dream_draft() -> None:
    try:
        dream_draft_path().unlink()
    except (FileNotFoundError, OSError):
        pass


def load_dream_state() -> dict:
    path = HOME / "dream_state.json"
    if path.exists():
        d = _slurp_json(path)
        if isinstance(d, dict):
            return d
    return {"last_dream": 0, "last_task": None}


VERIFY_SAMPLE_FILE = "verify-sample.json"


def write_verify_sample(pairs: list, task_id: str = "") -> dict | None:
    """#19 梦抽检：把本次合并的「合并前 → 合并后」抽 3 组落盘。

    AI 整理最容易在"把两条记忆揉成一条"时悄悄改掉原意，而合并结果默认是
    待审核状态——抽检就是给人一个**看差别**的短清单，不必翻整场梦的报告。
    写 dreams/verify-sample.json，`dream verify` 查看，`dream verify --done` 标记已看过。
    """
    if not pairs:
        return None
    # 均匀抽 3 组（首/中/尾），不引入随机数：同一场梦重复写入结果稳定
    n = len(pairs)
    picks = sorted({0, n // 2, n - 1})[:3]
    items = [pairs[i] for i in picks if i < n]
    data = {
        "task_id": task_id,
        "at": now_ts(),
        "reviewed": False,
        "reviewed_at": None,
        "total_merges": n,
        "items": items,
    }
    path = HOME / "dreams" / VERIFY_SAMPLE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, data)
    return data


def load_verify_sample() -> dict | None:
    """读取最近一次梦抽检样本；没有/坏文件返回 None。"""
    path = HOME / "dreams" / VERIFY_SAMPLE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def mark_verify_reviewed() -> dict | None:
    """标记抽检样本已看过（不删文件，保留对照供以后回看）。"""
    data = load_verify_sample()
    if not data:
        return None
    data["reviewed"] = True
    data["reviewed_at"] = now_ts()
    atomic_json(HOME / "dreams" / VERIFY_SAMPLE_FILE, data)
    return data


def apply_dream(result: dict) -> dict:
    ensure_home()
    pending_path = HOME / "dreams" / "pending.json"
    task_id = result.get("task_id")
    applied = {"memories": 0, "forgotten": 0, "persona": [], "rejected": []}

    # 读-校验-改-写-消费 pending 整体在一把锁里：
    # 1) 两个 apply 并发时，第二个进锁后 pending 已被删，直接拒绝，不会把同一份结果套两遍；
    # 2) 整库 save_all 不会把锁外并发写入的新记忆覆盖丢。
    with _locked(HOME / "hippocampus.jsonl"):
        # 先对任务号：apply 必须对着当前这场的 pending.json，防止把上一场的结果
        # 或乱写的 task_id 应用到现在的记忆上。不对就直接拒绝，不碰任何数据。
        pending_id = None
        if pending_path.exists():
            try:
                pending_id = (json.loads(pending_path.read_text("utf-8")) or {}).get("task_id")
            except Exception:
                pending_id = None
        if pending_id is None:
            raise ValueError("没有待应用的任务：dreams/pending.json 不存在")
        if not task_id:
            # 以前这里 fallback 到 pending_id，等于"缺 task_id 但也放行"——
            # 上一场漏写 task_id 的 apply.json 会静默套用到这一场。改为必须显式带上。
            raise ValueError(
                f"apply 里没有 task_id：为防止把上一场的整理结果套用到这一场，拒绝应用。"
                f"请在 apply.json 里带上 \"task_id\": \"{pending_id}\"。"
            )
        if task_id != pending_id:
            raise ValueError(
                f"任务对不上：apply 里的 task_id={task_id!r}，与 pending.json 的 {pending_id!r} 不一致，拒绝应用。"
            )
        records = load_all()
        by_id = {m["id"]: m for m in records if m.get("id")}
        _old_terms_cache = {}  # id → terms：锁内缓存旧记忆分词，多条新记忆不重复算
        _verify_pairs = []    # #19 合并前后对照：写入 dreams/verify-sample.json 供抽检

        # 写入时校验引用（借鉴图数据库 dangling edge 防线）：
        # AI 填的 source_ids 可能编造/抄错 id，老版本直接写进 links 就成了死链。
        # 现在落库前先对一遍现库，不存在的 id 丢弃并留痕。
        _known_ids = set(by_id)
        for item in result.get("memories") or []:
            if isinstance(item, dict) and item.get("source_ids"):
                bad = [sid for sid in item["source_ids"]
                       if isinstance(sid, str) and sid not in _known_ids]
                if bad:
                    applied["rejected"].append(
                        f"忽略 source_ids 中不存在的引用：{', '.join(bad)}")
                    item["source_ids"] = [s for s in item["source_ids"]
                                          if not (isinstance(s, str) and s in bad)]

        for item in result.get("memories") or []:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            rec = make_record(
                text,
                kind=item.get("kind", "fact"),
                importance=_num(item.get("importance"), 0.7),
                tags=item.get("tags"),
                entities=item.get("entities"),
                source="dream",
                layer="cortex",
                links=[x for x in (item.get("source_ids") or []) if isinstance(x, str)],
            )
            rec["gist"] = item.get("gist") or heuristic_gist(text)
            rec["consolidated"] = True
            rec["detail"] = 1.0
            # AI 整理出来的记忆先标"待审核"：防止做梦合并/编造的内容直接当事实沉淀。
            rec["review"] = "pending"
            rec["review_source"] = "dream"
            # #46 Memex 索引解引用：合并/蒸馏产物带 evidence_ids 指针（指向原始证据记忆），
            # 召回命中摘要时按需解引用取原文，"概括"与"细节"两级可导航。
            # 证据 = AI 声明的 source_ids + 确实被本条取代的旧记忆，宁可多指针不丢证据。
            _ev = [s for s in rec.get("links", []) if isinstance(s, str)]
            _ev += [old["id"] for old in records
                    if old.get("superseded_by") == rec["id"] and old.get("id")]
            if _ev:
                rec["evidence_ids"] = _dedupe(_ev)
            # #45 影响评估（SSGM）：合并/蒸馏决议附轻量 impact——这条记忆一旦确认，
            # 会改变多少召回注入文本、波及哪些实体，让用户优先审"影响大"的改动。
            rec["impact"] = {
                "chars": len(text),                      # 确认后进入注入文本的字数
                "entities": list(rec.get("entities", []) or []),
                "superseded": sum(1 for old in records
                                  if old.get("superseded_by") == rec["id"]),
            }
            # —— 矛盾记忆不覆盖，留链路 ——
            # 新记忆与旧记忆高度相似但内容不同（说法变了/事实更新）时，旧的不删，
            # 打上 superseded_by 指向新的：回忆默认跳过被取代者，需要时仍可回溯
            # "我以前是怎么想的"——像人一样，观念变了但旧观念还在记忆深处。
            # 大库时锁内 O(n²) 比对会堵死其他写进程：先用词交集粗筛出候选
            # （jaccard ≥0.55 必有足够交集），再只对候选做精比对。
            new_terms = terms(text)
            _cand_threshold = len(new_terms) * 0.55
            for old in records:
                # rec 是本函数刚新建的记录，不在 records 里，无需 is 判断
                if old.get("layer") == "dust" or old.get("superseded_by"):
                    continue
                # #28 冻结的记忆不许被做梦改写：用户明确要求"这条别动"
                if old.get("frozen"):
                    continue
                # #41：correction 改口记录不许被做梦产出 supersede——它是最新的用户更正，
                # 被取代等于把刚纠正过的事实又改回去
                if old.get("correction"):
                    continue
                old_terms = _old_terms_cache.get(old["id"])
                if old_terms is None:
                    old_terms = terms(str(old.get("text", "")))
                    _old_terms_cache[old["id"]] = old_terms
                if not old_terms:
                    continue
                # 粗筛：交集元素数 < 0.55×|new| 时 jaccard 必然 < 0.55，直接跳过
                if len(new_terms & old_terms) < _cand_threshold:
                    continue
                if jaccard(new_terms, old_terms) >= 0.55 and str(old.get("text", "")).strip() != text:
                    old["superseded_by"] = rec["id"]
                    rec.setdefault("links", [])
                    if old["id"] not in rec["links"]:
                        rec["links"].append(old["id"])
                    applied.setdefault("superseded", []).append(old["id"])
                    # #19 留一组"合并前 → 合并后"对照，供事后抽检 AI 有没有改坏原意
                    _verify_pairs.append({
                        "new_id": rec["id"],
                        "after": text,
                        "before": [{"id": old["id"], "text": str(old.get("text", ""))}],
                    })
            records.append(rec)
            applied["memories"] += 1
            for sid in rec["links"]:
                src = by_id.get(sid)
                if src:
                    src["consolidated"] = True
                    src["links"] = _dedupe(src.get("links", []) + [rec["id"]])
                    if src.get("layer") == "hippo" and src.get("importance", 0.5) >= 0.5:
                        src["layer"] = "cortex"

        _forget_n = 0
        for sid in result.get("forget") or []:
            m = by_id.get(sid)
            if m and m.get("layer") != "dust":
                # pinned 与 dream 主循环同一待遇：用户置顶的不许 AI 梦里淡忘——
                # 否则 apply.json 里给个 pinned id 就能绕过防线
                if m.get("pinned"):
                    applied["rejected"].append(f"拒绝淡忘 {sid}：用户置顶的记忆不参与做梦淡忘")
                    continue
                # #28 冻结的记忆同样不参与淡忘
                if m.get("frozen"):
                    applied["rejected"].append(f"拒绝淡忘 {sid}：已冻结的记忆不参与做梦淡忘")
                    continue
                # #41：correction 改口记录不许梦淡忘——教训提炼的证据链不能断
                if m.get("correction"):
                    applied["rejected"].append(f"拒绝淡忘 {sid}：改口（correction）记录永不自动淡忘")
                    continue
                m["layer"] = "dust"
                m["forgotten_at"] = now_ts()
                applied["forgotten"] += 1
                _forget_n += 1
        if _forget_n:
            # 留痕：淡忘是"移入 dust 层"而非删除，但日志记一笔数量，方便审计核对没丢数据
            log_event("dream", f"本次做梦淡忘 {_forget_n} 条记忆（已移入 dust 淡忘区，可 revive 找回）")

        save_all(records)
        # #19 抽检样本：落盘 3 组合并前后对照（失败不影响做梦主流程）
        try:
            write_verify_sample(_verify_pairs, task_id)
        except Exception as exc:
            log_event("dream", f"写抽检样本失败（不影响巩固）：{exc}", level="warn")
        # 人格更新也放在锁内、且必须在"删 pending"之前：AI 产的 persona_updates 可能带 NaN
        # 等坏值，若先删 pending 再更新人格，人格段一崩这批整理就永远无法重试（pending 已丢）。
        upd = result.get("persona_updates")
        upd = upd if isinstance(upd, dict) else {}
        if upd:
            evid = [sid for it in (result.get("memories") or [])
                    if isinstance(it, dict) for sid in (it.get("source_ids") or [])
                    if isinstance(sid, str)][:10]
            # 成形时刻检测：近 14 天内若有峰值情绪（formative=True）事件，本场梦的
            # traits 漂移走旁路（×2.5 封顶 ±25，绕过双门限）——一次真正的峰值情绪
            # 本来就足以重塑人，不该被"攒账"机制压制。事件按 ts 倒序，取第一条。
            formative_ev = None
            try:
                import emotion as _emo
                fe = _emo.formative_events(days=14)
                formative_ev = fe[0] if fe else None
            except Exception as exc:
                log_event("dream", f"读取成形时刻事件失败，按普通漂移处理：{exc}", level="warn")
            if formative_ev and upd.get("traits"):
                boosted = {}
                for k, v in upd["traits"].items():
                    try:
                        boosted[k] = max(-10, min(10, float(v) * 2.5))
                    except (TypeError, ValueError):
                        log_event("dream", f"成形旁路时 traits.{k} 值非法 {v!r}，跳过", level="warn")
                if boosted:
                    upd = {**upd, "traits": boosted}
                    fwhy = (formative_ev.get("why") or formative_ev.get("about") or "").strip()
                    fdesc = f"{formative_ev.get('emotion') or '强烈的情绪'}" + (f"（{fwhy}）" if fwhy else "")
                    try:
                        applied["rejected"] = update_persona(
                            upd, why="做梦整理", source="dream_formative", evidence=evid,
                            bypass_gate=True, formative_event=fdesc)
                    except Exception as exc:
                        log_event("dream", f"做梦应用时人格更新失败，pending 已保留可重试：{exc}", level="error")
                        raise
                else:
                    try:
                        applied["rejected"] = update_persona(upd, why="做梦整理", source="dream", evidence=evid)
                    except Exception as exc:
                        log_event("dream", f"做梦应用时人格更新失败，pending 已保留可重试：{exc}", level="error")
                        raise
            else:
                try:
                    applied["rejected"] = update_persona(upd, why="做梦整理", source="dream", evidence=evid)
                except Exception as exc:
                    # 人格更新失败：保留 pending apply 文件让整场可重试（persona 层有锁与校验，
                    # 正常情况下不该崩，这里兜底防 NaN 之外的意外把数据搞丢）
                    log_event("dream", f"做梦应用时人格更新失败，pending 已保留可重试：{exc}", level="error")
                    raise
        # 在锁内消费 pending：写入与"这场梦已应用"必须同时成立，
        # 否则并发 apply / 崩溃重试会把这批记忆再套一遍（重复沉淀）。
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass

    narrative = (result.get("narrative") or "").strip()
    if narrative or applied["memories"]:
        path = HOME / "dreams" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_apply.md"
        body = [f"# 梦的沉淀 · {fmt_time(now_ts())}", f"任务：{task_id}", "",
                f"新增长期记忆 {applied['memories']} 条，遗忘 {applied['forgotten']} 条。"]
        if narrative:
            body += ["", "## 梦里的叙述", narrative]
        if upd.get("insights"):
            body += ["", "## 洞见"] + [f"- {x}" for x in upd["insights"]]
        rej = applied.get("rejected") or []
        if rej:
            # 被双门限/锁定/不存在拦下的改动留痕在梦报告里——AI 与人都能看到
            # "梦想改什么、防线拦了什么"，而不是只看到悄悄没生效
            body += ["", "## 被防线拦下的改动"] + [f"- {x}" for x in rej]
        try:
            write_text_atomic(path, "\n".join(body) + "\n")
        except OSError:
            pass   # 梦境笔记写不进去不影响记忆已落盘，别把成功报成失败
    # 自认识沉淀：放在 _locked 块之外（autosink 自己加 persona.json 锁，
    # 嵌套调用同一文件锁会死锁）；失败只记日志，不影响梦已落盘。
    try:
        applied["self_model"] = self_model_autosink()
    except Exception as exc:
        log_event("dream", f"做梦后自认识沉淀失败（不影响梦）：{exc}", level="warn")
        applied["self_model"] = {"ok": False, "error": str(exc)}
    clear_dream_draft()  # 应用成功后，网页里编辑过的那份草稿也一并清掉
    return applied
