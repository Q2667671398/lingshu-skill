#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""engine 副本同步：根 scripts/*.py ↔ scripts/astrbot_plugin_lingshu/engine/scripts/*.py。

AstrBot 桥接插件内置一份核心引擎副本（engine/scripts/）。两边内容必须一致，
否则用户装插件得到旧核心。历史教训：CRLF 差异会让"裸 sha256 比较"全部误报，
因此本脚本按「换行归一化（CRLF→LF）后逐字比较」为准。

**文档副本也归这里管**（v3.47）：插件里还随包发 SKILL.md / README / CHANGELOG /
docs，以前从没人同步，实测插件里躺着一份 v2.87 的 SKILL.md（主仓库已 v3.47，差
60 个版本）——AstrBot 用户看到的说明书是半年前的。现在它们与 .py 一起校验。

用法：
  python scripts/sync_engine.py           # 把根目录的 .py 与文档同步到 engine 副本
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
ENGINE = ROOT / "scripts" / "astrbot_plugin_lingshu" / "engine"

# 不参与同步的辅助脚本（桥接侧用不到 / 依赖仓库根的其他文件）
# 注意：panel_server.py **不在此列**（v3.47 B 方案）。插件 README 的「打开面板 → 拓展商店
# 一键导出」流程要求 `engine/scripts/panel_server.py` 真的存在；它只依赖标准库 + 同目录
# 的 brain/panels/growth，`ASSETS` 按 `__file__` 的父父目录解析，正好指向 engine/assets。
# 以前把它跳过，插件里就只剩一份跑不起来的面板产物。
# run_tests.py 是开发期跑测入口（v3.54），桥接侧不需要，不进副本。
SKIP = {"sync_engine.py", "make_manifest.py", "make_checksums.py",
        "check_version.py", "make_plugin_zip.py", "run_tests.py"}

# 随插件发布的文件副本（路径相对仓库根，保持同名同目录）。
# assets/panel.html 是 panel_src 的构建产物：`panel_build.py --check` 保证源码↔产物一致，
# 这里再保证「主仓库产物 ↔ 插件副本一致」——两道闸合起来，插件里的面板不会再停在旧代。
DOC_FILES = [
    "SKILL.md", "SKILL-lite.md", "SKILL-tiny.md", "README.md", "CHANGELOG.md",
    "docs/panel-api.md", "docs/commands.md", "docs/mechanics.md", "docs/models.md",
    "docs/recovery.md", "assets/panel.html",
]


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def sources() -> list[Path]:
    return sorted(p for p in SRC.glob("*.py") if p.name not in SKIP and not p.name.startswith("test_"))


def sync() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    changed = 0
    for src in sources():
        dst = DST / src.name
        if dst.exists() and _norm(_read(dst)) == _norm(_read(src)):
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
    # 文档副本：主仓库有就同步过去，主仓库删了就同步删掉（不给插件留过期页面）
    doc_changed = doc_removed = 0
    for rel in DOC_FILES:
        src, dst = ROOT / rel, ENGINE / rel
        if not src.is_file():
            if dst.exists():
                dst.unlink()
                doc_removed += 1
            continue
        if dst.exists() and _norm(_read(dst)) == _norm(_read(src)):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        doc_changed += 1
    print(f"[sync] engine 副本已同步：脚本 更新 {changed} 个 / 清理 {removed} 个；"
          f"文档 更新 {doc_changed} 个 / 清理 {doc_removed} 个")
    return 0


def check() -> int:
    bad = []
    names = {p.name for p in sources()}
    for src in sources():
        dst = DST / src.name
        if not dst.exists():
            bad.append(f"{src.name}: 副本缺失")
            continue
        if _norm(_read(dst)) != _norm(_read(src)):
            bad.append(f"{src.name}: 内容不一致（换行归一化后）")
    for dst in sorted(DST.glob("*.py")):
        if dst.name not in names:
            bad.append(f"{dst.name}: 副本多出僵尸文件（根目录已删除）")
    doc_checked = 0
    for rel in DOC_FILES:
        src, dst = ROOT / rel, ENGINE / rel
        if not src.is_file():
            if dst.exists():
                bad.append(f"{rel}: 主仓库已删除，但副本还在")
            continue
        doc_checked += 1
        if not dst.exists():
            bad.append(f"{rel}: 文档副本缺失")
        elif _norm(_read(dst)) != _norm(_read(src)):
            bad.append(f"{rel}: 文档副本过期（内容与主仓库不一致）")
    if bad:
        print("[sync] engine 副本与根 scripts / 文档不一致：", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        print("运行 python scripts/sync_engine.py 修复。", file=sys.stderr)
        return 1
    print(f"[sync] OK：engine 副本 {len(names)} 个脚本 + {doc_checked} 个文档"
          f"与根目录一致（换行归一化后）")
    return 0


def _ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台（cp1252/cp936）编不了中文就崩。

    为什么必须有：CI 的 windows-latest stdout 是 cp1252，脚本一 print 中文就
    UnicodeEncodeError 直接退出（实测 sync_engine.py 因此挂掉，Ubuntu 同期正常）。
    这不是业务 bug，是"中文输出 + 非 UTF-8 控制台"的必然结果。

    reconfigure 不可用（3.6 及更早 / 被替换过的流）时静默跳过，绝不因此再崩。
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
        # 退路：包一层 UTF-8 writer（errors=replace，坏字符变 ?，不打断程序）
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="灵枢：engine 副本同步与校验")
    ap.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    args = ap.parse_args(argv)
    return check() if args.check else sync()


if __name__ == "__main__":
    _ensure_utf8_stdout()
    sys.exit(main())
