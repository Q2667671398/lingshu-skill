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


def sha256_file(p: Path) -> tuple[str, int]:
    """规范形态哈希：CRLF→LF 归一化 + 去 UTF-8 BOM 后再算，返回 (sha256, size)。

    manifest 是发布契约——它必须对应"发布内容的规范形态"，而不是作者本机的
    磁盘形态。作者机器 core.autocrlf=true + 记事本存盘会把工作区文件写成
    CRLF+带 BOM，逐字节哈希记录的就是这套形态；用户按 raw / zip 拿到的是
    LF+无 BOM，逐字节比对永远失败（v2.52 只修了比对侧，没修生成侧）。
    换行符与 BOM 都不改变内容语义，归一化后哈希才与发布内容对齐；
    真正的内容篡改仍拦得住。

    分块注意：\r 若恰好落在 64KB 块尾必须暂存到下一块拼回来再替换——否则
    边界处的 CRLF 被切成 \r|\n 两段各自漏掉，大文件哈希漂移（与比对侧同一坑）。
    """
    h = hashlib.sha256()
    size = 0
    first = True
    tail = b""
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            if first:
                first = False
                if chunk.startswith(b"\xef\xbb\xbf"):
                    chunk = chunk[3:]
            chunk = tail + chunk
            tail = chunk[-1:] if chunk.endswith(b"\r") else b""
            if tail:
                chunk = chunk[:-1]
            chunk = chunk.replace(b"\r\n", b"\n")
            h.update(chunk)
            size += len(chunk)
    if tail:
        h.update(tail.replace(b"\r\n", b"\n"))
        size += len(tail)
    return h.hexdigest(), size


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
            sha, size = sha256_file(p)
            files[name] = {"sha256": sha, "size": size}
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
            sha, size = sha256_file(f)
            files[rel] = {"sha256": sha, "size": size}
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
    # 覆盖面自检（#73：逐文件比对）：发布目录里每个"应发布"的磁盘文件都必须在
    # 清单里；反向清单里也不该有磁盘上不存在的 key。整目录漏扫（如 cordis-plugin
    # 没进 SCAN_DIRS）会让该目录全部文件报"漏"，部分漏（10 进 1）也能检出。
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        disk = {f.relative_to(root).as_posix()
                for f in base.rglob("*") if f.is_file()
                and not _excluded(f.relative_to(root).as_posix(), f.name)}
        keys = {k for k in cur["files"] if k.startswith(d + "/")}
        missing = sorted(disk - keys)
        extra = sorted(keys - disk)
        if missing or extra:
            if missing:
                print(f"[manifest] 拒绝：{d}/ 下 {len(missing)} 个磁盘文件不在清单（如 {missing[:3]}），"
                      "检查 SCAN_DIRS 与排除规则", file=sys.stderr)
            if extra:
                print(f"[manifest] 拒绝：{d}/ 清单里有 {len(extra)} 个磁盘上不存在的文件（如 {extra[:3]}）",
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
