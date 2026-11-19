# -*- coding: utf-8 -*-
"""跨对话摄取回归测试：编码、分段读取、偏移去重与文件锁。"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401
import core  # noqa: E402
import memory  # noqa: E402


def _setup_home(tmp: Path, sources: list) -> None:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["dialogue_ingest"] = True
    cfg["dialogue_sources"] = [str(p) for p in sources]
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    core.atomic_json(tmp / "config.json", cfg)


def _offsets() -> dict:
    return json.loads((core.HOME / "dialogue_offsets.json").read_text(encoding="utf-8"))


def test_ingest_gbk_dialogue_without_replacement_chars():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        _setup_home(root / "home", [src])
        (src / "gbk.txt").write_bytes("用户: 我想去云南旅行\n我: 可以安排大理\n".encode("gb18030"))
        rep = memory.ingest_dialogues()
        assert rep["imported"] == 1, rep
        texts = [m.get("text", "") for m in memory.load_all() if m.get("source") == "dialogue"]
        assert any("云南" in text for text in texts), texts
        assert all("\ufffd" not in text for text in texts), texts


def test_ingest_reads_at_most_eight_mib_then_resumes():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        _setup_home(root / "home", [src])
        path = src / "large.txt"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for i in range(9):
                fh.write(f"用户: 分段{i}-" + chr(ord("甲") + i) * 1_000_000 + "\n")
        memory.ingest_dialogues()
        entry = next(iter(_offsets().values()))
        assert 0 < entry["offset"] <= 8 * 1024 * 1024, entry
        assert entry["offset"] < path.stat().st_size, entry
        for _ in range(6):
            memory.ingest_dialogues()
            entry = next(iter(_offsets().values()))
            if entry["offset"] == path.stat().st_size:
                break
        assert entry["offset"] == path.stat().st_size, entry


def test_ingest_two_files_is_idempotent():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        _setup_home(root / "home", [src])
        (src / "a.txt").write_text("用户: 第一份会话记录\n", encoding="utf-8")
        (src / "b.md").write_text("用户: 第二份会话记录\n", encoding="utf-8")
        first = memory.ingest_dialogues()
        second = memory.ingest_dialogues()
        assert first["imported"] == 2, first
        assert second["imported"] == 0, second
        rows = [m for m in memory.load_all() if m.get("source") == "dialogue"]
        assert len(rows) == 2, rows


def test_ingest_dry_run_does_not_scan_memory_database():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        _setup_home(root / "home", [src])
        (src / "a.txt").write_text("用户: 跨会话测试内容\n", encoding="utf-8")
        calls = []
        original = memory.load_all

        def counted():
            calls.append(1)
            return original()

        memory.load_all = counted
        try:
            memory.ingest_dialogues(dry_run=True)
        finally:
            memory.load_all = original
        assert not calls, f"dry-run 不应全量扫描记忆库，load_all 调用 {len(calls)} 次"


def test_ingest_offsets_hold_chunk_hashes_and_lock():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        _setup_home(root / "home", [src])
        (src / "a.txt").write_text("用户: 跨会话去重内容\n", encoding="utf-8")
        locked = []
        original = memory._locked

        @contextmanager
        def spy(path, timeout=10.0):
            locked.append(Path(path).name)
            with original(path, timeout):
                yield

        memory._locked = spy
        try:
            memory.ingest_dialogues()
        finally:
            memory._locked = original
        assert "dialogue_offsets.json" in locked, locked
        entry = next(iter(_offsets().values()))
        assert entry.get("chunk_hashes"), entry


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
