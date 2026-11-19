#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话摘要的触发与落盘（附录 C · P2 收尾）。

三件事：
  1. **触发规则**：每 N 轮 / 水位 ≥60% / 用户显式要求，三者任一即触发；
  2. **落盘**：`~/.brain-memory/sessions/summaries/<session>.md`，保留最近 3 版；
  3. **回滚**：`rollback(n)` 把上一版恢复成当前版（摘要漂移时的人工兜底）。

摘要生成复用 summarizer.summarize()（模型可选、fail-open）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

KEEP_VERSIONS = 3
DEFAULT_TURNS = 8
DEFAULT_PCT = 0.60


def summaries_dir(home=None) -> Path:
    return Path(home or _home()) / "sessions" / "summaries"


def _home():
    try:
        import core
        return core.HOME
    except Exception:
        return Path.home() / ".brain-memory"


def _paths(session_id: str, home=None) -> list[Path]:
    """[当前, .1, .2] 三个版本槽。"""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session_id or "default"))
    base = summaries_dir(home)
    return [base / f"{safe}.md"] + [base / f"{safe}.{i}.md" for i in range(1, KEEP_VERSIONS)]


# ---------------------------------------------------------------- 触发

def decide(turns_since_last: int = 0, used_pct: float | None = None,
           force: bool = False, cfg: dict | None = None) -> dict:
    """返回 {trigger: bool, reason: str}。三个条件任一满足就触发。"""
    cfg = cfg or {}
    every = int(cfg.get("summary_turns") or DEFAULT_TURNS)
    pct_th = float(cfg.get("summary_pct") or DEFAULT_PCT)
    if force:
        return {"trigger": True, "reason": "force（用户/命令显式要求）"}
    if used_pct is not None and float(used_pct) >= pct_th * 100:
        return {"trigger": True, "reason": f"水位 {float(used_pct):.0f}% ≥ {pct_th * 100:.0f}%"}
    if turns_since_last >= every:
        return {"trigger": True, "reason": f"距上次摘要 {turns_since_last} 轮 ≥ {every}"}
    return {"trigger": False, "reason": f"未触发（{turns_since_last} 轮 / "
                                        f"{'—' if used_pct is None else f'{float(used_pct):.0f}%'}）"}


# ---------------------------------------------------------------- 落盘 / 读取 / 回滚

def save(text: str, session_id: str = "default", home=None, meta: dict | None = None) -> dict:
    """写入当前版，旧版顺次后移（.1 ← 旧当前，.2 ← 旧 .1）。"""
    if not str(text or "").strip():
        return {"ok": False, "reason": "空摘要"}
    paths = _paths(session_id, home)
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    for i in range(len(paths) - 1, 0, -1):
        if paths[i - 1].exists():
            try:
                paths[i].write_text(paths[i - 1].read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
    payload = str(text).rstrip() + "\n"
    if meta:
        payload = f"<!-- {json.dumps(meta, ensure_ascii=False)} -->\n" + payload
    paths[0].write_text(payload, encoding="utf-8")
    return {"ok": True, "path": str(paths[0]), "chars": len(text)}


def load(session_id: str = "default", home=None) -> str:
    p = _paths(session_id, home)[0]
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def rollback(session_id: str = "default", home=None, steps: int = 1) -> dict:
    """回滚：把最近一版备份恢复成当前版（保留被替换的那版到 .2）。"""
    paths = _paths(session_id, home)
    steps = max(1, int(steps))
    if steps >= len(paths) or not paths[1].exists():
        return {"ok": False, "reason": "没有可回滚的备份"}
    restored = paths[steps].read_text(encoding="utf-8")
    for i in range(len(paths) - 1, 0, -1):
        if paths[i - 1].exists():
            try:
                paths[i].write_text(paths[i - 1].read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
    paths[0].write_text(restored, encoding="utf-8")
    return {"ok": True, "path": str(paths[0]), "chars": len(restored)}


# ---------------------------------------------------------------- 一步到位

def maybe_summarize(turns: list, *, session_id: str = "default", turns_since_last: int = 0,
                    used_pct: float | None = None, force: bool = False,
                    cfg: dict | None = None, home=None, prev_summary: str = "",
                    max_chars: int = 1500) -> dict:
    """触发判断 → 生成 → 落盘。不触发时不生成（省一次模型/IO）。"""
    from summarizer import summarize
    d = decide(turns_since_last, used_pct, force, cfg)
    if not d["trigger"]:
        return {"ok": True, "skipped": True, "reason": d["reason"]}
    res = summarize(turns, cfg=cfg, prev_summary=prev_summary or load(session_id, home),
                    max_chars=max_chars)
    saved = save(res["text"], session_id, home,
                 meta={"ts": int(time.time()), "source": res["source"], "reason": d["reason"]})
    return {"ok": bool(saved.get("ok")), "skipped": False, "reason": d["reason"],
            "source": res["source"], "chars": res["chars"], "path": saved.get("path"),
            "warnings": res.get("warnings") or [], "text": res["text"]}
