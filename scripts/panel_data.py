# -*- coding: utf-8 -*-
"""面板「数据」页后端：备份列表 / 立即备份 / 从备份恢复。

复用 CLI 已有的 cmd_backup / cmd_restore——它们已经做了：完整性校验、
恢复前自动备份、坏包回退、保留份数修剪。面板只负责触发与展示，
不另写一套打包逻辑（两套实现必然分叉）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import core


def _backup_dir() -> Path:
    """与 CLI 同源的备份目录选址（跨卷优先）。"""
    env = os.environ.get("BRAIN_MEMORY_BACKUP_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        import safety
        return safety.pick_backup_dir(core.HOME)
    except Exception:
        return Path(core.HOME) / "backups"


def list_backups(limit: int = 30) -> dict:
    """列出备份目录里的 zip（含大小/时间/是否有 sha 记录）。"""
    d = _backup_dir()
    items = []
    try:
        files = sorted(d.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for p in files[:limit]:
        try:
            st = p.stat()
        except OSError:
            continue
        sha_file = p.with_name(p.name + ".sha256")
        items.append({
            "name": p.name,
            "path": str(p),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "mtime_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            # 22 字节的空 zip（PK\x05\x06）是失败残壳，标出来别当备份用
            "suspicious": st.st_size < 1024,
            "has_sha": sha_file.is_file(),
        })
    return {"dir": str(d), "dir_exists": d.is_dir(), "items": items,
            "count": len(items)}


def make_backup(keep: int = 0) -> dict:
    """立即备份。复用 CLI 的 cmd_backup（assume_yes=True：面板是显式点击）。

    **keep 默认 0 = 不修剪**：手动点一下备份不该静默删掉历史备份——这是
    不可逆操作（cmd_backup 用 unlink，不进回收站）。要清理请显式传 keep。
    """
    if core.READONLY:
        raise PermissionError("记忆库当前只读，无法备份。")
    import cli
    before = {x["name"] for x in list_backups(limit=500)["items"]}
    code = cli.cmd_backup(target=None, keep=int(keep or 0), password=None,
                          assume_yes=True)
    after = list_backups(limit=200)["items"]
    new = [x for x in after if x["name"] not in before]
    return {"ok": code == 0, "code": code,
            "created": new[0] if new else None,
            "total": len(after), "dir": str(_backup_dir())}


def restore_backup(name: str, exact: bool = False) -> dict:
    """从指定备份恢复。只接受备份目录内的 .zip 文件名（防路径穿越）。

    cmd_restore 内部会：校验 zip 完整性 → 恢复前自动给现状打一份备份 →
    坏包回退提示。这里不重复实现，只做入参收敛。
    """
    if core.READONLY:
        raise PermissionError("记忆库当前只读，无法恢复。")
    safe = Path(str(name or "")).name           # 只取文件名，杜绝 ../ 穿越
    if not safe.lower().endswith(".zip"):
        raise ValueError("只支持 .zip 备份文件")
    d = _backup_dir()
    target = d / safe
    if not target.is_file():
        raise FileNotFoundError(f"备份目录里没有这个文件：{safe}")
    import cli
    code = cli.cmd_restore(str(target), yes=True, password=None, exact=bool(exact))
    return {"ok": code == 0, "code": code, "restored": safe, "exact": bool(exact)}


def data_stats() -> dict:
    """数据规模概览（给「数据」页顶部卡片）。"""
    home = core.HOME
    out = {"home": str(home), "layers": {}, "total_bytes": 0}
    for name in ("hippocampus.jsonl", "cortex.jsonl", "dust.jsonl",
                 "archive.jsonl", "knowledge.jsonl", "notes.jsonl"):
        p = home / name
        try:
            size = p.stat().st_size if p.is_file() else 0
            n = 0
            if p.is_file():
                n = sum(1 for ln in p.open(encoding="utf-8", errors="replace") if ln.strip())
        except OSError:
            size, n = 0, 0
        out["layers"][name.replace(".jsonl", "")] = {"count": n, "bytes": size}
        out["total_bytes"] += size
    return out
