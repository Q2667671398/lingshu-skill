# -*- coding: utf-8 -*-
"""发布漂移自检（growth.release_drift）回归测试。

背景：v3.45 写好的护栏 / 空库熔断在本机根本没生效——运行时装的还是 v3.44，而
doctor 只查"包内三处版本互相对账"，从不问"这台机器上跑的是哪一版"。本测试锁住
新增的 `release-drift` 判定，防止它以后退化成假绿。

契约：
  - 版本按 主.次(.修订) 归一：3.46 与 3.46.0 是同一版本，末尾 0 不参与比较；
  - 已安装副本落后源码 → stale，drift=True，且 message 里给出可执行的升级命令；
  - 已安装副本比源码新 → ahead（说明当前在跑旧检出），同样要报；
  - 版本读不出来 → unknown，不猜、不静默放过；
  - 没有副本 → 不算漂移（这台机器只跑源码）；
  - 扫描过程任何异常都降级为"查不到"，绝不把 doctor 拖垮。

运行：python tests/test_release_drift.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import growth  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def make_copy(base: Path, name: str, version: str | None) -> Path:
    """造一个"已安装副本"目录：给了 version 就写 SKILL.md，否则留空目录。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (d / "SKILL.md").write_text(f"---\nname: lingshu\nversion: {version}\n---\n",
                                    encoding="utf-8")
    return d


class Sandbox:
    """把 release_drift 的三个外部依赖换成临时目录（不碰真实 home）。"""

    def __init__(self, src_version: str, states: list, skill_roots: list | None = None,
                 boom: bool = False):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.src = make_copy(self.base, "src", src_version)
        self.states = states
        self.skill_roots = skill_roots or []
        self.boom = boom

    def __enter__(self):
        self._saved = (growth._panel_root, growth.dsh_lingshu_copy_state, growth._skill_roots)
        growth._panel_root = lambda: self.src
        growth.dsh_lingshu_copy_state = self._fake_state
        growth._skill_roots = lambda: self.skill_roots
        return self

    def _fake_state(self):
        if self.boom:
            raise RuntimeError("模拟扫描失败")
        return self.states

    def __exit__(self, *exc):
        growth._panel_root, growth.dsh_lingshu_copy_state, growth._skill_roots = self._saved
        self.td.cleanup()
        return False


def state(profile: str, path: Path, ver: str = "", st: str = "ok") -> dict:
    return {"profile": profile, "path": str(path), "state": st, "version": ver}


def main() -> int:
    # 0) 版本归一
    check("3.46 == 3.46.0", growth._ver_tuple("3.46") == growth._ver_tuple("3.46.0"))
    check("3.46 < 3.46.1", growth._ver_tuple("3.46") < growth._ver_tuple("3.46.1"))
    check("读不出版本 → 空元组", growth._ver_tuple("") == () and growth._ver_tuple("??") == ())

    # 1) 一致：不报漂移
    with Sandbox("3.46", []) as sb:
        d = make_copy(sb.base, "installed", "3.46.0")
        sb.states = [state("desktop", d)]
        r = growth.release_drift()
    check("副本与源码同版本 → 不漂移", r["drift"] is False and not r["stale"], r["message"])
    check("一致时 message 报数量", "1 个已安装副本均为 v3.46" in r["message"], r["message"])

    # 2) 落后：要报，并给升级命令
    with Sandbox("3.46", []) as sb:
        d = make_copy(sb.base, "installed", "3.44")
        sb.states = [state("desktop", d)]
        r = growth.release_drift()
    check("副本落后 → drift=True", r["drift"] is True and len(r["stale"]) == 1, r["stale"])
    check("落后 message 指出版本与升级命令",
          "v3.44" in r["message"] and "update_dsh_copy.ps1" in r["message"], r["message"])
    check("落后副本带 status/label", r["stale"][0]["status"] == "stale"
          and r["stale"][0]["label"] == "DSH/desktop")

    # 3) 副本比源码新（在跑旧检出）
    with Sandbox("3.44", []) as sb:
        d = make_copy(sb.base, "installed", "3.46")
        sb.states = [state("desktop", d)]
        r = growth.release_drift()
    check("副本比源码新 → ahead 也要报", r["drift"] is True and len(r["ahead"]) == 1, r["message"])
    check("ahead message 说明旧检出", "比源码" in r["message"] and "3.46" in r["message"],
          r["message"])

    # 4) 版本读不出来
    with Sandbox("3.46", []) as sb:
        d = make_copy(sb.base, "installed", None)
        sb.states = [state("desktop", d)]
        r = growth.release_drift()
    check("版本未知 → unknown + drift", r["drift"] is True and len(r["unknown"]) == 1, r["message"])
    check("unknown message 不猜版本", "读不出来" in r["message"], r["message"])

    # 5) 没有副本：不算漂移
    with Sandbox("3.46", []) as sb:
        sb.states = []
        r = growth.release_drift()
    check("没有副本 → 不算漂移", r["drift"] is False and "未检测到" in r["message"], r["message"])

    # 6) 残缺/未安装状态不参与（空壳归 doctor 的 dsh-repair 管）
    with Sandbox("3.46", []) as sb:
        empty = make_copy(sb.base, "empty_copy", None)
        sb.states = [state("desktop", empty, st="empty"),
                     state("laptop", sb.base / "nope", st="not_installed")]
        r = growth.release_drift()
    check("空壳/未装副本跳过", r["drift"] is False and not r["copies"], r["copies"])

    # 7) 全局技能目录里的副本也要看见
    with Sandbox("3.46", []) as sb:
        roots = [sb.base / "skills"]
        make_copy(sb.base / "skills", "lingshu", "3.45")
        sb.skill_roots = [("Claude Code", roots[0])]
        r = growth.release_drift()
    check("全局技能副本被发现", len(r["copies"]) == 1
          and r["copies"][0]["kind"] == "skill"
          and r["copies"][0]["label"] == "技能/Claude Code", r["copies"])
    check("技能副本落后也算漂移", r["drift"] is True, r["message"])
    check("技能副本给的是 skill install 而不是 DSH 脚本",
          "skill install lingshu" in r["message"] and "update_dsh_copy" not in r["message"],
          r["message"])

    # 8) 扫描炸了：降级成"查不到"，不抛异常
    with Sandbox("3.46", [], boom=True) as sb:
        r = growth.release_drift()
    check("扫描异常不抛、结果仍可用", isinstance(r, dict) and r["drift"] is False, r["message"])

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
