# -*- coding: utf-8 -*-
"""测试：鸡生蛋防线——install_skill 把灵枢整目录装进（隔离的）全局技能目录，且哈希一致。

隔离要求：安装目标与记忆库都必须落在临时目录，绝不能碰用户真实的
~/.trae-cn/skills 或 ~/.brain-memory（沙箱/CI 上会 PermissionError，本机也会污染环境）。
所以在 import growth 之前先设 LINGSHU_GLOBAL_SKILLS 与 BRAIN_MEMORY_HOME。

发布基准前提：manifest.json 是 make_manifest.py 在发布时生成的产物，CI 里由
`make_manifest --check` 单独对账。开发工作区改了 scripts/ 但还没重新生成 manifest
时，"安装副本 vs manifest 哈希"这个断言没有可信基准：安装器本身会按设计把
「源与清单不一致」当成篡改并拒绝安装（安全防线）。此时本用例显式 SKIP 并说明原因，
避免开发期每个瞬间都假失败；CI 的干净检出仍会完整执行本用例。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _norm(b: bytes) -> bytes:
    """与 make_manifest.py / apply_update 一致的规范形态：剥 UTF-8 BOM + CRLF→LF。"""
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    return b.replace(b"\r\n", b"\n")


def _stale_manifest_files(mf: dict) -> list:
    """返回工作区与 manifest 不一致的文件 [(rel, 'hash'|'missing')]。"""
    stale = []
    for rel, info in (mf.get("files") or {}).items():
        p = ROOT / rel
        if not p.is_file():
            stale.append((rel, "missing"))
            continue
        want = str(info.get("sha256") or "").lower()
        if hashlib.sha256(_norm(p.read_bytes())).hexdigest() != want:
            stale.append((rel, "hash"))
    return stale


def main() -> int:
    mf = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8-sig"))
    stale = _stale_manifest_files(mf)
    if stale:
        print("SKIP: 开发工作区与 manifest.json 不一致（manifest 属发布产物，"
              "由 CI 的 make_manifest --check 单独把关）：")
        for rel, why in stale[:8]:
            print(f"  - {rel} ({why})")
        print("SKIP: 安装哈希校验用例跳过——干净检出/发布构建时仍会完整执行。")
        return 0

    TMP = Path(tempfile.mkdtemp(prefix="lingshu-egg-"))
    os.environ["BRAIN_MEMORY_HOME"] = str(TMP / "home")
    os.environ["LINGSHU_GLOBAL_SKILLS"] = str(TMP / "skills")

    sys.path.insert(0, str(ROOT / "scripts"))
    import growth  # noqa: E402  （必须在上面的环境变量之后导入：GLOBAL_SKILLS 在导入时求值）

    ok = False
    try:
        r = growth.install_skill("lingshu", from_dir=str(ROOT), force=True, accept_risk=True)
        print("state:", r.get("state"))
        print("path:", r.get("path"))
        ok = r.get("state") == "installed"

        # 校验装好的副本哈希与 dev manifest 一致
        dst = Path(r["path"]).parent

        bad = []
        for rel, info in mf["files"].items():
            p = dst / rel
            if not p.is_file():
                bad.append((rel, "missing"))
                continue
            if hashlib.sha256(_norm(p.read_bytes())).hexdigest() != info["sha256"].lower():
                bad.append((rel, "hash"))
        print("mismatches:", bad or "NONE")
        if bad:
            ok = False
        print("RESULT:", "PASS" if ok else "FAIL")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if ok else 1


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
    sys.exit(main())
