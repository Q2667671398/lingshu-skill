# -*- coding: utf-8 -*-
"""P4 灰度记账接线回归：build_injection(report=...) 装配完成后调 context_ab.record。

背景：context_ab.record()/assembler.plan_from_report() 早已写好，但此前全仓只有
CLI `ab-stats` 读账、没有生产调用，所以采样永远是 0 条。v3.41 起把记账接到
memory.build_injection(report=...) 的装配路径上（只记账、不切换装配）。

契约：
  - 默认配置 context_ab.enabled=false：注入路径一个字节都不写 metrics.jsonl，
    且开关判断先短路——连 plan_from_report 都不提前试排（零开销）；
  - context_ab.enabled=true + sample=1.0：注入完成即落**一条** kind=context_ab
    的记账，legacy/budgeted tokens 与 saved_pct 全部与 report / 五段计划同源；
  - 记账行 mode 取 config.context_mode（灰度目标模式，ab-stats 按它分组）；
    但本轮真实装配仍是 legacy——config set context_mode budgeted 只换记账标签，
    注入文本与 legacy 完全一致，不产生任何行为变化；
  - context_ab.record 内部抛异常不影响注入结果；异常被吞掉并记 failures；
  - 记账接线不得改变注入文本：固定 HOME / 固定时间 / 固定输入下，
    带 report 的新路径 == 不传 report 的旧路径。

纯标准库，可单独运行：python tests/test_context_ab_wiring.py
"""
from __future__ import annotations

import copy
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
import context_ab  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


# ---------------------------------------------------------------- 工具

def _setup_home(tmp: Path) -> dict:
    """把 HOME 切到临时目录并落一份可控 config（离线、无注入缓存）。"""
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
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
    """喂几条记忆，保证 report 的各段有非零 token，记账数字才不是空壳。"""
    memory.remember("接线测试：用户喜欢桂花乌龙茶", kind="preference", importance=0.8)
    memory.remember("接线测试：上次把测试目录写进仓库，下次先看 .gitignore",
                    kind="mistake", importance=0.9)
    memory.work_add("P4 记账接线", tag="task")
    memory.topic_touch("周末露营", "chat", 0.8)
    (core.HOME / "lingshu.user.md").write_text("不要动 DSH 数据目录\n", encoding="utf-8")


def _fixed_time():
    """把分钟级时间语境钉死：连续两次注入渲染才能做严格文本比对。"""
    old = memory._time_context
    memory._time_context = lambda: "2026-01-01 00:00，固定测试时间。"
    return old


def _metrics_rows() -> list[dict]:
    """只读回 kind=context_ab 的记账行（metrics.jsonl 里还可能有每日 metrics 行）。"""
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


def _failures_rows() -> list[dict]:
    p = core.HOME / "failures.jsonl"
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
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


# ---------------------------------------------------------------- 1. 默认关闭零开销

def test_default_config_writes_nothing_and_skips_plan():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        cfg = _setup_home(tmp)
        _seed()
        assert cfg["context_ab"]["enabled"] is False, cfg.get("context_ab")
        assert cfg["context_mode"] == "legacy", cfg.get("context_mode")

        # 探针：默认配置下 plan_from_report 不该被记账接线提前调用（零开销）
        called = []
        orig_plan = assembler.plan_from_report

        def _spy_plan(*args, **kwargs):
            called.append(1)
            return orig_plan(*args, **kwargs)

        assembler.plan_from_report = _spy_plan
        orig_record = context_ab.record

        def _unexpected_record(*args, **kwargs):
            raise AssertionError("默认 enabled=false 时不应调用 context_ab.record")

        context_ab.record = _unexpected_record
        try:
            memory.build_injection(report={})       # 热身（首轮可能完成旧结构迁移）
            rep: dict = {}
            text = memory.build_injection(report=rep)
        finally:
            assembler.plan_from_report = orig_plan
            context_ab.record = orig_record

        assert called == [], "默认关闭时不得提前算 plan（应先看 enabled 开关）"
        assert not context_ab.path().exists(), "默认配置下 build_injection 不得写 metrics.jsonl"
        assert rep["total_chars"] == len(text) > 0, (rep.get("total_chars"), len(text))
        ab_fails = [r for r in _failures_rows() if r.get("kind") == "context_ab"]
        assert ab_fails == [], f"默认路径不该走异常分支：{ab_fails}"


# ---------------------------------------------------------------- 2. 开启+必采样 → 一条记账

def test_enabled_sample_one_records_one_row_with_tokens_and_saved_pct():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        cfg = _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})

        old = _fixed_time()
        try:
            rep: dict = {}
            text = memory.build_injection(report=rep)
        finally:
            memory._time_context = old

        rows = _metrics_rows()
        assert len(rows) == 1, f"sample=1.0 应恰好记一条，实际 {len(rows)}：{rows}"
        row = rows[0]
        plan = assembler.plan_from_report(rep, budget=rep["budget"])
        cmp = assembler.compare(rep, rep["budget"])

        assert row["kind"] == context_ab.KIND
        assert row["mode"] == cfg["context_mode"] == "legacy"
        assert isinstance(row["ts"], int) and row["ts"] > 0
        assert row["legacy_tokens"] == rep["total_tokens"] > 0
        assert row["budgeted_tokens"] == plan["total_tokens"] >= 0
        assert row["budgeted_tokens"] <= row["legacy_tokens"], "预算裁剪只应省 token"
        assert row["saved_pct"] == cmp["saved_pct"]
        assert 0.0 <= row["saved_pct"] <= 100.0, row["saved_pct"]
        assert row["legacy_chars"] == rep["total_chars"] == len(text)
        assert row["budgeted_chars"] == cmp["budgeted_chars"]
        for key in ("ts", "session", "mode", "legacy_tokens", "budgeted_tokens", "saved_pct"):
            assert key in row, f"记账行缺字段 {key}：{row}"


# ---------------------------------------------------------------- 3. budgeted 只换标签不换装配

def test_context_mode_budgeted_relabels_row_but_keeps_legacy_assembly():
    """config set context_mode=budgeted：本轮仍走 legacy 装配，只换记账标签。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})

        old = _fixed_time()
        try:
            memory.build_injection()                     # 热身（固定时间下先稳定现场）
            rep_a: dict = {}
            text_legacy = memory.build_injection(report=rep_a)
            assert _metrics_rows()[-1]["mode"] == "legacy"
            assert context_ab.context_mode() == "legacy"

            # 切灰度目标模式：真实装配代码路径没变，注入文本必须一字不差
            _write_cfg(tmp, {"context_mode": "budgeted"})
            assert context_ab.context_mode() == "budgeted"
            rep_b: dict = {}
            text_budgeted = memory.build_injection(report=rep_b)
            assert _metrics_rows()[-1]["mode"] == "budgeted", \
                "记录里的 mode 必须等于 config.context_mode"

            # 再跑一次 legacy，确认不是时间/状态抖动导致的偶然相等
            _write_cfg(tmp, {"context_mode": "legacy"})
            rep_c: dict = {}
            text_again = memory.build_injection(report=rep_c)
        finally:
            memory._time_context = old

        assert text_legacy == text_budgeted == text_again, \
            "context_mode=budgeted 本轮不得切换真实装配（文本必须与 legacy 一致）"
        assert rep_a["total_chars"] == rep_b["total_chars"] == rep_c["total_chars"] == len(text_legacy)
        assert len(_metrics_rows()) == 3, _metrics_rows()


# ---------------------------------------------------------------- 4. record 抛异常不影响注入

def test_record_exception_is_swallowed_and_logged():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})

        old_time = _fixed_time()
        old_record = context_ab.record

        def _boom(*args, **kwargs):
            raise RuntimeError("模拟记账故障")

        context_ab.record = _boom
        try:
            memory.build_injection()                    # 热身
            plain = memory.build_injection()            # 旧路径（不传 report，不记账）
            rep: dict = {}
            text = memory.build_injection(report=rep)   # 记账接线内部抛异常
        finally:
            context_ab.record = old_record
            memory._time_context = old_time

        assert text == plain, "记账异常不得改变注入结果"
        assert rep["total_chars"] == len(text)
        assert not context_ab.path().exists(), "record 已抛错，metrics.jsonl 不该落盘"
        ab_fails = [r for r in _failures_rows() if r.get("kind") == "context_ab"]
        assert len(ab_fails) == 1, f"异常应被吞掉并记一条 failures：{ab_fails}"
        assert "RuntimeError" in ab_fails[0]["msg"], ab_fails[0]


# ---------------------------------------------------------------- 5. 固定输入：带 report == 旧路径

def test_injection_text_unchanged_with_fixed_input():
    """固定 HOME/时间/预算/查询：带 report 的新路径与不传 report 的旧路径文本一致。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed()

        old = _fixed_time()
        try:
            memory.build_injection("桂花乌龙茶", 2400)          # 热身（固定输入）
            rep: dict = {}
            with_report = memory.build_injection("桂花乌龙茶", 2400, rep)
            legacy_plain = memory.build_injection("桂花乌龙茶", 2400)
            rep2: dict = {}
            with_report2 = memory.build_injection("桂花乌龙茶", 2400, rep2)
        finally:
            memory._time_context = old

        assert with_report == legacy_plain == with_report2, "记账接线不得改变注入文本"
        assert rep["total_chars"] == rep2["total_chars"] == len(with_report)
        assert rep["total_tokens"] == rep2["total_tokens"]
        assert not context_ab.path().exists(), "默认关闭：固定输入对比也不得写 metrics.jsonl"


if __name__ == "__main__":
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
