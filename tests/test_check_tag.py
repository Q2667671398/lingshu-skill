# -*- coding: utf-8 -*-
"""发版 tag 守卫（check_tag）回归测试。

背景：这个钩子曾在两小时内挡了两次正常收尾——打完 tag 再改 CI/文档，旧规则要求
"tag 必须正好指向 HEAD"，于是只能 `LINGSHU_SKIP_TAG_CHECK=1` 绕过；一旦习惯性绕过，
这道闸就名存实亡。v3.50 把规则放宽成「tag 是 HEAD 的祖先且确实打在这个版本上」，
语义不变（marketplace 的版本说明仍来自那个 tag 指向的提交）。

契约：
  - 没有当前版本的 tag → 拒绝（1）；给了 --create 则自动打 tag 并拒绝本次推送；
  - tag 正好指向 HEAD → 放行（0）；
  - tag 在 HEAD 之前的提交上（祖先），且那个提交的 SKILL.md 版本 == 当前版本 → 放行（0）；
  - tag 在祖先上，但那个提交的 SKILL.md 是**别的版本** → 拒绝（1，tag 打错位置）；
  - tag 不在当前分支（不是 HEAD 祖先）→ 拒绝（1）；
  - LINGSHU_SKIP_TAG_CHECK=1 → 直接放行（0）。

跑法：python tests/test_check_tag.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_tag  # noqa: E402

FAILS: list[str] = []
GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def check(name: str, ok: bool, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=GIT_ENV)
    return (r.stdout or "").strip()


def commit(repo: Path, version: str, msg: str) -> str:
    """造一个真提交：SKILL.md 带版本，另加一个随 msg 变化的小文件保证内容不同。

    只改 SKILL.md 的话，前后两次"同版本"提交内容一样，git 会直接报 nothing to commit，
    结果 HEAD 没动、测试场景就静默变形了（第一版就是这么踩的）。
    """
    (repo / "SKILL.md").write_text(f"---\nname: lingshu\nversion: {version}\n---\n",
                                   encoding="utf-8")
    (repo / "MARKER.txt").write_text(msg + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class Repo:
    def __init__(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name)
        git(self.path, "init", "-q", "-b", "main")
        # 仓库级身份：check_tag 内部会自己起 git 子进程（打 tag），不走本测试的 env
        git(self.path, "config", "user.name", "t")
        git(self.path, "config", "user.email", "t@example.com")

    def __enter__(self):
        self._saved = check_tag.ROOT
        check_tag.ROOT = self.path
        os.environ.pop("LINGSHU_SKIP_TAG_CHECK", None)
        return self

    def __exit__(self, *exc):
        check_tag.ROOT = self._saved
        self.td.cleanup()
        return False


def quiet(fn, *a):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


def main() -> int:
    if shutil.which("git") is None:
        print("SKIP 没有 git")
        return 0

    # 1) 没有任何 tag → 拒绝
    with Repo() as r:
        commit(r.path, "9.10", "release 9.10")
        rc, out = quiet(check_tag.check, False)
        check("没有 tag → 拒绝", rc == 1, out.splitlines()[-1] if out else "")

    # 2) --create：自动打 tag，但仍拒绝本次推送（提示下一步）
    with Repo() as r:
        commit(r.path, "9.10", "release 9.10")
        rc, _ = quiet(check_tag.check, True)
        tag_exists = git(r.path, "tag", "--list", "v9.10")
        check("缺 tag 时自动创建并拒绝本次推送", rc == 1 and tag_exists == "v9.10", tag_exists)

    # 3) tag 正好指向 HEAD → 放行
    with Repo() as r:
        head = commit(r.path, "9.10", "release 9.10")
        git(r.path, "tag", "-a", "v9.10", "-m", "v9.10", head)
        rc, out = quiet(check_tag.check, False)
        check("tag 指向 HEAD → 放行", rc == 0, out.strip().splitlines()[-1])

    # 4) tag 在祖先上（tag 之后只有文档/CI 提交）→ 放行（v3.50 放宽）
    with Repo() as r:
        rel = commit(r.path, "9.10", "release 9.10")
        git(r.path, "tag", "-a", "v9.10", "-m", "v9.10", rel)
        (r.path / "README.md").write_text("# 文档改动\n", encoding="utf-8")
        git(r.path, "add", "-A")
        git(r.path, "commit", "-q", "-m", "docs: 收尾")
        rc, out = quiet(check_tag.check, False)
        check("tag 在祖先上且版本一致 → 放行", rc == 0, out.strip().splitlines()[-1])

    # 5) tag 在祖先上，但那个提交是别的版本 → 拒绝（tag 打错位置）
    with Repo() as r:
        old = commit(r.path, "9.09", "release 9.9")
        git(r.path, "tag", "-a", "v9.10", "-m", "v9.10", old)   # 故意打错
        commit(r.path, "9.10", "bump 9.10")
        rc, out = quiet(check_tag.check, False)
        check("tag 打在别的版本提交上 → 拒绝", rc == 1, out.strip().splitlines()[-1])

    # 6) tag 不在当前分支（不是祖先）→ 拒绝
    with Repo() as r:
        commit(r.path, "9.10", "base")
        git(r.path, "checkout", "-q", "-b", "side")
        side = commit(r.path, "9.10", "side work")
        git(r.path, "tag", "-a", "v9.10", "-m", "v9.10", side)
        git(r.path, "checkout", "-q", "main")
        commit(r.path, "9.10", "main work")
        rc, out = quiet(check_tag.check, False)
        check("tag 不在当前分支 → 拒绝", rc == 1, out.strip().splitlines()[-1])

    # 7) 逃生阀
    with Repo() as r:
        commit(r.path, "9.10", "release")
        os.environ["LINGSHU_SKIP_TAG_CHECK"] = "1"
        try:
            rc, _ = quiet(check_tag.check, False)
        finally:
            os.environ.pop("LINGSHU_SKIP_TAG_CHECK", None)
        check("SKIP 环境变量放行", rc == 0)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
