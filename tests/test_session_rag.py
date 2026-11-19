# -*- coding: utf-8 -*-
"""附录 B P0：跨会话原文检索服务（SQLite + FTS5 trigram）。"""
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
import session_rag as rag  # noqa: E402


def _home(td: Path) -> Path:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    return td


def test_init_ingest_and_chinese_substring_search():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        assert rag.init_db()["ok"]
        rows = [
            {"role": "user", "content": "上次说的缓存方案是先落盘再回填", "ts": 1},
            {"role": "assistant", "content": "对，用 WAL 模式", "ts": 2},
            {"role": "user", "content": "完全无关的一句话", "ts": 3},
        ]
        rep = rag.ingest_messages(rows, session="s1", platform="dsh")
        assert rep["added"] == 3
        hits = rag.search("缓存方案", k=5)
        assert hits and "缓存方案" in hits[0]["text"], hits
        assert rag.search("不存在的词xyz") == []


def test_ingest_is_idempotent_and_counts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        rows = [{"role": "user", "content": "幂等测试的原文", "ts": 10}]
        a = rag.ingest_messages(rows, session="s2")
        b = rag.ingest_messages(rows, session="s2")
        assert a["added"] == 1 and b["added"] == 0 and b["skipped"] == 1
        st = rag.stats()
        assert st["messages"] == 1 and st["sessions"] == 1


def test_ingest_jsonl_uses_stem_as_session():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        f = Path(td) / "chat_20261119.jsonl"
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"role": "user", "content": "文件里的云南旅行"},
            {"role": "assistant", "content": "大理和沙溪"},
        ]), encoding="utf-8")
        rep = rag.ingest_jsonl(f)
        assert rep["added"] == 2
        hits = rag.search("云南", k=3)
        assert hits and hits[0]["session"] == "chat_20261119"


def test_empty_query_and_bad_match_do_not_raise():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        rag.init_db()
        assert rag.search("") == []
        assert rag.search('"') == []      # 语法非法也不能炸


def test_cli_roundtrip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        home.mkdir()
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(home)
        py = sys.executable
        subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "init"],
                       capture_output=True, env=env)
        f = Path(td) / "t.jsonl"
        f.write_text(json.dumps({"role": "user", "content": "CLI 里的中文子串"}, ensure_ascii=False),
                     encoding="utf-8")
        r = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "rag", "ingest", str(f)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        assert r.returncode == 0, r.stderr
        r2 = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "rag", "search", "中文子串", "--json"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
        assert r2.returncode == 0, r2.stderr
        rows = json.loads(r2.stdout)
        assert rows and "中文子串" in rows[0]["text"]


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
