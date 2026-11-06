#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新生成 scripts/astrbot_plugin_lingshu/CHECKSUMS.json（#7）。

与根 make_manifest.py 共用归一化逻辑：读文件按 utf-8-sig，内容按 CRLF→LF
归一化后哈希，跨平台（Windows 开发 / Linux 部署）不会因换行差异误报。

用法：
  python scripts/astrbot_plugin_lingshu/make_checksums.py            # 重新生成
  python scripts/astrbot_plugin_lingshu/make_checksums.py --check    # 只校验（CI 用）
改动插件任何文件后必须重跑，否则 CHECKSUMS.json 失真、发布完整性校验无意义。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKSUMS = HERE / "CHECKSUMS.json"

# 只纳入会随版本变化的发布文件；engine/ 整目录递归
INCLUDE_PREFIXES = ("engine/",)
INCLUDE_FILES = {"main.py", "_conf_schema.json", "metadata.yaml", "README.md", "PATCH_NOTES.md"}
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _norm(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _sha(abs_path: Path) -> str:
    return hashlib.sha256(_norm(abs_path.read_bytes())).hexdigest()


def scan() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(HERE.rglob("*")):
        rel = p.relative_to(HERE)
        posix = rel.as_posix()
        parts = set(rel.parts)
        if parts & EXCLUDE_PARTS or rel.suffix.lower() in EXCLUDE_SUFFIXES or not p.is_file():
            continue
        if posix in INCLUDE_FILES or any(posix.startswith(x) for x in INCLUDE_PREFIXES):
            out[posix] = _sha(p)
    return out


def main(argv: list[str] | None = None) -> int:
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    current = scan()
    if check:
        try:
            recorded = json.loads(CHECKSUMS.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            print("[checksums] CHECKSUMS.json 缺失或损坏，运行 make_checksums.py 重新生成",
                  file=sys.stderr)
            return 1
        bad = []
        for k, v in current.items():
            if recorded.get(k) != v:
                bad.append(k)
        extra = sorted(set(recorded) - set(current))
        if bad or extra:
            if bad:
                print(f"[checksums] 不一致（{len(bad)} 个）：{bad[:5]}", file=sys.stderr)
            if extra:
                print(f"[checksums] 清单里磁盘上没有的条目：{extra[:5]}", file=sys.stderr)
            print("运行 python scripts/astrbot_plugin_lingshu/make_checksums.py 重新生成。",
                  file=sys.stderr)
            return 1
        print(f"[checksums] OK：{len(current)} 个文件哈希一致（换行归一化后）")
        return 0
    CHECKSUMS.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    print(f"[checksums] 已重新生成：{len(current)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
