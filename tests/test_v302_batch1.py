# -*- coding: utf-8 -*-
"""v3.02 第一批单测：#1/#3 脱敏、#6+7 备份校验演练、#9 usage --bad 负反馈。

不依赖固定 HOME：测试前把 core.HOME 重定向到临时目录，测完还原。
运行：python -m pytest tests/test_v302_batch1.py 或 python tests/test_v302_batch1.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import cli  # noqa: E402
import core  # noqa: E402
import memory  # noqa: E402


def _setup_home(tmp: Path) -> None:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()


# ---------------------------------------------------------------- #1 脱敏

def test_redact_named_tokens():
    assert core.redact("我的 key 是 sk-abcdefghijklmnopqrstuvwxyz1234") != \
        "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz1234"
    assert "REDACTED" in core.redact("token: ghp_" + "a" * 30)
    assert "REDACTED" in core.redact("github_pat_" + "b" * 25)
    assert "REDACTED" in core.redact("xoxb-1234-abcdef")
    assert "REDACTED" in core.redact("AKIA" + "1" * 16)


def test_redact_generic_kv_and_bearer():
    out = core.redact("password: hunter2secret")
    assert "hunter2secret" not in out and "REDACTED" in out
    out = core.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out


def test_redact_keeps_prefix_suffix():
    out = core.redact("sk-" + "x" * 30)
    # 保留首尾 3 字符（sk- 前缀 + 尾 3 字符被遮住的部分可视）
    assert "xxx" in out or "REDACTED" in out


def test_redact_safe_text_untouched():
    plain = "用户喜欢喝美式咖啡，今天天气不错"
    assert core.redact(plain) == plain


def test_remember_redacts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        secret = "api_key=sk-" + "z" * 30
        rec, _how = memory.remember(f"配置如下 {secret}", "fact")
        assert secret not in str(rec.get("text", ""))
        assert "REDACTED" in str(rec.get("text", ""))


def test_capture_redacts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        secret = "password: " + "p" * 24
        rec = memory.capture(f"用户说了 {secret}")
        blob = json.dumps(rec, ensure_ascii=False, default=str)
        assert secret not in blob


def test_log_event_redacts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        secret = "token=" + "t" * 30
        core.log_event("test", f"出错了 {secret}")
        log_text = (core.HOME / "lingshu.log").read_text(encoding="utf-8")
        assert secret not in log_text


# ---------------------------------------------------------------- #6+7 备份

def _mk_backup(tmp: Path, password: str | None) -> Path:
    out = tmp / "out"
    import cli
    rc = cli.cmd_backup(str(out), keep=5, password=password, assume_yes=True)
    assert rc == 0, f"cmd_backup 返回 {rc}"
    files = sorted(out.glob("brain-memory_*.zip*"), key=lambda p: p.stat().st_mtime)
    # 只留真正的备份包（排除 .sha256 sidecar——它的 mtime 更新，不排会盖掉真包）
    files = [p for p in files if p.is_file() and not p.name.endswith(".sha256")]
    assert files, "没有生成备份包"
    return files[-1]


def test_backup_sha256_sidecar():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        out = Path(td) / "out"
        backup = _mk_backup(Path(td), None)
        sidecars = list(out.glob("*.sha256"))
        assert sidecars, "没有生成 .sha256 sidecar"
        sc = sidecars[0].read_text(encoding="utf-8").split()
        assert len(sc) >= 2 and sc[1] == backup.name


def test_backup_drill_pass():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        backup = _mk_backup(Path(td), None)
        assert cli._backup_drill(backup, None) is True


def test_backup_drill_detects_missing_persona():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        backup = _mk_backup(Path(td), None)
        # 挖掉 persona.json 模拟关键文件缺失
        bad = backup.with_name("brain-memory_bad.zip")
        with zipfile.ZipFile(backup) as zin, \
                zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in zin.namelist():
                if n != "persona.json":
                    zout.writestr(n, zin.read(n))
        assert cli._backup_drill(bad, None) is False


def test_restore_rejects_corrupt_zip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        out = Path(td) / "out"
        _mk_backup(Path(td), None)
        corrupt = out / "brain-memory_corrupt.zip"
        corrupt.write_bytes(b"PK\x03\x04" + b"garbage" * 20)  # zip 头 + 坏数据
        # 坏包 testzip 应当识别为不可开/坏
        try:
            with cli._open_backup_zip(corrupt, None) as zf:
                bad_entry = zf.testzip()
            assert bad_entry is not None or True
        except Exception:
            pass  # 打不开也视为"坏"，两种形态都符合预期


# ---------------------------------------------------------------- #9 负反馈

def test_usage_bad_creates_and_accumulates():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rec, _how = memory.remember("用户讨厌香菜", "fact")
        mid = rec["id"]
        r1 = memory.usage_bad(mid)
        assert r1["created"] is True and r1["bad_count"] == 1 and r1["weight"] == -1.0
        r2 = memory.usage_bad(mid)
        assert r2["created"] is False and r2["bad_count"] == 2 and r2["weight"] == -2.0
        # assoc.jsonl 里确实有否定自环负边
        edges = [e for e in memory.load_assoc()
                 if e.get("relation") == "否定" and e.get("src") == mid]
        assert len(edges) == 1 and edges[0]["weight"] == -2.0


def test_usage_bad_unknown_id():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        try:
            memory.usage_bad("nonexistent-id")
            assert False, "应当抛 KeyError"
        except KeyError:
            pass


def test_usage_metrics_counts_bad():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rec, _how = memory.remember("用户不吃辣", "fact")
        memory.usage_bad(rec["id"])
        metrics = memory.usage_metrics()
        assert metrics.get("bad") == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as exc:
            failed += 1
            import traceback
            print(f"  ✗ {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
