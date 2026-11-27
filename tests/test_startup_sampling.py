# -*- coding: utf-8 -*-
"""P4 startup 采样接线回归：cmd_startup 把 report 传给 build_injection。

背景：v3.42 让 memory.build_injection(report=...) 在汇总分段账单后按采样率调
context_ab.record()，但生产调用方里只有 inject / context-stats 传 report，**主会话
真正每轮用的 startup 还没接**——所以灰度采样覆盖不到主会话，A/B 数据始终是 0 条。
本文件锁 startup 接线契约：

  - 默认 context_ab.enabled=false：startup 仍走旧的 build_injection(query, budget)
    调用（report=None，与不传等价），不算 plan、不建记账 list、一个字节都不写
    metrics.jsonl——默认零开销、零行为变化；ab-stats/summary 都是 0 样本；
  - context_ab.enabled=true + sample=1.0：startup 本身落**一条** kind=context_ab
    样本（mode=legacy、tokens/saved_pct 与注入账单同源），summary / ab-stats 可见；
  - 采样只记账不改输出：固定 HOME / 时间 / 输入下，enabled 前后 startup 输出逐字节一致；
  - 采样率仍由 context_ab.sample 决定：enabled=true + sample=0 不落盘（startup 不绕过采样）。

纯标准库，可单独运行：python tests/test_startup_sampling.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共工具（瞬时 IO 重试）

from io_retry_util import run_with_io_retry  # noqa: E402

import assembler  # noqa: E402
import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import cli  # noqa: E402
import context_ab  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


# ---------------------------------------------------------------- 工具

def _setup_home(tmp: Path) -> dict:
    """把 HOME 切到临时目录并落一份可控 config（离线、无注入缓存）。"""
    core.HOME = tmp
    core._recompute_paths()
    core.ensure_home()
    cfg = copy.deepcopy(core.load_config())
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = False
    core.atomic_json(tmp / "config.json", cfg)
    persona.load_persona()
    return cfg


def _write_cfg(tmp: Path, patch: dict) -> dict:
    cfg = copy.deepcopy(core.load_config())
    cfg.update(patch)
    core.atomic_json(tmp / "config.json", cfg)
    return cfg


def _seed() -> None:
    """喂几条记忆，保证注入包有非零长度、report 各段不是空壳。"""
    memory.remember("startup 采样测试：用户喜欢桂花乌龙茶", kind="preference", importance=0.8)
    memory.remember("startup 采样测试：上次把测试目录写进仓库，下次先看 .gitignore",
                    kind="mistake", importance=0.9)
    memory.work_add("P4 startup 采样", tag="task")
    memory.topic_touch("周末露营", "chat", 0.8)
    (core.HOME / "lingshu.user.md").write_text("不要动 DSH 数据目录\n", encoding="utf-8")


def _fixed_time():
    """把分钟级时间语境钉死，让「现在是什么时候」块在多次渲染间稳定。

    core 与 memory 两处都要打补丁：cmd_startup 经 cli._main 进入，开头
    core._sync_state() 会用 core 的快照回灌各子模块——只补 memory 会被还原成真实时间。
    """
    fixed = "2026-01-01 00:00，固定测试时间。"
    old = (getattr(core, "_time_context", None), memory._time_context)
    core._time_context = lambda: fixed
    memory._time_context = lambda: fixed
    return old


def _restore_time(old) -> None:
    if old[0] is not None:
        core._time_context = old[0]
    memory._time_context = old[1]


def _run_startup(*argv) -> tuple:
    """跑真实 CLI 入口（`startup` 子命令），返回 (退出码, stdout)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._main(["startup", *argv])
    return rc, buf.getvalue()


def _run_cli(*argv) -> tuple:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._main(list(argv))
    return rc, buf.getvalue()


def _ab_rows() -> list:
    """只读回 kind=context_ab 的记账行（metrics.jsonl 里也可能有每日 metrics 行）。"""
    p = context_ab.path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == context_ab.KIND:
            rows.append(obj)
    return rows


# ---------------------------------------------------------------- 1. 默认零写盘 / 零开销

def test_default_config_startup_writes_no_metrics_and_keeps_old_call():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        cfg = _setup_home(tmp)
        _seed()
        assert cfg["context_ab"]["enabled"] is False, cfg.get("context_ab")
        assert cfg["context_mode"] == "legacy", cfg.get("context_mode")

        old = _fixed_time()
        try:
            rc, out = _run_startup("--query", "桂花乌龙茶")
        finally:
            _restore_time(old)

        assert rc == 0, rc
        assert "===== 长期记忆注入（灵枢）=====" in out
        assert not context_ab.path().exists(), "默认 enabled=false：startup 不得写 metrics.jsonl"
        assert context_ab.summary(days=7)["total"] == 0

        rc_ab, txt = _run_cli("ab-stats", "--json")
        assert rc_ab == 0, rc_ab
        rep = json.loads(txt)
        assert rep["total"] == 0 and rep["by_mode"] == {}, rep
        assert rep["context_mode"] == "legacy"
        rc_txt, txt2 = _run_cli("ab-stats")
        assert rc_txt == 0 and "0 个样本" in txt2, txt2

        # 零开销探针：默认配置下 cmd_startup 必须仍以 report=None 调 build_injection
        # （旧路径，连 _SectionList 记账都不建），且不得提前算五段 plan。
        # 三处都要打补丁：cli._main 开头的 core._sync_state() 会用 core 的快照回灌
        # 各子模块，只补 cli.build_injection 会被还原成真实函数。
        captured = {}
        plan_called = []
        targets = (core, memory, cli)
        old_builds = [getattr(m, "build_injection", None) for m in targets]
        real_build = memory.build_injection
        real_plan = assembler.plan_from_report

        def _spy_build(*args, **kwargs):
            captured["report"] = kwargs.get("report")
            return real_build(*args, **kwargs)

        def _spy_plan(*args, **kwargs):
            plan_called.append(1)
            return real_plan(*args, **kwargs)

        for _m in targets:
            _m.build_injection = _spy_build
        assembler.plan_from_report = _spy_plan
        try:
            rc2, _ = _run_startup()
        finally:
            for _m, _old in zip(targets, old_builds):
                if _old is not None:
                    _m.build_injection = _old
            assembler.plan_from_report = real_plan

        assert rc2 == 0
        assert "report" in captured, "默认 startup 必须调用 build_injection（spy 未命中）"
        assert captured["report"] is None, "默认 enabled=false：startup 必须走旧的 report=None 调用"
        assert plan_called == [], "默认 enabled=false：startup 不得提前算五段 plan（零开销）"
        assert not context_ab.path().exists(), "第二次 startup 后也不得写 metrics.jsonl"


# ---------------------------------------------------------------- 2. enabled + sample=1 → 一条样本

def test_enabled_sample_one_startup_records_exactly_one_sample():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})

        old = _fixed_time()
        try:
            rc, out = _run_startup("--query", "桂花乌龙茶")
        finally:
            _restore_time(old)

        assert rc == 0, rc
        rows = _ab_rows()
        assert len(rows) == 1, f"enabled+sample=1.0：startup 应恰好记一条，实际 {len(rows)}：{rows}"
        row = rows[0]
        assert row["kind"] == context_ab.KIND
        assert row["mode"] == "legacy"
        assert isinstance(row["ts"], int) and row["ts"] > 0
        assert row["legacy_tokens"] > 0
        assert row["legacy_chars"] > 0
        assert 0.0 <= row["saved_pct"] <= 100.0, row["saved_pct"]
        for key in ("ts", "session", "mode", "legacy_tokens", "budgeted_tokens", "saved_pct"):
            assert key in row, f"记账行缺字段 {key}：{row}"

        summary = context_ab.summary(days=7)
        assert summary["total"] == 1, summary
        assert summary["by_mode"]["legacy"]["count"] == 1, summary
        assert summary["mean_legacy_tokens"] == row["legacy_tokens"]

        rc_ab, txt = _run_cli("ab-stats", "--json")
        assert rc_ab == 0, rc_ab
        ab = json.loads(txt)
        assert ab["total"] == 1 and ab["by_mode"]["legacy"]["count"] == 1, ab
        rc_txt, txt2 = _run_cli("ab-stats")
        assert rc_txt == 0 and "1 个样本" in txt2, txt2


# ---------------------------------------------------------------- 3. 采样不改变 startup 输出文本

def test_startup_output_text_identical_with_and_without_sampling():
    """固定 HOME/时间/输入：enabled 前后 startup 输出逐字节一致（只记账，不改注入）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        _write_cfg(tmp, {"context_ab": {"enabled": False, "sample": 1.0, "log": True}})

        old = _fixed_time()
        try:
            _run_startup("--query", "桂花乌龙茶")          # 热身：旧结构迁移/回忆强化先稳定
            snap = (tmp / "config.json").read_bytes()      # 记住 last_startup / wake_count

            _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})
            rc_on, out_on = _run_startup("--query", "桂花乌龙茶")

            (tmp / "config.json").write_bytes(snap)        # 复位到 enabled 那次运行前的状态
            rc_off, out_off = _run_startup("--query", "桂花乌龙茶")
        finally:
            _restore_time(old)

        assert rc_on == rc_off == 0, (rc_on, rc_off)
        assert "2026-01-01 00:00，固定测试时间。" in out_on, "时间语境没钉死，文本对拍不可信"
        assert out_on == out_off, "开启 startup 采样只记账，不得改变输出文本"
        assert len(_ab_rows()) == 1, "只有 enabled 那次该落账，关闭那次不得追加"


# ---------------------------------------------------------------- 4. 采样率仍生效

def test_enabled_sample_zero_startup_writes_no_sample():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 0.0, "log": True}})

        old = _fixed_time()
        try:
            rc, _ = _run_startup("--query", "桂花乌龙茶")
        finally:
            _restore_time(old)

        assert rc == 0
        assert not context_ab.path().exists(), "sample=0：startup 不得绕过采样率写盘"
        assert context_ab.summary(days=7)["total"] == 0


def _ensure_utf8_stdout() -> None:
    """stdout/stderr 切 UTF-8——Windows CI 是 cp1252，print 中文会直接崩。"""
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        _fn = getattr(_s, "reconfigure", None)
        if callable(_fn):
            try:
                _fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            _buf = getattr(_s, "buffer", None)
            if _buf is not None:
                setattr(sys, _name,
                        io.TextIOWrapper(_buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            run_with_io_retry(fn)
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
