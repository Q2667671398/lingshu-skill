#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_update 原子性自测试（纯标准库，不依赖灵枢数据目录）。

用例：
  A. 模拟复制第 N 个文件时抛异常强制终止 → 断言目标目录仍是完整旧版本（无半新半旧）。
  B. 目标目录放一个清单外垃圾文件 → 跑 apply 后断言垃圾文件被移到备份 removed/ 且原路径无残留。
  C. 附加：正常更新成功、SHA256 不匹配拒部署。

运行：python scripts/test_apply_update.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import growth   # noqa: E402

PASS = []


def ok(name: str, cond: bool, detail: str = ""):
    PASS.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    return cond


def make_tree(root: Path, version: str, n: int = 12) -> None:
    """造一个最小技能目录：SKILL.md + scripts/f01.py…f{n}.py + manifest.json。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: demo\nversion: {version}\n---\nbody\n",
                                   encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    files = {}
    for i in range(1, n + 1):
        p = root / "scripts" / f"f{i:02d}.py"
        content = f"# v{version} file {i}\n"
        p.write_text(content, encoding="utf-8")
        files[f"scripts/f{i:02d}.py"] = {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size": p.stat().st_size}
    files["SKILL.md"] = {
        "sha256": hashlib.sha256((root / "SKILL.md").read_bytes()).hexdigest(),
        "size": (root / "SKILL.md").stat().st_size}
    (root / "manifest.json").write_text(
        json.dumps({"version": version, "files": files}, indent=2), encoding="utf-8")


def tree_signature(d: Path) -> dict[str, str]:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in d.rglob("*") if p.is_file()}


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="lingshu-apply-test-"))
    try:
        src_old, src_new = base / "src-old", base / "src-new"
        dst = base / "installed" / "demo"
        make_tree(src_old, "1.0")
        make_tree(src_new, "2.0")

        # 首次正常安装：目标 = 旧版本
        growth.apply_update(src_old, dst)
        sig_old = tree_signature(dst)
        ok("首次安装落成完整旧版本", "SKILL.md" in sig_old and len(sig_old) == 13)

        # ---- 用例 A：复制第 10 个文件时抛异常 → 目标必须仍是完整旧版本 ----
        def boom(_src, _dst_, copied):
            if copied >= 9:   # 第 10 个文件（copied 从 0 计）处模拟中断
                raise OSError("simulated interrupt at file #10")
        growth._COPY_HOOK = boom
        try:
            growth.apply_update(src_new, dst)
            ok("中断抛出异常", False, "apply_update 竟然没抛")
        except OSError:
            ok("中断抛出异常", True)
        finally:
            growth._COPY_HOOK = None
        ok("中断后目标仍是完整旧版本（原子性）", tree_signature(dst) == sig_old,
           "目标目录签名与更新前不一致——出现半新半旧")
        staging_left = [p.name for p in dst.parent.glob("demo.apply-staging-*")]
        ok("中断后 staging 已清理", not staging_left, str(staging_left))

        # ---- 用例 B：垃圾文件归档到 removed/ ----
        junk = dst / "junk-leftover.txt"
        junk.write_text("garbage", encoding="utf-8")
        (dst / "nested").mkdir()
        (dst / "nested" / "junk2.log").write_text("j", encoding="utf-8")  # 日志算垃圾外的清单外文件
        growth.apply_update(src_new, dst)
        ok("更新成功落到新版本", tree_signature(dst).get("SKILL.md")
           == tree_signature(src_new).get("SKILL.md"))
        backups = sorted((dst.parent / "_lingshu-backups").glob("apply-*"))
        removed = backups[-1] / "removed"
        ok("垃圾文件已移入备份 removed/", (removed / "junk-leftover.txt").is_file())
        ok("嵌套垃圾文件也归档", (removed / "nested" / "junk2.log").is_file())
        ok("原路径无残留", not junk.exists() and not (dst / "nested").exists())

        # ---- 用例 C：SHA256 不匹配 → 拒绝部署、目标不动 ----
        sig_new = tree_signature(dst)
        (src_new / "scripts" / "f01.py").write_text("# tampered\n", encoding="utf-8")
        try:
            growth.apply_update(src_new, dst)
            ok("SHA256 不符拒绝部署", False, "竟然部署成功了")
        except ValueError:
            ok("SHA256 不符拒绝部署", True)
        ok("拒绝部署后目标不变", tree_signature(dst) == sig_new)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    failed = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(failed)}/{len(PASS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
