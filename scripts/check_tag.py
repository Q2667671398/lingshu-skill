#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢发版 tag 守卫：推送 main 之前，确认当前版本号有对应的 git tag。

为什么需要它：插件市场显示的「版本说明」来自仓库 tag/Release 那条线；
仓库不打 tag 时（历史上有 3.05–3.13 一段空档），市场就一直显示旧版本，
用户会以为更新到了旧版本。这个钩子在 push 时把「打 tag」变成硬动作。

用法：
  python scripts/check_tag.py            # pre-push 钩子调用（读 stdin 的 refs）
  python scripts/check_tag.py --check    # 只检查、不建 tag（CI / 手动核对）
  python scripts/check_tag.py --install  # 安装 pre-commit + pre-push 钩子

行为：
  · 推送到 main/master 时，若当前版本没有对应 tag → 自动在 HEAD 打一个
    带注释的 vX.Y tag，然后拒绝本次推送，并给出下一步命令；
  · tag 存在但打在**更早的提交**上：只要它是 HEAD 的祖先、且那个提交里的
    SKILL.md 版本就是当前版本 → 放行（v3.50 起）。打完 tag 再改文档/CI 是
    正常收尾动作，旧规则要求"tag 必须正好指向 HEAD"，逼得人用 SKIP 绕过；
  · tag 打在别的分支上、或打在另一个版本的提交上 → 拒绝；
  · 推送标签或其它分支 → 直接放行；
  · 真的要走捷径也可用 LINGSHU_SKIP_TAG_CHECK=1 临时跳过（不建议）。

退出码：0 放行；1 拒绝（缺 tag / tag 指向不对）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^version:\s*([0-9][0-9.]*)\s*$", re.MULTILINE)
PROTECTED_BRANCHES = ("refs/heads/main", "refs/heads/master")


def _git(args: list[str]) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def skill_version() -> str | None:
    try:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def head_sha() -> str | None:
    return _git(["rev-parse", "HEAD"])


def tag_candidates(version: str) -> list[str]:
    return [f"v{version}", version]


def find_tag(version: str) -> tuple[str | None, str | None]:
    """返回 (tag 名, tag 指向的提交 sha)；没有则 (None, None)。

    注意用 ^{commit} 解引用：带注释的 tag 是独立对象，直接 rev-parse
    拿到的是 tag 对象 sha，会和 HEAD 的提交 sha 永远不等。
    """
    for name in tag_candidates(version):
        sha = _git(["rev-parse", "-q", "--verify", f"refs/tags/{name}^{{commit}}"])
        if sha:
            return name, sha
    return None, None


def is_ancestor(sha: str, head: str) -> bool:
    """sha 是否是 head 的祖先（含相等）。"""
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, head],
                           cwd=str(ROOT), capture_output=True)
    except OSError:
        return False
    return r.returncode == 0


def commit_version(sha: str) -> str | None:
    """读某个提交里 SKILL.md 的 version（校验"tag 确实打在那个版本上"）。"""
    text = _git(["show", f"{sha}:SKILL.md"])
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def pushed_refs(stdin_text: str) -> list[str]:
    """pre-push 的 stdin：每行 <local ref> <local sha> <remote ref> <remote sha>"""
    refs = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            refs.append(parts[2])
    return refs


def create_tag(version: str) -> str | None:
    name = f"v{version}"
    r = subprocess.run(["git", "tag", "-a", name, "-m", name], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return name if r.returncode == 0 else None


def check(create_if_missing: bool) -> int:
    if os.environ.get("LINGSHU_SKIP_TAG_CHECK") == "1":
        print("[tag] LINGSHU_SKIP_TAG_CHECK=1，跳过 tag 检查", file=sys.stderr)
        return 0

    version = skill_version()
    if not version:
        print("[tag] 读不到 SKILL.md 的 version，跳过检查", file=sys.stderr)
        return 0

    head = head_sha()
    name, sha = find_tag(version)
    if name and sha and head and sha == head:
        print(f"[tag] OK：v{version} 已存在并指向 HEAD（{name}）")
        return 0

    if name and sha and head:
        # tag 在 HEAD 之前的老提交上：只要①它确实是 HEAD 的祖先（没跑到别的分支去）、
        # ②那个提交里的 SKILL.md 版本就是这个版本，就放行。
        # 为什么放宽（v3.50）：打完 tag 再改文档/CI 是很正常的收尾动作，旧规则要求
        # "tag 必须正好指向 HEAD"，于是每次收尾都要么改 tag（公有 tag 不该动）、
        # 要么用 LINGSHU_SKIP_TAG_CHECK=1 绕过——绕过一次就失去这道闸的意义。
        # 语义没变：marketplace 的版本说明仍然来自「那一个 tag 指向的提交」。
        if not is_ancestor(sha, head):
            print(f"[tag] 拒绝：tag {name} 指向 {sha[:8]}，它不在当前分支上"
                  f"（HEAD {head[:8]}）。发版 tag 必须打在这个版本所在的提交上。", file=sys.stderr)
            print(f"  修正：git tag -d {name} && git tag -a v{version} -m v{version} && "
                  f"git push --force <remote> v{version}", file=sys.stderr)
            return 1
        tagged_ver = commit_version(sha)
        if tagged_ver != version:
            print(f"[tag] 拒绝：tag {name} 打在 {sha[:8]} 上，但那个提交的 SKILL.md 版本是"
                  f" {tagged_ver or '读不出'}，不是 v{version}。tag 打错了位置。", file=sys.stderr)
            return 1
        print(f"[tag] OK：v{version} 的 tag 在 {sha[:8]}（HEAD 的祖先），"
              f"其后的提交不改版本 → 放行")
        return 0

    if create_if_missing and head:
        created = create_tag(version)
        if created:
            print(f"[tag] 当前版本 v{version} 还没有 tag —— 已在 HEAD 自动创建 {created}。",
                  file=sys.stderr)
            print("  下一步：git push --follow-tags <remote> main", file=sys.stderr)
            print("  （或用 git push <remote> v" + version + " 单独推 tag）", file=sys.stderr)
            return 1

    print(f"[tag] 拒绝：当前版本 v{version} 没有对应的 tag。", file=sys.stderr)
    print("  下一步：git tag -a v" + version + " -m v" + version + " 然后 git push --follow-tags",
          file=sys.stderr)
    return 1


def install_hooks() -> int:
    hook_dir = ROOT / "scripts" / "hooks"
    git_dir = _git(["rev-parse", "--git-dir"])
    if git_dir is None:
        print("[tag] 不是 git 仓库，无法安装钩子", file=sys.stderr)
        return 2
    target_dir = (ROOT / git_dir.strip()).resolve() / "hooks"
    target_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for src in sorted(hook_dir.glob("*")):
        if not src.is_file():
            continue
        target = target_dir / src.name
        if target.exists() and target.read_bytes() != src.read_bytes():
            import time
            import shutil
            backup = target.with_name(f"{src.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(target, backup)
            print(f"[tag] 已备份原钩子：{backup}")
        target.write_bytes(src.read_bytes())
        try:
            target.chmod(target.stat().st_mode | 0o111)
        except OSError:
            pass
        installed.append(src.name)
    print(f"[tag] 已安装钩子：{', '.join(installed)} → {target_dir}")
    print("  · pre-commit：代码变更必须升 SKILL.md 版本")
    print("  · pre-push  ：推 main 前必须有与版本对应的 tag（缺则自动创建并拒绝本次推送）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="灵枢：推送前检查发版 tag")
    ap.add_argument("--check", action="store_true", help="只检查，不自动创建 tag")
    ap.add_argument("--install", action="store_true", help="安装 pre-commit + pre-push 钩子")
    args = ap.parse_args(argv)
    if args.install:
        return install_hooks()

    if args.check:
        return check(create_if_missing=False)

    refs = pushed_refs(sys.stdin.read() if not sys.stdin.isatty() else "")
    if refs and not any(r in PROTECTED_BRANCHES for r in refs):
        return 0  # 只推 tag / 其它分支，不拦
    return check(create_if_missing=True)


def _ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台（cp1252/cp936）编不了中文就崩。

    CI 的 windows-latest stdout 是 cp1252：脚本一 print 中文就 UnicodeEncodeError
    直接退出（实测 sync_engine.py / make_checksums.py 都因此挂过，Ubuntu 同期正常）。
    这不是业务 bug，是"中文输出 + 非 UTF-8 控制台"的必然结果。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        fn = getattr(stream, "reconfigure", None)
        if callable(fn):
            try:
                fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    sys.exit(main())
