# -*- coding: utf-8 -*-
"""程序目录「垃圾桶」：AI 不删除文件，只把待删的移进来，由用户自己清空。

用户定的长期规矩（`lingshu.user.md` 里也记了一条）：
  要删的文件一律放进程序目录的 垃圾桶/，不要直接删除。
所以本模块**只做 move，绝不做 unlink/rmtree**——连"清空垃圾桶"都不提供，
那是用户自己的事。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

TRASH_DIRNAME = "垃圾桶"


def trash_root() -> Path:
    """垃圾桶位置：程序目录（本文件所在 scripts/ 的上一级）下的 垃圾桶/。"""
    return Path(__file__).resolve().parent.parent / TRASH_DIRNAME


def _safe_target(p: Path) -> Path:
    """禁止把垃圾桶自己或程序目录整体搬进去（防手滑把仓库搬空）。"""
    root = trash_root().resolve()
    p = p.resolve()
    if p == root or root in p.parents:
        raise ValueError("目标已经在垃圾桶里")
    if p == root.parent:
        raise ValueError("不能把程序目录本身搬进垃圾桶")
    if not p.exists():
        raise FileNotFoundError(f"找不到：{p}")
    return p


def move_to_trash(paths: list[str], why: str = "") -> dict:
    """把文件/目录移进垃圾桶，返回搬了哪些、搬到哪。

    命名带时间戳避免重名；同时在垃圾桶里写一条 `.来源.txt` 记原路径与原因，
    方便用户判断该不该留、以及想找回时移回哪里。
    """
    root = trash_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved, failed = [], []
    notes = []
    for raw in paths:
        try:
            src = _safe_target(Path(str(raw)).expanduser())
        except (ValueError, FileNotFoundError) as exc:
            failed.append({"path": str(raw), "error": str(exc)})
            continue
        dst = root / f"{stamp}_{src.name}"
        k = 1
        while dst.exists():
            dst = root / f"{stamp}_{k}_{src.name}"
            k += 1
        try:
            shutil.move(str(src), str(dst))
        except OSError as exc:
            failed.append({"path": str(src), "error": str(exc)})
            continue
        moved.append({"from": str(src), "to": str(dst)})
        notes.append(f"{src}\n  → 垃圾桶/{dst.name}")
    if moved:
        (root / f"{stamp}.来源.txt").write_text(
            "以下是本次移入垃圾桶的文件，原路径：\n\n"
            + "\n".join(notes)
            + (f"\n\n原因：{why}\n" if why else "")
            + "\n想找回：把文件移回上面的原路径即可。\n",
            encoding="utf-8")
    return {"ok": not failed, "moved": moved, "failed": failed,
            "trash_dir": str(root)}


def list_trash() -> dict:
    """看垃圾桶里有什么（给用户自己判断要不要清）。"""
    root = trash_root()
    items = []
    if root.is_dir():
        for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": st.st_size if p.is_file() else 0,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            })
    return {"dir": str(root), "exists": root.is_dir(), "items": items,
            "count": len(items)}
