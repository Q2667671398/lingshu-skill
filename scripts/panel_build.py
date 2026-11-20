#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板构建：把 panel_src/ 拼成 assets/panel.html（无依赖、字节级可复现）。

为什么不直接手改 271KB 单文件：4900 行里 4 段 script / 3 段 style，改一处要全文搜，
review 也没法看。拆成源码后：
    panel_src/index.html        外壳（样式/脚本位置留 <!--@@BLOCK:n@@--> 占位）
    panel_src/styles/*.css      样式块（按出现顺序）
    panel_src/js/*.js           脚本块（按出现顺序）
    panel_src/blocks.json       每个占位的标签属性 + 文件名（保证重建字节一致）

用法：
    python scripts/panel_build.py --split   # 一次性：把现有 assets/panel.html 拆成源码
    python scripts/panel_build.py           # 从源码重建
    python scripts/panel_build.py --check   # 只比对：源码重建结果 == 现成文件
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "panel_src"
OUT = ROOT / "assets" / "panel.html"

BLOCK_RE = re.compile(rb"(<(style|script)\b[^>]*>)(.*?)(</\2>)", re.S)
NAMES = {
    "style": ["base.css", "views.css", "enhance.css"],
    "script": ["app.js", "core.js", "panels.js", "enhance.js"],
}


def split(out: Path = OUT, src: Path = SRC) -> int:
    raw = out.read_bytes()
    blocks = list(BLOCK_RE.finditer(raw))
    if not blocks:
        print("[panel] 没找到 <style>/<script> 块", file=sys.stderr)
        return 1
    src.mkdir(parents=True, exist_ok=True)
    (src / "styles").mkdir(exist_ok=True)
    (src / "js").mkdir(exist_ok=True)
    meta = []
    pos = 0
    shell = []
    counters = {"style": 0, "script": 0}
    for m in blocks:
        shell.append(raw[pos:m.start()])
        kind = m.group(2).decode()
        idx = counters[kind]
        counters[kind] += 1
        names = NAMES[kind]
        name = names[idx] if idx < len(names) else f"{kind}{idx}.{'css' if kind == 'style' else 'js'}"
        sub = "styles" if kind == "style" else "js"
        (src / sub / name).write_bytes(m.group(3))
        token = f"<!--@@BLOCK:{len(meta)}@@-->".encode()
        shell.append(token)
        meta.append({"kind": kind, "open": m.group(1).decode(), "file": f"{sub}/{name}"})
        pos = m.end()
    shell.append(raw[pos:])
    (src / "index.html").write_bytes(b"".join(shell))
    (src / "blocks.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[panel] 拆出 {len(meta)} 块 → panel_src/（{counters['style']} 样式 / {counters['script']} 脚本）")
    return 0


def _norm_bytes(data: bytes) -> bytes:
    """比较用规范形态：去 BOM + CRLF→LF。

    和 manifest / sync_engine / make_checksums 保持同一口径。否则在 Windows 上
    只要 git 按 core.autocrlf 把源码写成 CRLF、产物是 LF，`--check` 就会假红
    （实测踩过：git checkout 之后一次次报"不同步"，其实内容一模一样）。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n")


def build(src: Path = SRC, out: Path | None = OUT) -> bytes:
    """拼装产物；out=None 时只返回字节、不落盘（--check 用，检查不该改文件）。"""
    shell = (src / "index.html").read_bytes()
    meta = json.loads((src / "blocks.json").read_text(encoding="utf-8"))
    for i, blk in enumerate(meta):
        token = f"<!--@@BLOCK:{i}@@-->".encode()
        body = (src / blk["file"]).read_bytes()
        close = b"</style>" if blk["kind"] == "style" else b"</script>"
        shell = shell.replace(token, blk["open"].encode() + body + close, 1)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(shell)
    return shell


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--split" in argv:
        return split()
    if "--check" in argv:
        old = OUT.read_bytes() if OUT.exists() else b""
        new = build(out=None)          # 只读：检查不能顺手把产物重写掉
        if _norm_bytes(old) != _norm_bytes(new):
            print("[panel] 不同步：assets/panel.html != panel_src 重建结果"
                  "（改了源码就跑 python scripts/panel_build.py）", file=sys.stderr)
            return 1
        print(f"[panel] OK：{len(new)} 字节，源码与产物一致")
        return 0
    data = build()
    print(f"[panel] 已生成 assets/panel.html：{len(data)} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
