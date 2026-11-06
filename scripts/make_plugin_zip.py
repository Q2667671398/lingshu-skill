#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 AstrBot 插件 zip（#24）。

在仓库根生成 astrbot.zip（.gitignore 已忽略），内容为
scripts/astrbot_plugin_lingshu/ 整目录（顶层目录名 astrbot_plugin_lingshu/，
AstrBot 可直接上传安装）。排除 __pycache__、.pyc、运行数据。

用法：python scripts/make_plugin_zip.py [--out astrbot.zip]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

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
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if out.exists():
        out.unlink()
    make(out)
    n = len(collect())
    print(f"OK {out} ({n} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
