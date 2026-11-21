#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""npm publish 前的准备：生成 engine 副本 + 清掉 pycache，保证包里没有垃圾。

npm 的 files 白名单里的目录无法被 .npmignore 排除，所以 pycache 必须在
打包前从磁盘上清掉，而不是靠 ignore 规则。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sync_engine  # noqa: E402

if sync_engine.sync() != 0:
    raise SystemExit("[prepack] engine 副本同步失败，拒绝发布空壳包")

removed = 0
for d in ROOT.rglob("__pycache__"):
    # 只清仓库内的（数据目录不在仓库下，不受影响）
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
for p in ROOT.rglob("*.pyc"):
    p.unlink(missing_ok=True)
    removed += 1
print(f"[prepack] engine 已同步，清理 {removed} 个 pyc 缓存")
