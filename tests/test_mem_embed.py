# -*- coding: utf-8 -*-
"""mem_embed.py 拆分回归（embedding / 降级向量从 core.py 拆出）。

背景：v3.39 起 embedding 实现（_embed_cfg / fallback_encode_cached / embed_texts）
从 core.py 拆到 mem_embed.py，core.py 末尾 `from mem_embed import *` 保留调用面。
本测试锁住拆分契约与原有熔断语义（不重试远端、降级向量兜底）：

  - core.* / `from core import *` 拿到的仍是 mem_embed 的同一函数（re-export 无感）；
  - 先 import mem_embed 再 import core 不能循环导入；
  - 远端失败 → 冷却（默认 300s）且同一批立即走降级向量；冷却期内不碰远端、不重复告警；
  - 冷却到期 → 允许重试；远端成功 → 清冷却；
  - 未配置远端时降级向量接管（256 维、L2 归一化），同文本命中进程内缓存。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401  （登记 _SYNC_TARGETS，切 HOME 后各子模块同步）
import core  # noqa: E402
import fallback_embed  # noqa: E402
import mem_embed  # noqa: E402
import persona  # noqa: E402

SCRIPTS = str(ROOT / "scripts")


def _home(td: Path, **embed) -> None:
    """把 core.HOME 锁到临时目录，写一份带 embed 段的 config（远端默认指向必然连不上的端口）。"""
    os.environ.pop("LINGSHU_EMBED_URL", None)   # 环境变量优先于 config，测试里必须清掉
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
    mem_embed._FB_EMBED_CACHE.clear()


class _Counter:
    """替身 urlopen：按 mode 失败或返回固定 8 维向量，并记录被调用次数。"""

    def __init__(self, mode="fail"):
        self.mode = mode
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.mode == "fail":
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        return _Resp()


class _Resp:
    def __init__(self):
        self._data = b'{"embedding": [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]}'

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_core_reexports_mem_embed():
    """拆出去后调用面不变：core.* 与 from core import * 仍解析到 mem_embed 的同一函数。"""
    assert core.embed_texts is mem_embed.embed_texts
    assert core._embed_cfg is mem_embed._embed_cfg
    assert core.fallback_encode_cached is mem_embed.fallback_encode_cached
    for name in ("embed_texts", "_embed_cfg", "fallback_encode_cached"):
        assert name in core.__all__, f"{name} 应从 core.__all__ 导出"
    ns: dict = {}
    exec("from core import *", ns)   # 模拟 memory.py / cli.py / emotion.py 的调用面
    assert ns["embed_texts"] is mem_embed.embed_texts
    assert ns["_embed_cfg"] is mem_embed._embed_cfg
    src = (ROOT / "scripts" / "core.py").read_text(encoding="utf-8")
    assert "from mem_embed import *" in src
    for gone in ("def embed_texts(", "def fallback_encode_cached(", "def _embed_cfg("):
        assert gone not in src, f"{gone[:-1]} 应已搬离 core.py"


def test_import_mem_embed_before_core_no_cycle():
    """先 import mem_embed 再 import core 也不能出现循环导入/半初始化。"""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {SCRIPTS!r})\n"
        "import mem_embed\n"
        "assert getattr(mem_embed, '_core_mod', None) is None, '导入期不应加载 core'\n"
        "import core\n"
        "assert core.embed_texts is mem_embed.embed_texts\n"
        "print('IMPORT_OK')\n"
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        env = dict(os.environ)
        env["BRAIN_MEMORY_HOME"] = str(td)
        env.pop("LINGSHU_EMBED_URL", None)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120, cwd=SCRIPTS, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_OK" in (proc.stdout or "")


def test_failure_enters_cooldown_and_skips_retry():
    """远端失败 → 冷却约 300s + 同一批降级；冷却期内不碰远端、不重复刷 warn 日志。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("fail")
        logs = []
        orig_urlopen = urllib.request.urlopen
        orig_log = core.log_event
        urllib.request.urlopen = counter
        core.log_event = lambda kind, message, level="info", **extra: logs.append((kind, message, level))
        try:
            first = mem_embed.embed_texts(["第一条"])
            assert counter.calls == 1, "第一次应该尝试远端"
            assert first and len(first) == 1 and len(first[0]) == 256, "远端失败应走降级向量"
            remaining = core._EMBED_DOWN_UNTIL - time.time()
            assert 290 <= remaining <= 300, f"默认冷却应约 300s，实际 {remaining:.1f}s"
            second = mem_embed.embed_texts(["第二条"])
            assert counter.calls == 1, "冷却期内不该再碰远端"
            assert second and len(second) == 1 and len(second[0]) == 256
            assert len(logs) == 1, f"冷却期内不该重复刷日志，实际 {len(logs)} 条"
            assert logs[0][0] == "embed" and logs[0][2] == "warn"
        finally:
            urllib.request.urlopen = orig_urlopen
            core.log_event = orig_log


def test_cooldown_expiry_allows_retry():
    """冷却到期后允许重新尝试远端（不会永久熔断）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("fail")
        orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = counter
        try:
            mem_embed.embed_texts(["x"])
            assert counter.calls == 1
            core._EMBED_DOWN_UNTIL = time.time() - 1.0   # 模拟冷却到期
            mem_embed.embed_texts(["y"])
            assert counter.calls == 2, "冷却到期后应允许重试"
        finally:
            urllib.request.urlopen = orig_urlopen


def test_success_clears_cooldown_and_cooling_skips_remote():
    """冷却期内直接降级不碰远端；远端成功一次就把冷却清零。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        counter = _Counter("ok")
        orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = counter
        try:
            core._EMBED_DOWN_UNTIL = time.time() + 300   # 冷却中
            cooling = mem_embed.embed_texts(["冷却中"])
            assert counter.calls == 0, "冷却期内不该碰远端"
            assert cooling and len(cooling[0]) == 256
            core._EMBED_DOWN_UNTIL = 1.0   # 历史时间戳 = 冷却已到期
            vecs = mem_embed.embed_texts(["成功"])
            assert vecs == [[0.1] * 8]
            assert core._EMBED_DOWN_UNTIL == 0.0, "远端成功应清空冷却"
        finally:
            urllib.request.urlopen = orig_urlopen


def test_fallback_effective_and_cached():
    """未配置远端时降级向量接管（256 维、L2 归一化）；同文本第二次命中进程内缓存。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td), url="")   # 不配远端 → 纯降级向量
        calls = {"n": 0}
        orig_encode = fallback_embed.encode

        def counting(text, dims=256):
            calls["n"] += 1
            return orig_encode(text, dims)

        fallback_embed.encode = counting
        try:
            v1 = mem_embed.embed_texts(["灵枢 记忆"])
            assert v1 and len(v1) == 1 and len(v1[0]) == 256
            norm = math.sqrt(sum(x * x for x in v1[0]))
            assert 0.99 <= norm <= 1.01, "降级向量应做 L2 归一化"
            v2 = mem_embed.embed_texts(["灵枢 记忆"])
            assert v2 == v1
            assert calls["n"] == 1, f"同文本应命中缓存，实际 encode {calls['n']} 次"
        finally:
            fallback_embed.encode = orig_encode
            mem_embed._FB_EMBED_CACHE.clear()


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
