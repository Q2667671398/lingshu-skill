#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 灰度 A/B 采样记账（附录 C P4 的最小骨架）。

背景：五段式装配（assembler.plan_from_report）是"预算裁剪版"，与现有整段注入
（legacy）并存。要判断灰度到底省了多少，必须有可比数据：同一份注入包在两种装配下
各占多少 token、省了多少比例。本模块只做三件事：

  record(mode, report, plan)  记一次装配对比：默认关闭；开启后按 sample 采样落盘
  summary(days)               聚合近 N 天样本：样本数、均值/p50/p95 节省比例、按 mode 分组
  context_mode(cfg)           读 config.context_mode（legacy / budgeted），默认 legacy

落盘：~/.brain-memory/metrics.jsonl（与每日 metrics 同文件，用 kind="context_ab" 区分），
单文件超过 512KB 自动轮转为 metrics.jsonl.1（思路同 failures.py 的 512KB 轮转）。

约定：
  - 默认 context_ab.enabled=false → 连对比都不算、一个字节都不写，装配路径行为不变；
  - record 本身绝不抛异常（观测代码不能成为故障源），失败返回 None；
  - context_ab.log=false 时只算不落盘（给未来的面板实时对比用）；
  - summary 只读、只聚合 A/B 行，不碰每日 metrics 行。
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

import assembler

MAX_BYTES = 512 * 1024          # 单文件上限，超了轮转成 metrics.jsonl.1
METRICS_NAME = "metrics.jsonl"
KIND = "context_ab"             # 与每日 metrics 行区分的标记字段
AB_DEFAULTS = {"enabled": False, "sample": 0.1, "log": True}
VALID_MODES = ("legacy", "budgeted")


# ---------------------------------------------------------------- 路径 / 配置

def _home(home=None) -> Path:
    if home:
        return Path(home)
    try:
        import core
        return Path(core.HOME)
    except Exception:
        return Path.home() / ".brain-memory"


def path(home=None) -> Path:
    """A/B 记账落盘路径（默认跟着 core.HOME，测试/--home 可显式指定）。"""
    return _home(home) / METRICS_NAME


def _ab_cfg(cfg: dict | None = None) -> dict:
    """取 context_ab 配置，老 config / 半截配置都按默认值补齐；坏值静默回退。"""
    out = dict(AB_DEFAULTS)
    try:
        if cfg is None:
            import core
            cfg = core.load_config()
        raw = (cfg or {}).get("context_ab")
        if isinstance(raw, dict):
            out.update({k: raw[k] for k in AB_DEFAULTS if k in raw})
    except Exception:
        pass
    out["enabled"] = bool(out.get("enabled"))
    out["log"] = bool(out.get("log", True))
    try:
        out["sample"] = min(1.0, max(0.0, float(out.get("sample", 0.1))))
    except (TypeError, ValueError):
        out["sample"] = 0.1
    return out


def context_mode(cfg: dict | None = None) -> str:
    """当前装配模式（config.context_mode）：legacy（默认）/ budgeted；非法值回退 legacy。

    灰度期只是把开关读出来；真正按模式装配由后续调用方决定（本任务不改变默认路径）。
    """
    try:
        if cfg is None:
            import core
            cfg = core.load_config()
        mode = str((cfg or {}).get("context_mode") or "legacy").strip().lower()
    except Exception:
        mode = "legacy"
    return mode if mode in VALID_MODES else "legacy"


# ---------------------------------------------------------------- 写记账

def _row(mode: str, report: dict, plan: dict, session: str) -> dict:
    """把一次装配的 report + plan 压成一行记账（数字全部来自 compare / plan）。"""
    budget = plan.get("budget") or report.get("budget")
    cmp = assembler.compare(report, budget)
    legacy_tokens = int(report.get("total_tokens") or 0)
    if legacy_tokens <= 0:          # 手工构造的 report 可能没填 tokens：按字符/比例兜底
        chars = int(cmp.get("legacy_chars") or 0)
        try:
            ratio = float(report.get("chars_per_token") or 2) or 2.0
        except (TypeError, ValueError):
            ratio = 2.0
        legacy_tokens = int(math.ceil(chars / ratio))
    return {
        "kind": KIND,
        "ts": int(time.time()),
        "session": str(session or "default")[:80],
        "mode": str(mode or "legacy")[:20],
        "legacy_tokens": legacy_tokens,
        "budgeted_tokens": int(plan.get("total_tokens") or 0),
        "saved_pct": float(cmp.get("saved_pct") or 0.0),
        # 下面三个是排障用的附加字段（spec 要求的 6 个字段之外）
        "legacy_chars": int(cmp.get("legacy_chars") or 0),
        "budgeted_chars": int(cmp.get("budgeted_chars") or 0),
        "degraded": list(cmp.get("degraded") or []),
    }


def _append(row: dict, home=None) -> None:
    """追加一行 JSONL；超 512KB 先轮转（思路同 failures.record），写锁复用 core。"""
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and p.stat().st_size > MAX_BYTES:
            p.replace(p.with_name(p.name + ".1"))
    except OSError:
        pass  # 轮转失败不拦记账：顶多文件继续长大
    try:
        import core
        core._append_jsonl(p, row)
        return
    except Exception:
        pass  # core 不可用（脱离灵枢环境 import）时退回裸追加
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record(mode: str, report: dict, plan: dict, *, session: str = "default",
           home=None, cfg: dict | None = None, force: bool = False) -> dict | None:
    """按采样率记一次装配对比，返回记下的行（log=false 时只算不写）；未采样/关闭返回 None。

    参数：
      mode    "legacy" | "budgeted"（本次装配实际用的模式）
      report  memory.build_injection(report=...) 填好的分段账单
      plan    assembler.plan(...) / plan_from_report(...) 的装配计划
      force   True 时跳过采样（测试/手动补记），仍受 enabled 开关约束
    任何异常都不外抛：观测账本不能把正常装配带崩。
    """
    try:
        if not isinstance(report, dict) or not isinstance(plan, dict):
            return None
        c = _ab_cfg(cfg)
        if not c["enabled"]:
            return None
        if not force and random.random() >= c["sample"]:
            return None
        row = _row(mode, report, plan, session)
        if c["log"]:
            _append(row, home)
        return row
    except Exception:
        return None


# ---------------------------------------------------------------- 读聚合

def _read(home=None) -> list[dict]:
    """读回轮转前后两个文件的所有合法 JSON 行。"""
    p = path(home)
    out: list[dict] = []
    for f in (p.with_name(p.name + ".1"), p):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _is_ab_row(r: dict) -> bool:
    """过滤出 A/B 行：优先认 kind 标记，兼容手写/旧行时按字段形状识别。"""
    if not isinstance(r, dict):
        return False
    if r.get("kind") == KIND:
        return True
    return ("mode" in r and "legacy_tokens" in r and "saved_pct" in r
            and "date" not in r)


def percentile(values, q: float):
    """线性插值分位数（与 numpy 默认口径一致）；空列表返回 None。"""
    vals = sorted(float(v) for v in (values or []))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 2)
    q = min(100.0, max(0.0, float(q)))
    pos = (len(vals) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    val = vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)
    return round(val, 2)


def _stats(rows: list[dict]) -> dict:
    """一组样本的节省比例 / token 统计。"""
    pcts = [float(r.get("saved_pct") or 0.0) for r in rows]
    lts = [float(r.get("legacy_tokens") or 0.0) for r in rows]
    bts = [float(r.get("budgeted_tokens") or 0.0) for r in rows]
    n = len(rows)
    return {
        "count": n,
        "mean_saved_pct": round(sum(pcts) / n, 2) if n else None,
        "p50_saved_pct": percentile(pcts, 50),
        "p95_saved_pct": percentile(pcts, 95),
        "mean_legacy_tokens": int(round(sum(lts) / n)) if n else None,
        "mean_budgeted_tokens": int(round(sum(bts) / n)) if n else None,
    }


def summary(days: float = 7, *, home=None, now: float | None = None) -> dict:
    """聚合近 days 天的 A/B 样本：样本数、均值/p50/p95 节省比例、按 mode 分组。

    只读：不建目录、不写盘；没有样本时返回 total=0 的空壳（各统计量为 None）。
    """
    now = time.time() if now is None else float(now)
    try:
        window = max(0.0, float(days))
    except (TypeError, ValueError):
        window = 7.0
    since = now - window * 86400
    rows: list[dict] = []
    for r in _read(home):
        if not _is_ab_row(r):
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= since:
            rows.append(r)
    top = _stats(rows)
    modes = sorted({str(r.get("mode") or "legacy") for r in rows})
    top["window_days"] = window
    top["total"] = len(rows)
    top["by_mode"] = {m: _stats([r for r in rows if str(r.get("mode") or "legacy") == m])
                      for m in modes}
    top["generated_ts"] = int(now)
    return top
