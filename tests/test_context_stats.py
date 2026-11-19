# -*- coding: utf-8 -*-
"""P0 上下文账单（context-stats）回归测试。

覆盖：分段字符求和必须严格等于最终注入文本长度、预算 2400 下总 token 不超太多、
CLI --json 可解析、不传 report 的旧调用兼容（类型/长度/文本不变）、
chars_per_token 可配、缓存开启时账单模式仍重新渲染、CLI 表格含全部段与总计。

纯标准库，可单独运行：python tests/test_context_stats.py
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import cli  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402

SECTION_NAMES = ("persona", "rules", "skills", "mistakes", "relationships",
                 "topics", "recent", "related", "dynamic", "water", "other")


def _setup_home(tmp: Path, extra: dict | None = None) -> dict:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = False
    if extra:
        cfg.update(extra)
    core.atomic_json(tmp / "config.json", cfg)
    persona.load_persona()  # 先完成旧档案迁移，避免首轮注入因迁移而变
    return cfg


def _seed(tmp: Path) -> None:
    """喂满各段：错误 / 人际 / 工作记忆 / 话题栈 / 用户规则 / 最近记忆。"""
    memory.remember("用户喜欢喝桂花乌龙茶", kind="preference", importance=0.8)
    memory.remember("上次把测试目录写进仓库，下次先看 .gitignore", kind="mistake", importance=0.9)
    memory.remember("小王是用户同事，帮过我们一次", kind="relationship", importance=0.8)
    memory.work_add("把上下文账单做完", tag="task")
    memory.topic_touch("周末露营", "chat", 0.8)
    (tmp / "lingshu.user.md").write_text("不要动 DSH 数据目录\n", encoding="utf-8")


def _run_cli(*argv):
    """直接走 cli._main（含 argparse 解析），捕获 stdout。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._main(list(argv))
    return rc, buf.getvalue()


def _fixed_time():
    """把分钟级时间语境钉死：连续两次注入渲染才能做严格文本比对。"""
    old = memory._time_context
    memory._time_context = lambda: "2026-01-01 00:00，固定测试时间。"
    return old


# ---------------------------------------------------------------- 1. 分段求和

def test_sections_chars_sum_equals_total_length():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        rep: dict = {}
        text = memory.build_injection(report=rep)
        assert isinstance(text, str) and text, "注入包应为非空字符串"
        secs = rep["sections"]
        assert [s["name"] for s in secs] == list(SECTION_NAMES), [s["name"] for s in secs]
        # 核心约定：各段 chars 求和 === 最终文本长度（连接换行也记进所属段）
        assert sum(s["chars"] for s in secs) == rep["total_chars"], rep
        assert rep["total_chars"] == len(text), (rep["total_chars"], len(text))
        for s in secs:
            assert set(s) == {"name", "chars", "tokens", "budget_frac"}, s
            assert s["chars"] >= 0 and s["tokens"] >= 0
        # 分段不是空架子：有钱的记忆/工作记忆/水位提醒等段应有内容
        by_name = {s["name"]: s for s in secs}
        assert by_name["persona"]["chars"] > 0
        assert by_name["dynamic"]["chars"] > 0
        assert by_name["water"]["chars"] > 0
        assert by_name["mistakes"]["chars"] > 0
        assert by_name["relationships"]["chars"] > 0
        assert by_name["topics"]["chars"] > 0
        assert by_name["recent"]["chars"] > 0
        assert by_name["rules"]["chars"] > 0
        assert by_name["other"]["chars"] > 0


def test_tokens_follow_chars_per_token_ratio():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        rep: dict = {}
        memory.build_injection(report=rep)
        assert rep["chars_per_token"] == 2.0, rep["chars_per_token"]
        for s in rep["sections"]:
            assert s["tokens"] == math.ceil(s["chars"] / 2), s
            assert s["budget_frac"] == round(s["tokens"] / rep["budget"], 4), s
        assert rep["total_tokens"] == sum(s["tokens"] for s in rep["sections"])


# ---------------------------------------------------------------- 2. 预算

def test_budget_2400_total_stays_near_budget():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        rep: dict = {}
        text = memory.build_injection(budget_tokens=2400, report=rep)
        assert rep["budget"] == 2400
        assert rep["total_chars"] == len(text)
        # 正文预算 char_budget = 1.8 × 2400，中文 2 字符/token → 常态应在预算内；
        # 硬块（人格/规则/水位守则）不参与砍，留 25% 余量兜环境差异，但绝不失控。
        assert rep["total_tokens"] <= int(2400 * 1.25), rep["total_tokens"]
        assert rep["total_tokens"] > 0


def test_chars_per_token_configurable():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), {"context_chars_per_token": 1})
        _seed(Path(td))
        rep: dict = {}
        memory.build_injection(report=rep)
        assert rep["chars_per_token"] == 1.0, rep["chars_per_token"]
        assert rep["total_tokens"] == rep["total_chars"]
        for s in rep["sections"]:
            assert s["tokens"] == s["chars"], s


# ---------------------------------------------------------------- 3. CLI --json

def test_cli_json_report_parseable():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        rc, out = _run_cli("context-stats", "--json", "--budget", "2400",
                           "--query", "桂花乌龙茶")
        assert rc == 0, rc
        rep = json.loads(out)
        assert set(rep) >= {"sections", "total_chars", "total_tokens",
                            "budget", "chars_per_token"}, set(rep)
        assert rep["budget"] == 2400
        assert rep["chars_per_token"] == 2.0
        assert [s["name"] for s in rep["sections"]] == list(SECTION_NAMES)
        assert rep["total_chars"] == sum(s["chars"] for s in rep["sections"])
        assert rep["total_tokens"] == sum(s["tokens"] for s in rep["sections"])
        # 查询路径：与「桂花乌龙茶」相关的记忆应落进 related 段
        by_name = {s["name"]: s for s in rep["sections"]}
        assert by_name["related"]["chars"] > 0, by_name["related"]
        assert by_name["related"]["tokens"] == math.ceil(by_name["related"]["chars"] / 2)


def test_cli_table_has_sections_and_total():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        rc, out = _run_cli("context-stats")
        assert rc == 0, rc
        assert "上下文账单" in out, out
        for name in SECTION_NAMES:
            assert name in out, f"表格缺段 {name}：{out}"
        assert "总计" in out and "预算" in out
        assert ("未超限" in out) or ("超限" in out)
        # 最后一行必须是预算结论（总计行紧跟其后）
        tail = [ln for ln in out.splitlines() if ln.strip()][-1]
        assert "预算" in tail and "tokens" in tail, tail


# ---------------------------------------------------------------- 4. 旧调用兼容

def test_legacy_call_without_report_returns_same_text():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        _seed(Path(td))
        old = _fixed_time()
        try:
            memory.build_injection()  # 热身：首次可能完成旧记忆结构迁移
            plain = memory.build_injection()
            rep: dict = {}
            with_report = memory.build_injection(report=rep)
            positional = memory.build_injection("", 2400)  # 旧的位置参数调用
        finally:
            memory._time_context = old
        assert type(plain) is str and type(positional) is str
        assert len(plain) > 1000, len(plain)
        assert len(plain) == len(positional) == len(with_report), (
            len(plain), len(positional), len(with_report))
        assert plain == with_report, "记账模式不得改变注入文本"
        assert plain == positional
        assert rep["total_chars"] == len(with_report)


def test_report_mode_ignores_cached_text():
    """缓存里只有整段文本没有分段信息：账单模式必须重新渲染，不能报 0 段。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), {"session_state_cache": True})
        _seed(Path(td))
        memory.build_injection()  # 预热并落一份缓存快照
        rep: dict = {}
        text = memory.build_injection(report=rep)
        assert rep["total_chars"] == len(text) > 0
        assert any(s["chars"] for s in rep["sections"]), rep["sections"]
        assert sum(s["chars"] for s in rep["sections"]) == len(text)


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
