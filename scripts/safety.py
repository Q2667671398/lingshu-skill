#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 数据目录与破坏性操作的护栏。

为什么要有这一层（真实事故）：用户的用户目录被某个未知进程整片清空，
灵枢记忆库 `~/.brain-memory` 一起没了；事后翻备份才发现，那阵子的备份
默认落在 `~/Documents/brain-memory-backups` —— 备份和用户内容在同一个
目录、同一块盘，被同一次事故一起端掉，等于没有备份。最后只能靠更早的
一块独立盘（D:）上的旧备份把人格救回来。

三条规则，写死在代码里，不靠自觉：

1. **不能把记忆库放在「会被清理的东西」里**。盘符根、用户主目录本体、
   用户库文件夹（桌面/文档/下载/图片/视频/音乐/OneDrive）、系统目录，
   一律拒绝当记忆库。`not set` 也不行，必须是「一个自己的目录」。
2. **删整库之前先证明这确实是灵枢库**。靠标记文件判断，不靠路径长得像；
   认不出来的文件一律不删（restore --exact 也保留）。
3. **备份尽量跨卷**。同卷备份 = 同一个炸弹上的两个引信。

所有函数都不写盘（除了 `pick_backup_dir` 会 mkdir 备份目录），
可以在只读降级下安全调用。
"""
from __future__ import annotations

import os
import string
import sys
from pathlib import Path

__all__ = [
    "UnsafeHomeError",
    "dangerous_reason",
    "assert_safe_home",
    "is_managed_home",
    "assert_managed_home",
    "is_managed_relpath",
    "same_volume",
    "backup_dir_candidates",
    "pick_backup_dir",
    "MANAGED_TOP_FILES",
    "MANAGED_TOP_DIRS",
]

# 用户库文件夹的**磁盘真名**（Windows 本地化只影响显示名，落盘永远是这些英文名）。
# 刻意不收中文「文档/下载」：中文目录名太常见，会误伤 D:\文档\xxx 这种正常布局。
LIBRARY_FOLDER_NAMES = frozenset({
    "desktop", "documents", "downloads", "pictures", "videos", "music",
    "onedrive", "favorites", "saved games", "searches", "links", "contacts",
})

# 明确不能当记忆库的系统目录（小写、绝对路径前缀）。
# 注意不要写 "c:\\users"：用户自己的数据本来就住在 C:\Users\<name>\ 下面。
_SYSTEM_PREFIXES = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\$recycle.bin",
    "/etc", "/usr", "/bin", "/sbin", "/var", "/boot", "/dev", "/proc", "/sys",
)

# 判定「这像不像灵枢库」的标记文件：任意一个存在即认为像。
MARKER_FILES = ("_meta.json", "persona.json", "config.json",
                "hippocampus.jsonl", "cortex.jsonl")

# 灵枢自己管理的顶层文件（restore --exact 允许删除的白名单）。
MANAGED_TOP_FILES = frozenset({
    "_meta.json", "config.json", "persona.json",
    "hippocampus.jsonl", "cortex.jsonl", "dust.jsonl", "archive.jsonl",
    "assoc.jsonl", "topics.jsonl", "working.jsonl", "emotions.jsonl",
    "desires.json", "desire_history.jsonl", "notes.jsonl", "knowledge.jsonl",
    "evolution.jsonl", "audit.jsonl", "consent.json", "daily_state.json",
    "dream_state.json", "last_retrieval.json", "recall_usage.jsonl",
    "metrics.jsonl", "failures.jsonl", "persona_pending.jsonl",
    "persona_snapshots.jsonl", "skills.json", "update_check.json",
    "lingshu.user.md", "lingshu.log", "session_rag.db",
    "session_rag.db-shm", "session_rag.db-wal",
})

# 灵枢自己管理的顶层目录。
MANAGED_TOP_DIRS = frozenset({
    "dreams", "dream-snapshots", "persona_snapshots", "skills", "verbatim",
    "knowledge", "extensions", "panels", "backups", "handoffs", "sessions",
    "downloads",
})


class UnsafeHomeError(RuntimeError):
    """记忆库指向了一个不该被当作记忆库的目录。"""


def _expand(p: Path | str) -> Path:
    return Path(p).expanduser()


def _resolved(p: Path | str) -> Path:
    try:
        return _expand(p).resolve()
    except OSError:  # 目录还不存在 / 短路径解析失败时退回字面路径
        return _expand(p)


def _is_drive_root(p: Path) -> bool:
    """盘符根（C:\\、D:\\）或 POSIX 的 /。"""
    if p.parent == p:          # "/" 或 "C:\\"
        return True
    anchor = p.anchor               # "C:\\" / "/" / "\\\\server\\share\\"
    try:
        return p == Path(anchor) or str(p) in (anchor, anchor.rstrip("\\/"))
    except Exception:
        return False


def _is_home_dir(p: Path) -> bool:
    try:
        return p == Path.home().resolve()
    except OSError:
        return False


def dangerous_reason(home: Path | str) -> str | None:
    """这个路径为什么不能当记忆库？返回中文原因，安全则返回 None。"""
    raw = _expand(home)
    r = _resolved(home)
    if _is_drive_root(r):
        return "盘符根目录不能当记忆库（会把整盘的文件当成自己的）"
    if _is_home_dir(r):
        return "用户主目录本体不能当记忆库（restore --exact 会清空整个用户目录）"
    low = str(r).lower().replace("/", "\\") if sys.platform == "win32" else str(r).lower()
    for pref in _SYSTEM_PREFIXES:
        if low == pref or low.startswith(pref.rstrip("\\") + "\\"):
            return f"系统目录不能当记忆库：{raw}"
    # 所有用户的父目录（C:\Users 本身）
    try:
        if r.name.lower() == "users" and r.parent == Path(r.anchor):
            return "用户根目录（所有账户的父目录）不能当记忆库"
    except OSError:
        pass
    # 用户库文件夹及其内部：~/Documents、~/Desktop/brain-memory …
    try:
        user_home = Path.home().resolve()
    except OSError:
        user_home = Path.home()
    for name in LIBRARY_FOLDER_NAMES:
        lib = user_home / name
        if r == lib or lib in r.parents:
            return (f"路径落在用户库文件夹「{lib.name}」里 —— 这类目录最容易被清理、"
                    f"重装或误删整片带走，备份也会跟着一起没")
    return None


def assert_safe_home(home: Path | str, action: str = "使用") -> None:
    """危险路径直接抛异常；`BRAIN_MEMORY_ALLOW_UNSAFE_HOME=1` 可强行越过。

    越过时会往 stderr 打一行醒目的警告 —— 允许，但不装作没发生。
    """
    reason = dangerous_reason(home)
    if not reason:
        return
    if os.environ.get("BRAIN_MEMORY_ALLOW_UNSAFE_HOME") == "1":
        print(f"[警告] {action}记忆库的路径选择不安全：{reason}\n"
              f"       已按 BRAIN_MEMORY_ALLOW_UNSAFE_HOME=1 强制继续，风险自负。",
              file=sys.stderr)
        return
    raise UnsafeHomeError(
        f"拒绝{action}记忆库：{reason}\n"
        f"       路径：{_expand(home)}\n"
        f"       换一个独立目录，例如：{_suggest_safe_home()}\n"
        f"       （万不得已要强行使用，设 BRAIN_MEMORY_ALLOW_UNSAFE_HOME=1）"
    )


def _suggest_safe_home() -> str:
    return str(Path.home() / ".brain-memory")


def is_managed_home(home: Path | str) -> bool:
    """目录里有没有灵枢的标记文件 —— 有才敢对它做整库删除。"""
    r = _resolved(home)
    try:
        return any((r / name).exists() for name in MARKER_FILES)
    except OSError:
        return False


def assert_managed_home(home: Path | str) -> None:
    """整库删除前的第二道闸：不像灵枢库就拒绝。"""
    if is_managed_home(home):
        return
    raise UnsafeHomeError(
        f"拒绝整库删除：{_expand(home)} 里找不到任何灵枢标记文件"
        f"（{ '、'.join(MARKER_FILES) }）。\n"
        f"       目录不像记忆库时，清空它只会毁掉别的数据。"
    )


def is_managed_relpath(rel: Path | str) -> bool:
    """rel 是相对记忆库根的路径：这个路径归灵枢管吗？

    True = 可删可覆盖；False = 用户自己放进去的东西，任何“清空/恢复”都不许碰。
    """
    rel = Path(rel)
    parts = rel.parts
    if not parts:
        return False
    # 灵枢自己的临时件与锁文件
    name = parts[-1]
    if name.startswith(".restore-tmp-") or name == ".write-probe":
        return True
    if name.endswith((".lock", ".tmp")):
        return True
    if len(parts) == 1:
        return name in MANAGED_TOP_FILES or name in MANAGED_TOP_DIRS
    return parts[0] in MANAGED_TOP_DIRS


def same_volume(a: Path | str, b: Path | str) -> bool:
    """两个路径是不是同一块卷（Windows 按盘符，POSIX 按 st_dev）。"""
    try:
        ra, rb = _resolved(a), _resolved(b)
    except OSError:
        return True
    if sys.platform == "win32":
        return (ra.drive or "").lower() == (rb.drive or "").lower()
    try:
        return ra.stat().st_dev == rb.stat().st_dev
    except OSError:
        return True


def _windows_drives() -> list[Path]:
    drives = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                drives.append(root)
        except OSError:
            continue
    return drives


def backup_dir_candidates(home: Path | str) -> list[Path]:
    """备份目录候选，**跨卷优先**。

    顺序：别卷根 → 记忆库同级盘上的固定名 → 记忆库内部（最后兜底）。
    刻意不再用 ~/Documents 和 ~/Desktop：那正是会被整片清掉的地方。
    """
    home = _expand(home)
    out: list[Path] = []
    if sys.platform == "win32":
        for root in _windows_drives():
            if same_volume(root, home):
                continue
            out.append(root / "brain-memory-backups")
    else:
        for root in (Path("/mnt"), Path("/media"), Path("/Volumes")):
            if root.is_dir():
                for child in sorted(root.iterdir()):
                    if child.is_dir() and not same_volume(child, home):
                        out.append(child / "brain-memory-backups")
    out.append(home / "backups")
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def pick_backup_dir(home: Path | str) -> Path:
    """挑一个能建的备份目录并 mkdir；全都建不了就退回 `<home>/backups`。

    返回的目录保证**已经存在且可写**（除极端情况）。
    """
    home = _expand(home)
    for cand in backup_dir_candidates(home):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return cand
        except Exception:
            continue
    fallback = home / "backups"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fallback
