# -*- coding: utf-8 -*-
"""任务收尾清点（checkpoint）回归。

契约：
  - 用 git 证据生成收尾文本（项目/分支/HEAD/最后提交/工作区/版本）；
  - 同一 HEAD + 工作区干净 → 不重复写；有新提交或有未提交改动 → 写；
  - force 强制写；dry-run 不写库、不记状态；
  - 非 git 目录 → 明确跳过，不报错。
"""
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


def _git_repo(td: Path) -> Path:
    repo = td / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "SKILL.md").write_text("version: 9.9\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first commit"], cwd=repo, check=True)
    return repo


def test_non_git_dir_skips_cleanly():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td) / "home")
        res = ck.checkpoint(str(Path(td) / "plain"), home=home)
        assert res["ok"] is False and res["skipped"] is True
        assert "不是 git 仓库" in res["reason"]


def test_collect_and_text_include_evidence():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        repo = _git_repo(Path(td))
        ev = ck.collect(str(repo))
        assert ev["name"] == "proj" and ev["head"]
        assert ev["subject"] == "first commit"
        assert ev["version"] == "9.9"
        text = ck.build_text(ev, "把收尾清点加上")
        for frag in ("任务收尾", "proj", ev["head"], "first commit", "干净", "9.9", "收尾清点"):
            assert frag in text, (frag, text)


def test_first_checkpoint_writes_then_skips_unchanged():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td) / "home")
        repo = _git_repo(Path(td))
        first = ck.checkpoint(str(repo), home=home)
        assert first["ok"] and not first["skipped"], first
        assert first["memory_id"]
        second = ck.checkpoint(str(repo), home=home)
        assert second["skipped"] is True
        assert "不重复" in second["reason"]
        # 记忆里确实有一条
        texts = " ".join(str(m.get("text") or "") for m in memory.load_all())
        assert "任务收尾" in texts


def test_new_commit_or_dirty_tree_triggers_again():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td) / "home")
        repo = _git_repo(Path(td))
        ck.checkpoint(str(repo), home=home)
        # 脏工作区
        (repo / "draft.txt").write_text("wip", encoding="utf-8")
        dirty = ck.checkpoint(str(repo), home=home)
        assert dirty["ok"] and not dirty["skipped"]
        # 提交后 HEAD 变
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "second commit"], cwd=repo, check=True)
        after = ck.checkpoint(str(repo), home=home)
        assert after["ok"] and not after["skipped"]
        assert "second commit" in after["text"]


def test_dry_run_writes_nothing_and_force_overrides():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td) / "home")
        repo = _git_repo(Path(td))
        dry = ck.checkpoint(str(repo), home=home, dry_run=True)
        assert dry["ok"] and not dry["skipped"]
        assert ck._load_state(home) == {}
        real = ck.checkpoint(str(repo), home=home)
        assert not real["skipped"]
        forced = ck.checkpoint(str(repo), home=home, force=True)
        assert forced["ok"] and not forced["skipped"]
        state = ck._load_state(home)
        assert state[ck.collect(str(repo))["repo"]]["head"]


def test_cli_json_roundtrip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td) / "home"
        home.mkdir()
        repo = _git_repo(Path(td))
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(home)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "brain.py"), "init"],
                       capture_output=True, env=env)
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "brain.py"),
                            "checkpoint", "--repo", str(repo), "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        # 失败时必须带上下文：原来这两条断言的消息是空的，flaky 时只知道"挂了"、
        # 不知道是 returncode 非零还是 ok=False（台账 #71）。
        ctx = (f"\n  returncode={r.returncode}"
               f"\n  stdout={r.stdout!r}"
               f"\n  stderr={r.stderr!r}"
               f"\n  home={home}  repo={repo}")
        assert r.returncode == 0, "CLI 返回码非零：" + ctx
        data = json.loads(r.stdout)
        assert data.get("ok") and data.get("memory_id"), "ok/memory_id 缺失：" + ctx
        assert "任务收尾" in data["text"], "text 缺关键词：" + ctx


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
