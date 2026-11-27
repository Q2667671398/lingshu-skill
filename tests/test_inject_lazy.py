# -*- coding: utf-8 -*-
"""五-C：记忆正文惰性注入（inject_mode=full|lazy）回归。

契约：
  full（默认）→ 正文照旧，不出现 id 指针
  lazy        → 每条一句要点 + id，并带"正文用 recall 取"提示
  预算紧张    → 即使 full，也会把放不下的整行降级成指针，而不是直接丢
  recall 路径 → 指针里的 id 能 dereference 回原文（信息没丢）
"""
from __future__ import annotations
import re

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import core  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402


def _setup_home(tmp: Path, **cfg_over) -> dict:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = False
    for k, v in cfg_over.items():
        cfg[k] = v
    core.atomic_json(tmp / "config.json", cfg)
    persona.load_persona()
    return cfg


def _save_config(cfg: dict) -> None:
    core.atomic_json(core.HOME / "config.json", cfg)


def test_full_mode_keeps_body_without_pointer():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        memory.remember("用户喜欢在深夜写代码，讨厌被打断", kind="preference", importance=0.9)
        text = memory.build_injection()
        assert "深夜写代码" in text
        assert "id:" not in text
        assert "指针条目" not in text


def test_lazy_mode_emits_pointer_and_hint():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), inject_mode="lazy")
        memory.remember("用户喜欢在深夜写代码，讨厌被打断", kind="preference", importance=0.9)
        text = memory.build_injection()
        assert "id:" in text, text
        assert "深夜写代码" in text          # 要点还在
        assert "recall" in text              # 指针里带取正文的办法


def test_lazy_truncates_long_body():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), inject_mode="lazy", inject_lazy_chars=30)
        long_text = "很长的一段记忆正文，" * 40 + "结尾暗号"
        memory.remember(long_text, kind="fact", importance=0.9)
        text = memory.build_injection()
        assert "结尾暗号" not in text, "lazy 应该只留要点，不该带全文"
        assert "…" in text


def test_budget_pressure_degrades_to_pointer_in_full_mode():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), inject_mode="full")
        # 把 last_startup 推到未来，让这些记忆走「最近的记忆」而不是「刚刚发生」
        cfg = core.load_config()
        cfg["last_startup"] = {"ts": core.now_ts() + 3600}
        _save_config(cfg)
        for i in range(12):
            memory.remember(("长记忆 %d 号：" % i) + ("细节内容" * 60),
                            kind="fact", importance=0.5)
        text = memory.build_injection(budget_tokens=500)
        # 指针形态有两种写法（≥3 条时紧凑 `（7f3ca9ac）`；少于 3 条自解释 `（id:7f3ca9ac，…）`）
        assert ("id:" in text) or re.search(r"（[0-9a-f]{8}）", text), \
            "预算紧张时应自动降级成指针"


def test_pointer_id_resolves_back_to_full_text():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td), inject_mode="lazy", inject_lazy_chars=20)
        body = "指针可解引用验证：" + "细节" * 30
        memory.remember(body, kind="fact", importance=0.9)
        text = memory.build_injection()
        # 新契约：指针行带 8 位短号（完整 id 的 hash 段前缀），用 memory.show 取正文。
        m_short = re.search(r"id:([0-9a-f]{6,12})", text)
        assert m_short, text
        sid = m_short.group(1)
        got = memory.fetch(sid)          # 唯一前缀解析
        assert got.get("ok"), f"短 id {sid} 取不回正文"
        assert "细节" in str((got.get("memory") or {}).get("text") or ""), got
        # 再兜一道：正文应该还能被 recall 找回（信息没丢）
        res = memory.recall("指针可解引用验证", k=3)
        texts = " ".join(str((m or {}).get("text") or "") for m, _s, _p in res)
        assert "细节" in texts, texts[:200]


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
