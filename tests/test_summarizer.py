# -*- coding: utf-8 -*-
"""附录 C P2：会话摘要层（scripts/summarizer.py）回归。

契约：
  - 没配置模型 / 模型失败 / 模型输出不合格 → 一律回退离线抽取式（fail-open），不抛异常；
  - 提交前校验：五段小标题齐全、不超长、不回显提示词；
  - 「原话引用」保留用户纠正句，作为漂移锚；
  - prompt 带上上一版摘要。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import summarizer  # noqa: E402

TURNS = [
    {"role": "user", "text": "帮我把灵枢的注入改懒加载，不要动记忆正文"},
    {"role": "assistant", "text": "好，我加一个 inject_mode 开关，默认 full。"},
    {"role": "user", "text": "不对，默认应该是 full，但预算紧张要自动降级"},
    {"role": "assistant", "text": "已实现自动降级，并补了 5 个测试。"},
]

VALID = """## 目标
- 给灵枢加惰性注入
## 已做
- 加了 inject_mode 开关
## 未决
- 还要做 A/B
## 约束
- 不要动记忆正文
## 原话引用
- 不对，默认应该是 full，但预算紧张要自动降级"""


def test_fallback_has_five_sections_and_passes_validation():
    text = summarizer.extractive_fallback(TURNS)
    ok, why = summarizer.validate(text)
    assert ok, why
    for h in summarizer.HEADERS:
        assert f"## {h}" in text
    assert "预算" in text  # 关键约束被抽到


def test_validate_rejects_missing_header_and_overlong_and_prompt_echo():
    ok, why = summarizer.validate("## 目标\nx")
    assert not ok and "缺小标题" in why
    ok, why = summarizer.validate(VALID, max_chars=10)
    assert not ok and "超长" in why
    ok, why = summarizer.validate(VALID + "\n规则：\n上一版摘要")
    assert not ok


def test_summarize_falls_back_when_disabled():
    res = summarizer.summarize(TURNS, cfg={"summary": {"enabled": False}})
    assert res["source"] == "fallback"
    assert summarizer.validate(res["text"])[0]
    assert "预算" in res["text"]


def test_summarize_uses_model_when_valid():
    res = summarizer.summarize(TURNS, cfg={"summary": {"enabled": True, "model": "x"}},
                               call=lambda prompt: VALID)
    assert res["source"] == "model"
    assert res["text"] == VALID


def test_summarize_falls_back_when_model_fails_or_invalid():
    bad = summarizer.summarize(TURNS, cfg={"summary": {"enabled": True}},
                               call=lambda p: None)
    assert bad["source"] == "fallback" and bad["warnings"]

    garbage = summarizer.summarize(TURNS, cfg={"summary": {"enabled": True}},
                                   call=lambda p: "我觉得这样挺好")
    assert garbage["source"] == "fallback"
    assert any("不合格" in w for w in garbage["warnings"])

    boom = summarizer.summarize(TURNS, cfg={"summary": {"enabled": True}},
                                call=lambda p: (_ for _ in ()).throw(RuntimeError("timeout")))
    assert boom["source"] == "fallback"


def test_prompt_carries_prev_summary_and_normalizes_roles():
    prompt = summarizer.build_prompt(TURNS, prev_summary="上一版：目标 A")
    assert "上一版摘要" in prompt and "目标 A" in prompt
    turns = summarizer.normalize_turns([
        {"role": "用户", "content": [{"type": "text", "text": "你说的对"}]},
        {"sender": "ai", "message": "收到"},
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "你说的对"


def test_cli_json_roundtrip():
    import subprocess
    py = sys.executable
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        inp = Path(td) / "turns.jsonl"
        inp.write_text("\n".join(json.dumps({"role": t["role"], "content": t["text"]},
                                            ensure_ascii=False) for t in TURNS),
                       encoding="utf-8")
        r = subprocess.run([py, str(ROOT / "scripts" / "brain.py"), "summarize",
                            "--input", str(inp), "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["source"] in ("fallback", "model")
        assert summarizer.validate(data["text"])[0]


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
