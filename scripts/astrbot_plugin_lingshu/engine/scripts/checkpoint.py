#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务/项目收尾清点（checkpoint）。

动机：任务做完就散了，记忆里只剩零碎的过程，没有"这件事收尾时是什么状态"。
本模块在收尾时用**可验证的证据**（git 状态、提交、版本号）写一条 episode，
不靠模型编、也不需要你复述。

规则：
  - 只在证据变化时写（HEAD 变了 / 工作区有改动），同一状态不重复写；
  - 写一条 `source=checkpoint` 的 episode，tags 带「收尾 + 项目名」；
  - 上次清点记录在 `~/.brain-memory/sessions/checkpoints.json`，按仓库路径存 HEAD。

CLI：
    brain.py checkpoint [--repo 路径] [--note "一句话"] [--dry-run] [--json] [--force]
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


def _home() -> Path:
    try:
        import core
        return Path(core.HOME)
    except Exception:
        return Path.home() / ".brain-memory"


def _state_path(home: Path | None = None) -> Path:
    return (home or _home()) / "sessions" / "checkpoints.json"


def _load_state(home: Path | None = None) -> dict:
    try:
        return json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, home: Path | None = None) -> None:
    p = _state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _git(repo: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def collect(repo: str | None = None) -> dict | None:
    """收集收尾证据；不是 git 仓库返回 None。"""
    base = str(repo or ".")
    root = _git(base, "rev-parse", "--show-toplevel")
    if not root:
        return None
    head = _git(root, "rev-parse", "--short", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    subject = _git(root, "log", "-1", "--format=%s")
    last_ts = _git(root, "log", "-1", "--format=%ct")
    status = _git(root, "status", "--porcelain")
    changed = len([x for x in status.splitlines() if x.strip()])
    version = ""
    for cand, pat in (("SKILL.md", "version:"), ("package.json", '"version"')):
        f = Path(root) / cand
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if pat in line:
                    version = line.split(pat, 1)[1].strip().strip('":, ').strip()
                    break
        except OSError:
            pass
        if version:
            break
    return {
        "repo": root,
        "name": Path(root).name,
        "head": head,
        "branch": branch,
        "subject": subject,
        "commit_ts": int(last_ts) if last_ts.isdigit() else 0,
        "changed": changed,
        "version": version,
    }


def decide(ev: dict, state: dict, force: bool = False) -> dict:
    """是否需要写：HEAD 变了 / 工作区有改动 / 强制。"""
    prev = state.get(ev["repo"]) or {}
    if force:
        return {"write": True, "reason": "force"}
    if not prev:
        return {"write": True, "reason": "首次收尾"}
    if prev.get("head") != ev["head"]:
        return {"write": True, "reason": f"HEAD {prev.get('head', '?')} → {ev['head']}"}
    if ev["changed"] > 0:
        return {"write": True, "reason": f"工作区有 {ev['changed']} 处未提交改动"}
    return {"write": False, "reason": "与上次收尾一致，不重复记"}


def build_text(ev: dict, note: str = "", extra: str = "") -> str:
    head_line = f"项目：{ev['name']}（分支 {ev['branch'] or '?'}，HEAD {ev['head'] or '?'}）"
    lines = [f"任务收尾：{ev['name']}", head_line]
    if ev["subject"]:
        lines.append(f"最后提交：{ev['subject']}")
    lines.append("工作区：" + ("干净" if ev["changed"] == 0 else f"还有 {ev['changed']} 处未提交改动"))
    if ev["version"]:
        lines.append(f"版本：{ev['version']}")
    if note.strip():
        lines.append(f"收尾备注：{note.strip()}")
    if extra.strip():
        lines.append(extra.strip())
    return "\n".join(lines)


def checkpoint(repo: str | None = None, note: str = "", *, dry_run: bool = False,
               force: bool = False, home: Path | None = None) -> dict:
    """一步到位：收集 → 判断 → 写记忆 + 记状态。"""
    ev = collect(repo)
    if ev is None:
        return {"ok": False, "skipped": True, "reason": "不是 git 仓库，没法取证（可显式传 --repo）"}
    state = _load_state(home)
    d = decide(ev, state, force)
    text = build_text(ev, note)
    if not d["write"]:
        return {"ok": True, "skipped": True, "reason": d["reason"], "text": text, "evidence": ev}
    mid = ""
    if not dry_run:
        try:
            import memory
            mid, _rec = memory.remember(
                text, kind="episode", importance=0.55,
                tags=["收尾", ev["name"]], source="checkpoint",
                project=ev["name"])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "skipped": False, "reason": f"写记忆失败：{exc}",
                    "text": text, "evidence": ev}
        state[ev["repo"]] = {"head": ev["head"], "ts": int(time.time()),
                             "changed": ev["changed"], "version": ev["version"]}
        _save_state(state, home)
    return {"ok": True, "skipped": False, "reason": d["reason"], "text": text,
            "memory_id": mid, "evidence": ev, "dry_run": dry_run}
