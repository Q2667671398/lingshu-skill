# -*- coding: utf-8 -*-
"""面板模块化 + 命令面板 + 懒渲染（v3.38）。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "panel_src"
OUT = ROOT / "assets" / "panel.html"


def test_panel_build_reproduces_committed_asset():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "panel_build.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    raw = OUT.read_text(encoding="utf-8")
    assert "<!-- ls-enhance" in raw
    assert "LSVirtual" in raw and "LSCommand" in raw
    assert "/api/insights" in raw


def test_sources_are_split_by_concern():
    meta = json.loads((SRC / "blocks.json").read_text(encoding="utf-8"))
    files = {m["file"] for m in meta}
    assert len(meta) == 9, meta
    for need in ("styles/base.css", "styles/views.css", "styles/enhance.css",
                 "js/app.js", "js/core.js", "js/panels.js", "js/enhance.js",
                 "js/virtual-list.js", "js/command-palette.js"):
        assert need in files, need
    for f in files:
        assert (SRC / f).is_file(), f


def test_command_palette_and_virtual_list_contract():
    pal = (SRC / "js" / "command-palette.js").read_text(encoding="utf-8")
    assert "metaKey" in pal and "ctrlKey" in pal
    assert "textContent" in pal            # 结果一律文本节点；innerHTML 只用于清空
    vl = (SRC / "js" / "virtual-list.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in vl and "chunk" in vl
    core = (SRC / "js" / "core.js").read_text(encoding="utf-8")
    assert "LSVirtual.render" in core and "> 120" in core


def test_new_js_syntax_with_node():
    node = shutil.which("node")
    if not node:
        return
    for name in ("virtual-list.js", "command-palette.js", "core.js"):
        r = subprocess.run([node, "--check", str(SRC / "js" / name)], capture_output=True)
        assert r.returncode == 0, (name, r.stderr[-300:])


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
