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
    python scripts/bench.py --quick --save  # 把本次 p50/p95 存成基线
    python scripts/bench.py --quick --check # 与基线比对，超 1.5 倍即失败

零依赖，纯标准库。退出码 0=全部完成；--check 超阈值退出 1。
基线文件放 tests/bench-baseline.json（tests 不进发布 manifest，随仓库走）。
"""

from __future__ import annotations

import argparse
import json
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
BASELINE = ROOT.parent / "tests" / "bench-baseline.json"
CHECK_RATIO = 1.5

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
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
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


def _save_baseline(n: int, results: dict) -> None:
    """把本次 p50/p95 写成基线（同机同 n 的后续 --check 用它比对）。"""
    data = {"n": n, "generated": time.strftime("%Y-%m-%d %H:%M"),
            "results": {k: _pctl(v) for k, v in results.items() if v}}
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"[bench] 基线已保存：{BASELINE}")


def _check_baseline(n: int, results: dict) -> int:
    """与基线比对 p50；超过 CHECK_RATIO 倍告警并返回 1。

    不同 n / 不同机器之间绝对值不可比，所以只做"同机回归"的粗门禁；
    没有基线文件时直接放行（首次接入不阻塞）。
    """
    if not BASELINE.is_file():
        print(f"[bench] 未找到基线 {BASELINE}，跳过比对（先跑一次 --save）")
        return 0
    try:
        base = json.loads(BASELINE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"[bench] 基线读取失败（{e}），跳过比对")
        return 0
    base_res = base.get("results") or {}
    if int(base.get("n") or 0) != n:
        print(f"[bench] 基线 n={base.get('n')} 与本轮 n={n} 不同，仅打印不判定")
    worst = 0
    print("\n===== 与基线比对（阈值 ×{:.1f}）=====".format(CHECK_RATIO))
    for name, samples in results.items():
        if not samples or name not in base_res:
            continue
        cur = _pctl(samples)["p50"]
        old = float(base_res[name].get("p50") or 0)
        if old <= 0:
            continue
        ratio = cur / old
        flag = "WARN" if ratio > CHECK_RATIO else "ok"
        print(f"{name:<10} 基线 {old*1000:7.0f} ms → 本次 {cur*1000:7.0f} ms  ×{ratio:.2f}  [{flag}]")
        if ratio > CHECK_RATIO:
            worst = 1
    return worst



def main() -> int:
    ap = argparse.ArgumentParser(description="灵枢性能基准（隔离临时库，不碰真实数据）")
    ap.add_argument("--n", type=int, default=1000, help="合成记忆条数（默认 1000）")
    ap.add_argument("--quick", action="store_true", help="快速模式：n=300")
    ap.add_argument("--keep", action="store_true", help="保留临时库（调试用）")
    ap.add_argument("--save", action="store_true", help="把本次结果存为基线")
    ap.add_argument("--check", action="store_true", help="与基线比对，超 1.5 倍退出 1")
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
        if opts.save:
            _save_baseline(opts.n, results)
        if opts.check:
            return _check_baseline(opts.n, results)
        return 0
    except Exception as e:
        print(f"[bench] 失败：{e}", file=sys.stderr)
        return 1
    finally:
        if not opts.keep:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
