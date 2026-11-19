# -*- coding: utf-8 -*-
"""读写健壮性回归：read_jsonl 瞬时占用重试 + mark_reviewed 落盘失败可见。

修的是两个已确认的真 bug：
1) core.read_jsonl 旧实现 `if not path.exists(): return []` 把 Windows 上 stat 被拒
   的瞬时错误当成"文件不存在"，且读取本身无重试 → 上层静默读到空库。
   现在：判存在性与读取都按 3 次短退避重试（只对 PermissionError / WinError
   5/32/33/145）；真不存在仍返回 []，读不到则明确抛异常；JSON 坏行保持原有
   宽容修复策略（好记录保留、坏块进 .corrupt 备份）。
2) memory.mark_reviewed 旧实现忽略 _patch_memory_fields(found) 的返回值，
   落盘失败仍返回成功形状 dict → cli.py review-confirm 会打印"已确认复核"，
   而磁盘上仍是到期态。现在：补丁失败抛 LookupError（cli.py 现有
   `except (LookupError, PermissionError)` 能接住并返回 1），写盘异常照旧上抛。

不依赖固定 HOME：需要数据目录的用例把 core.HOME 重定向到临时目录。
只依赖标准库。运行：python tests/test_read_write_robustness.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import core  # noqa: E402
import memory  # noqa: E402
from io_retry_util import run_with_io_retry  # noqa: E402


def _home(td: Path) -> Path:
    """把数据目录重定向到 `td/home/`，并关掉 embedding 后台线程。

    两个确定性措施（不做的话全量跑会偶发 flaky）：
    * HOME 指向 td/home，而用例注入故障的目标文件放在 td 根目录——后台线程只会
      读 HOME 里的文件，永远碰不到被测目标路径；
    * config 关掉 embed.fallback.enabled 且 embed.url=""（环境变量覆盖同理清空），
      remember() 不再起 _embed_worker daemon 线程。该线程会 load_all() 看书，
      在后续用例全局 mock Path.read_text 时进来读，把"读了几次"的计数顶偏。
    """
    os.environ["LINGSHU_EMBED_URL"] = ""
    os.environ["LINGSHU_RERANK_URL"] = ""
    home = td / "home"
    core.HOME = home
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = copy.deepcopy(core.DEFAULT_CONFIG)
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    core.atomic_json(home / "config.json", cfg)
    return home


def _write_rows(p: Path, rows: list) -> None:
    p.write_bytes("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8"))


def _path_key(p) -> str:
    return os.path.normcase(str(p))


def _flaky_read_text(target: Path, failures: int | None = None, exc_factory=None):
    """read_text 故障注入，**只对 target 路径生效**。

    failures=None 表示 target 每次读都失败；failures=N 表示前 N 次失败、之后放行。
    其它路径（含任何后台线程读的 HOME 文件）一律原样透传且不计数，返回的
    calls["n"] 只统计 target 的读取次数——计数与进程内其它 I/O 完全解耦。
    """
    real = Path.read_text
    key = _path_key(target)
    calls = {"n": 0}
    make_exc = exc_factory or (
        lambda self: PermissionError(13, "拒绝访问（模拟瞬时占用）", str(self)))

    def flaky(self, *args, **kwargs):
        if _path_key(self) == key:
            calls["n"] += 1
            if failures is None or calls["n"] <= failures:
                raise make_exc(self)
        return real(self, *args, **kwargs)

    return mock.patch.object(Path, "read_text", flaky), calls


def _flaky_exists(target: Path, failures: int):
    """exists() 故障注入，只对 target 路径生效（前 failures 次抛瞬时 PermissionError）。"""
    real = Path.exists
    key = _path_key(target)
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        if _path_key(self) == key:
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(13, "拒绝访问（模拟 stat 被拒）", str(self))
        return real(self, *args, **kwargs)

    return mock.patch.object(Path, "exists", flaky), calls


# ---------------------------------------------------------------- read_jsonl 重试

def test_read_jsonl_retries_transient_read_error():
    """读瞬时被拒 2 次后第 3 次成功：短退避重试内拿到完整数据。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        p = Path(td) / "hippocampus.jsonl"   # 放 HOME 之外：后台线程不会读它
        _write_rows(p, [{"id": "a"}, {"id": "b"}])
        patch, calls = _flaky_read_text(p, failures=2)
        with patch, mock.patch.object(core, "_READ_RETRY_DELAY", 0.001):
            rows = core.read_jsonl(p)
        assert calls["n"] == 3, f"应重试到第 3 次才成功，实际读 {calls['n']} 次"
        assert [r["id"] for r in rows] == ["a", "b"]


def test_read_jsonl_retries_transient_stat_error():
    """exists() 抛 OSError（stat 被拒）不能被当成"文件不存在"返回空库，重试后读全。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        p = Path(td) / "cortex.jsonl"
        _write_rows(p, [{"id": "x"}])
        patch, calls = _flaky_exists(p, failures=1)
        with patch, mock.patch.object(core, "_READ_RETRY_DELAY", 0.001):
            rows = core.read_jsonl(p)
        assert calls["n"] == 2
        assert [r["id"] for r in rows] == ["x"], "stat 瞬时失败被误判成空库"


def test_read_jsonl_persistent_transient_error_raises_not_empty():
    """瞬时错误 3 次都失败：明确抛异常，绝不返回 []；非瞬时错误不重试。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        p = Path(td) / "archive.jsonl"
        _write_rows(p, [{"id": "y"}])
        patch, calls = _flaky_read_text(p, failures=None)
        with patch, mock.patch.object(core, "_READ_RETRY_DELAY", 0.001):
            try:
                rows = core.read_jsonl(p)
            except PermissionError:
                pass
            else:
                assert False, f"读不到不能降级成空库，却返回 {rows!r}"
        assert calls["n"] == 3, f"瞬时错误应重试 3 次，实际 {calls['n']}"

        # 非瞬时错误（例如读取途中文件被删）不重试，原样抛出。
        patch2, calls2 = _flaky_read_text(
            p, failures=None,
            exc_factory=lambda self: FileNotFoundError(2, "不存在", str(self)))
        with patch2:
            try:
                core.read_jsonl(p)
            except FileNotFoundError:
                pass
            else:
                assert False, "非瞬时错误不应被吞掉"
        assert calls2["n"] == 1, f"非瞬时错误不应重试，实际 {calls2['n']} 次"


# ---------------------------------------------------------------- 正常路径 / 损坏容忍

def test_read_jsonl_normal_paths_unchanged():
    """正常路径不变：真不存在/空文件返回 []，普通文件与 BOM 文件正常解析。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        root = Path(td)
        assert core.read_jsonl(root / "missing.jsonl") == []
        empty = root / "empty.jsonl"
        empty.write_bytes(b"")
        assert core.read_jsonl(empty) == []
        normal = root / "dust.jsonl"
        _write_rows(normal, [{"id": "n1"}, {"id": "n2"}])
        assert [r["id"] for r in core.read_jsonl(normal)] == ["n1", "n2"]
        bom = root / "bom.jsonl"
        bom.write_bytes(("\ufeff" + '{"id": "bom"}\n').encode("utf-8"))
        assert core.read_jsonl(bom) == [{"id": "bom"}]


def test_read_jsonl_corrupt_blocks_still_tolerated():
    """真损坏不被掩盖也不被当成 IO 故障：好记录照读，坏块进 .corrupt 备份。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = _home(Path(td))
        # 用记忆层文件名：坏块自愈会写 .corrupt 备份（任意 jsonl 都走同一修法）
        p = home / "hippocampus.jsonl"
        p.write_bytes(b'{"id": "good1"}\r\nnot-a-json-line\r\n{"id": "good2"}\r\n')
        # _corrupt_reported 是进程级去重缓存：同一进程重跑时先清掉，保证本次会修
        if getattr(core.read_jsonl, "_corrupt_reported", None) is not None:
            core.read_jsonl._corrupt_reported.clear()
        rows = core.read_jsonl(p)
        assert [r["id"] for r in rows] == ["good1", "good2"]
        corrupt = p.with_name(p.name + ".corrupt")
        assert corrupt.exists(), "坏块应写进 .corrupt 备份，不能静默丢弃"
        assert "not-a-json-line" in corrupt.read_text(encoding="utf-8")
        # 自愈后原文件只剩好记录；再次读取不丢数据
        assert [r["id"] for r in core.read_jsonl(p)] == ["good1", "good2"]


# ---------------------------------------------------------------- mark_reviewed 落盘返回值

def test_mark_reviewed_write_failure_is_visible():
    """落盘失败不再返回成功形状：补丁返回 False → LookupError；写盘异常 → 上抛。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        rec, _ = memory.remember("待复核的事实", "fact", ttl_days=7)
        mid = rec["id"]

        # A) 定向补丁没落盘（锁内复读找不到目标 / 磁盘读不到）：必须显式失败
        with mock.patch.object(core, "_patch_memory_fields", lambda _rec: False):
            try:
                result = memory.mark_reviewed(mid)
            except LookupError as exc:
                assert "未落盘" in str(exc) and mid in str(exc), str(exc)
            else:
                assert False, f"落盘失败仍返回成功形状：{result!r}"
        on_disk = [m for m in memory.load_all() if m.get("id") == mid][0]
        assert not on_disk.get("review_by_confirmed"), "A 场景不应改动磁盘"

        # B) 真正写盘抛 PermissionError：异常上抛，调用方不会误报成功
        with mock.patch.object(core, "write_jsonl",
                               side_effect=PermissionError(13, "拒绝访问")):
            try:
                result = memory.mark_reviewed(mid)
            except PermissionError:
                pass
            else:
                assert False, f"写盘失败仍返回成功形状：{result!r}"
        still = [m for m in memory.load_all() if m.get("id") == mid][0]
        assert not still.get("review_by_confirmed"), "B 场景写盘失败不应改动磁盘"


def test_mark_reviewed_normal_path_unchanged():
    """正常路径不变：返回确认记录、磁盘同步、due_for_review 不再提醒。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        rec, _ = memory.remember("待复核的事实", "fact", ttl_days=7)
        rec["review_by"] = core.now_ts() - 100
        assert core._patch_memory_fields(rec) is True
        assert rec["id"] in [m["id"] for m in memory.due_for_review()]

        m = memory.mark_reviewed(rec["id"])

        assert m.get("id") == rec["id"]
        assert m.get("review_by_confirmed")
        assert m["review_by"] > core.now_ts()
        assert rec["id"] not in [x["id"] for x in memory.due_for_review()]
        on_disk = [r for r in memory.load_all() if r.get("id") == rec["id"]][0]
        assert on_disk.get("review_by_confirmed") == m["review_by_confirmed"]
        assert on_disk.get("review_by") == m["review_by"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            run_with_io_retry(fn)
            print(f"  [PASS] {fn.__name__}")
        except Exception as exc:
            failed += 1
            import traceback
            print(f"  [FAIL] {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
