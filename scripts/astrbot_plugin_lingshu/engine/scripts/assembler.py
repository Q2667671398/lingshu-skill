#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文五段式装配器（附录 C · P1）。

把"这一轮该带什么"显式写成五段，并按预算裁剪：
    stable   稳定系统层（人格/规则/技能，放最前，吃缓存）
    summary  会话摘要层（结构化要点）
    retrieved 检索片段（可裁）
    recent   最近窗口（原文，保底）
    current  本轮输入

`plan()` 是纯函数：不读盘、不调模型、不发网络。裁剪顺序照附录 C：
retrieved → summary → recent；stable 与 current 只在超限时告警，不静默砍。
输出里的 tokens 是**估算**（默认 2 字符 ≈ 1 token），用于预算决策，不是计费口径。
"""
from __future__ import annotations

import math

SECTIONS = ("stable", "summary", "retrieved", "recent", "current")
# 各段默认占比（相对总预算）；未列出的段不设硬上限，只受总预算约束
DEFAULT_RATIOS = {
    "stable": 0.10,
    "summary": 0.04,
    "retrieved": 0.20,
    "recent": 0.15,
    "current": 0.05,
}
DEGRADE_ORDER = ("retrieved", "summary", "recent")
MIN_KEEP_TOKENS = {"retrieved": 0, "summary": 0, "recent": 200}


def estimate_tokens(text: str, chars_per_token: int = 2) -> int:
    """粗估 token：中文字符约 2 字/token。只用于预算，不当计费。"""
    ratio = chars_per_token if isinstance(chars_per_token, int) and chars_per_token > 0 else 2
    return int(math.ceil(len(text or "") / ratio))


def _trim_to_tokens(text: str, target_tokens: int, chars_per_token: int) -> str:
    """按 token 目标截断文本（保留头部 + 省略号）。"""
    if target_tokens <= 0:
        return ""
    limit = target_tokens * chars_per_token
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    return text[:limit - 1].rstrip() + "…"


def plan(parts: dict, budget: int = 40000, *, ratios: dict | None = None,
         output_reserve: float = 0.25, max_output_tokens: int | None = None,
         chars_per_token: int = 2) -> dict:
    """按预算排一版五段装配计划。

    parts 里缺的段按空串处理；返回：
      {blocks:[{name,chars,tokens,cap_tokens,trimmed_chars}],
       total_tokens, budget, usable_tokens, reserve_tokens,
       actions:[...], warnings:[...]}
    """
    budget = max(1, int(budget))
    ratios = {**DEFAULT_RATIOS, **(ratios or {})}
    # 输出预留：min(max_tokens, 25% 窗口)，与附录 C 的 outputHeadroomMaxPct 一致
    if max_output_tokens:
        reserve = min(int(max_output_tokens), int(budget * output_reserve))
    else:
        reserve = int(budget * output_reserve)
    usable = max(1, budget - reserve)

    texts = {name: str(parts.get(name) or "") for name in SECTIONS}
    # 段占比只对"窗口级预算"有意义：2400 这种小预算下，10% 的 stable 上限
    # 比人格本身还小，天天告警等于没告警。小预算只按总量压。
    use_caps = budget >= 8000
    caps = ({name: max(0, int(budget * ratios[name])) for name in SECTIONS if name in ratios}
            if use_caps else {})

    actions: list[str] = []
    warnings: list[str] = []

    def total_tokens() -> int:
        return sum(estimate_tokens(texts[n], chars_per_token) for n in SECTIONS)

    # 1) 先按段上限裁（只裁可裁的三段；stable/current 超限只告警）
    for name in DEGRADE_ORDER:
        cap = caps.get(name)
        if cap is None:
            continue
        tk = estimate_tokens(texts[name], chars_per_token)
        if tk > cap:
            texts[name] = _trim_to_tokens(texts[name], cap, chars_per_token)
            actions.append(f"{name}: 按段上限 {cap} tokens 裁剪")
    for name in ("stable", "current"):
        cap = caps.get(name)
        if cap and estimate_tokens(texts[name], chars_per_token) > cap:
            warnings.append(f"{name} 超过建议上限 {cap} tokens（不自动裁，属配置问题）")

    # 2) 总量仍超 usable → 按 retrieved → summary → recent 继续压
    for name in DEGRADE_ORDER:
        if total_tokens() <= usable:
            break
        over = total_tokens() - usable
        tk = estimate_tokens(texts[name], chars_per_token)
        floor = MIN_KEEP_TOKENS.get(name, 0)
        target = max(floor, tk - over - 1)
        if target >= tk:
            continue
        texts[name] = _trim_to_tokens(texts[name], target, chars_per_token)
        actions.append(f"{name}: 为压回总预算裁剪 {tk - target} tokens")

    blocks = []
    for name in SECTIONS:
        blocks.append({
            "name": name,
            "chars": len(texts[name]),
            "tokens": estimate_tokens(texts[name], chars_per_token),
            "cap_tokens": caps.get(name),
            "trimmed_chars": len(str(parts.get(name) or "")) - len(texts[name]),
        })
    total = sum(b["tokens"] for b in blocks)
    if total > usable:
        warnings.append(f"总预算仍超限：{total} > {usable}（stable/current 不可裁，需调预算或拆输入）")
    return {
        "blocks": blocks,
        "total_tokens": total,
        "budget": budget,
        "usable_tokens": usable,
        "reserve_tokens": reserve,
        "chars_per_token": chars_per_token,
        "actions": actions,
        "warnings": warnings,
    }


def plan_from_report(report: dict, budget: int = 40000, **kwargs) -> dict:
    """把 memory.build_injection(report=...) 的分段映射成五段计划。

    映射（附录 C 的落地近似）：
      stable    = persona + rules + skills
      summary   = topics + mistakes + relationships
      retrieved = related
      recent    = recent + other（梦结论等历史块）
      current   = dynamic（会话实时状态；真正的"本轮输入"由宿主提供，这里只做占位）
    """
    secs = {s.get("name"): int(s.get("chars") or 0) for s in (report.get("sections") or [])}
    parts = {
        "stable": "x" * (secs.get("persona", 0) + secs.get("rules", 0) + secs.get("skills", 0)),
        "summary": "x" * (secs.get("topics", 0) + secs.get("mistakes", 0)
                          + secs.get("relationships", 0)),
        "retrieved": "x" * secs.get("related", 0),
        "recent": "x" * (secs.get("recent", 0) + secs.get("other", 0) + secs.get("water", 0)),
        "current": "x" * secs.get("dynamic", 0),
    }
    ratio = int(report.get("chars_per_token") or 2)
    out = plan(parts, budget=budget, chars_per_token=ratio, **kwargs)
    out["source"] = "injection-report"
    out["source_chars"] = report.get("total_chars")
    return out
