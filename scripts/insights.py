#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板「运行洞察」聚合（只读、全部 fail-open）。

一次请求返回面板需要的小结：版本 / 上下文账单 / 原文检索库 / 收尾清点 /
群聊鉴权 / 嵌入状态 / A/B 灰度样本。任何一块失败只降级该块并记 warning，
不影响其余。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _version() -> str:
    for cand, pat in (("SKILL.md", "version:"), ("package.json", '"version"')):
        try:
            for line in (_root() / cand).read_text(encoding="utf-8").splitlines():
                if pat in line:
                    return line.split(pat, 1)[1].strip().strip('":, ').strip()
        except OSError:
            continue
    return ""


def _context() -> dict:
    import memory
    rep: dict = {}
    memory.build_injection(report=rep)
    secs = sorted((s for s in rep.get("sections") or []),
                  key=lambda s: -int(s.get("chars") or 0))[:3]
    return {"total_tokens": rep.get("total_tokens"), "total_chars": rep.get("total_chars"),
            "budget": rep.get("budget"), "top": secs}


def _rag() -> dict:
    import session_rag
    return session_rag.stats()


def _checkpoints() -> dict:
    import checkpoint
    st = checkpoint._load_state()
    items = [(k, v) for k, v in st.items() if isinstance(v, dict)]
    items.sort(key=lambda kv: -int(kv[1].get("ts") or 0))
    return {"count": len(items),
            "last": ({"repo": Path(k).name, "head": v.get("head"),
                      "ts": v.get("ts"), "version": v.get("version")}
                     if items else None)}


def _guard() -> dict:
    import core
    cfg = core.load_config()
    g = cfg.get("guard") or {}
    out = {"enabled": g.get("enabled", True) is not False,
           "shadow": g.get("shadow", True) is not False,
           "events": []}
    try:
        log = Path(core.HOME) / "lingshu-guard.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        for line in lines:
            try:
                out["events"].append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _embed() -> dict:
    import core
    cfg = core.load_config()
    emb = cfg.get("embed") or {}
    left = max(0, int(core._EMBED_DOWN_UNTIL - time.time())) if getattr(core, "_EMBED_DOWN_UNTIL", 0) else 0
    return {"url": str(emb.get("url") or ""), "model": str(emb.get("model") or ""),
            "cooldown_sec": left}


def _failures() -> dict:
    import failures
    return failures.summarize(1)


def _ab() -> dict:
    """P4 灰度 A/B 采样小结：只读 metrics.jsonl，无样本时 total=0。

    面板只关心「最近 7 天省了多少、当前跑哪个模式」，所以这里把
    context_ab.summary() 的三段分位数裁成 p50/p95，并附上
    config.context_mode。summary 本身不写盘，失败由 build() 统一降级。
    """
    import context_ab
    s = context_ab.summary(7)
    return {"total": int(s.get("total") or 0),
            "mean_saved_pct": s.get("mean_saved_pct"),
            "p50": s.get("p50_saved_pct"),
            "p95": s.get("p95_saved_pct"),
            "by_mode": s.get("by_mode") or {},
            "mode": context_ab.context_mode()}


def build(light: bool = False) -> dict:
    """light=True 跳过最重的上下文账单（面板自动刷新时用）。

    A/B 样本块只有一次 512KB 上限的小文件读，不进 light 跳过名单：
    面板自动刷新走 light=1，跳了它「A/B 样本」这行就永远是空的。
    """
    warnings = []
    data = {"version": _version()}
    for name, fn in (("context", _context), ("rag", _rag), ("checkpoints", _checkpoints),
                     ("guard", _guard), ("embed", _embed), ("failures", _failures),
                     ("ab", _ab)):
        if name == "context" and light:
            continue
        try:
            data[name] = fn()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            data[name] = None
            try:
                import failures
                failures.record("insights." + name, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
    return {"ok": True, "ts": int(time.time()), "data": data, "warnings": warnings}
