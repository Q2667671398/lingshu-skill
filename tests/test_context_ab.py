# -*- coding: utf-8 -*-
"""附录 C P4 灰度骨架回归：context_mode 双模 + A/B 采样记账 + ab-stats。

契约：
  - assembler.compare() 纯计算：不改入参、不读盘写盘，给 legacy/budgeted 字符对比；
  - config 默认 context_mode=legacy、context_ab.enabled=false → 默认路径零行为变化；
  - record() 受 enabled/sample/log 控制：关时一个字节不写，log=false 只算不落盘；
  - summary() 聚合近 N 天样本的均值/p50/p95，按 mode 分组，忽略每日 metrics 行；
  - CLI `ab-stats [--days N] [--json]` 只读聚合，无样本也正常退出 0；
  - metrics.jsonl 超 512KB 自动轮转为 metrics.jsonl.1。

纯标准库，可单独运行：python tests/test_context_ab.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import sys
import tempfile
import time
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

SECTION_ORDER = ("persona", "rules", "skills", "mistakes", "relationships",
                 "topics", "recent", "related", "dynamic", "water", "other")


# ---------------------------------------------------------------- 工具

def _setup_home(tmp: Path) -> dict:
    """把 HOME 切到临时目录并落一份可控 config（离线、无缓存），返回配置。"""
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


def _seed(tmp: Path) -> None:
    memory.remember("灰度测试：用户喜欢桂花乌龙茶", kind="preference", importance=0.8)
    memory.remember("灰度测试：上次把测试目录写进仓库", kind="mistake", importance=0.9)
    memory.remember("灰度测试：小王是用户同事，帮过一次", kind="relationship", importance=0.8)
    memory.work_add("P4 灰度骨架", tag="task")
    memory.topic_touch("周末露营", "chat", 0.8)
    (tmp / "lingshu.user.md").write_text("不要动 DSH 数据目录\n", encoding="utf-8")


def _report(sections: dict, *, ratio: int = 2, budget: int = 2400) -> dict:
    """按 build_injection(report=...) 的形状造一份合成账单（纯数据，不起服务）。"""
    secs, total_chars, total_tokens = [], 0, 0
    for name in SECTION_ORDER:
        c = int(sections.get(name, 0) or 0)
        t = int(math.ceil(c / ratio))
        secs.append({"name": name, "chars": c, "tokens": t,
                     "budget_frac": round(t / budget, 4) if budget else 0.0})
        total_chars += c
        total_tokens += t
    return {"sections": secs, "total_chars": total_chars, "total_tokens": total_tokens,
            "budget": budget, "chars_per_token": ratio}


def _run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._main(list(argv))
    return rc, buf.getvalue()


def _metrics_rows(home=None) -> list[dict]:
    p = context_ab.path(home)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _add_ab_row(ts: float, mode: str, saved: float, *, legacy_tokens: int = 2400,
                budgeted_tokens: int = 2000, session: str = "s") -> None:
    core._append_jsonl(context_ab.path(), {
        "kind": "context_ab", "ts": int(ts), "session": session, "mode": mode,
        "legacy_tokens": legacy_tokens, "budgeted_tokens": budgeted_tokens,
        "saved_pct": saved,
    })


# ---------------------------------------------------------------- 1. compare 纯计算

def test_compare_under_budget_has_no_savings():
    report = _report({"persona": 400, "recent": 400})
    out = assembler.compare(report, budget=40000)
    # 预算宽松 → 一个字都不裁：saved_pct=0、degraded 空
    assert out == {"legacy_chars": 800, "budgeted_chars": 800,
                   "saved_pct": 0.0, "degraded": []}, out
    # 空 report / 非法预算不炸
    assert assembler.compare({}, 2400)["saved_pct"] == 0.0
    assert assembler.compare({"total_chars": 0}, 0)["legacy_chars"] == 0


def test_compare_over_budget_trims_and_reports_degraded():
    report = _report({"persona": 1000, "related": 4000})   # related = 可裁的 retrieved 段
    snapshot = copy.deepcopy(report)
    out = assembler.compare(report, 2400)

    assert report == snapshot, "compare() 必须是纯计算，不得改入参"
    assert out["legacy_chars"] == 5000 == report["total_chars"]
    # 预算 2400（usable 1800）→ retrieved 先被裁到 1299 tokens=2598 字符，over 700 一次裁完
    assert out["budgeted_chars"] == 3598, out
    assert out["saved_pct"] == 28.04, out
    assert out["degraded"] == ["retrieved"], out

    plan_out = assembler.plan_from_report(report, budget=2400)
    assert out["budgeted_chars"] == sum(b["chars"] for b in plan_out["blocks"])
    assert plan_out["total_tokens"] <= plan_out["usable_tokens"], plan_out
    # 预算放开后不再有降级段
    assert assembler.compare(report, 40000)["degraded"] == []


# ---------------------------------------------------------------- 2. 默认路径零变化

def test_default_config_is_legacy_and_record_is_noop():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed(tmp)
        cfg = core.load_config()
        assert cfg["context_mode"] == "legacy", cfg.get("context_mode")
        assert cfg["context_ab"] == {"enabled": False, "sample": 0.1, "log": True}, cfg.get("context_ab")
        assert context_ab.context_mode() == "legacy"
        metrics = context_ab.path()
        assert not metrics.exists(), "新建测试 HOME 不应凭空出现 metrics.jsonl"

        old_time = memory._time_context
        try:
            memory.build_injection()  # 热身（首轮可能完成旧结构迁移）
            memory._time_context = lambda: "2026-01-01 00:00，固定测试时间。"
            rep: dict = {}
            text = memory.build_injection(report=rep)
            plain = memory.build_injection()   # 旧调用（无 report）
        finally:
            memory._time_context = old_time
        # 默认 legacy：记账模式不改变注入文本
        assert plain == text

        plan = assembler.plan_from_report(rep, budget=cfg["budget_tokens"])
        assert context_ab.record("legacy", rep, plan) is None      # 默认关闭 → 不记
        assert not metrics.exists(), "默认路径下 record() 不得写盘"

        before_rep, before_plan = copy.deepcopy(rep), copy.deepcopy(plan)
        assembler.compare(rep, cfg["budget_tokens"])
        assert rep == before_rep and plan == before_plan, "compare 不得改 report/plan"

        # 灰度开关可切：budgeted 能读回；非法值回退 legacy（本任务只立配置，不接实际装配）
        _write_cfg(tmp, {"context_mode": "budgeted"})
        assert core.load_config()["context_mode"] == "budgeted"
        assert context_ab.context_mode() == "budgeted"
        _write_cfg(tmp, {"context_mode": "weird-mode"})
        assert context_ab.context_mode() == "legacy"


# ---------------------------------------------------------------- 3. 采样开关

def test_sampling_switch_and_record_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        _seed(tmp)
        rep: dict = {}
        memory.build_injection(report=rep)
        plan = assembler.plan_from_report(rep, budget=core.load_config()["budget_tokens"])
        metrics = context_ab.path()

        # sample=0：即使 enabled=true 也不记（5% 采样率的 0 档）
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 0.0, "log": True}})
        for _ in range(5):
            assert context_ab.record("legacy", rep, plan, session="sess-0") is None
        assert not metrics.exists()

        # sample=1：必记；字段形状与数值来源正确
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})
        row = context_ab.record("budgeted", rep, plan, session="sess-1")
        assert isinstance(row, dict), row
        rows = _metrics_rows()
        assert len(rows) == 1, rows
        got = rows[0]
        saved = assembler.compare(rep, plan["budget"])
        assert got["kind"] == "context_ab"
        assert got["session"] == "sess-1" and got["mode"] == "budgeted"
        assert isinstance(got["ts"], int) and got["ts"] > 0
        assert got["legacy_tokens"] == rep["total_tokens"]
        assert got["budgeted_tokens"] == plan["total_tokens"]
        assert got["saved_pct"] == saved["saved_pct"]
        for key in ("ts", "session", "mode", "legacy_tokens", "budgeted_tokens", "saved_pct"):
            assert key in got, f"记账行缺字段 {key}：{got}"

        # log=false：只算不落盘
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": False}})
        only_calc = context_ab.record("legacy", rep, plan, session="sess-2")
        assert isinstance(only_calc, dict)
        assert len(_metrics_rows()) == 1, "log=false 不得追加写盘"

        # force=True：采样率为 0 也能手动补记
        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 0.0, "log": True}})
        forced = context_ab.record("legacy", rep, plan, session="sess-3", force=True)
        assert isinstance(forced, dict) and len(_metrics_rows()) == 2

        # enabled=false：force 也不记
        _write_cfg(tmp, {"context_ab": {"enabled": False, "sample": 1.0, "log": True}})
        assert context_ab.record("legacy", rep, plan, session="sess-4", force=True) is None
        assert len(_metrics_rows()) == 2

        # 坏入参不抛异常（观测代码不能带崩装配）
        assert context_ab.record("legacy", None, plan) is None
        assert context_ab.record("legacy", rep, []) is None


# ---------------------------------------------------------------- 4. summary 聚合

def test_summary_percentiles_by_mode_and_window():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        now = time.time()
        _add_ab_row(now, "legacy", 10.0, legacy_tokens=2400, budgeted_tokens=2160)
        _add_ab_row(now - 60, "legacy", 20.0, legacy_tokens=2400, budgeted_tokens=1920)
        _add_ab_row(now - 120, "budgeted", 30.0, legacy_tokens=2400, budgeted_tokens=1680)
        _add_ab_row(now - 20 * 86400, "legacy", 99.0)   # 7 天窗口外
        # 每日 metrics 行（同文件共存）必须被忽略
        core._append_jsonl(context_ab.path(), {"ts": int(now), "date": "2026-01-01", "total": 10})

        s = context_ab.summary(days=7)
        assert s["window_days"] == 7.0
        assert s["total"] == 3, s
        assert s["mean_saved_pct"] == 20.0
        assert s["p50_saved_pct"] == 20.0
        assert s["p95_saved_pct"] == 29.0
        assert s["mean_legacy_tokens"] == 2400
        assert s["mean_budgeted_tokens"] == 1920
        assert set(s["by_mode"]) == {"legacy", "budgeted"}
        lg = s["by_mode"]["legacy"]
        assert lg["count"] == 2 and lg["mean_saved_pct"] == 15.0
        assert lg["p50_saved_pct"] == 15.0 and lg["p95_saved_pct"] == 19.5
        bg = s["by_mode"]["budgeted"]
        assert bg["count"] == 1 and bg["p50_saved_pct"] == 30.0

        # 时间窗口可调：30 天把 20 天前那条也拉进来
        assert context_ab.summary(days=30)["total"] == 4

        # 分位数工具本身（线性插值口径）
        assert context_ab.percentile([10, 20, 30], 50) == 20.0
        assert context_ab.percentile([10, 20], 95) == 19.5
        assert context_ab.percentile([5], 95) == 5.0
        assert context_ab.percentile([], 95) is None

        # 没有任何样本时：空壳但不炸、统计量为 None
        empty_home = tmp / "no_metrics_home"
        empty = context_ab.summary(days=7, home=empty_home)
        assert empty["total"] == 0 and empty["by_mode"] == {}
        assert empty["mean_saved_pct"] is None and empty["p95_saved_pct"] is None


# ---------------------------------------------------------------- 5. CLI

def test_cli_ab_stats_json_and_text():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        # 默认无样本：JSON 可解析、total=0、模式 legacy，退出码 0
        rc, out = _run_cli("ab-stats", "--json")
        assert rc == 0, rc
        rep = json.loads(out)
        for key in ("window_days", "total", "mean_saved_pct", "p50_saved_pct",
                    "p95_saved_pct", "by_mode", "context_mode"):
            assert key in rep, f"ab-stats JSON 缺键 {key}：{rep}"
        assert rep["total"] == 0 and rep["by_mode"] == {}
        assert rep["window_days"] == 7.0 and rep["context_mode"] == "legacy"

        rc, txt = _run_cli("ab-stats", "--days", "30")
        assert rc == 0 and "A/B" in txt and "0 个样本" in txt, txt

        # 塞两个样本后：JSON 聚合数字正确、文本模式给出均值/p50/p95
        now = time.time()
        _add_ab_row(now, "legacy", 10.0)
        _add_ab_row(now - 60, "budgeted", 20.0)
        rc, out = _run_cli("ab-stats", "--json", "--days", "30")
        assert rc == 0, rc
        rep = json.loads(out)
        assert rep["total"] == 2 and rep["mean_saved_pct"] == 15.0
        assert set(rep["by_mode"]) == {"legacy", "budgeted"}

        rc, txt = _run_cli("ab-stats")
        assert rc == 0, rc
        assert "2 个样本" in txt and "节省比例" in txt and "p95" in txt, txt
        assert "legacy" in txt and "budgeted" in txt, txt


# ---------------------------------------------------------------- 6. 轮转

def test_metrics_rotation_keeps_file_small():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _setup_home(tmp)
        p = context_ab.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        filler = json.dumps({"kind": "context_ab", "ts": 1, "session": "x", "mode": "legacy",
                             "legacy_tokens": 1, "budgeted_tokens": 1, "saved_pct": 0.0},
                            ensure_ascii=False) + "\n"
        n = (context_ab.MAX_BYTES // len(filler)) + 5
        p.write_text(filler * n, encoding="utf-8")
        assert p.stat().st_size > context_ab.MAX_BYTES

        _write_cfg(tmp, {"context_ab": {"enabled": True, "sample": 1.0, "log": True}})
        report = _report({"persona": 300, "related": 2000})
        plan = assembler.plan_from_report(report, budget=2400)
        row = context_ab.record("legacy", report, plan, session="rot")
        assert isinstance(row, dict)

        rotated = p.with_name(p.name + ".1")
        assert rotated.exists(), "超 512KB 应轮转为 metrics.jsonl.1"
        assert rotated.stat().st_size > context_ab.MAX_BYTES
        assert p.stat().st_size < context_ab.MAX_BYTES, p.stat().st_size
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1 and json.loads(lines[0])["session"] == "rot"


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
