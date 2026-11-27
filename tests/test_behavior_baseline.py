# -*- coding: utf-8 -*-
"""行为基线工具（scripts/behavior_baseline.py）的确定性回归。

背景：这个工具的价值完全建立在"同一份输入产出同一份输出"上。第一版写完当场就
翻车——`record` 之后连续 `check` 有 1/3 概率报差异，排查后是两个独立的坑：

  1. 「刚刚发生」段取的是 `[m for m in _recent40 if created >= last_startup][:4]`
     ——**前 4 条**。种子记忆同一秒写入时 created 全相等、排序并列，谁进这 4 条
     取决于文件先后。
  2. 修法一版给种子钉时间戳时又按 `(created, text)` 排序，结果 6 条写入只要跨过
     秒边界，按 created 排出来的顺序每次都不一样 → 谁拿到最新时间戳跟着变 → 切分点
     又飘了。最终改成**只按 text 定序**才真正稳定。

本测试锁住：连续多次采集必须逐字相同，且抹平规则只抹该抹的（id/时间/耗时）。
零依赖：`python tests/test_behavior_baseline.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import behavior_baseline as bb  # noqa: E402


def test_normalize_redacts_only_listed_shapes():
    raw = ("id=m_1795000000_abcdef12 created 2026-11-21 14:05:03 "
           "date 2026-11-21 ts 1795000001 took 123.4 ms kept=hello")
    out = bb._normalize(raw)
    assert "m_<ID>" in out, out
    assert "<DATETIME>" in out, out
    assert "<DATE>" in out, out
    assert "<TS>" in out, out
    assert "<DUR>" in out, out
    assert "kept=hello" in out, f"不该抹平普通内容：{out}"


def test_capture_is_deterministic():
    """核心断言：连续两次采集必须逐字相同（这条不过，基线工具就没有意义）。"""
    a = bb._fresh_capture()
    b = bb._fresh_capture()
    assert a.keys() == b.keys(), (sorted(a), sorted(b))
    for key in a:
        assert a[key] == b[key], f"输出 {key!r} 不稳定（同一份种子库产出不同文本）"


def test_snapshot_covers_expected_keys():
    snap = bb._fresh_capture()
    assert "injection" in snap, sorted(snap)
    assert "recall_json" in snap, sorted(snap)
    # 空 query 与至少一个真实 query 都要在
    assert any(k != "injection" and k != "recall_json" for k in snap), sorted(snap)


def test_seed_memories_have_distinct_text():
    """种子文本必须互不相同——若重复会被 remember 合并，条数就不稳了。"""
    texts = [t for t, *_ in bb.SEED_MEMORIES]
    assert len(texts) == len(set(texts)), "种子文本有重复"


def test_pinned_times_are_distinct():
    """钉完时间戳后 created 必须互不相同，否则 `[:4]` 的切分点仍会飘。"""
    import json
    import tempfile
    import shutil
    home = Path(tempfile.mkdtemp(prefix="ls-pin-"))
    try:
        bb._seed(home)
        f = home / "hippocampus.jsonl"
        if not f.is_file():
            return  # 该版本把记忆写进别的层，跳过
        rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        created = [r.get("created") for r in rows]
        assert len(created) == len(set(created)), f"created 有重复：{created}"
    finally:
        shutil.rmtree(home, ignore_errors=True)


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
