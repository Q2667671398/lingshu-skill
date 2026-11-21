#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打 DSH 可安装 tgz（tgz 直链方案）。

产物 lingshu-skill-<version>.tgz 符合 npm/pnpm tgz 结构（顶层 package/），
DSH 读 package.json 的 dsh.bundle.patch 挂载插件。与 git 源码安装的区别：
pnpm 下载 tgz 解压即可，**不需要 git、不需要 clone GitHub**——给网络受限
（国内 GitHub ECONNREFUSED / 没装 git）的用户直接装。

打包前先跑 sync_engine.sync()，把 engine/ 副本从根 scripts/docs 生成出来
（engine/ 已从 git 移除、不入库，但插件运行需要它），确保 tgz 不是空壳。
打包内容在 manifest.json 的发布文件基础上 **额外包含整个 engine/ 目录**，
排除 __pycache__/.pyc/本地运行数据。

用法：python scripts/make_dsh_tgz.py [--out <路径>]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

EXCLUDE_PARTS = {"__pycache__", ".git", "node_modules", "downloads", "verbatim"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".tgz", ".jsonl", ".tmp"}
EXCLUDE_NAMES = {"panel-token.txt", ".env", "LINGSHU_PANEL_TOKEN", "config.json"}


def collect() -> list[Path]:
    """manifest 发布文件 + engine/ 全目录（都要先 sync 生成）。"""
    # manifest.json 里的发布文件（git 跟踪的核心文件）
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    files = [ROOT / rel for rel in manifest["files"]]
    # engine/ 派生副本（打包时生成，不入库）
    engine = ROOT / "scripts" / "astrbot_plugin_lingshu" / "engine"
    if engine.is_dir():
        for p in engine.rglob("*"):
            if p.is_file():
                files.append(p)
    # 去重 + 过滤
    out, seen = [], set()
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if rel in seen or not p.is_file():
            continue
        if any(part in EXCLUDE_PARTS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIX or p.name in EXCLUDE_NAMES:
            continue
        seen.add(rel)
        out.append(p)
    return sorted(out, key=lambda p: p.relative_to(ROOT).as_posix())


def make(out: Path) -> Path:
    import sync_engine
    if sync_engine.sync() != 0:
        raise SystemExit("[错误] engine 副本同步失败，拒绝打空壳包")
    files = collect()
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = pkg["version"]
    out.parent.mkdir(parents=True, exist_ok=True)
    # npm tgz：gzip + 顶层 package/ 目录 + pax headers
    with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for p in files:
            rel = "package/" + p.relative_to(ROOT).as_posix()
            info = tf.gettarinfo(str(p), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with p.open("rb") as fh:
                tf.addfile(info, fh)
    print(f"[tgz] {out}  v{version}  {len(files)} 文件  {out.stat().st_size/1024:.0f} KB")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打 DSH 可安装 tgz（tgz 直链方案）")
    ver = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    ap.add_argument("--out", default=str(ROOT / f"lingshu-skill-{ver}.tgz"))
    args = ap.parse_args()
    make(Path(args.out).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
