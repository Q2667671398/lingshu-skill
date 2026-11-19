# -*- coding: utf-8 -*-
"""测试：SHA256 规范化哈希的跨 64KB 块边界 CRLF 归一化"""
import atexit, hashlib, os, shutil, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

# 隔离要求：growth / brain 在 import 时就把 HOME 与全局技能目录求值了，
# 必须先把 BRAIN_MEMORY_HOME / LINGSHU_GLOBAL_SKILLS 指到临时目录再 import，
# 否则 apply_update 之类的写路径会摸到真实的 ~/.brain-memory（历史上就是这类
# 泄漏让同一用例在不同机器上时绿时红）。
_TMP_HOME = Path(tempfile.mkdtemp(prefix="ls-sha-home-"))
os.environ["BRAIN_MEMORY_HOME"] = str(_TMP_HOME)
os.environ.setdefault("LINGSHU_GLOBAL_SKILLS", str(_TMP_HOME / "skills"))
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

import growth
import make_manifest as mm

failures = []

# 构造一个 CRLF 恰好跨 65536 边界的文件：LF*32767 后，65535-65536 字节处放 \r\n
half = 32767  # 每行 "a\n" 2 字节，32767 行 = 65534 字节
body = b"a\n" * half + b"\r\n" + b"b\n" * 100
assert body[65534:65536] == b"\r\n" and len(body) > 65536, len(body)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
    p = Path(d) / "big.md"
    p.write_bytes(body)
    # 正确形态：全量 replace
    expect = hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest()
    got_mm, size = mm.sha256_file(p)
    ok = got_mm == expect
    print("make_manifest:", "OK" if ok else f"FAIL {got_mm[:12]} vs {expect[:12]}")
    if not ok:
        failures.append("make_manifest")

# growth 侧：直接跑一个临时目录的 apply_update，源带 manifest
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
    d = Path(d)
    src = d / "src"; dst = d / "dst"
    src.mkdir(); dst.mkdir()
    (src / "SKILL.md").write_bytes(b"---\nname: t\nversion: 1.0\n---\nhi\r\n")
    (src / "big.md").write_bytes(body)
    import json
    h = hashlib.sha256
    mf = {"version": "1.0", "files": {
        "SKILL.md": {"sha256": h(b"---\nname: t\nversion: 1.0\n---\nhi\n").hexdigest(), "size": 0},
        "big.md": {"sha256": h(body.replace(b"\r\n", b"\n")).hexdigest(), "size": 0},
    }}
    (src / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
    try:
        growth.apply_update(src, dst)
        print("growth apply_update boundary:", "OK")
    except Exception as e:
        print("growth apply_update boundary: FAIL —", e)
        failures.append("apply_update")

print("RESULT:", "PASS" if not failures else "FAIL")
sys.exit(0 if not failures else 1)
