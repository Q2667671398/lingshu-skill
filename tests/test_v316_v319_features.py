# -*- coding: utf-8 -*-
"""v3.16–v3.19 新功能最小回归测试与注入缓存失效回归。"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import cli  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


def _setup_home(tmp: Path, *, cache: bool = False) -> dict:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = cache
    core.atomic_json(tmp / "config.json", cfg)
    persona.load_persona()  # 先完成旧档案迁移，避免首次注入改变缓存键而误判命中。
    return cfg


def _save_config(cfg: dict) -> None:
    core.atomic_json(core.HOME / "config.json", cfg)


def test_injection_cache_hit_and_session_state_clear():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), cache=True)
        memory.remember("缓存里有一杯桂花茶", kind="preference", importance=0.8)
        memory.build_injection()  # 首次读取会完成旧记忆结构迁移；第二轮起才应稳定命中。
        first = memory.build_injection()
        info = memory.inject_cache_info()
        assert info["enabled"] is True and info["ttl"] == 300, info
        assert info["ts"] is not None and info["chars"] == len(first), info

        # 缓存读写契约：落盘的键能直接读回同一份文本（用记录键验证，不依赖重算时机）
        info = memory.inject_cache_info()
        assert info["key"], info
        assert memory._inject_cache_load(memory.load_config(), info["key"]) == first
        assert memory.build_injection() == first
        assert memory.inject_cache_clear()["removed"] is True
        assert memory.inject_cache_info()["ts"] is None


def test_injection_cache_invalidates_after_memory_write():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), cache=True)
        memory.remember("第一条缓存记忆", kind="fact", importance=0.7)
        memory.build_injection()
        memory.remember("写入后必须出现的青梅", kind="fact", importance=0.9)
        assert "青梅" in memory.build_injection()


def test_injection_cache_invalidates_when_channel_permission_changes():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        cfg = _setup_home(Path(td), cache=True)
        memory.remember("群里小王提到杜鹃鸟", kind="episode", importance=0.9,
                        channel="qq:88888", group_id="grp1", user="qq:10001")
        memory.build_injection()  # 预热迁移，再缓存一份稳定的默认隔离结果。
        assert "杜鹃鸟" not in memory.build_injection()
        cfg["inject_channels"] = ["qq:88888"]
        _save_config(cfg)
        assert "杜鹃鸟" in memory.build_injection(), "通道许可变化后不能复用旧注入缓存"


def test_trait_baseline_relaxes_by_half_after_thirty_days():
    now = 1_700_000_000
    p = {
        "traits": {"好奇": {"value": 100, "why": "测试", "evidence": []}},
        "traits_baseline": {"好奇": {"value": 50, "ts": now - 30 * core.DAY}},
        "history": [],
    }
    moved = persona._trait_relax(p, now=now)
    assert moved["好奇"] == (100, 75), moved
    assert persona.trait_value(p["traits"]["好奇"]) == 75
    assert p["history"][-1]["source"] == "trait_relax"


def test_growth_timeline_is_descending_stable_and_limited():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        now = int(time.time())
        p = persona.load_persona()
        p["history"] = [
            {"ts": now - 20, "field": "traits.好奇", "from": 40, "to": 55, "source": "a"},
            {"ts": now - 20, "field": "traits.理智", "from": 60, "to": 45, "source": "b"},
            {"ts": now - 10, "field": "traits.温和", "from": 50, "to": 70, "source": "c"},
        ]
        persona.save_persona(p)
        report = persona.growth_timeline(days=30, limit=3)
        assert [e["ts"] for e in report["events"]] == [now - 10, now - 20, now - 20], report
        assert [e["title"] for e in report["events"][1:]] == ["好奇 +15", "理智 -15"], report
        assert len(persona.growth_timeline(days=30, limit=2)["events"]) == 2


def test_growth_trends_enforces_count_and_ratio_thresholds():
    now = int(time.time())
    old_ts = now - 21 * core.DAY
    new_ts = now - 7 * core.DAY
    rows = []

    def add(tag: str, old: int, new: int) -> None:
        for i in range(old):
            rows.append({"kind": "episode", "tags": [tag], "created": old_ts, "text": f"{tag}-旧-{i}"})
        for i in range(new):
            rows.append({"kind": "episode", "tags": [tag], "created": new_ts, "text": f"{tag}-新-{i}"})

    add("上升", 1, 2)
    add("消退", 2, 1)
    add("仅两次", 0, 2)
    add("比例不足", 2, 2)
    original = memory.load_all
    memory.load_all = lambda: rows
    try:
        report = persona.growth_trends(days=28)
    finally:
        memory.load_all = original
    assert [x["name"] for x in report["rising"]] == ["上升"], report
    assert [x["name"] for x in report["fading"]] == ["消退"], report


def test_persona_replay_reports_offset_without_writing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        p = persona.load_persona()
        persona.set_trait(p["traits"], "好奇", 50, why="测试基线")
        persona.save_persona(p)
        persona.persona_snapshot("回放基线")
        p = persona.load_persona()
        persona.set_trait(p["traits"], "好奇", 70, why="测试偏移")
        persona.save_persona(p)
        report = persona.persona_replay()
        assert report["readonly"] is True and report["unchanged"] is True, report
        assert not report["error"], report
        offset = next(x for x in report["offsets"] if x["key"] == "好奇")
        assert (offset["from"], offset["to"], offset["delta"]) == (50.0, 70.0, 20.0), offset


def test_saturation_report_scores_edges_and_alerts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        p = persona.load_persona()
        for name, value in (("居中", 50), ("偏高", 96), ("偏低", 3)):
            persona.set_trait(p["traits"], name, value, why="测试")
        persona.save_persona(p)
        report = persona.saturation_report(threshold=0.8)
        scores = {x["key"]: x["saturation"] for x in report["traits"]}
        assert scores == {"居中": 0.0, "偏高": 0.92, "偏低": 0.94}, scores
        assert {x["key"] for x in report["alerts"]} == {"偏高", "偏低"}
        assert report["max"] == 0.94 and report["alert_count"] == 2


def test_startup_ingest_runs_in_background_process():
    """startup 的增量摄取必须是独立子进程，不能占住首轮注入。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        cfg = _setup_home(Path(td))
        cfg["dialogue_ingest"] = True
        cfg["dialogue_ingest_auto"] = True
        _save_config(cfg)
        calls = []

        class FakePopen:
            def __init__(self, cmd, **kw):
                calls.append((cmd, kw))

        old = subprocess.Popen
        subprocess.Popen = FakePopen
        try:
            ok = cli._spawn_background_ingest(cfg)
        finally:
            subprocess.Popen = old
        assert ok is True, "配置开启时应拉起子进程"
        assert len(calls) == 1, calls
        cmd, kw = calls[0]
        assert "ingest-dialogues" in cmd, cmd
        assert "--limit" in cmd and "20" in cmd, cmd
        assert kw["stdout"] is subprocess.DEVNULL
        assert kw["stderr"] is subprocess.DEVNULL


def test_startup_ingest_noop_when_disabled():
    """开关关闭时不许起进程（默认就是关的，跨会话隐私）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        cfg = _setup_home(Path(td))
        cfg["dialogue_ingest"] = False
        cfg["dialogue_ingest_auto"] = True
        _save_config(cfg)
        called = []

        class FakePopen:
            def __init__(self, *a, **kw):
                called.append(1)

        old = subprocess.Popen
        subprocess.Popen = FakePopen
        try:
            ok = cli._spawn_background_ingest(cfg)
        finally:
            subprocess.Popen = old
        assert ok is False
        assert not called


def test_injection_survives_recent_failure():
    """最近记忆读取失败只能退化为空块，不能让整个注入崩掉。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        old = memory.recent
        memory.recent = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            text = memory.build_injection("")
        finally:
            memory.recent = old
        assert "长期记忆注入" in text
        assert "我是谁" in text      # 人格段仍在：降级只丢"最近记忆"这一块


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
