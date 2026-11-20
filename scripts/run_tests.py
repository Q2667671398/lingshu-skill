# -*- coding: utf-8 -*-
"""灵枢测试统一入口：**先隔离 HOME，再逐个跑**。

为什么需要它：测试都是纯标准库脚本，直接 `python tests/test_xxx.py` 跑时，
`BRAIN_MEMORY_HOME` 没设就是真实记忆库——有些用例（如 `test_release_drift` 里
模拟扫描失败的那条）会通过 `core.log_event` 往真实 `~/.brain-memory/lingshu.log`
写日志。2026-11-20 就因此在本机真库日志里留下两条假的「扫描 DSH 副本失败」，
排障时把我自己带偏过一次。

本入口做三件事：
  1. 把 `BRAIN_MEMORY_HOME` / `LINGSHU_GLOBAL_SKILLS` 指到临时目录（每个测试文件
     一个独立目录，互不干扰，跑完删掉）；
  2. 逐个运行 `tests/test_*.py`，失败时打印该文件最后 40 行输出；
  3. 汇总 `N/M 通过`，有失败则退出码 1。

用法：
    python scripts/run_tests.py            # 全量
    python scripts/run_tests.py restore    # 只跑文件名含 restore 的
    python scripts/run_tests.py -v         # 打印每个文件的 PASS 行
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    keywords = [a for a in argv if not a.startswith("-")]
    files = sorted(TESTS.glob("test_*.py"))
    if keywords:
        files = [f for f in files if any(k in f.name for k in keywords)]
    if not files:
        print("[错误] 没有匹配的测试文件", file=sys.stderr)
        return 2

    print(f"共 {len(files)} 个测试文件（每个独立临时 HOME）")
    failed: list[tuple[str, str]] = []
    ran = 0
    for f in files:
        ran += 1
        tmp = Path(tempfile.mkdtemp(prefix="ls-test-"))
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(tmp / "home")
        env["LINGSHU_GLOBAL_SKILLS"] = str(tmp / "skills")
        env["PYTHONIOENCODING"] = "utf-8"
        (tmp / "home").mkdir(parents=True, exist_ok=True)
        (tmp / "skills").mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800, env=env)
        except subprocess.TimeoutExpired:
            failed.append((f.name, "超时（>30 分钟）"))
            print(f"FAIL {f.name} 超时")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            print(f"PASS {f.name}")
            if verbose:
                for line in out.splitlines():
                    if line.startswith(("PASS ", "FAIL ", "ERROR ", "   SKIP")):
                        print("     " + line)
        else:
            tail = "\n".join(out.strip().splitlines()[-40:])
            failed.append((f.name, tail))
            print(f"FAIL {f.name}（exit {r.returncode}）")
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        for name, detail in failed:
            print("=" * 70)
            print(f"失败：{name}")
            print(detail)
        print()
        print(f"{ran - len(failed)}/{ran} 通过，{len(failed)} 个失败")
        return 1
    print(f"{ran}/{ran} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
