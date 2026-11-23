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
    """只读状态：不加锁、不建目录/锁文件（面板等只读调用不能有写副作用）。"""
    try:
        data = json.loads(_state_path(home).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, home: Path | None = None) -> None:
    """写回状态：整个「重读最新 → 合并 → 落盘」持 core._locked 写锁。

    checkpoint 是先 load 判断要不要写、再写记忆、最后 save，所以调用方手里的
    state 可能已是旧快照；直接整档覆盖会把另一个进程刚写进去的仓库抹掉。
    这里在锁内重读磁盘再 update，两个进程各写各的仓库时都能留下（不丢更新）。
    锁文件与状态文件同目录（core._locked 用 `<状态文件>.lock`），超时/陈旧锁
    清理沿用 core._locked 的统一语义；落盘用 core.atomic_json，锁外只读者
    不会读到写了一半的 JSON。
    """
    p = _state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    import core
    with core._locked(p):
        disk = _load_state(home)
        disk.update(state or {})
        core.atomic_json(p, disk)


_GIT_TRANSIENT_WINERRORS = {5, 32, 33, 145}   # 拒绝访问 / 共享冲突 / 锁冲突 / 目录非空


class GitUnavailable(RuntimeError):
    """git 命令反复失败（瞬时占用/超时），不是"这不是仓库"——两者必须分开报。"""


def _git(repo: str, *args: str, attempts: int = 3) -> str:
    """跑 git 返回 stdout；**瞬时**失败重试，仍失败抛 GitUnavailable。

    原来这里 `except Exception: return ""` 把瞬时占用/超时和"不是仓库"混为一谈，
    一次杀软扫过就能让 checkpoint 误报"不是 git 仓库"（台账 #71）。
    非零退出码（真的不是仓库、路径不对）仍返回 ""，由调用方按业务判断。
    """
    last = ""
    for i in range(max(1, attempts)):
        try:
            r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=20)
            if r.returncode == 0:
                return r.stdout.strip()
            return ""          # 非零退出：业务层面的"没有/不是"，不重试
        except subprocess.TimeoutExpired:
            last = f"git {args[0] if args else ''} 超时"
        except OSError as exc:
            # Windows 原生错误看 winerror；纯 OSError(errno=32) 没有 winerror，也要认
            winerr = getattr(exc, "winerror", None)
            errno_ = getattr(exc, "errno", None)
            if (winerr not in _GIT_TRANSIENT_WINERRORS
                    and errno_ not in _GIT_TRANSIENT_WINERRORS
                    and not isinstance(exc, PermissionError)):
                raise GitUnavailable(f"git 不可用：{exc}") from exc
            last = f"git 被占用（winerror={winerr} errno={errno_}）：{exc}"
        except Exception as exc:  # noqa: BLE001
            raise GitUnavailable(f"git 调用失败：{exc}") from exc
        if i + 1 < attempts:
            time.sleep(0.3 * (i + 1))    # 退避：0.3s / 0.6s
    raise GitUnavailable(last or "git 多次失败")


def collect(repo: str | None = None) -> dict | None:
    """收集收尾证据；不是 git 仓库返回 None。"""
    base = str(repo or ".")
    root = _git(base, "rev-parse", "--show-toplevel")
    if not root:
        # 空 root 有两种可能：真的不是仓库，或 git 反复失败。
        # .git 存在说明"是仓库、只是这次取证没成功"——这时不能谎报"不是仓库"。
        if (Path(base) / ".git").exists():
            raise GitUnavailable("git 仓库存在，但取证失败（可能被杀软/索引器短暂占用），请稍后重试")
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
    try:
        ev = collect(repo)
    except GitUnavailable as exc:
        # 取证失败 ≠ 不是仓库：给可重试的明确理由，别让用户以为路径给错了
        return {"ok": False, "skipped": True, "transient": True, "reason": str(exc)}
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
