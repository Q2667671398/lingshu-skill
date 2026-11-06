#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""engine 副本同步：根 scripts/*.py ↔ scripts/astrbot_plugin_lingshu/engine/scripts/*.py。

AstrBot 桥接插件内置一份核心引擎副本（engine/scripts/）。两边内容必须一致，
否则用户装插件得到旧核心。历史教训：CRLF 差异会让"裸 sha256 比较"全部误报，
因此本脚本按「换行归一化（CRLF→LF）后逐字比较」为准。

用法：
  python scripts/sync_engine.py           # 把根 scripts 的 .py 同步到 engine 副本
  python scripts/sync_engine.py --check   # 只校验一致性，不一致退出码 1（CI/pre-commit 用）
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts"
DST = ROOT / "scripts" / "astrbot_plugin_lingshu" / "engine" / "scripts"

# 不参与同步的辅助脚本（桥接侧用不到 / 依赖宿主环境）
SKIP = {"sync_engine.py", "make_manifest.py", "make_checksums.py",
        "check_version.py", "make_plugin_zip.py", "panel_server.py"}


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sources() -> list[Path]:
    return sorted(p for p in SRC.glob("*.py") if p.name not in SKIP and not p.name.startswith("test_"))


def sync() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    changed = 0
    for src in sources():
        dst = DST / src.name
        if dst.exists() and _norm(dst.read_text(encoding="utf-8-sig")) == _norm(src.read_text(encoding="utf-8-sig")):
            continue
        shutil.copy2(src, dst)
        changed += 1
    # 反向清理：副本里有、根目录已删掉的脚本一并移除，避免僵尸核心
    removed = 0
    keep = {p.name for p in sources()}
    for dst in sorted(DST.glob("*.py")):
        if dst.name not in keep:
            dst.unlink()
            removed += 1
    print(f"[sync] engine 副本已同步：更新 {changed} 个，清理 {removed} 个")
    return 0


def check() -> int:
    bad = []
    names = {p.name for p in sources()}
    for src in sources():
        dst = DST / src.name
        if not dst.exists():
            bad.append(f"{src.name}: 副本缺失")
            continue
        if _norm(dst.read_text(encoding="utf-8-sig")) != _norm(src.read_text(encoding="utf-8-sig")):
            bad.append(f"{src.name}: 内容不一致（换行归一化后）")
    for dst in sorted(DST.glob("*.py")):
        if dst.name not in names:
            bad.append(f"{dst.name}: 副本多出僵尸文件（根目录已删除）")
    if bad:
        print("[sync] engine 副本与根 scripts 不一致：", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        print("运行 python scripts/sync_engine.py 修复。", file=sys.stderr)
        return 1
    print(f"[sync] OK：engine 副本 {len(names)} 个脚本与根 scripts 一致（换行归一化后）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="灵枢：engine 副本同步与校验")
    ap.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    args = ap.parse_args(argv)
    return check() if args.check else sync()


if __name__ == "__main__":
    sys.exit(main())
