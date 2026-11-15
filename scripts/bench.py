#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 性能基准（第四轮提案 #5）。

生成 N 条合成记忆到**临时隔离 HOME**（绝不碰真实记忆库），量四条主路径的
p50/p95 耗时，回答"5000 条时还快不快"这类问题——改召回阈值/换 embed 兜底
前后各跑一次，用数字代替手感。

用法：
    python scripts/bench.py                 # 默认 1000 条，常规套件
    python scripts/bench.py --n 5000        # 大库压力
    python scripts/bench.py --quick         # 300 条，1 分钟内出结果

零依赖，纯标准库。退出码 0=全部完成。
"""

from __future__ import annotations

import argparse
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRAIN = ROOT / "brain.py"

TOPICS = ["部署", "数据库", "前端", "算法", "会议", "测试", "文档", "安全",
          "缓存", "权限", "日志", "重构", "网络", "备份", "监控", "配置"]
USERS = ["alice", "bob", "carol"]
PROJECTS = ["proj-alpha", "proj-beta", "proj-gamma"]


def _run(args: list, home: Path, timeout: float = 120, strict: bool = True) -> tuple[float, str]:
    """跑一次 brain.py 子进程，返回 (耗时秒, stdout)。strict=False 时不校验退出码。"""
    env = {**__import__("os").environ,
           "BRAIN_MEMORY_HOME": str(home),
           "BRAIN_MEMORY_BACKUP_DIR": str(home / "backups")}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(BRAIN), *args],
                       capture_output=True, text=True, timeout=timeout, env=env)
    dt = time.perf_counter() - t0
    if strict and r.returncode not in (0,):
        raise RuntimeError(f"brain.py {' '.join(args)} 退出码 {r.returncode}: {r.stderr[-300:]}")
    return dt, r.stdout


def _pctl(samples: list) -> dict:
    """p50 / p95（样本少时 p95 就是最大值近似）。"""
    xs = sorted(samples)
    n = len(xs)
    return {"p50": statistics.median(xs),
            "p95": xs[max(0, int(round(n * 0.95)) - 1)] if n else 0.0,
            "n": n}


def main() -> int:
    ap = argparse.ArgumentParser(description="灵枢性能基准（隔离临时库，不碰真实数据）")
    ap.add_argument("--n", type=int, default=1000, help="合成记忆条数（默认 1000）")
    ap.add_argument("--quick", action="store_true", help="快速模式：n=300")
    ap.add_argument("--keep", action="store_true", help="保留临时库（调试用）")
    opts = ap.parse_args()
    if opts.quick:
        opts.n = min(opts.n, 300)

    home = Path(tempfile.mkdtemp(prefix="lingshu-bench-"))
    print(f"[bench] 隔离 HOME：{home}")
    rng = random.Random(20261115)

    results: dict[str, list] = {"remember": [], "recall": [], "inject": [], "doctor": []}
    try:
        # 0. 初始化
        _run(["init"], home)

        # 1. 写入基准：批量 remember（每条一个子进程也量得到真实端到端成本）
        print(f"[bench] 生成 {opts.n} 条合成记忆…")
        t0 = time.perf_counter()
        for i in range(opts.n):
            topic = TOPICS[i % len(TOPICS)]
            text = (f"{topic}方案第{i}条：{rng.choice(['决定', '发现', '总结'])}"
                    f"{topic}相关做法{rng.randint(1, 999)}，注意版本{rng.randint(1, 9)}")
            _run(["remember", text, "--kind", "fact", "--importance",
                  str(rng.choice([0.4, 0.5, 0.7, 0.7, 0.8, 0.9])),
                  "--project", PROJECTS[i % len(PROJECTS)]], home, timeout=60)
            results["remember"].append(time.perf_counter() - t0)
            t0 = time.perf_counter()
        # 批量写完后再补几条批量型写入：单条走 --importance 的解析已覆盖，
        # 这里换 fast 路径量小查询延迟基线（在空查询上测意义不大，略）。

        # 2. 召回基准：混合 fast/deep 模式查询
        for i in range(20):
            q = rng.choice(TOPICS) + rng.choice(["方案", "做法", "决定"])
            dt, _ = _run(["recall", q, "--k", "8"], home, timeout=60)
            results["recall"].append(dt)
        # 同项目召回（第四轮#3 加成路径）
        for i in range(5):
            dt, _ = _run(["recall", "方案做法", "--k", "8",
                          "--project", rng.choice(PROJECTS)], home, timeout=60)
            results["recall"].append(dt)

        # 3. 注入基准：build_injection 主路径（空 query = 无定向注入）
        for i in range(10):
            dt, _ = _run(["inject", "--budget", "2000", ""], home, timeout=60)
            results["inject"].append(dt)

        # 4. 体检基准：doctor 全检查（含 engine-sync 子进程）——隔离 HOME 下
        #    engine-sync 等检查项可能报 FAIL/WARN 属预期（退出码 2），只量耗时
        dt, _ = _run(["doctor"], home, timeout=120, strict=False)
        results["doctor"].append(dt)

        # 报告
        print("\n===== 灵枢性能基准结果 =====")
        print(f"库规模：{opts.n} 条合成记忆  |  隔离 HOME：{home if opts.keep else '(已清理)'}")
        for name, samples in results.items():
            if not samples:
                continue
            p = _pctl(samples)
            print(f"{name:<10} n={p['n']:>3}  p50={p['p50']*1000:8.0f} ms  "
                  f"p95={p['p95']*1000:8.0f} ms")
        print("说明：单次调用含 Python 启动 ~100ms 基线；关注相对变化而非绝对值。")
        return 0
    except Exception as e:
        print(f"[bench] 失败：{e}", file=sys.stderr)
        return 1
    finally:
        if not opts.keep:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
