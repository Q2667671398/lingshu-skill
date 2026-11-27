# -*- coding: utf-8 -*-
"""checkpoint 状态文件加锁回归（文档 §十 #10）。

契约：
  - `_save_state` 的「重读最新 → 合并 → 落盘」整段持 core._locked，锁文件与
    checkpoints.json 同目录，锁超时沿用 core._locked 默认语义，写完释放；
  - 两个线程同时 checkpoint 不同仓库，最终状态里两个仓库都在（不丢更新）；
  - 锁释放后状态文件仍可继续写，并发写也不会互相覆盖；
  - `_load_state` 是纯读：不建目录/锁文件、不改内容与 mtime、坏档不“修复”。
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import checkpoint as ck  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


def _home(td: Path) -> Path:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()
    return td


def _git_repo(td: Path, name: str) -> Path:
    repo = td / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "SKILL.md").write_text("version: 9.9\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"{name} first commit"], cwd=repo, check=True)
    return repo


def _lock_path(home: Path) -> Path:
    p = ck._state_path(home)
    return p.with_name(p.name + ".lock")


def test_save_state_holds_core_lock_next_to_state_file():
    """写状态必须走 core._locked：锁文件与 checkpoints.json 同目录、默认超时、用完释放。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        state_path = ck._state_path(home)
        seen = []
        original = core._locked
        default_timeout = inspect.signature(original).parameters["timeout"].default

        @contextmanager
        def spy(path, timeout=default_timeout):
            seen.append((Path(path), timeout))
            with original(path, timeout):
                lock = Path(path).with_name(Path(path).name + ".lock")
                # 进锁时要能看到锁文件，且就在 checkpoints.json 旁边
                assert lock.parent == state_path.parent, (lock, state_path)
                assert lock.exists(), f"锁文件不存在：{lock}"
                yield

        core._locked = spy
        try:
            ck._save_state({"C:\\proj-a": {"head": "aaa"}}, home)
        finally:
            core._locked = original
        assert len(seen) == 1, seen
        assert seen[0][0] == state_path, seen
        assert seen[0][0].name == "checkpoints.json", seen
        assert seen[0][1] == default_timeout, (seen, default_timeout)
        assert not _lock_path(home).exists(), "写完锁没释放"
        assert ck._load_state(home) == {"C:\\proj-a": {"head": "aaa"}}


def test_concurrent_checkpoints_keep_both_repos():
    """两个线程同时 checkpoint 不同仓库：各自都写成功，状态里两个仓库都留下。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        home = _home(root / "home")
        repo_a = _git_repo(root, "repo_a")
        repo_b = _git_repo(root, "repo_b")
        ev_a = ck.collect(str(repo_a))
        ev_b = ck.collect(str(repo_b))
        assert ev_a and ev_b and ev_a["repo"] != ev_b["repo"]

        results, errors = [], []
        barrier = threading.Barrier(2)

        def worker(repo: Path) -> None:
            try:
                barrier.wait(timeout=30)  # 两线程对齐后再同时跑，尽量让「读-改-写」重叠
                results.append(ck.checkpoint(str(repo), home=home))
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(r,), daemon=True,
                                    name=f"ck-{r.name}") for r in (repo_a, repo_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "并发 checkpoint 卡死（锁没释放？）"
        assert not errors, errors
        assert len(results) == 2, results
        assert all(r["ok"] and not r["skipped"] for r in results), results

        state = ck._load_state(home)
        print(f"  并发两次 checkpoint → 状态键：{sorted(state)}")
        assert ev_a["repo"] in state, state
        assert ev_b["repo"] in state, state  # 关键：并发写不能互相覆盖（丢更新）
        assert state[ev_a["repo"]]["head"] == ev_a["head"], state[ev_a["repo"]]
        assert state[ev_b["repo"]]["head"] == ev_b["head"], state[ev_b["repo"]]
        assert not _lock_path(home).exists(), "并发写完后锁文件残留"


def test_lock_released_and_state_stays_writable():
    """锁释放后可继续写；多线程抢同一把锁也不丢任何键。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        ck._save_state({"r1": {"head": "h1", "ts": 1}}, home)
        assert not _lock_path(home).exists(), "写完后锁文件残留"
        ck._save_state({"r2": {"head": "h2", "ts": 2}}, home)
        state = ck._load_state(home)
        assert state["r1"]["head"] == "h1" and state["r2"]["head"] == "h2", state

        errors = []

        def writer(i: int) -> None:
            try:
                ck._save_state({f"k{i}": {"head": f"h{i}"}}, home)
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "并发写卡死（锁没释放？）"
        assert not errors, errors
        state = ck._load_state(home)
        for i in range(4):
            assert f"k{i}" in state, (i, state)
        assert not _lock_path(home).exists(), "并发写完后锁文件残留"


def test_load_state_read_has_no_side_effects():
    """读状态是纯读：不建 sessions 目录/锁文件、不改内容与 mtime、坏档不写回。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        state_path = ck._state_path(home)
        assert ck._load_state(home) == {}
        assert not state_path.parent.exists(), "只读不该创建 sessions/ 目录"
        assert not state_path.exists()
        assert not _lock_path(home).exists()

        state_path.parent.mkdir(parents=True)
        payload = {"C:\\proj": {"head": "abc1234", "ts": 1, "changed": 0, "version": "9.9"}}
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        state_path.write_text(raw, encoding="utf-8")
        before = state_path.stat()
        assert ck._load_state(home) == payload
        after = state_path.stat()
        assert state_path.read_text(encoding="utf-8") == raw, "读改动了状态文件内容"
        assert after.st_mtime_ns == before.st_mtime_ns, "读改动了状态文件 mtime"
        assert sorted(p.name for p in state_path.parent.iterdir()) == ["checkpoints.json"]

        # 坏档按空态处理，但不“自愈”写盘（读没有副作用）
        state_path.write_text("{broken", encoding="utf-8")
        assert ck._load_state(home) == {}
        assert state_path.read_text(encoding="utf-8") == "{broken", "坏档被读操作改写了"
        assert sorted(p.name for p in state_path.parent.iterdir()) == ["checkpoints.json"]


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
