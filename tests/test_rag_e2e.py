# -*- coding: utf-8 -*-
"""附录 B P0 收尾：检索服务端到端打通（真 HTTP，不 mock）。

链路：session_rag.init/ingest → session_rag.serve（真 ThreadingHTTPServer，随机端口）
      → retriever.search 走 POST /v1/search → 服务关掉后回退 memory.recall + failures 记账。

不起固定 8791 端口：`serve(port=0)` 交给内核分配，测试之间不抢端口。
`session_rag.serve` 内部自己 new server 并 serve_forever，外部拿不到实例，
所以这里临时把 `http.server.ThreadingHTTPServer` 换成「记录实例」的子类，
拿到 server 后才能在用例结束时 shutdown/server_close（否则线程压着端口到进程退出）。
"""
from __future__ import annotations

import contextlib
import http.server
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import brain  # noqa: E402,F401
import core  # noqa: E402
import failures  # noqa: E402
import memory  # noqa: E402
import persona  # noqa: E402
import retriever  # noqa: E402
import session_rag as rag  # noqa: E402


def _home(td: Path, *, url: str = "", timeout: float = 1.0) -> None:
    """隔离 HOME（同 tests/test_retriever.py 的写法），关掉 embedding 免联网。"""
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    cfg["session_state_cache"] = False
    cfg["session_rag"] = {**(cfg.get("session_rag") or {}), "url": url, "timeout": timeout}
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()


def _set_rag_url(td: Path, url: str, *, timeout: float = 1.0) -> None:
    cfg = core.load_config()
    cfg["session_rag"] = {**(cfg.get("session_rag") or {}), "url": url, "timeout": timeout}
    core.atomic_json(td / "config.json", cfg)


def _wait_health(url: str, timeout: float = 15.0) -> None:
    """等服务线程真的在 accept：/health 回 ok 之前不发业务请求。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if json.loads(r.read() or b"{}").get("ok"):
                    return
        except Exception as exc:  # noqa: BLE001 - 未就绪就是继续等
            last = exc
        time.sleep(0.02)
    raise AssertionError(f"session-rag /health 一直没就绪: {last!r}")


@contextlib.contextmanager
def _rag_service():
    """真起 session_rag.serve（随机端口、daemon 线程），退出时干净关掉。"""
    created: list = []
    errors: list = []

    class _RecordingServer(http.server.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)  # serve() 里 new 出来的实例，外部靠它 shutdown

    def _run() -> None:
        try:
            rag.serve("127.0.0.1", 0)
        except BaseException as exc:  # noqa: BLE001 - 线程异常带回主线程一起断言
            errors.append(exc)

    orig = http.server.ThreadingHTTPServer
    http.server.ThreadingHTTPServer = _RecordingServer
    th = threading.Thread(target=_run, name="session-rag-e2e", daemon=True)
    th.start()
    try:
        deadline = time.time() + 15
        while not created and not errors and time.time() < deadline:
            time.sleep(0.02)
        assert created, f"session_rag.serve 没能在 15s 内起来: {errors!r}"
        base = f"http://127.0.0.1:{created[0].server_address[1]}"
        _wait_health(base + "/health")
        yield base
    finally:
        http.server.ThreadingHTTPServer = orig
        if created:
            try:
                created[0].shutdown()
            finally:
                created[0].server_close()
        th.join(timeout=10)


def test_http_search_returns_ingested_rows():
    """两条中文进原文库 → retriever 真走 HTTP 拿到候选（含 URL 尾斜杠拼接）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td)
        assert rag.ingest_messages([
            {"role": "user", "content": "上次说的缓存方案是先落盘再回填", "ts": 1},
            {"role": "assistant", "content": "对，用 WAL 模式", "ts": 2},
        ], session="e2e", platform="dsh")["added"] == 2

        with _rag_service() as base:
            direct = rag.search("缓存方案", 5)
            assert direct and "缓存方案" in direct[0]["text"], direct
            for url in (base, base + "/"):  # 尾斜杠不该拼成 //v1/search
                _set_rag_url(td, url)
                res = retriever.search("缓存方案", k=5)
                assert res["source"] == "session-rag", res
                assert res["warnings"] == [], res
                assert res["results"], res
                assert any("缓存方案" in r["text"] for r in res["results"]), res
                # 响应解析没丢字段：id/score/source 都还在，顺序与服务端一致
                assert [r["id"] for r in res["results"]] == [r["id"] for r in direct], (res, direct)
                assert all(r["id"].startswith("sr_") for r in res["results"]), res
                assert all(isinstance(r["score"], float) for r in res["results"]), res
                assert {r["source"] for r in res["results"]} == {"dsh"}, res
        # 服务在线且返回正常：不该有任何 retriever 失败记账（那意味着偷偷退回了本地）
        assert not [r for r in failures.recent(20, home=td)
                    if str(r.get("kind") or "").startswith("retriever.")], failures.recent(5, home=td)


def test_short_two_char_query_like_fallback_over_http():
    """FTS5 trigram 索引不了 2 字词元：短查询的 LIKE 兜底在 HTTP 路径也必须生效。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td)
        assert rag.ingest_messages([
            {"role": "user", "content": "上次说的云南旅行是去大理和沙溪", "ts": 1},
            {"role": "assistant", "content": "完全无关的另一条中文记录", "ts": 2},
        ], session="e2e-short")["added"] == 2

        query = "云南"
        assert len(query) < 3, "用例前提：2 字查询走 LIKE 兜底而非 FTS MATCH"
        with _rag_service() as base:
            _set_rag_url(td, base)
            res = retriever.search(query, k=5)
            assert res["source"] == "session-rag", res
            assert res["warnings"] == [], res
            assert any("云南" in r["text"] for r in res["results"]), res
            assert [r["id"] for r in res["results"]] == [r["id"] for r in rag.search(query, 5)]
            # 查不到 ≠ 服务失败：空结果仍算服务成功，不能误回退本地
            miss = retriever.search("西藏", k=5)
            assert miss["source"] == "session-rag" and miss["results"] == [], miss
        # 2 字查询走的是 LIKE 分支，不是 FTS MATCH 抛错被吞（后者会留 session_rag.search 记账）
        assert not [r for r in failures.recent(20, home=td)
                    if r.get("kind") == "session_rag.search"], failures.recent(5, home=td)


def test_service_stopped_falls_back_local_and_records_failure():
    """服务关掉（shutdown+close）后：回退 memory.recall，warning 与 failures 都要留痕。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        _home(td)
        with _rag_service() as base:
            _set_rag_url(td, base, timeout=1.0)
            rag.ingest_messages([
                {"role": "user", "content": "服务在的时候这条能查到", "ts": 1},
            ], session="e2e-down")
            online = retriever.search("服务在的时候", k=3)
            assert online["source"] == "session-rag", online

        # 出了 with：serve_forever 已退出、监听 socket 已关，端口不再应答
        memory.remember("服务关掉之后仍然要能从本地记忆里想起检索", kind="fact", importance=0.9)
        res = retriever.search("本地记忆", k=5)
        assert res["source"] == "lingshu", res
        assert res["warnings"] and any("回退" in w for w in res["warnings"]), res
        assert any("本地记忆" in r["text"] for r in res["results"]), res

        rows = failures.recent(20, home=td)
        hits = [r for r in rows if r.get("kind") == "retriever.session_rag"]
        assert hits, rows
        assert hits[0].get("level") == "warn", hits[0]
        url = str((hits[0].get("ctx") or {}).get("url") or "")
        assert url.startswith("http://127.0.0.1:"), hits[0]


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
