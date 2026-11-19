# -*- coding: utf-8 -*-
"""embedding 远端失败后的冷却（circuit breaker）回归。

背景：Ollama 没起时每次 recall 都会等满 8s 超时、并往 lingshu.log 刷一条 warn。
契约：
  - 第一次失败 → 冷却（默认 300s），走降级向量；
  - 冷却期内再调用 → 不再碰远端（urlopen 不被调用），仍返回兜底向量；
  - 冷却到期 → 允许重试；
  - 远端成功 → 清掉冷却。
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import core  # noqa: E402
import persona  # noqa: E402


def _home(td: Path, **embed) -> None:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"].update({"url": "http://127.0.0.1:9/api/embeddings",
                         "model": "fake", "timeout_sec": 0.2})
    cfg["embed"].update(embed)
    cfg["embed"].setdefault("fallback", {})["enabled"] = True
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()
    core._EMBED_DOWN_UNTIL = 0.0


class _Counter:
    def __init__(self, mode="fail"):
        self.mode = mode
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.mode == "fail":
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        payload = json.dumps({"embedding": [0.1] * 8}).encode()
        return _Resp(payload)


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_failure_sets_cooldown_and_skips_retry():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("fail")
        orig = urllib.request.urlopen
        urllib.request.urlopen = counter
        try:
            first = core.embed_texts(["第一条"])
            assert counter.calls == 1
            assert first and len(first) == 1, "第一次失败应走兜底向量"
            second = core.embed_texts(["第二条"])
            assert counter.calls == 1, "冷却期内不该再碰远端"
            assert second and len(second) == 1
            assert core._EMBED_DOWN_UNTIL > 0
        finally:
            urllib.request.urlopen = orig


def test_cooldown_expiry_allows_retry():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("fail")
        orig = urllib.request.urlopen
        urllib.request.urlopen = counter
        try:
            core.embed_texts(["x"])
            assert counter.calls == 1
            core._EMBED_DOWN_UNTIL = 0.0   # 模拟冷却到期
            core.embed_texts(["y"])
            assert counter.calls == 2
        finally:
            urllib.request.urlopen = orig


def test_success_clears_cooldown():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("ok")
        orig = urllib.request.urlopen
        urllib.request.urlopen = counter
        try:
            core._EMBED_DOWN_UNTIL = 1.0   # 先设为"冷却中"也不该影响成功路径之外
            vecs = core.embed_texts(["成功"])
            assert vecs == [[0.1] * 8]
            assert core._EMBED_DOWN_UNTIL == 0.0
        finally:
            urllib.request.urlopen = orig


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
