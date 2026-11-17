# -*- coding: utf-8 -*-
"""测试：鸡生蛋防线——install_skill 把灵枢整目录装进（隔离的）全局技能目录，且哈希一致。

隔离要求：安装目标与记忆库都必须落在临时目录，绝不能碰用户真实的
~/.trae-cn/skills 或 ~/.brain-memory（沙箱/CI 上会 PermissionError，本机也会污染环境）。
所以在 import growth 之前先设 LINGSHU_GLOBAL_SKILLS 与 BRAIN_MEMORY_HOME。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lingshu-egg-"))
os.environ["BRAIN_MEMORY_HOME"] = str(TMP / "home")
os.environ["LINGSHU_GLOBAL_SKILLS"] = str(TMP / "skills")

sys.path.insert(0, str(ROOT / "scripts"))
import growth  # noqa: E402  （必须在上面的环境变量之后导入：GLOBAL_SKILLS 在导入时求值）

try:
    r = growth.install_skill("lingshu", from_dir=str(ROOT), force=True, accept_risk=True)
    print("state:", r.get("state"))
    print("path:", r.get("path"))
    ok = r.get("state") == "installed"

    # 校验装好的副本哈希与 dev manifest 一致
    mf = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8-sig"))
    dst = Path(r["path"]).parent

    def norm(b: bytes) -> bytes:
        if b.startswith(b"\xef\xbb\xbf"):
            b = b[3:]
        return b.replace(b"\r\n", b"\n")

    bad = []
    for rel, info in mf["files"].items():
        p = dst / rel
        if not p.is_file():
            bad.append((rel, "missing"))
            continue
        if hashlib.sha256(norm(p.read_bytes())).hexdigest() != info["sha256"].lower():
            bad.append((rel, "hash"))
    print("mismatches:", bad or "NONE")
    if bad:
        ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(0 if ok else 1)
