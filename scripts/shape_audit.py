# -*- coding: utf-8 -*-
"""六爻位体检（shape-audit）：把一条记忆的六个二元特征当"六爻"，映射到 64 格，
用「当位 / 得中 / 有应」三组结构谓词判它合不合理。

**纯只读**：只扫描、只输出，绝不写任何文件。

## 为什么这么映射（不是玄学，是六个真问题）

| 爻位 | 特征 | 0 | 1 |
|---|---|---|---|
| 初 | 来源 | ai / 派生 | user / 亲历 |
| 二 | 审核 | pending 等 | confirmed |
| 三 | 层 | dust / archive | hippo / cortex |
| 四 | 类型 | 一次性 episode | 稳定 fact/preference/norm |
| 五 | 置信 | < 0.6 | ≥ 0.6 |
| 上 | 时效 | 已过 review_by | 未过期 / 无期限 |

## 三组谓词（易经里本来就有的约束系统）

- **当位**：阳（1）居奇位（初/三/五）、阴（0）居偶位（二/四/上）
- **得中**：居第二或第五爻
- **有应**：初↔四、二↔五、三↔上 三对，阴阳相反为有应

违反这些谓词，往往正是**真实的坏数据**：
- 初=1（用户亲历）但 二=0（待审）→ 不当位（v3.80 修过的 bug 就是这个）
- 五=1（高置信）但 三=0（在 dust）→ 不当位：高置信不该沉在淡忘区
- 二=1（已确认）但 初=0（AI 派生）且 五=0（低置信）→ 失应：最易沉淀幻觉的组合
"""
from __future__ import annotations

import json
from collections import Counter

import core
import memory as mem

STABLE_KINDS = {"fact", "preference", "norm", "intent", "relationship", "insight"}
HOT_LAYERS = {"hippo", "cortex"}

POSITION_NAMES = ["初", "二", "三", "四", "五", "上"]


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def signature(rec: dict) -> int:
    """六爻 → 6 位整数（初爻是最低位）。"""
    bits = []
    src = str(rec.get("source") or "")
    bits.append(1 if src in ("user", "checkpoint") else 0)          # 初：来源
    bits.append(1 if str(rec.get("review") or "") == "confirmed" else 0)  # 二：审核
    bits.append(1 if str(rec.get("layer") or "") in HOT_LAYERS else 0)    # 三：层
    bits.append(1 if str(rec.get("kind") or "") in STABLE_KINDS else 0)   # 四：类型
    # 五：置信。**不能用 importance 当代理**——做梦产物根本没有 confidence 字段，
    # 它的 importance 是默认值（0.45），当成"低置信"会批量误判（实测 13 条全是假阳性）。
    # 规则：有 confidence 就用它；没有就按"来源是否亲历"判断，只有明确 AI 派生
    # 且 importance 也偏低才算低置信。
    conf = rec.get("confidence")
    if conf is None:
        src_l = str(rec.get("source") or "")
        imp = _num(rec.get("importance"), 0.5)
        conf_hi = 1 if (src_l in ("user", "checkpoint") or imp >= 0.6) else 0
    else:
        conf_hi = 1 if _num(conf, 0.5) >= 0.6 else 0
    bits.append(conf_hi)                                            # 五：置信
    rb = rec.get("review_by")
    expired = bool(rb) and _num(rb, 0) > 0 and _num(rb, 0) < core.now_ts()
    bits.append(0 if expired else 1)                                # 上：时效
    val = 0
    for i, b in enumerate(bits):
        if b:
            val |= (1 << i)
    return val


def analyse(rec: dict) -> dict:
    """该记录的结构分析。

    ⚠ **实测结论（2026-11-24，真库 306 条）**：易经里字面的三组谓词——
    「当位」（阳居奇位/阴居偶位）、「得中」、「有应」——在真实记忆数据上
    **几乎不区分**：不当位 100%、失应 74%、未得中 75%。原因是这六位特征
    并不是为"交替排列"设计的，硬套字面规则等于给每条记录都判一次违规，
    也就等于什么都没说（这正是"伪精度"，必须诚实标出来）。

    所以这里保留真正有用的部分——**64 格状态分布**（能看出数据集中在哪、
    有哪些罕见组合）——而把约束换成三条**有领域含义的不变量**。
    """
    sig = signature(rec)
    bits = [(sig >> i) & 1 for i in range(6)]
    # bits: [初=来源, 二=审核, 三=层, 四=类型, 五=置信, 上=时效]
    src_user, confirmed, hot, stable, conf_hi, fresh = bits

    issues = []
    # ① 幻觉风险：AI 派生 + 已确认 + 低置信
    if (not src_user) and confirmed and (not conf_hi):
        issues.append("AI 派生却已确认且低置信（最易沉淀幻觉）")
    # ② 高价值沉底：高置信 + 在淡忘/归档层
    if conf_hi and not hot:
        issues.append("高置信却在淡忘/归档层（可能被误淡忘，可 memory boost）")
    # ③ 稳定事实过期：稳定类型 + 已过 review_by
    if stable and not fresh:
        issues.append("稳定事实已过期未复核（应 review-list 处理）")

    return {"sig": sig, "bits": bits, "issues": issues, "clean": not issues}


def audit(limit: int = 2000) -> dict:
    """扫描记忆库，输出 64 格分布 + 不变量违规清单。**只读**。"""
    try:
        records = mem.load_all()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取记忆库失败：{exc}"}
    # 做梦聚合条目是已知噪音（派生视图，无独立信息），不计入体检——
    # 否则它们会占满"幻觉风险"桶，把真问题淹掉（实测 13/13 全是这类）。
    records = [r for r in records
               if isinstance(r, dict) and r.get("id")
               and not mem._is_dream_aggregate(r.get("text", ""))][:limit]
    skipped_agg = sum(1 for r in (mem.load_all() or [])
                      if isinstance(r, dict) and r.get("id")
                      and mem._is_dream_aggregate(r.get("text", "")))

    cells = Counter()
    buckets = {"hallucination": [], "sunk": [], "stale": []}
    clean = 0
    for r in records:
        a = analyse(r)
        cells[a["sig"]] += 1
        if a["clean"]:
            clean += 1
            continue
        brief = {"id": r.get("id"), "text": str(r.get("text") or "")[:60],
                 "layer": r.get("layer"), "source": r.get("source"),
                 "kind": r.get("kind"), "review": r.get("review"),
                 "importance": r.get("importance"), "sig": a["sig"]}
        for msg in a["issues"]:
            if msg.startswith("AI 派生"):
                buckets["hallucination"].append(brief)
            elif msg.startswith("高置信"):
                buckets["sunk"].append(brief)
            else:
                buckets["stale"].append(brief)

    return {
        "total": len(records),
        "cells": dict(sorted(cells.items())),
        "distinct_cells": len(cells),
        "clean": clean,
        "hallucination": len(buckets["hallucination"]),
        "sunk": len(buckets["sunk"]),
        "stale": len(buckets["stale"]),
        "samples": {k: v[:8] for k, v in buckets.items()},
        "skipped_aggregates": skipped_agg,
        "note": ("字面「当位/得中/有应」在本数据上无区分度（实测 100%/75%/74%），"
                 "已改用三条领域不变量；做梦聚合条目已跳过（它们是已知噪音）"),
    }


def render(rep: dict, top: int = 12) -> str:
    """人看的文本报告。"""
    if rep.get("error"):
        return f"[错误] {rep['error']}"
    total = rep["total"]
    skip = rep.get("skipped_aggregates") or 0
    extra = f"；已跳过 {skip} 条做梦聚合条目（已知噪音）" if skip else ""
    lines = [f"六爻位体检：共 {total} 条记忆，落在 {rep['distinct_cells']}/64 格"
             f"（结构干净 {rep['clean']} 条{extra}）", ""]
    lines.append("领域不变量违规（这些是真问题，不是玄学）：")
    for key, label in (("hallucination", "① AI 派生+已确认+低置信（幻觉风险）"),
                       ("sunk", "② 高置信却在淡忘/归档层（可能被误淡忘）"),
                       ("stale", "③ 稳定事实已过期未复核")):
        n = rep.get(key) or 0
        pct = (n * 100 / total) if total else 0
        lines.append(f"  {label}：{n} 条（{pct:.0f}%）")

    cells = rep.get("cells") or {}
    if cells:
        lines.append("")
        lines.append(f"最密集的 {top} 格（卦号 = 六位二进制，初爻为最低位）：")
        for sig, n in sorted(cells.items(), key=lambda kv: -kv[1])[:top]:
            bits = "".join(str((sig >> i) & 1) for i in range(5, -1, -1))
            lines.append(f"  卦 {sig:>2} [{bits}]  {n} 条")

    for key, title in (("hallucination", "幻觉风险样例"),
                       ("sunk", "高置信沉底样例"),
                       ("stale", "过期稳定事实样例")):
        rows = (rep.get("samples") or {}).get(key) or []
        if not rows:
            continue
        lines.append("")
        lines.append(f"{title}：")
        for b in rows[:6]:
            lines.append(f"  {b['id']} · 层={b['layer']} 源={b['source']} "
                         f"类={b['kind']} 审={b['review']} · {b['text']}")
    if rep.get("note"):
        lines.append("")
        lines.append(f"注：{rep['note']}")
    return "\n".join(lines)
