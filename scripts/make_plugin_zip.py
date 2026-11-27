#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 AstrBot 插件 zip（#24）。

在仓库根生成 astrbot.zip（.gitignore 已忽略），内容为
scripts/astrbot_plugin_lingshu/ 整目录（顶层目录名 astrbot_plugin_lingshu/，
AstrBot 可直接上传安装）。排除 __pycache__、.pyc、运行数据。

**打包前会先跑 sync_engine.sync()**（P1-1）：engine/ 是根目录 scripts/docs 的
副本，两份都在 git 里维护代价太大，正解是"打包时生成、不打进版本库"。所以这里
不再假设 engine/ 已就绪，而是每次都从根目录重新铺一遍，杜绝把陈旧副本打进包里。
确实需要跳过（比如刚跑过、只想重新压一次）用 `--no-sync`。

用法：python scripts/make_plugin_zip.py [--out astrbot.zip] [--no-sync]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "astrbot_plugin_lingshu"
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip"}
EXCLUDE_NAMES = {"panel-token.txt", ".home", "consent.json",
                 "hippocampus.jsonl", "persona.json", "emotions.jsonl"}


def collect() -> list[Path]:
    files = []
    for p in sorted(SRC.rglob("*")):
        if p.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in p.relative_to(SRC).parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIX or p.name in EXCLUDE_NAMES:
            continue
        files.append(p)
    return files


def make(out: Path) -> Path:
    if not SRC.is_dir():
        print(f"[错误] 找不到插件目录：{SRC}", file=sys.stderr)
        raise SystemExit(1)
    files = collect()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, Path("astrbot_plugin_lingshu") / p.relative_to(SRC))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 AstrBot 插件 zip")
    ap.add_argument("--out", default=str(REPO / "astrbot.zip"))
    ap.add_argument("--no-sync", action="store_true",
                    help="跳过 engine 副本同步（默认打包前会从根目录重新生成）")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if not args.no_sync:
        try:
            import sync_engine
            if sync_engine.sync() != 0:
                print("[错误] engine 副本同步失败，拒绝打出可能过期的包", file=sys.stderr)
                return 1
        except SystemExit as exc:  # sync_engine 里若有 sys.exit
            if exc.code not in (0, None):
                print(f"[错误] engine 同步异常退出：{exc.code}", file=sys.stderr)
                return 1
        except Exception as exc:  # noqa: BLE001  绝不让打包静默带着旧副本
            print(f"[错误] engine 同步失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if out.exists():
        out.unlink()
    make(out)
    n = len(collect())
    print(f"OK {out} ({n} files)")
    return 0


def _ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台（cp1252/cp936）编不了中文就崩。

    CI 的 windows-latest stdout 是 cp1252：脚本一 print 中文就 UnicodeEncodeError
    直接退出（实测 sync_engine.py / make_checksums.py 都因此挂过，Ubuntu 同期正常）。
    这不是业务 bug，是"中文输出 + 非 UTF-8 控制台"的必然结果。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        fn = getattr(stream, "reconfigure", None)
        if callable(fn):
            try:
                fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    sys.exit(main())
