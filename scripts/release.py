#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢发版自动化（把台账第四节的发布流程串成一条命令）。

子命令
------
  python scripts/release.py bump <X.Y> ["小节标题"]
      1) 校验新版本 > 当前版本；
      2) 改四处版本：SKILL.md / package.json(X.Y.0) / README.md / CHANGELOG 首节占位；
      3) 按依赖序重生成：sync_engine → make_checksums → make_manifest；
      4) 跑六项检查 + 行为基线 + 全量测试，全绿才成功。
      不做 git 提交/tag/push（用户明确不做 tag/push，提交仍按"一个版本一个提交"手工或另配）。
      注意：若有新代码文件（如本脚本自己）尚未 git add，manifest --check 会拒绝——
      这是既有门禁，先 git add 新文件再 bump，脚本不替你决定哪些文件入库。

  python scripts/release.py publish
      1) 校验版本四处一致（check_version）+ 六项检查全绿；
      2) npm whoami 确认登录；
      3) 确认包版本比线上大（避免 E409 重复发）；
      4) npm publish；
      5) 轮询 registry 直到 latest==本地版本（npm staged 窗口最多 ~17 分钟）。
      不构建 tgz、不打 tag、不推 git。

  python scripts/release.py status
      打印当前版本 / 线上 latest / 是否可发。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
REGISTRY = "https://registry.npmjs.org"
PKG_NAME = "lingshu-skill"

CHECKS = [
    ("manifest", ["scripts/make_manifest.py", "--check"]),
    ("sync", ["scripts/sync_engine.py", "--check"]),
    ("panel", ["scripts/panel_build.py", "--check"]),
    ("checksums", ["scripts/astrbot_plugin_lingshu/make_checksums.py", "--check"]),
    ("version", ["scripts/check_version.py"]),
    ("pressure", ["scripts/check_pressure_sync.py"]),
]
GENERATE = [
    ["scripts/sync_engine.py"],
    ["scripts/astrbot_plugin_lingshu/make_checksums.py"],
    ["scripts/make_manifest.py"],
]


def _run(args: list, *, capture: bool = False) -> tuple[int, str]:
    cmd = [PY] + args
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr) if capture else ""
    if not capture:
        # 直接透传，用户能看见进度
        print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr)
    return r.returncode, out


def cur_version() -> str:
    t = (ROOT / "SKILL.md").read_text(encoding="utf-8-sig")
    m = re.search(r"^version:\s*([\d.]+)", t, re.M)
    return m.group(1) if m else ""


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def registry_latest(timeout: int = 15) -> str | None:
    try:
        with urllib.request.urlopen(f"{REGISTRY}/{PKG_NAME}", timeout=timeout) as r:
            d = json.load(r)
        return str(d.get("dist-tags", {}).get("latest", "")) or None
    except Exception:
        return None


def do_bump(new_ver: str, title: str) -> int:
    new_ver = new_ver.strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+", new_ver):
        print(f"[release] 版本格式应为 X.Y，收到 {new_ver!r}", file=sys.stderr)
        return 1
    old = cur_version()
    if _ver_tuple(new_ver) <= _ver_tuple(old):
        print(f"[release] 新版本 {new_ver} 必须大于当前 {old}", file=sys.stderr)
        return 1

    # 1) 改四处版本（bytes，保 BOM/换行原样）
    edits = [
        ("SKILL.md", f"version: {old}".encode(), f"version: {new_ver}".encode()),
        ("package.json", f'"version": "{old}.0"'.encode(), f'"version": "{new_ver}.0"'.encode()),
        ("README.md", f"**v{old}**".encode(), f"**v{new_ver}**".encode()),
    ]
    for rel, o, n in edits:
        p = ROOT / rel
        b = p.read_bytes()
        if o not in b:
            print(f"[release] {rel} 没找到版本标记 {o!r}，拒绝继续", file=sys.stderr)
            return 1
        p.write_bytes(b.replace(o, n, 1))
    # CHANGELOG 首节改写为新版本（占位，正文留空由维护者补）
    cl = ROOT / "CHANGELOG.md"
    t = cl.read_text(encoding="utf-8")
    first = re.search(r"^## v[\d.]+（[^）]*）", t, re.M)
    if not first:
        print("[release] CHANGELOG.md 找不到首节标题，拒绝继续", file=sys.stderr)
        return 1
    today = time.strftime("%Y-%m-%d")
    t = t[:first.start()] + f"## v{new_ver}（{today}）" + t[first.end():]
    cl.write_text(t, encoding="utf-8", newline="")

    # 2) 重生成派生文件
    print(f"[release] 版本 {old} → {new_ver}；重生成派生文件...")
    for gen in GENERATE:
        code, _ = _run(gen)
        if code != 0:
            print(f"[release] 生成失败：{gen}", file=sys.stderr)
            return 1

    # 3) 验证
    return do_verify()


def do_verify() -> int:
    bad = []
    for name, args in CHECKS:
        code, _ = _run(args)
        print(f"  {name}: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            bad.append(name)
    print("[release] 行为基线...")
    code, _ = _run(["scripts/behavior_baseline.py", "check"])
    print(f"  baseline: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        bad.append("baseline")
    print("[release] 全量测试...")
    code, _ = _run(["scripts/run_tests.py"])
    print(f"  tests: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        bad.append("tests")
    if bad:
        print(f"[release] 验证失败：{', '.join(bad)}", file=sys.stderr)
        return 1
    print("[release] 全部验证通过 ✔")
    return 0


def do_publish(wait_minutes: int) -> int:
    ver = cur_version()
    if do_verify() != 0:
        return 1
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        print("[release] 找不到 npm 命令", file=sys.stderr)
        return 1
    who = subprocess.run([npm, "whoami"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout.strip()
    if not who:
        print("[release] npm 未登录（npm whoami 无输出）", file=sys.stderr)
        return 1
    online = registry_latest()
    if online and _ver_tuple(online) >= _ver_tuple(ver):
        print(f"[release] 线上 latest={online} >= 本地 {ver}，无需重复发布", file=sys.stderr)
        return 1
    print(f"[release] npm 发布 {PKG_NAME}@{ver}（登录用户 {who}；线上 {online or '无'}）...")
    r = subprocess.run([npm, "publish"], cwd=ROOT)
    if r.returncode != 0:
        print("[release] npm publish 失败", file=sys.stderr)
        return 1
    # staged 窗口轮询
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        latest = registry_latest()
        if latest == ver:
            print(f"[release] 已可见：{PKG_NAME}@{latest}（latest）✔")
            return 0
        print(f"  staged 窗口：latest={latest}，等待可见（目标 {ver}）...")
        time.sleep(20)
    print(f"[release] 超时（{wait_minutes} 分钟）仍不可见，请手动查 registry", file=sys.stderr)
    return 1


def do_status() -> int:
    ver = cur_version()
    online = registry_latest()
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8-sig"))["version"]
    print(f"  本地 SKILL: {ver}  package.json: {pkg}")
    print(f"  线上 latest: {online or '查询失败'}")
    if online:
        print("  可发布:" , "是" if _ver_tuple(ver) > _ver_tuple(online) else "否（线上已 >= 本地）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灵枢发版自动化")
    sub = ap.add_subparsers(dest="action", required=True)
    p_bump = sub.add_parser("bump", help="升版本+重生成+验证")
    p_bump.add_argument("version", help="新版本 X.Y")
    p_bump.add_argument("title", nargs="?", default="", help="CHANGELOG 小节标题（可选）")
    p_pub = sub.add_parser("publish", help="校验后 npm publish 并等 staged 可见")
    p_pub.add_argument("--wait", type=int, default=20, help="staged 可见轮询最长分钟数（默认20）")
    sub.add_parser("status", help="查看本地/线上版本")
    args = ap.parse_args()
    if args.action == "bump":
        return do_bump(args.version, args.title)
    if args.action == "publish":
        return do_publish(getattr(args, "wait", 20))
    return do_status()


if __name__ == "__main__":
    sys.exit(main())
