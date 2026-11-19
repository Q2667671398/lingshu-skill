# -*- coding: utf-8 -*-
"""附录 C P2 收尾：会话摘要的触发 / 落盘 / 回滚。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import core  # noqa: E402
import persona  # noqa: E402
import session_summary as ss  # noqa: E402

TURNS = [
    {"role": "user", "text": "把摘要触发接上，水位 60% 或每 8 轮"},
    {"role": "assistant", "text": "好，我加 decide() 和落盘轮转。"},
    {"role": "user", "text": "不对，回滚要保留至少 3 版"},
]


def _home(td: Path) -> Path:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["summary"]["enabled"] = False
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()
    return td


def test_decide_rules():
    assert ss.decide(force=True)["trigger"] is True
    assert ss.decide(used_pct=65)["trigger"] is True
    assert ss.decide(turns_since_last=8)["trigger"] is True
    d = ss.decide(turns_since_last=2, used_pct=30)
    assert d["trigger"] is False and "未触发" in d["reason"]
    # 阈值可配：把每 N 轮改成 3，2 轮不触发、3 轮触发
    assert ss.decide(turns_since_last=2, cfg={"summary_turns": 3})["trigger"] is False
    assert ss.decide(turns_since_last=3, cfg={"summary_turns": 3})["trigger"] is True


def test_save_rotates_and_keeps_three_versions():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        ss.save("第一版", "s1", home)
        ss.save("第二版", "s1", home)
        ss.save("第三版", "s1", home)
        assert "第三版" in ss.load("s1", home)
        paths = ss._paths("s1", home)
        assert "第二版" in paths[1].read_text(encoding="utf-8")
        assert "第一版" in paths[2].read_text(encoding="utf-8")


def test_rollback_restores_previous_version():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        ss.save("v1", "s1", home)
        ss.save("v2", "s1", home)
        res = ss.rollback("s1", home, steps=1)
        assert res["ok"]
        assert "v1" in ss.load("s1", home)

        assert ss.rollback("nope", home)["ok"] is False


def test_maybe_summarize_skips_without_trigger():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        res = ss.maybe_summarize(TURNS, session_id="s2", turns_since_last=1,
                                 used_pct=10, cfg=core.load_config(), home=home)
        assert res["skipped"] is True
        assert not ss._paths("s2", home)[0].exists()


def test_maybe_summarize_forced_falls_back_offline_and_persists():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        res = ss.maybe_summarize(TURNS, session_id="s3", force=True,
                                 cfg=core.load_config(), home=home)
        assert res["ok"] and res["source"] == "fallback"
        text = ss.load("s3", home)
        for h in ("目标", "已做", "未决", "约束", "原话引用"):
            assert f"## {h}" in text


def test_cli_build_show_rollback():
    py = sys.executable
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        home.mkdir()
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(home)
        inp = Path(td) / "turns.jsonl"
        inp.write_text("\n".join(json.dumps({"role": t["role"], "content": t["text"]},
                                            ensure_ascii=False) for t in TURNS), encoding="utf-8")
        subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "init"],
                       capture_output=True, env=env)
        r = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "session-summary",
                            "build", "--input", str(inp), "--force", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["ok"] and data["path"]
        r2 = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "session-summary",
                             "show", "--json"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        assert "目标" in json.loads(r2.stdout)["text"]


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
