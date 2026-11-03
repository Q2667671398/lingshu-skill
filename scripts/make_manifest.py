#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成技能发布清单 manifest.json（文件相对路径 + SHA256 + size + version）。

用法：
  python scripts/make_manifest.py            # 在仓库根生成 manifest.json
  python scripts/make_manifest.py --check    # 只校验现有 manifest 与工作区是否一致

扫描范围：SKILL.md、SKILL-lite.md、SKILL-tiny.md、scripts/、assets/、docs/、
cordis-plugin/、CHANGELOG.md、README.md、package.json、cordis.patch.yml、
index.js、start-panel.bat。
排除：__pycache__、.git、*.pyc、.lingshu-* 标记、_ 开头的临时目录、
scripts/test_*.py（自测脚本）、scripts/astrbot_plugin_lingshu/（宿主侧插件，
独立发布，不随灵枢安装）。

清单即发布契约：apply_update 只部署清单里的文件，所以漏扫目录 = 文件永远
装不到用户机器；check() 会做目录覆盖面自检，防止再漏。

manifest 内置 version 与 SKILL.md 一致性校验，不一致 exit 非 0；
pre-commit 钩子与 check_version.py 可复用本文件的 read functions。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^version:\s*([0-9][0-9.]*)\s*$", re.MULTILINE)
SCAN_FILES = ["SKILL.md", "SKILL-lite.md", "SKILL-tiny.md", "CHANGELOG.md",
              "README.md", "package.json", "cordis.patch.yml", "index.js",
              "start-panel.bat"]
SCAN_DIRS = ["scripts", "assets", "docs", "cordis-plugin"]
EXCLUDE_PARTS = {"__pycache__", ".git"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".tmp", ".lock", ".corrupt", ".log")
EXCLUDE_NAMES = {".lingshu-source", ".lingshu-sha", "AGENTS.md", "manifest.json"}
# 不随技能发布的子树：AstrBot 宿主插件（需独立装进 AstrBot 的 data/plugins）
EXCLUDE_PREFIXES = ("scripts/astrbot_plugin_lingshu/",)


def _excluded(rel: str, name: str) -> bool:
    """rel 是否属于发布排除项（测试脚本、宿主插件、缓存/临时文件等）。"""
    if (EXCLUDE_PARTS & set(rel.split("/"))
            or rel.endswith(EXCLUDE_SUFFIX)
            or name in EXCLUDE_NAMES
            or name.startswith("_")
            or rel.startswith(EXCLUDE_PREFIXES)):
        return True
    # 自测脚本（test_*.py）是开发资产，不装进用户机器
    return name.startswith("test_") and name.endswith(".py")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_skill_version(root: Path = ROOT) -> str:
    m = VERSION_RE.search((root / "SKILL.md").read_text(encoding="utf-8-sig"))
    if not m:
        raise SystemExit("[manifest] SKILL.md 里读不出 version 字段")
    return m.group(1).strip()


def scan_files(root: Path = ROOT) -> dict:
    """返回 {相对路径(posix): {"sha256":…, "size":…}}，键已排序。"""
    files: dict[str, dict] = {}
    for name in SCAN_FILES:
        p = root / name
        if p.is_file():
            files[name] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            if _excluded(rel, f.name):
                continue
            files[rel] = {"sha256": sha256_file(f), "size": f.stat().st_size}
    return dict(sorted(files.items()))


def build_manifest(root: Path = ROOT) -> dict:
    version = read_skill_version(root)
    return {"version": version, "files": scan_files(root)}


def check(root: Path = ROOT) -> int:
    mf = root / "manifest.json"
    if not mf.is_file():
        print("[manifest] manifest.json 不存在，先用 --build 生成", file=sys.stderr)
        return 1
    cur = build_manifest(root)
    old = json.loads(mf.read_text(encoding="utf-8"))
    old_v = str(old.get("version", ""))
    if old_v != cur["version"]:
        print(f"[manifest] 拒绝：manifest.json version（{old_v}）与 SKILL.md version"
              f"（{cur['version']}）不一致。重新运行 python scripts/make_manifest.py。",
              file=sys.stderr)
        return 1
    if old.get("files") != cur["files"]:
        drift = sorted(set(old.get("files") or {}) ^ set(cur["files"]))[:5]
        print(f"[manifest] manifest.json 与工作区不一致（如 {drift}）。"
              "重新运行 python scripts/make_manifest.py。", file=sys.stderr)
        return 1
    # 覆盖面自检：发布目录里只要还有该发布的文件，就必须有人进清单。
    # 只比对「清单 vs 工作区」发现不了整个目录漏扫（如 cordis-plugin）——那种情况下
    # 两边都没有该目录的文件，校验照样通过，但插件永远装不到用户机器。
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        inside = [f for f in base.rglob("*") if f.is_file()
                  and not _excluded(f.relative_to(root).as_posix(), f.name)]
        if inside and not any(k.startswith(d + "/") for k in cur["files"]):
            print(f"[manifest] 拒绝：{d}/ 存在但没有文件进清单，检查 SCAN_DIRS 与排除规则",
                  file=sys.stderr)
            return 1
    print(f"[manifest] OK：v{cur['version']}，{len(cur['files'])} 个文件哈希全部一致")
    return 0


def build(root: Path = ROOT) -> int:
    data = build_manifest(root)
    out = root / "manifest.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"[manifest] 已生成 {out}：v{data['version']}，{len(data['files'])} 个文件")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="灵枢：生成/校验 manifest.json 发布清单")
    ap.add_argument("--check", action="store_true", help="校验现有 manifest 与工作区一致")
    args = ap.parse_args(argv)
    return check() if args.check else build()


if __name__ == "__main__":
    sys.exit(main())
