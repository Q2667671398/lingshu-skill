# -*- coding: utf-8 -*-
"""附录 B · P1：DSH 会话导入（多帧 zstd → session_rag）+ 工作区/平台过滤。

覆盖：真 Node 造多帧 zstd 会话 → 导入 → 检索命中（含 role/workspace/platform/ts 字段）、
幂等、search 的 workspace/platform 过滤、stats 分组、无 Node/zstd 时明确报错、
CLI 的 --dry-run/--workspace/--limit/--json 语义。

隔离：每个用例独立临时 HOME（core.HOME），不碰真实 ~/.brain-memory；
会话文件全部落在临时 sessions 目录，不读真实 ~/.dsh/sessions。
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401  （命名空间合并：ensure_home 需要 compile_persona）
import core  # noqa: E402
import session_import as imp  # noqa: E402
import session_rag as rag  # noqa: E402

# 用 Node 把每行 JSON 压成独立 zstd 帧（与 DSH 落盘格式一致：多帧拼接 + checksum）。
# 注意：落成临时 .js 文件再执行，而不是 `node -e "多行脚本"` —— 本机 PATH 上的
# ServBay Node v23.11 对含换行的 -e 脚本只执行第一行（DSH 自带 v24 不会），
# 写文件这种调用方式对所有版本都稳。
_NODE_WRITE_JS = r"""
const fs = require('node:fs')
const zlib = require('node:zlib')
const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const frames = payload.lines.map((line) => zlib.zstdCompressSync(
  Buffer.from(line + '\n', 'utf8'),
  { params: { [zlib.constants.ZSTD_c_checksumFlag]: 1 } },
))
fs.writeFileSync(payload.path, Buffer.concat(frames))
process.stdout.write(String(frames.length))
"""


def _home(td: Path) -> Path:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    return td


def _node_or_skip() -> str | None:
    """本机没有可解 zstd 的 Node 时，跳过需要造 zstd 文件的用例。"""
    node = imp.find_node_exe()
    if not node:
        print("SKIP: 没有可用的 Node 22+（node:zlib.zstdDecompressSync 探测失败），"
              "跳过真 zstd 会话导入用例")
    return node


def _write_dsh_session(sessions_root: Path, project_dir: str, session_dir: str,
                       lines: list[dict], node: str) -> Path:
    """造 <sessions>/<工作区目录>/<会话目录>/session.v4.jsonl.zstd。"""
    target = sessions_root / project_dir / session_dir
    target.mkdir(parents=True, exist_ok=True)
    out = target / "session.v4.jsonl.zstd"
    payload = target / "_payload.json"
    writer = target / "_writer.js"
    payload.write_text(json.dumps({
        "path": str(out),
        "lines": [json.dumps(item, ensure_ascii=False) for item in lines],
    }, ensure_ascii=False), encoding="utf-8")
    writer.write_text(_NODE_WRITE_JS, encoding="utf-8")
    try:
        rep = subprocess.run([node, str(writer), str(payload)],
                             capture_output=True, timeout=120)
    finally:
        payload.unlink(missing_ok=True)
        writer.unlink(missing_ok=True)
    assert rep.returncode == 0, rep.stderr.decode("utf-8", "replace")
    assert out.is_file() and out.stat().st_size > 0
    return out


def _events(session_id: str, cwd: str) -> list[dict]:
    """一场典型 DSH 会话：会话 header + 真对话 + 工具/推理/系统注入噪声。"""
    return [
        {"type": "session", "version": 4, "id": session_id,
         "createdAt": 1795105587187, "cwd": cwd, "isSeeded": False,
         "delegationDepth": 0},
        {"type": "user/message", "seq": 2, "time": 1795105587277,
         "data": {"content": [{"type": "text", "text": "上次说的缓存方案要先落盘再回填"},
                              {"type": "image", "mime": "image/png"}],
                  "source": {"kind": "user"}, "role": "user", "id": "u1"}},
        {"type": "tool/call", "seq": 3, "time": 1795105587300,
         "data": {"id": "c1", "name": "pwsh", "arguments": "{}"}},
        {"type": "assistant/message", "seq": 4, "time": 1795105587400,
         "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
             {"type": "reasoning", "text": "这段推理不该入库"},
             {"type": "text", "text": "对，用 WAL 模式，先落盘再回填"},
             {"type": "tool-call", "id": "c2", "name": "read",
              "arguments": '{"file_path":"a.py"}'}]}}},
        {"type": "tool/result", "seq": 5, "time": 1795105587500,
         "data": {"content": [{"type": "text", "text": "工具结果不该入库"}]}},
        {"type": "assistant/message", "seq": 6, "time": 1795105587600,
         "data": {"turn": 1, "step": 2, "message": {"role": "assistant", "content": [
             {"type": "reasoning", "text": "只有推理没有正文的轮次"},
             {"type": "tool-call", "id": "c3", "name": "read", "arguments": "{}"}]}}},
        {"type": "user/message", "seq": 7, "time": 1795105587700,
         "data": {"content": [{"type": "text",
                               "text": "<system-reminder>注入块不该入库</system-reminder>"}],
                  "source": {"kind": "runtime-context", "form": "snapshot"},
                  "role": "user", "id": "u2"}},
        {"type": "system/message", "seq": 8, "time": 1795105587800,
         "data": {"content": [{"type": "text", "text": "系统事件不该入库"}]}},
        {"type": "command/run", "seq": 9, "time": 1795105587900,
         "data": {"command": "ls", "output": "命令输出不该入库"}},
    ]


def test_import_zstd_session_and_search():
    """Node 造真多帧 zstd → 导入 → 检索命中；只导入对话文本（工具/推理/注入不入库）。"""
    node = _node_or_skip()
    if not node:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td / "home")
        sessions = td / "sessions"
        cwd = "E:\\工作区\\default-workspace"
        _write_dsh_session(sessions, "--E-~5DE5~4F5C~533A--default-workspace--",
                           "19ea91ef-37e8-443d-9f17-2abf4716b00f",
                           _events("19ea91ef-37e8-443d-9f17-2abf4716b00f", cwd), node)

        rep = imp.import_sessions(sessions)
        assert rep["ok"] and rep["errors"] == 0, rep
        assert rep["decoder"]["backend"] == "node", rep["decoder"]
        totals = rep["totals"]
        assert totals["files"] == 1 and totals["sessions"] == 1, totals
        assert totals["messages"] == 2 and totals["added"] == 2, totals
        assert totals["bad_lines"] == 0 and totals["torn_bytes"] == 0, totals
        one = rep["files"][0]
        assert one["session"] == "19ea91ef-37e8-443d-9f17-2abf4716b00f", one
        assert one["workspace"] == cwd, one
        assert one["decode"]["frames"] == len(_events("x", cwd)), one["decode"]

        # 用户原话与 assistant 正文都能检索到，且 platform/workspace/session 字段正确
        hits = rag.search("缓存方案", k=5)
        assert hits and "缓存方案" in hits[0]["text"], hits
        assert hits[0]["session"] == "19ea91ef-37e8-443d-9f17-2abf4716b00f", hits[0]
        assert hits[0]["workspace"] == cwd and hits[0]["platform"] == "dsh", hits[0]
        assert hits[0]["role"] == "user", hits[0]
        # ms → 秒：DSH event.time 是毫秒时间戳
        assert abs(hits[0]["ts"] - 1795105587.277) < 0.01, hits[0]["ts"]
        assert any("WAL" in h["text"] for h in rag.search("WAL", k=5))
        # 非对话条目一律不入库：推理 / 工具结果 / 系统注入 / 命令输出
        for noise in ("这段推理不该入库", "工具结果不该入库", "注入块不该入库", "系统事件不该入库"):
            assert rag.search(noise, k=5) == [], (noise, rag.search(noise, k=5))
        assert rag.search("只有推理没有正文的轮次", k=5) == []


def test_import_is_idempotent():
    """同一份会话重复导入：内容哈希幂等，第二次 added=0、skipped 等于首次数。"""
    node = _node_or_skip()
    if not node:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td / "home")
        sessions = td / "sessions"
        _write_dsh_session(sessions, "--ws-a--", "sess-aaa",
                           _events("sess-aaa", "E:\\ws-a"), node)

        first = imp.import_sessions(sessions)
        assert first["totals"]["added"] == 2 and first["totals"]["skipped"] == 0, first["totals"]
        second = imp.import_sessions(sessions)
        assert second["totals"]["added"] == 0, second["totals"]
        assert second["totals"]["skipped"] == 2, second["totals"]
        st = rag.stats()
        assert st["messages"] == 2 and st["sessions"] == 1, st
        assert st["by_platform"] == {"dsh": 2}, st
        assert st["by_workspace"] == {"E:\\ws-a": 2}, st


def test_search_workspace_and_platform_filters():
    """search(..., workspace=/platform=) 精确过滤；不传时保持旧行为（全库）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        ws_a, ws_b = "E:\\ws-a", "E:\\ws-b"
        assert rag.ingest_messages([
            {"role": "user", "content": "工作区甲的共同话题：缓存方案先落盘", "ts": 1},
            {"role": "assistant", "content": "工作区甲的回答：共同话题用 WAL", "ts": 2},
        ], session="s-a", platform="dsh", workspace=ws_a)["added"] == 2
        assert rag.ingest_messages([
            {"role": "user", "content": "工作区乙的共同话题：另一个缓存方案", "ts": 3},
        ], session="s-b", platform="dsh", workspace=ws_b)["added"] == 1
        assert rag.ingest_messages([
            {"role": "user", "content": "另一平台的共同话题：向量检索", "ts": 4},
        ], session="s-c", platform="trae", workspace=ws_a)["added"] == 1

        # 不传过滤 = 旧行为：全库 4 条；两字 LIKE 分支同样受过滤影响
        assert len(rag.search("共同话题", k=10)) == 4
        assert len(rag.search("缓存", k=10)) == 2
        # workspace 过滤（FTS 分支）
        only_a = rag.search("共同话题", k=10, workspace=ws_a)
        assert len(only_a) == 3 and {r["workspace"] for r in only_a} == {ws_a}, only_a
        # platform 过滤
        only_dsh = rag.search("共同话题", k=10, platform="dsh")
        assert len(only_dsh) == 3 and {r["platform"] for r in only_dsh} == {"dsh"}, only_dsh
        # 两个条件同时生效
        combo = rag.search("共同话题", k=10, workspace=ws_a, platform="dsh")
        assert len(combo) == 2 and {r["session"] for r in combo} == {"s-a"}, combo
        # 短查询走 LIKE 兜底时过滤也生效
        assert len(rag.search("缓存", k=10, platform="dsh")) == 2
        assert len(rag.search("缓存", k=10, workspace=ws_b)) == 1
        # 过滤值没命中 → 空结果，不是报错
        assert rag.search("共同话题", k=10, workspace=r"E:\不存在") == []
        assert rag.search("共同话题", k=10, platform="claude") == []


def test_stats_groups_by_platform_and_workspace():
    """stats() 保留 messages/sessions，并新增 by_platform/by_workspace 分组计数。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        assert rag.stats()["messages"] == 0
        assert rag.stats()["by_platform"] == {} and rag.stats()["by_workspace"] == {}
        ws_a, ws_b = "E:\\ws-a", "E:\\ws-b"
        rag.ingest_messages([
            {"role": "user", "content": "甲一", "ts": 1},
            {"role": "assistant", "content": "甲二", "ts": 2},
        ], session="s-a", platform="dsh", workspace=ws_a)
        rag.ingest_messages([
            {"role": "user", "content": "乙一", "ts": 3},
        ], session="s-b", platform="dsh", workspace=ws_b)
        rag.ingest_messages([
            {"role": "user", "content": "甲三", "ts": 4},
        ], session="s-c", platform="trae", workspace=ws_a)

        st = rag.stats()
        assert st["messages"] == 4 and st["sessions"] == 3, st
        assert st["by_platform"] == {"dsh": 3, "trae": 1}, st
        assert st["by_workspace"] == {ws_a: 3, ws_b: 1}, st
        assert "path" in st, st


def test_decoder_unavailable_raises_clear_error():
    """无 Node 22+ 且无 zstd 命令时：明确报错（monkeypatch），CLI 返回 2，不静默。"""
    orig_node, orig_zstd = imp.find_node_exe, imp.find_zstd_exe
    imp.find_node_exe = lambda: None      # type: ignore[assignment]
    imp.find_zstd_exe = lambda: None      # type: ignore[assignment]
    try:
        try:
            imp.detect_decoder()
            raise AssertionError("没有解码器时必须抛 DecoderUnavailable")
        except imp.DecoderUnavailable as exc:
            msg = str(exc)
            assert "Node 22+" in msg, msg
            assert "zstd" in msg, msg
            assert "需要" in msg, msg

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = imp.main(["--sessions-dir", str(sessions), "--json"])
            assert code == 2, code
            assert "需要 Node 22+ 或 zstd 命令" in err.getvalue(), err.getvalue()
    finally:
        imp.find_node_exe, imp.find_zstd_exe = orig_node, orig_zstd


def test_cli_dry_run_workspace_filter_and_limit():
    """CLI 语义：--dry-run 不写库；--workspace 只导入匹配工作区；--limit 限制会话数。"""
    node = _node_or_skip()
    if not node:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td / "home")
        sessions = td / "sessions"
        alpha = "E:\\alpha"
        beta = "E:\\beta"
        _write_dsh_session(sessions, "--E-alpha--", "sess-alpha-1",
                           _events("sess-alpha-1", alpha), node)
        _write_dsh_session(sessions, "--E-beta--", "sess-beta-1",
                           _events("sess-beta-1", beta), node)

        # --dry-run --json：匹配 1 个会话、解析 2 条、未写库
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = imp.main(["--sessions-dir", str(sessions), "--workspace", "alpha",
                             "--dry-run", "--json"])
        assert code == 0, code
        report = json.loads(buf.getvalue())
        assert report["dry_run"] is True, report
        assert report["totals"]["files"] == 1, report["totals"]
        assert report["totals"]["messages"] == 2, report["totals"]
        assert report["totals"]["added"] == 0, report["totals"]
        matched = [f for f in report["files"] if f.get("workspace_matched")]
        assert len(matched) == 1 and matched[0]["workspace"] == alpha, report["files"]
        assert "db" not in report, report
        assert rag.stats()["messages"] == 0, "dry-run 不能写库"

        # --limit 1：只取最近 1 个会话文件
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = imp.main(["--sessions-dir", str(sessions), "--limit", "1",
                             "--dry-run", "--json"])
        assert code == 0, code
        assert json.loads(buf.getvalue())["totals"]["files"] == 1

        # 真导入只挑 alpha 工作区：beta 不入库
        rep = imp.import_sessions(sessions, workspace="alpha")
        assert rep["totals"]["files"] == 1 and rep["totals"]["added"] == 2, rep["totals"]
        st = rag.stats()
        assert st["messages"] == 2 and st["by_workspace"] == {alpha: 2}, st
        assert rag.search("缓存方案", k=5, workspace=alpha), rag.search("缓存方案", k=5)
        assert rag.search("缓存方案", k=5, workspace=beta) == []

        # 再全量导入（beta 补上），已导入的 alpha 仍幂等
        rep2 = imp.import_sessions(sessions)
        assert rep2["totals"]["files"] == 2, rep2["totals"]
        assert rep2["totals"]["added"] == 2, rep2["totals"]
        assert rep2["totals"]["skipped"] == 2, rep2["totals"]
        st2 = rag.stats()
        assert st2["messages"] == 4 and set(st2["by_workspace"]) == {alpha, beta}, st2


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
