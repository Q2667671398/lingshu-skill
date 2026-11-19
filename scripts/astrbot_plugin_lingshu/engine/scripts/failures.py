#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失败可见化：把被吞掉的异常记成可数的账。

灵枢大量使用 fail-open（`except: pass`），这是设计——但"吞了多少、都吞了什么"
不可见，就没法判断哪条路径在悄悄坏掉。

本模块只做三件事：
  record(kind, msg, level)  追加一行 JSON（超 512KB 自动轮转 .1）
  summarize(days, top)      按 (kind, 归一化消息) 聚合：次数 / 最近时间 / 样本
  recent(n)                 最近 n 条原始记录

约定：只在"本来就要吞掉"的分支里调用，不改变控制流；本身也绝不抛异常。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

MAX_BYTES = 512 * 1024


def _home() -> Path:
    try:
        import core
        return Path(core.HOME)
    except Exception:
        return Path.home() / ".brain-memory"


def path(home=None) -> Path:
    return Path(home or _home()) / "failures.jsonl"


def record(kind: str, msg: str, *, level: str = "warn", ctx: dict | None = None,
           home=None) -> None:
    """记一次被吞掉的失败；任何 IO 错误都静默（日志本身不能成为故障源）。"""
    try:
        p = path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.exists() and p.stat().st_size > MAX_BYTES:
                p.replace(p.with_suffix(".jsonl.1"))
        except OSError:
            pass
        row = {"ts": int(time.time()), "kind": str(kind or "other")[:40],
               "level": str(level or "warn")[:10],
               "msg": re.sub(r"\s+", " ", str(msg or ""))[:300]}
        if ctx:
            row["ctx"] = {str(k)[:30]: str(v)[:120] for k, v in list(ctx.items())[:6]}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _norm(msg: str) -> str:
    """归一化：数字/长 hex/路径压成占位符，避免每个 id 各算一类。"""
    s = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", msg)
    s = re.sub(r"\d+", "<n>", s)
    s = re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<path>", s)
    return s[:160]


def _read(home=None) -> list[dict]:
    out = []
    for p in (path(home).with_suffix(".jsonl.1"), path(home)):
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            continue
    return out


def summarize(days: float = 7, top: int = 10, *, home=None) -> dict:
    now = time.time()
    since = now - max(0.0, float(days)) * 86400
    rows = [r for r in _read(home) if isinstance(r, dict) and float(r.get("ts") or 0) >= since]
    by_kind: dict[str, int] = {}
    groups: dict[tuple, dict] = {}
    for r in rows:
        kind = str(r.get("kind") or "other")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        msg = str(r.get("msg") or "")
        key = (kind, _norm(msg))
        g = groups.setdefault(key, {"kind": kind, "normalized": _norm(msg),
                                    "count": 0, "last_ts": 0, "sample": msg[:200]})
        g["count"] += 1
        g["last_ts"] = max(g["last_ts"], int(r.get("ts") or 0))
    items = sorted(groups.values(), key=lambda g: (-g["count"], -g["last_ts"]))[:max(1, int(top))]
    return {"window_days": float(days), "total": len(rows), "by_kind": by_kind,
            "top": items}


def recent(n: int = 20, *, home=None) -> list[dict]:
    rows = sorted(_read(home), key=lambda r: -int(r.get("ts") or 0))
    return rows[:max(1, int(n))]
