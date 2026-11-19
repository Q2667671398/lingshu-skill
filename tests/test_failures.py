# -*- coding: utf-8 -*-
"""失败可见化（failures）回归：记录、聚合归一化、轮转、CLI、面板聚合。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import core  # noqa: E402
import failures  # noqa: E402
import insights  # noqa: E402


def _home(td: Path) -> Path:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    return td


def test_record_and_summarize_groups_normalized():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        failures.record("embed", "connection refused to 127.0.0.1:11434 id=abcd1234ef")
        failures.record("embed", "connection refused to 127.0.0.1:11434 id=99998888aa")
        failures.record("retriever", "timeout after 3s")
        rep = failures.summarize(1)
        assert rep["total"] == 3
        assert rep["by_kind"] == {"embed": 2, "retriever": 1}
        top = rep["top"][0]
        assert top["kind"] == "embed" and top["count"] == 2
        assert "<n>" in top["normalized"]


def test_recent_and_window_filter():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        failures.record("a", "旧记录", home=home)
        p = failures.path(home)
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
        rows[0]["ts"] = int(time.time()) - 10 * 86400   # 10 天前
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        assert failures.summarize(1, home=home)["total"] == 0
        assert failures.summarize(30, home=home)["total"] == 1
        assert failures.recent(5, home=home)[0]["msg"] == "旧记录"


def test_rotation_keeps_old_file():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        p = failures.path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * (failures.MAX_BYTES + 10), encoding="utf-8")
        failures.record("big", "触发轮转", home=home)
        assert p.with_suffix(".jsonl.1").exists()
        assert p.stat().st_size < failures.MAX_BYTES


def test_insights_includes_failures():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        failures.record("panel", "聚合里的失败")
        data = insights.build(light=True)["data"]
        assert data["failures"]["total"] == 1


def test_cli_json():
    import os
    import subprocess
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        home.mkdir()
        _home(home)                      # 让本进程的 failures 写进同一个 HOME
        failures.record("cli", "命令行里能看到")
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(home)
        py = sys.executable
        subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "init"],
                       capture_output=True, env=env)
        r = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "failures", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["total"] >= 1


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
