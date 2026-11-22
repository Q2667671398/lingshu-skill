#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水位阈值双份默认值一致性检查（P6 #28）。

core.py 的 PRESSURE_DEFAULTS 与 lingshu-cron/plugin.js 的同名常量是两份默认值
（config.json 的 pressure 段可覆盖两边，所以只有"改默认值忘改另一边"才是风险）。
本检查从两处源码各解析一份字面量，不一致就 exit=1，让"改一处忘另一处"在提交前
被拦住，而不是等用户发现提示档位与实际执行档位错位。

用法：python scripts/check_pressure_sync.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_FILE = ROOT / "scripts" / "core.py"
JS_FILE = ROOT / "cordis-plugin" / "lingshu-cron" / "plugin.js"
KEYS = ("lazy_at", "minimal_at", "hard_at", "force_at")


def _parse_py() -> dict:
    m = re.search(r"PRESSURE_DEFAULTS\s*=\s*\{([^}]*)\}", PY_FILE.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("[压力阈值检查] core.py 未找到 PRESSURE_DEFAULTS 字面量")
    return {k: int(v) for k, v in re.findall(r'["\']?(\w+)["\']?\s*:\s*(\d+)', m.group(1))}


def _parse_js() -> dict:
    m = re.search(r"const PRESSURE_DEFAULTS\s*=\s*\{([^}]*)\}", JS_FILE.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("[压力阈值检查] plugin.js 未找到 PRESSURE_DEFAULTS 字面量")
    return {k: int(v) for k, v in re.findall(r'["\']?(\w+)["\']?\s*:\s*(\d+)', m.group(1))}


def main() -> int:
    py, js = _parse_py(), _parse_js()
    bad = []
    for k in KEYS:
        a, b = py.get(k), js.get(k)
        if a != b:
            bad.append(f"{k}: core.py={a} / plugin.js={b}")
    if bad:
        print("[压力阈值检查] 不一致（两边默认值必须相同，或同步改 config 的 pressure 段）：")
        for line in bad:
            print("  - " + line)
        return 1
    print(f"[压力阈值检查] OK：{', '.join(f'{k}={py[k]}' for k in KEYS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
