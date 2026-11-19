# -*- coding: utf-8 -*-
"""附录 C P1：五段式装配器（scripts/assembler.py）回归。

契约：
  - 预算内不裁、不告警；
  - 窗口级预算（≥8000）才有段占比上限，超了先按段上限裁；小预算只按总量压；
  - 总量超限按 retrieved → summary → recent 的顺序压；
  - stable/current 不自动裁，只告警；
  - 输出预留默认 25%，max_output_tokens 可把它压更小。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import assembler  # noqa: E402
import brain  # noqa: E402,F401
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


def _t(n: int) -> str:
    """n 个字符的占位文本（估算 token 时 2 字符≈1 token）。"""
    return "字" * n


def test_small_budget_no_section_caps():
    res = assembler.plan({"stable": _t(100), "recent": _t(200)}, budget=2400)
    assert res["actions"] == [], res["actions"]
    assert res["warnings"] == [], res["warnings"]


def test_window_budget_caps_retrieved_and_warns_stable():
    parts = {"stable": _t(10000), "retrieved": _t(20000), "current": _t(2000)}
    res = assembler.plan(parts, budget=40000)
    names = [b["name"] for b in res["blocks"]]
    assert names == list(assembler.SECTIONS)
    assert any("retrieved" in a for a in res["actions"]), res["actions"]
    assert any("stable" in w for w in res["warnings"]), res["warnings"]
    # stable 只告警不裁
    stable = next(b for b in res["blocks"] if b["name"] == "stable")
    assert stable["trimmed_chars"] == 0


def test_total_over_budget_degrades_in_order():
    parts = {"stable": _t(2000), "summary": _t(8000), "retrieved": _t(12000),
             "recent": _t(12000), "current": _t(1000)}
    res = assembler.plan(parts, budget=9000, output_reserve=0.25)
    order = [a.split(":")[0] for a in res["actions"]]
    assert order, "应当发生裁剪"
    # retrieved 一定先于 summary 被压
    if "summary" in order and "retrieved" in order:
        assert order.index("retrieved") < order.index("summary"), order
    assert res["total_tokens"] <= res["usable_tokens"] or res["warnings"]


def test_reserve_is_quarter_by_default_and_capped_by_max_output():
    a = assembler.plan({}, budget=40000)
    assert a["reserve_tokens"] == 10000 and a["usable_tokens"] == 30000
    b = assembler.plan({}, budget=40000, max_output_tokens=500)
    assert b["reserve_tokens"] == 500 and b["usable_tokens"] == 39500


def test_plan_from_report_maps_sections_and_counts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        core.HOME = Path(td)
        core._recompute_paths()
        core._sync_state()
        core.ensure_home()
        cfg = core.load_config()
        cfg["embed"]["url"] = ""
        cfg["embed"]["fallback"]["enabled"] = False
        cfg["session_state_cache"] = False
        core.atomic_json(Path(td) / "config.json", cfg)
        persona.load_persona()
        memory.remember("装配器测试：用户偏好深夜工作", kind="preference", importance=0.8)
        rep: dict = {}
        memory.build_injection(report=rep)
        res = assembler.plan_from_report(rep, budget=40000)
        assert res["source"] == "injection-report"
        assert res["source_chars"] == rep["total_chars"]
        total_chars = sum(b["chars"] for b in res["blocks"])
        assert total_chars == rep["total_chars"], (total_chars, rep["total_chars"])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
