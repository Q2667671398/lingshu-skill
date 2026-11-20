# -*- coding: utf-8 -*-
"""manifest「未跟踪文件」守卫回归。

背景（v3.45 的真实红灯）：`manifest.json` 只收 **git 已跟踪** 的文件。新建了文件、
还没 `git add` 就跑生成，清单会**静默少收一条**——本地 `--check` 绿，CI 干净检出后
必然报"清单与工作区不一致"。v3.45 就是这么红的，v3.48 加 docs 文件时又差点重演。

契约：
  - 发布范围（SCAN_FILES / SCAN_DIRS）内、未跟踪、未被 .gitignore 忽略的文件会被报出来；
  - 被 .gitignore 忽略的本地草稿**不算**（那是刻意不发布的）；
  - 范围外的未跟踪文件（如仓库根的交接笔记）也不报；
  - `check()` 遇到这类文件判红（--check 不能假装通过）；
  - 非 git 目录 / 没有 git 命令时安静返回空列表，不抛。

跑法：python tests/test_manifest_untracked_guard.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import make_manifest  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def _git(root: Path, *args: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except OSError:
        return False
    return r.returncode == 0


def make_repo() -> Path:
    """建一个最小 git 仓库：SKILL.md 已跟踪，用于验证"未跟踪"判定。"""
    tmp = Path(tempfile.mkdtemp(prefix="ls-manifest-"))
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@example.com")
    _git(tmp, "config", "user.name", "t")
    (tmp / "SKILL.md").write_text("---\nname: lingshu\nversion: 9.99\n---\n", encoding="utf-8")
    (tmp / ".gitignore").write_text("docs/draft.md\n", encoding="utf-8")
    _git(tmp, "add", "SKILL.md", ".gitignore")
    return tmp


def main() -> int:
    if shutil.which("git") is None:
        print("SKIP 没有 git，跳过（CI 上一定有）")
        return 0
    tmp = make_repo()
    try:
        # 1) 范围内未跟踪 → 被报出
        (tmp / "docs").mkdir(exist_ok=True)
        (tmp / "docs" / "新文档.md").write_text("# 新\n", encoding="utf-8")
        got = make_manifest._untracked_candidates(tmp)
        check("范围内未跟踪文件被报出", got == ["docs/新文档.md"], got)

        # 2) git add 之后不再报
        _git(tmp, "add", "docs/新文档.md")
        check("git add 后不再报", make_manifest._untracked_candidates(tmp) == [])

        # 3) .gitignore 里的草稿不算
        (tmp / "docs" / "draft.md").write_text("# 草稿\n", encoding="utf-8")
        check("被忽略的草稿不报", "docs/draft.md" not in make_manifest._untracked_candidates(tmp))

        # 4) 范围外的未跟踪文件不报（仓库根的交接笔记这类）
        (tmp / "交接笔记.md").write_text("# 本地\n", encoding="utf-8")
        check("范围外文件不报", make_manifest._untracked_candidates(tmp) == [],
              make_manifest._untracked_candidates(tmp))

        # 5) check() 真的判红：清单里缺这个文件 + 它还没 git add
        (tmp / "docs" / "又一份.md").write_text("# 又\n", encoding="utf-8")
        make_manifest.build(tmp)                      # 生成时就会警告（此时已漏收）
        rc = make_manifest.check(tmp)
        check("未跟踪时 check 判红", rc == 1, f"check()={rc}")

        # 6) 收进清单后 check 变绿
        _git(tmp, "add", "docs/又一份.md")
        make_manifest.build(tmp)
        rc = make_manifest.check(tmp)
        check("全部跟踪后 check 变绿", rc == 0, f"check()={rc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 7) 非 git 目录：安静返回空，不抛
    plain = Path(tempfile.mkdtemp(prefix="ls-nogit-"))
    try:
        check("非 git 目录返回空", make_manifest._untracked_candidates(plain) == [])
    finally:
        shutil.rmtree(plain, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
