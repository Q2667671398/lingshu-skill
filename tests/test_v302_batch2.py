# -*- coding: utf-8 -*-
"""v3.02 第二批单测：#4 日志轮转、#5 metrics.jsonl、#8 export --md、
#10 事实保质期 --ttl/--review-at、#11 情绪候选、#14 doctor 版本矩阵。

不依赖固定 HOME：测试前把 core.HOME 重定向到临时目录，测完还原。
运行：python -m pytest tests/test_v302_batch2.py 或 python tests/test_v302_batch2.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import core  # noqa: E402
import memory  # noqa: E402
import emotion  # noqa: E402


def _setup_home(tmp: Path) -> None:
    core.HOME = tmp
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()


# ---------------------------------------------------------------- #4 日志轮转

def test_log_rotation_creates_dot1():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        # 塞满 >1MB 的旧日志，再写一条触发轮转
        core.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        core.LOG_FILE.write_text("x" * (1024 * 1024 + 100), encoding="utf-8")
        core.log_event("test", "触发轮转的一条")
        assert core.LOG_FILE.with_suffix(core.LOG_FILE.suffix + ".1").exists()
        # 新日志从零开始，且包含新事件
        assert core.LOG_FILE.stat().st_size < 1024 * 1024


def test_log_rotation_chain_capped_at_3():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        core.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            core.LOG_FILE.write_text("x" * (1024 * 1024 + 100), encoding="utf-8")
            core.log_event("test", f"第{i}次轮转")
        sfx = core.LOG_FILE.suffix
        assert core.LOG_FILE.with_suffix(sfx + ".1").exists()
        assert core.LOG_FILE.with_suffix(sfx + ".2").exists()
        assert core.LOG_FILE.with_suffix(sfx + ".3").exists()
        assert not core.LOG_FILE.with_suffix(sfx + ".4").exists()  # 最旧的被丢弃


def test_read_logs_merges_rotated():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        core.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 新文件 1 条，.1 两条（旧的）
        core.LOG_FILE.write_text(json.dumps({"kind": "t", "level": "info", "msg": "new"}, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        core.LOG_FILE.with_suffix(core.LOG_FILE.suffix + ".1").write_text(
            json.dumps({"kind": "t", "level": "info", "msg": "old1"}, ensure_ascii=False) + "\n" +
            json.dumps({"kind": "t", "level": "info", "msg": "old2"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        rows = core.read_logs(limit=10)
        msgs = [r["msg"] for r in rows]
        assert "new" in msgs and "old1" in msgs and "old2" in msgs
        # 最新在前
        assert msgs[0] == "new"


# ---------------------------------------------------------------- #5 metrics.jsonl

def test_append_jsonl_basic():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        p = Path(td) / "metrics.jsonl"
        core._append_jsonl(p, {"a": 1})
        core._append_jsonl(p, {"a": 2})
        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert [r["a"] for r in lines] == [1, 2]


def test_stats_has_metrics_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        memory.remember("用户喜欢美式", "preference")
        s = memory.stats()
        # metrics.jsonl 快照要用到的字段都存在
        for k in ("total", "hippo", "cortex", "dust", "archive",
                  "never_recalled_pct", "avg_retention", "assoc", "working",
                  "recall_usage"):
            assert k in s, f"stats() 缺字段 {k}"


# ---------------------------------------------------------------- #8 export --md

def _run_cli(*argv):
    """直接走 cli._main（含 parser），捕获 stdout。"""
    import io, contextlib
    import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._main(list(argv))
    return rc, buf.getvalue()


def test_export_md_prints_memories():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        memory.remember("用户喜欢美式咖啡", "preference", tags=["咖啡"])
        rc, out = _run_cli("export")
        assert rc == 0
        assert "灵枢记忆库导出" in out
        assert "美式咖啡" in out
        assert "长期（新皮层）" in out or "短期（海马体）" in out


def test_export_md_to_file_and_layer_filter():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        memory.remember("短期的一条", "episode")           # hippo
        memory.remember("长期的一条", "fact", layer="cortex")
        outp = Path(td) / "exp.md"
        rc, _ = _run_cli("export", "--out", str(outp))
        assert rc == 0 and outp.exists()
        text = outp.read_text(encoding="utf-8")
        assert "灵枢记忆库导出" in text
        # 只导 cortex 层：hippo 那条不出现
        rc, out = _run_cli("export", "--layer", "cortex")
        assert rc == 0
        assert "长期的一条" in out
        assert "短期的一条" not in out


# ---------------------------------------------------------------- #10 保质期

def test_remember_with_ttl():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rec, _ = memory.remember("用户当前用的是 Windows 11", "fact", ttl_days=30)
        assert rec.get("ttl_days") == 30
        assert rec.get("expires_at", 0) > core.now_ts()
        assert rec.get("review_by") == rec.get("expires_at")


def test_remember_with_review_at():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rec, _ = memory.remember("某个临时约定", "fact", review_at=core.now_ts() + 3600)
        assert rec.get("review_by", 0) > core.now_ts()


def test_due_for_review_only_expired():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        fresh, _ = memory.remember("还没到期的", "fact", ttl_days=30)
        expired, _ = memory.remember("早已到期的", "fact", ttl_days=0.001)
        # 手动把 expired 的 review_by 拨到过去
        expired["review_by"] = core.now_ts() - 100
        core._patch_memory_fields(expired)
        due = memory.due_for_review()
        ids = [m["id"] for m in due]
        assert expired["id"] in ids
        assert fresh["id"] not in ids


def test_mark_reviewed_silences_and_extends():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rec, _ = memory.remember("待复核的事实", "fact", ttl_days=7)
        rec["review_by"] = core.now_ts() - 100
        core._patch_memory_fields(rec)
        assert rec["id"] in [m["id"] for m in memory.due_for_review()]
        m = memory.mark_reviewed(rec["id"])
        assert m.get("review_by_confirmed")
        # 确认后顺延一个周期：review_by 回到未来
        assert m["review_by"] > core.now_ts()
        assert m["id"] not in [x["id"] for x in memory.due_for_review()]


def test_mark_reviewed_unknown_id():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        try:
            memory.mark_reviewed("no-such-id")
            assert False, "应当抛 LookupError"
        except LookupError:
            pass


# ---------------------------------------------------------------- #11 情绪候选

def test_emotion_candidate_negative():
    c = emotion.emotion_auto_candidate("烦死了，这个 bug 搞了一下午还没好")
    assert c and c["decision"] == "candidate"
    assert c["emotion"] in emotion.EMOTIONS
    assert 0 < c["intensity"] <= 1


def test_emotion_candidate_positive():
    c = emotion.emotion_auto_candidate("太好了！帮大忙了，谢谢！")
    assert c and c["emotion"] in emotion.EMOTIONS


def test_emotion_candidate_none_for_plain_text():
    assert emotion.emotion_auto_candidate("把 config.json 改一下") is None
    assert emotion.emotion_auto_candidate("") is None


def test_emotion_candidate_mixed_signals_returns_none():
    # 正负混着说不猜
    assert emotion.emotion_auto_candidate("烦死了，不过还是谢谢你了") is None


def test_emotion_candidate_rejects_long_paste():
    assert emotion.emotion_auto_candidate("x" * 600) is None


# ---------------------------------------------------------------- #14 版本矩阵

def test_version_matrix_consistent():
    import re
    v_skill = re.search(r'version["\']?\s*[:=]\s*["\']?([0-9][0-9a-zA-Z.\-]*)',
                        (ROOT / "SKILL.md").read_text(encoding="utf-8-sig")).group(1)
    v_pkg = re.search(r'"version"\s*:\s*"([^"]+)"',
                      (ROOT / "package.json").read_text(encoding="utf-8-sig")).group(1)
    v_meta = re.search(r'version:\s*([0-9][0-9a-zA-Z.\-]*)',
                       (ROOT / "scripts/astrbot_plugin_lingshu/metadata.yaml")
                       .read_text(encoding="utf-8-sig")).group(1)
    # SKILL.md 与 package.json 对齐主.次版本号（patch 可不同）；metadata.yaml 是
    # AstrBot 插件自己独立的 0.2.x 版本线，不与主版本同轨，只要求版本号存在。
    def _majorminor(v):
        m = re.match(r"(\d+)\.(\d+)", v)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    assert _majorminor(v_skill) == _majorminor(v_pkg), \
        f"主版本漂移：SKILL.md={v_skill} package.json={v_pkg}"
    assert v_meta, "metadata.yaml 缺少 version 字段"


def test_doctor_runs_with_version_matrix():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        rc, out = _run_cli("doctor")
        assert rc == 0
        assert "version-matrix" in out


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
