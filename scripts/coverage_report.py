# -*- coding: utf-8 -*-
"""零依赖行覆盖率报告：在**同一个进程**里打一遍主 API，用标准库 trace 统计。

为什么不用 `python -m trace` 跑整个测试集：
  测试全是 `subprocess.run([python, ...])` 起的子进程，父进程的 trace 抓不到子进程，
  报出来的覆盖率会低得毫无意义（还容易误判成"代码没被测到"）。

所以这里换个口径：**在单进程内真实调用主链路**（记/回忆/注入/做梦/体检/情绪/成长…），
统计 `scripts/*.py` 里有哪些行被走到。它不是"测试覆盖率"，而是
**"主 API 冒烟覆盖率"**——用来发现"某个模块整体没被任何入口碰过"这类问题。

用法：
    python scripts/coverage_report.py            # 报告 + 最低覆盖模块 Top10
    python scripts/coverage_report.py --min 60   # 低于 60% 的模块视为可疑，退出码 1
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import trace
import os

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _exercise() -> list:
    """在临时 HOME 里真实调用主链路。返回 [(说明, 是否成功)]。"""
    home = tempfile.mkdtemp(prefix="ls-cov-")
    os.environ["BRAIN_MEMORY_HOME"] = home
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.path.insert(0, str(SCRIPTS))

    import brain  # noqa: F401  合并命名空间
    import core
    import memory as mem
    import emotion as em
    import persona as pe
    import dream
    import growth
    import safety_net
    import shape_audit

    core.ensure_home()
    done = []

    def step(name, fn):
        try:
            fn()
            done.append((name, True))
        except Exception as exc:  # noqa: BLE001
            done.append((f"{name}（{type(exc).__name__}: {str(exc)[:60]}）", False))

    ids = {}

    def _remember():
        rec, _ = mem.remember("覆盖率冒烟：用户偏好把话说清楚，讨厌猜测",
                              kind="fact", importance=0.7, tags=["冒烟"])
        ids["a"] = rec["id"]
        mem.remember("覆盖率冒烟：备份要先留底再动手", kind="mistake", importance=0.8)

    step("remember", _remember)
    step("recall", lambda: mem.recall("备份 留底", k=5, mode="fast", strengthen=False))
    step("stats", mem.stats)
    step("inject", lambda: mem.build_injection("接着做覆盖率"))
    step("capture", lambda: mem.capture("user: 覆盖率冒烟对话\nassistant: 收到",
                                        tag="即记", kind="episode"))
    step("injectable_lessons", lambda: mem.injectable_lessons("覆盖率"))
    step("detect_conflicts", lambda: mem.detect_conflicts(limit=10))
    step("set_frozen", lambda: ids.get("a") and mem.set_frozen(ids["a"], True))
    step("touch_by_emotion", lambda: ids.get("a") and mem.touch_by_emotion(ids["a"], -0.7, 0.8))
    step("fetch", lambda: ids.get("a") and mem.fetch(ids["a"]))
    step("emotion now", em.mood_now)
    step("emotion feel", lambda: em.feel("满足", intensity=0.5, why="冒烟"))
    step("emotion auto", lambda: em.auto_appraise("这次真的很难过很委屈，气死了，"
                                                  "希望以后别再这样，我真的会很失望，也很难受"))
    step("persona load", pe.load_persona)
    step("persona snapshot", lambda: pe.persona_snapshot("覆盖率冒烟"))
    step("dream report", lambda: dream.dream_report() if hasattr(dream, "dream_report") else None)
    step("dream verify", lambda: dream.dream_verify() if hasattr(dream, "dream_verify") else None)
    step("growth digest", lambda: growth.digest(3))
    step("growth stage", growth.stage_state)
    step("growth health", growth.health_score)
    step("growth skill_evolve", lambda: growth.skill_evolve(days=7, dry_run=True))
    step("safety snapshot", lambda: safety_net.snapshot("覆盖率冒烟"))
    step("safety list", safety_net.list_snapshots)
    step("shape audit", lambda: shape_audit.audit())
    step("core doctor-ish", lambda: core.load_config())
    return done


def _count_coverage(tracer: trace.Trace) -> dict:
    """从 trace 结果算每个文件的 (hit_lines, total_lines)。"""
    counts = tracer.results().counts       # {(filename, lineno): count}
    hit = {}
    for (fname, lineno), cnt in counts.items():
        try:
            p = pathlib.Path(fname).resolve()
        except OSError:
            continue
        if p.parent != SCRIPTS.resolve() or p.suffix != ".py":
            continue
        if cnt > 0:
            hit.setdefault(p.name, set()).add(lineno)

    # 每个文件的可执行行数：用 ast 之外最省事的办法——非空、非纯注释行
    out = {}
    for py in sorted(SCRIPTS.glob("*.py")):
        try:
            lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        total = 0
        for raw in lines:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            total += 1
        n_hit = len(hit.get(py.name, ()))
        if total:
            out[py.name] = (n_hit, total)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="零依赖主 API 冒烟覆盖率")
    ap.add_argument("--min", type=int, default=0,
                    help="低于该百分比的模块视为可疑，退出码 1（默认 0 = 只报告）")
    ap.add_argument("--top", type=int, default=10, help="列出覆盖率最低的 N 个模块")
    args = ap.parse_args(argv)

    tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
    done = tracer.runfunc(_exercise)
    cov = _count_coverage(tracer)

    ok = [n for n, good in done if good]
    bad = [n for n, good in done if not good]
    print(f"主链路冒烟：{len(ok)} 步成功 / {len(bad)} 步失败")
    for name in bad:
        print(f"  ⚠ 失败：{name}")

    rows = [(name, h, t, (h / t * 100 if t else 0.0)) for name, (h, t) in cov.items()]
    rows.sort(key=lambda r: r[3])
    tot_h = sum(r[1] for r in rows)
    tot_t = sum(r[2] for r in rows)
    print(f"\n脚本行覆盖（主 API 冒烟口径）：{tot_h}/{tot_t} = "
          f"{tot_h / tot_t * 100:.1f}%" if tot_t else "无数据")

    print(f"\n覆盖率最低的 {args.top} 个模块：")
    for name, h, t, pct in rows[:args.top]:
        print(f"  {pct:>5.1f}%  {h:>4}/{t:<5}  {name}")

    suspects = [r for r in rows if r[3] < args.min and r[2] >= 20]
    if args.min and suspects:
        print(f"\n[失败] {len(suspects)} 个模块覆盖率低于 {args.min}%：")
        for name, h, t, pct in suspects[:12]:
            print(f"  {pct:.1f}%  {name}")
        return 1
    if bad:
        return 2
    return 0


def _ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台（cp1252/cp936）编不了中文就崩。

    CI 的 windows-latest stdout 是 cp1252：脚本一 print 中文就 UnicodeEncodeError
    直接退出（实测 sync_engine.py / make_checksums.py 都因此挂过，Ubuntu 同期正常）。
    这不是业务 bug，是"中文输出 + 非 UTF-8 控制台"的必然结果。
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
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    sys.exit(main())
