# -*- coding: utf-8 -*-
"""附录 C P3：检索接入（retriever）——服务优先、本地回退、二段式挑选。"""
from __future__ import annotations

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
import retriever  # noqa: E402


def _home(td: Path, **rag) -> dict:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = False
    cfg["session_rag"] = {**(cfg.get("session_rag") or {}), **rag}
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()
    return cfg


def test_search_falls_back_to_lingshu_without_service():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td), url="")
        memory.remember("用户在用灵枢做跨会话记忆", kind="fact", importance=0.8)
        res = retriever.search("灵枢 跨会话")
        assert res["source"] == "lingshu"
        assert any("灵枢" in r["text"] for r in res["results"])


def test_search_prefers_service_when_configured():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td), url="http://127.0.0.1:8791")
        memory.remember("本地不该被用到", kind="fact")
        res = retriever.search("缓存", call=lambda q, k: [
            {"id": "srv-1", "text": "服务返回的缓存经验", "score": 0.9, "source": "dsh"},
            {"id": "srv-2", "text": "服务返回的第二条", "score": 0.5},
        ])
        assert res["source"] == "session-rag"
        assert res["results"][0]["id"] == "srv-1"
        assert res["warnings"] == []


def test_search_warns_and_falls_back_when_service_fails():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td), url="http://127.0.0.1:9")
        memory.remember("服务挂了也要能检索", kind="fact")
        res = retriever.search("检索", call=lambda q, k: (_ for _ in ()).throw(RuntimeError("down")))
        assert res["source"] == "lingshu"
        assert any("回退" in w for w in res["warnings"])


def test_select_heuristic_respects_budget():
    cands = [{"id": f"c{i}", "text": "字" * 400, "score": 1.0 - i * 0.1} for i in range(10)]
    res = retriever.select(cands, "查询", budget_tokens=600)
    assert res["source"] == "heuristic"
    assert res["tokens"] <= 600
    assert [x["id"] for x in res["kept"]] == ["c0", "c1", "c2"], res["kept"]


def test_select_model_picks_ids_and_falls_back_on_garbage():
    cands = [{"id": "a1", "text": "甲", "score": 1}, {"id": "b2", "text": "乙", "score": 0.5}]
    ok = retriever.select(cands, "甲", call=lambda p: '{"keep":["a1"]}')
    assert ok["source"] == "model" and [x["id"] for x in ok["kept"]] == ["a1"]

    bad = retriever.select(cands, "甲", call=lambda p: "我觉得都行")
    assert bad["source"] == "heuristic" and bad["kept"]
    assert bad["warnings"]

    none = retriever.select(cands, "甲", call=lambda p: '{"keep":["不存在"]}')
    assert none["source"] == "heuristic"


def test_build_snippet_has_source_tags_and_empty_when_nothing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td), url="")
        memory.remember("拼片段时带上来源标记", kind="fact", importance=0.8)
        text = retriever.build_snippet("来源标记", k=5)
        assert "## 检索到的相关片段" in text
        assert "[lingshu:" in text
        assert retriever.build_snippet("完全不存在的词xyz", k=3) == ""


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
