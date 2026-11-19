# -*- coding: utf-8 -*-
"""v3.36 面板增强：运行洞察 API + 前端增强块的存在性/安全性。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401
import core  # noqa: E402
import insights  # noqa: E402
import panel_server  # noqa: E402
import persona  # noqa: E402


def _home(td: Path) -> None:
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    cfg = core.load_config()
    cfg["embed"]["url"] = ""
    cfg["embed"]["fallback"]["enabled"] = False
    core.atomic_json(td / "config.json", cfg)
    persona.load_persona()


def test_build_returns_all_sections():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        rep = insights.build()
        assert rep["ok"] and "ts" in rep
        d = rep["data"]
        assert d["version"], d
        for key in ("context", "rag", "checkpoints", "guard", "embed"):
            assert key in d, (key, d.keys())
        # 全空库也不能因为某一块失败而整体失败
        assert rep["warnings"] == [] or all(":" in w for w in rep["warnings"])


def test_light_mode_skips_context_billing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        d = insights.build(light=True)["data"]
        assert "context" not in d
        assert d["version"]


def test_http_insights_requires_token_and_returns_json():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _home(Path(td))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), panel_server.Handler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{port}/api/insights?light=1"
            # 无 token → 403
            try:
                urllib.request.urlopen(url, timeout=10)
                raise AssertionError("无 token 应该 403")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            # 有 token → 200 + JSON
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + panel_server.TOKEN})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            assert data["ok"] is True and data["data"]["version"]
            assert "context" not in data["data"]      # light=1
        finally:
            srv.shutdown()
            srv.server_close()


def test_panel_html_has_enhancement_markers():
    t = (ROOT / "assets" / "panel.html").read_text(encoding="utf-8")
    assert "<!-- ls-enhance v3.36 -->" in t
    assert "/api/insights" in t
    assert 'data-theme' in t
    assert 'localStorage' in t
    # 增强块不允许用 innerHTML 塞服务端数据（我们自己只拼静态骨架）
    idx = t.find("<!-- ls-enhance v3.36 -->")
    block = t[idx:]
    script = block[block.find("<script>"):block.find("</script>") + 9]
    assert "innerHTML" in script            # 只用于静态骨架
    assert "textContent" in script          # 数据一律 textContent


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
