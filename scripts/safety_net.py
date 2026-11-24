# -*- coding: utf-8 -*-
"""底层保险：写前快照 + 写后校验 + 失败自动回滚（灵枢版 ARIES-lite）。

## 为什么要做

`save_all()` 是全项目最高危的写入口：它按 layer 把整批 records 分写回
hippocampus/cortex/dust 三个文件，**一旦读写之间丢记录或层名写错，就是整层被
覆盖**（本会话就踩过一次：用 `write_jsonl(hippocampus, load_all())` 差点把
cortex/dust 全灌进 hippocampus）。而项目里有 17 个公开函数走 `save_all()`。

## 借鉴了什么

| 出处 | 借的东西 |
|---|---|
| **ARIES / WAL**（Mohan 1992） | "先记 undo 日志再改数据，出错用 undo 回滚" |
| **rsnapshot / Time Machine 本地快照** | **硬链接即快照**：`os.link()` 是 O(1)、零拷贝，天然 COW |
| **crash-only software**（Candea & Fox 2003） | 假定随时会崩；恢复逻辑放常路，不靠"优雅关闭" |
| **SQLite WAL / LMDB COW** | 单写者 + 原子替换（本项目 `_replace_retry` 已是 os.replace） |

**为什么硬链接就够**：本项目写盘一律"写临时文件 → `os.replace`"。替换只改目录项，
老 inode 不动，所以事先 `os.link` 出来的快照**永远保留旧内容**——不需要复制字节。

## 边界（重要）

- 只保护**记忆层文件**（hippocampus/cortex/dust/archive）+ persona.json。
- 快照保留 N 份，更旧的**移入 `.pruned/` 而不是删除**（用户 v4.3 规矩：只移不删）。
- 任何一步失败都不能让主写入失败：`fail-open`，只记 warn 日志。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import core

SNAP_DIRNAME = ".safety"
DEFAULT_KEEP = 20
PROTECTED = ("hippocampus.jsonl", "cortex.jsonl", "dust.jsonl",
             "archive.jsonl", "persona.json")
# 文件 → 该文件里记录应有的 layer 值。
# 用途：抓"整批记录被灌错层"的 bug——那种错 JSON 仍合法、只查语法查不出来。
_LAYER_OF_FILE = {
    "hippocampus.jsonl": "hippo",
    "cortex.jsonl": "cortex",
    "dust.jsonl": "dust",
    "archive.jsonl": "archive",
}


def _home() -> Path:
    return Path(core.HOME)


def snap_root() -> Path:
    return _home() / SNAP_DIRNAME


def _enabled() -> bool:
    """配置开关（默认开）；坏配置当开——保险不该因为配置读不出来就失效。"""
    try:
        return bool(core.load_config().get("safety_snapshots", True))
    except Exception:
        return True


def _verify_enabled() -> bool:
    try:
        return bool(core.load_config().get("safety_verify", True))
    except Exception:
        return True


def _keep() -> int:
    try:
        n = int(core.load_config().get("safety_keep") or DEFAULT_KEEP)
        return n if n > 0 else DEFAULT_KEEP
    except Exception:
        return DEFAULT_KEEP


def _slug(reason: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(reason or "write"))[:40]
    return s or "write"


def _resolve(names) -> list:
    """只接受受保护的文件名，其它一律忽略（防被当通用备份工具误用）。"""
    out = []
    home = _home()
    for n in names:
        name = Path(str(n)).name
        if name not in PROTECTED:
            continue
        p = home / name
        if p.is_file():
            out.append(p)
    return out


def snapshot(reason: str = "write", names=None) -> str | None:
    """给受保护文件做 O(1) 硬链接快照，返回快照 id；失败返回 None（不阻断写入）。"""
    if not _enabled():
        return None
    targets = _resolve(names or PROTECTED)
    if not targets:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = snap_root()
    sid = f"{stamp}_{_slug(reason)}"
    dst = base / sid
    k = 1
    while dst.exists():
        dst = base / f"{sid}_{k}"
        k += 1
    try:
        dst.mkdir(parents=True, exist_ok=True)
        linked = {}
        for p in targets:
            lp = dst / p.name
            try:
                os.link(p, lp)          # O(1) 零拷贝：硬链接
                linked[p.name] = "link"
            except OSError:
                # 跨卷/文件系统不支持硬链接 → 退回复制（慢但正确）
                shutil.copy2(p, lp)
                linked[p.name] = "copy"
        counts = {}
        for p in targets:
            try:
                counts[p.name] = sum(1 for ln in p.open(encoding="utf-8", errors="replace")
                                     if ln.strip())
            except OSError:
                counts[p.name] = -1
        # 给快照内容记指纹。硬链接快照只在"原子替换"写入下成立——万一有代码
        # 路径**就地截断**写（open("w")），共享 inode 会被改，快照当场失效。
        # 记 sha256 就能在回滚前发现"快照已被污染"，而不是拿脏快照去覆盖。
        digests = {}
        for p in targets:
            try:
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                digests[p.name] = h.hexdigest()
            except OSError:
                digests[p.name] = ""
        (dst / "meta.json").write_text(json.dumps({
            "id": dst.name, "reason": str(reason)[:120], "ts": int(time.time()),
            "mode": linked, "counts": counts, "sha256": digests,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        _prune()
        return dst.name
    except Exception as exc:  # noqa: BLE001
        try:
            core.log_event("safety", f"写前快照失败（不影响写入）：{exc}", level="warn")
        except Exception:
            pass
        return None


def _counts_of(targets) -> dict:
    out = {}
    for p in targets:
        try:
            out[p.name] = sum(1 for ln in p.open(encoding="utf-8", errors="replace")
                              if ln.strip())
        except OSError:
            out[p.name] = -1
    return out


def verify(names=None, allow_shrink=()) -> dict:
    """写后校验：文件在、每行可解析、没被截断、哈希链字段齐。

    返回 {"ok": bool, "issues": [...], "counts": {...}}。
    `allow_shrink` 里列出的文件名允许行数减少（删除类操作显式声明）。
    """
    targets = _resolve(names or PROTECTED)
    issues = []
    counts = {}
    for p in targets:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append(f"{p.name} 读不出来：{exc}")
            continue
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        counts[p.name] = len(lines)
        if raw and not raw.endswith("\n"):
            issues.append(f"{p.name} 末行没有换行——可能被截断")
        if p.name.endswith(".jsonl"):
            parsed = []
            for i, ln in enumerate(lines, 1):
                try:
                    parsed.append(json.loads(ln))
                except Exception:
                    issues.append(f"{p.name} 第 {i} 行不是合法 JSON（可能写到一半）")
                    break
            # 层级一致性：记录自带 layer 必须与"它落在哪个文件"自洽。
            # 这条能抓住"整批记录被灌错层"的 bug（JSON 合法但分层已坏）。
            want = _LAYER_OF_FILE.get(p.name)
            if want and len(parsed) == len(lines):
                bad = [r for r in parsed
                       if isinstance(r, dict) and r.get("layer")
                       and r.get("layer") != want]
                if bad:
                    issues.append(
                        f"{p.name} 有 {len(bad)} 条记录的 layer 与文件不匹配"
                        f"（该文件应只放 layer={want}，实际含 "
                        f"{sorted({str(r.get('layer')) for r in bad})[:3]}）")
    return {"ok": not issues, "issues": issues, "counts": counts}


def restore(snap_id: str) -> dict:
    """从快照回滚（只回写快照里存在的文件），走原子替换。

    **回滚前先验快照完整性**：硬链接快照与工作文件共享 inode，若期间有人
    就地写（非原子替换），快照会跟着被改。指纹不符就拒绝回滚并报错——
    拿一份被污染的快照去覆盖，比不回滚更糟。
    """
    src = snap_root() / Path(str(snap_id)).name
    if not src.is_dir():
        raise FileNotFoundError(f"没有这个快照：{snap_id}")
    meta = {}
    try:
        meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    want = meta.get("sha256") or {}
    restored, corrupt = [], []
    for f in sorted(src.glob("*")):
        if f.name == "meta.json" or f.is_dir():
            continue
        if f.name not in PROTECTED:
            continue
        w = want.get(f.name)
        if w:
            try:
                h = hashlib.sha256()
                with f.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != w:
                    corrupt.append(f.name)
                    continue
            except OSError:
                corrupt.append(f.name)
                continue
        dst = _home() / f.name
        tmp = dst.with_name(dst.name + ".restore.tmp")
        shutil.copy2(f, tmp)
        core._replace_retry(tmp, dst)
        restored.append(f.name)
    if corrupt:
        raise RuntimeError(
            "快照已被污染（内容与快照时记录的 sha256 不符），拒绝用它回滚："
            + ", ".join(corrupt)
            + "。原因通常是有代码路径就地写文件而不是原子替换。")
    try:
        core.log_event("safety", f"已从快照回滚 {snap_id}：{', '.join(restored)}",
                       level="warn")
    except Exception:
        pass
    return {"ok": True, "snapshot": src.name, "restored": restored}


def list_snapshots(limit: int = 20) -> list:
    root = snap_root()
    out = []
    if not root.is_dir():
        return out
    for d in sorted((x for x in root.iterdir() if x.is_dir() and x.name != ".pruned"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        out.append({"id": d.name,
                    "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
                    "reason": meta.get("reason", ""), "counts": meta.get("counts", {})})
    return out


def _prune() -> None:
    """超过保留份数的旧快照**移入 .pruned/**（不删除）。"""
    root = snap_root()
    if not root.is_dir():
        return
    dirs = sorted((x for x in root.iterdir() if x.is_dir() and x.name != ".pruned"),
                  key=lambda x: x.stat().st_mtime, reverse=True)
    keep = _keep()
    over = dirs[keep:]
    if not over:
        return
    pruned = root / ".pruned"
    pruned.mkdir(parents=True, exist_ok=True)
    for d in over:
        try:
            dst = pruned / d.name
            k = 1
            while dst.exists():
                dst = pruned / f"{d.name}_{k}"
                k += 1
            shutil.move(str(d), str(dst))
        except Exception:
            pass


def guarded_write(reason: str, writer, names=None, allow_shrink=()) -> dict:
    """**保险入口**：快照 → 执行 writer() → 校验 → 失败自动回滚。

    `writer` 是个无参可调用（真正的写盘动作）。返回
    {"ok", "snapshot", "verified", "restored", "issues"}。
    校验失败会回滚并把 ok=False 返回**而不抛异常**——保住老数据比暴露异常更重要，
    但调用方应当把 ok 当回事（记日志/提示用户）。
    """
    names = names or PROTECTED
    snap = snapshot(reason, names)
    err = None
    try:
        writer()
    except Exception as exc:  # noqa: BLE001
        err = exc
    report = {"ok": True, "snapshot": snap, "verified": None,
              "restored": None, "issues": [], "error": None}
    if err is not None:
        report["ok"] = False
        report["error"] = str(err)
        if snap:
            try:
                report["restored"] = restore(snap).get("restored")
                report["ok"] = True      # 回滚成功 = 数据保住了
                report["error"] = f"写入异常已回滚：{err}"
            except Exception as rexc:  # noqa: BLE001
                report["error"] = f"写入异常且回滚失败：{err} / {rexc}"
        return report
    if _verify_enabled():
        v = verify(names, allow_shrink=allow_shrink)
        report["verified"] = v["counts"]
        if not v["ok"]:
            report["ok"] = False
            report["issues"] = v["issues"]
            if snap:
                try:
                    report["restored"] = restore(snap).get("restored")
                    report["ok"] = True   # 已回滚，数据一致
                except Exception as rexc:  # noqa: BLE001
                    report["issues"].append(f"回滚失败：{rexc}")
    return report
