#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板「运行洞察」聚合（只读、全部 fail-open）。

一次请求返回面板需要的小结：版本 / 上下文账单 / 原文检索库 / 收尾清点 /
群聊鉴权 / 嵌入状态。任何一块失败只降级该块并记 warning，不影响其余。
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


def build(light: bool = False) -> dict:
    """light=True 跳过最重的上下文账单（面板自动刷新时用）。"""
    warnings = []
    data = {"version": _version()}
    for name, fn in (("context", _context), ("rag", _rag), ("checkpoints", _checkpoints),
                     ("guard", _guard), ("embed", _embed)):
        if name == "context" and light:
            continue
        try:
            data[name] = fn()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            data[name] = None
    return {"ok": True, "ts": int(time.time()), "data": data, "warnings": warnings}
