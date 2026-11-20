# -*- coding: utf-8 -*-
"""数据目录护栏（safety）回归。

背景：用户目录被整片清空、记忆库一起没了，而当时的备份默认落在
`~/Documents/brain-memory-backups` —— 备份和本体一起被端掉，等于没备份。
这里锁住三条承诺：

  1. 盘符根 / 用户主目录 / 用户库文件夹（桌面、文档、下载…）不能当记忆库；
  2. restore --exact 只清「灵枢自己管的」文件，用户自己放的东西不许动；
  3. 库从「有内容」变成「全空」时要报警，而不是安静地从头开始。

零依赖：纯标准库，直接 `python tests/test_safety_home_guard.py` 运行。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401
import core  # noqa: E402
import safety  # noqa: E402

_TMPDIRS: list[Path] = []


def _tmp(name: str = "ls-safe-") -> Path:
    d = Path(tempfile.mkdtemp(prefix=name))
    _TMPDIRS.append(d)
    return d


def _fresh_home(sub: str = "") -> Path:
    """建一个隔离的临时记忆库并切过去。"""
    td = _tmp() / (sub or "store")
    core.HOME = td
    core._recompute_paths()
    core._sync_state()
    core.WIPE_ALERT = None
    core.ensure_home()
    return td


class _raises:
    """极简 pytest.raises 替身：块内必须抛出指定异常，否则判失败。"""

    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.ok = False

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"期望抛出 {self.exc_type.__name__}，但没有抛")
        if not issubclass(et, self.exc_type):
            return False
        self.ok = True
        return True


# ------------------------------------------------------------------ 危险目录判定

def _danger_root() -> Path:
    """当前平台上"绝对不能当记忆库"的根路径。

    Windows 用盘符根；POSIX 用 `/`。**不能硬写 `Path("C:\\\\")`**：在 Linux 上它只是
    一个名叫 `C:\` 的相对目录（`Path("C:\\").parent != Path("C:\\")`），判定当然不危险——
    CI 就是跑在 ubuntu 上的，这类断言在那里必挂（v3.47 的 CI 红就是这个）。
    """
    return Path("C:\\") if sys.platform == "win32" else Path("/")


def test_drive_root_is_dangerous():
    assert safety.dangerous_reason(_danger_root())
    if sys.platform == "win32":
        assert safety.dangerous_reason(Path("D:\\"))


def test_user_home_itself_is_dangerous():
    reason = safety.dangerous_reason(Path.home())
    assert reason and "主目录" in reason


def test_library_folders_are_dangerous():
    for name in ("Documents", "Desktop", "Downloads", "Pictures"):
        assert safety.dangerous_reason(Path.home() / name), name
        # 库文件夹**里面**也不能当记忆库
        assert safety.dangerous_reason(Path.home() / name / "brain-memory"), name
        # 大小写不能影响判定：macOS 的真实目录名是 ~/Documents，Linux 也常大写 D，
        # 而 POSIX 路径比较区分大小写（Windows 不区分，所以这个坑只在非 Windows 暴露）。
        assert safety.dangerous_reason(Path.home() / name.lower()), name + "（全小写）"
        assert safety.dangerous_reason(Path.home() / name.upper()), name + "（全大写）"


def test_normal_dir_is_fine():
    d = _tmp()
    assert safety.dangerous_reason(d) is None
    assert safety.dangerous_reason(d / "brain-memory") is None


def test_assert_safe_home_raises():
    old = os.environ.pop("BRAIN_MEMORY_ALLOW_UNSAFE_HOME", None)
    try:
        with _raises(safety.UnsafeHomeError):
            safety.assert_safe_home(_danger_root(), action="初始化")
        safety.assert_safe_home(_tmp())  # 正常目录不抛
    finally:
        if old is not None:
            os.environ["BRAIN_MEMORY_ALLOW_UNSAFE_HOME"] = old


def test_escape_hatch_allows_but_warns():
    old = os.environ.get("BRAIN_MEMORY_ALLOW_UNSAFE_HOME")
    os.environ["BRAIN_MEMORY_ALLOW_UNSAFE_HOME"] = "1"
    try:
        safety.assert_safe_home(_danger_root(), action="初始化")  # 不抛
    finally:
        if old is None:
            os.environ.pop("BRAIN_MEMORY_ALLOW_UNSAFE_HOME", None)
        else:
            os.environ["BRAIN_MEMORY_ALLOW_UNSAFE_HOME"] = old


# ------------------------------------------------------------------ 受管路径白名单

def test_managed_relpath_keeps_user_files():
    assert safety.is_managed_relpath(Path("hippocampus.jsonl"))
    assert safety.is_managed_relpath(Path("persona.json"))
    assert safety.is_managed_relpath(Path("dreams/20260101.md"))
    assert safety.is_managed_relpath(Path("backups/brain-memory_x.zip"))
    # 用户自己塞进记忆库的东西：任何"清空"都不许碰
    assert not safety.is_managed_relpath(Path("我的草稿.txt"))
    assert not safety.is_managed_relpath(Path("照片/a.jpg"))
    assert not safety.is_managed_relpath(Path("导出/记忆.md"))


def test_managed_home_detection():
    d = _tmp()
    assert not safety.is_managed_home(d)      # 空目录不像库
    home = _fresh_home()
    assert safety.is_managed_home(home)       # 有了标记文件才像


def test_ensure_home_refuses_dangerous_path():
    old_env = os.environ.pop("BRAIN_MEMORY_ALLOW_UNSAFE_HOME", None)
    old_home = core.HOME
    try:
        core.HOME = Path.home()
        core._recompute_paths()
        core._sync_state()
        with _raises(safety.UnsafeHomeError):
            core.ensure_home()
    finally:
        core.HOME = old_home
        core._recompute_paths()
        core._sync_state()
        if old_env is not None:
            os.environ["BRAIN_MEMORY_ALLOW_UNSAFE_HOME"] = old_env


# ------------------------------------------------------------------ 备份选址跨卷

def test_backup_dir_never_lands_in_documents_or_desktop():
    home = _fresh_home()
    target = safety.pick_backup_dir(home)
    assert target.exists()
    low = str(target).lower()
    for lib in ("\\documents\\", "\\desktop\\"):
        assert lib not in low, f"备份又落回用户库文件夹：{target}"


def test_backup_candidates_prefer_other_volume():
    home = _tmp() / "store"
    candidates = safety.backup_dir_candidates(home)
    assert candidates                                     # 至少有一个兜底
    assert candidates[-1] == home / "backups"             # 最后才同卷兜底


def test_same_volume_self():
    d = _tmp()
    assert safety.same_volume(d, d / "x")


# ------------------------------------------------------------------ 空库熔断

def test_wipe_alert_fires_when_store_goes_empty():
    home = _fresh_home()
    mem = home / "hippocampus.jsonl"
    mem.write_text('{"id": "m_1", "text": "一条记忆"}\n', encoding="utf-8")
    core.WIPE_ALERT = None
    core._check_meta()                    # 记下"库里有东西"
    assert core.WIPE_ALERT is None

    mem.unlink()                          # 模拟被清空
    (home / "persona.json").unlink()
    alert = core.check_wipe()
    assert alert is not None
    assert alert["prev_bytes"] > 0
    assert alert["now_bytes"] < alert["prev_bytes"]   # 只剩 config.json 这类骨架
    assert alert["persona_lost"] is True


def test_wipe_alert_silent_on_healthy_store():
    home = _fresh_home()
    (home / "hippocampus.jsonl").write_text('{"id": "m_1"}\n', encoding="utf-8")
    core.WIPE_ALERT = None
    assert core.check_wipe() is None
    assert core.check_wipe() is None       # 反复检查不误报


def test_wipe_alert_suggests_backup():
    home = _fresh_home()
    (home / "hippocampus.jsonl").write_text('{"id": "m_1"}\n', encoding="utf-8")
    core.WIPE_ALERT = None
    core._check_meta()
    bk = home / "backups"
    bk.mkdir(exist_ok=True)
    (bk / "brain-memory_20260101_000000.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (home / "hippocampus.jsonl").unlink()
    (home / "persona.json").unlink()      # 人格没了 = 最典型的被清空信号
    alert = core.check_wipe()
    assert alert and alert["backup"] and alert["backup"].endswith(".zip")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    try:
        for fn in fns:
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {fn.__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
