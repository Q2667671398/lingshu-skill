# -*- coding: utf-8 -*-
"""桌面快捷键「灵枢面板」（P11 #76）。

做法：在数据目录生成一个隐藏窗口的启动器（.vbs），再在桌面放一个 .lnk 指向它。
为什么用 .vbs 包一层：直接指向 python.exe 会弹出黑色控制台窗口，用户点桌面图标
不该看到命令行；pythonw + vbs 双层保证静默启动。

桌面路径优先读注册表（OneDrive 重定向时 USERPROFILE\\Desktop 可能不是真桌面）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import core

SHORTCUT_NAME = "灵枢面板"
LAUNCHER_NAME = "灵枢面板启动器.vbs"
ICON_NAME = "lingshu.ico"


def _pythonw() -> str:
    """优先用 pythonw.exe（无控制台窗口）。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.is_file() else exe)


def _desktop_dir() -> Path:
    """桌面真实路径：先读注册表 Shell Folders，再退回 USERPROFILE\\Desktop。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            val, _ = winreg.QueryValueEx(k, "Desktop")
            if val:
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
    except Exception:
        pass
    for name in ("Desktop", "桌面"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home() / "Desktop"


def _panel_script() -> Path:
    root = Path(__file__).resolve().parent
    return root / "panel_server.py"


def _icon_path() -> Path | None:
    """快捷方式图标：优先用**数据目录**里的 ASCII 路径副本。

    为什么不直接用 `assets/lingshu.ico`：程序目录常常含中文
    （如 `E:\\丁厚同\\文档\\程序\\灵枢-skill`），Explorer 有概率加载不了
    IconLocation，回退成 wscript.exe 的**白色脚本图标**（实测踩过）。
    所以把图标复制到 `~/.brain-memory/lingshu.ico`（纯 ASCII、稳定存在）再用。
    """
    src = Path(__file__).resolve().parent.parent / "assets" / ICON_NAME
    if not src.is_file():
        return None
    try:
        dst = Path(core.HOME) / ICON_NAME
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return dst
    except Exception:
        return src      # 复制失败就退回原路径，总比没有图标好


def _write_launcher(port: int | None = None, script: Path | None = None) -> Path:
    """在数据目录写一个静默启动器的 .vbs。

    `script` 可显式指定 panel_server.py（安装到全局技能目录后，快捷方式应指向
    那一份，而不是当前运行的开发副本）。
    """
    home = Path(core.HOME)
    home.mkdir(parents=True, exist_ok=True)
    launcher = home / LAUNCHER_NAME
    pyw = _pythonw()
    script = Path(script) if script else _panel_script()
    arg = f" --port {port}" if port else ""
    # VBS：0 = 隐藏窗口运行；路径含空格用双引号包住（VBS 里 "" 表示一个 "）。
    # ⚠ 必须以 **UTF-16LE + BOM** 写盘：wscript 默认按 ANSI 解析 .vbs，
    # 而本项目路径常含中文（如 E:\丁厚同\...），UTF-8 会被当成 GBK 曲解，
    # 于是 pythonw 收到一个不存在的路径、静默退出（实测踩过）。
    lines = [
        "' 由灵枢生成：静默启动可视化面板",
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.Run """{pyw}"" ""{script}""{arg}", 0, False',
    ]
    launcher.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16-le")
    # 手动补 BOM（pathlib 的 utf-16-le 不写 BOM）
    raw = launcher.read_bytes()
    if not raw.startswith(b"\xff\xfe"):
        launcher.write_bytes(b"\xff\xfe" + raw)
    return launcher


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _icon_ps() -> str:
    """快捷方式图标那一段 PowerShell（没图标文件就返回空串，保持原样）。"""
    ico = _icon_path()
    if not ico:
        return ""
    return f"$s.IconLocation = {_ps_quote(str(ico) + ',0')}; "


def create(name: str = SHORTCUT_NAME, port: int | None = None,
           script: Path | None = None) -> dict:
    """在桌面创建「灵枢面板」快捷方式；已存在则覆盖。返回结果字典。"""
    if os.name != "nt":
        return {"ok": False, "reason": "只支持 Windows"}
    desktop = _desktop_dir()
    if not desktop.is_dir():
        return {"ok": False, "reason": f"桌面目录不存在：{desktop}"}
    launcher = _write_launcher(port, script)
    lnk = desktop / f"{name}.lnk"
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({_ps_quote(str(lnk))}); "
        f"$s.TargetPath = 'wscript.exe'; "
        f"$s.Arguments = '\"{launcher}\"'; "
        f"$s.WorkingDirectory = {_ps_quote(str(Path(launcher).parent))}; "
        f"$s.Description = '启动灵枢可视化面板'; "
        + _icon_ps()
        + "$s.WindowStyle = 7; "
        "$s.Save(); "
        "Write-Output 'ok'"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"创建快捷方式失败：{exc}"}
    ok = lnk.is_file() and "ok" in (r.stdout or "")
    if ok:
        core.log_event("shortcut", f"桌面快捷方式已创建：{lnk}")
    return {"ok": ok, "path": str(lnk), "launcher": str(launcher),
            "desktop": str(desktop),
            "reason": "" if ok else (r.stderr or r.stdout or "未知失败")[:300]}


def remove(name: str = SHORTCUT_NAME) -> dict:
    """删除桌面快捷方式（启动器保留在数据目录，可再生成）。"""
    desktop = _desktop_dir()
    lnk = desktop / f"{name}.lnk"
    removed = False
    try:
        if lnk.is_file():
            lnk.unlink()
            removed = True
    except OSError as exc:
        return {"ok": False, "reason": f"删除失败：{exc}"}
    return {"ok": True, "removed": removed, "path": str(lnk)}


def ensure(name: str = SHORTCUT_NAME, script: Path | None = None) -> dict:
    """**幂等**：桌面没有才创建，已有就不动。

    给"安装完成后自动生成"用——重复安装不该反复覆盖用户的快捷方式
    （用户可能自己改过图标/位置）。受 config.desktop_shortcut 控制（默认开）。
    """
    try:
        if not bool(core.load_config().get("desktop_shortcut", True)):
            return {"ok": True, "skipped": True, "reason": "config.desktop_shortcut=false"}
    except Exception:
        pass
    if shortcut_exists(name):
        return {"ok": True, "skipped": True, "reason": "桌面已有快捷方式，未改动",
                "path": str(_desktop_dir() / f"{name}.lnk")}
    return create(name, script=script)


def shortcut_exists(name: str = SHORTCUT_NAME) -> bool:
    return (_desktop_dir() / f"{name}.lnk").is_file()
