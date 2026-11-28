# -*- coding: utf-8 -*-
"""面板快捷方式的全局快捷键：读 / 写 `灵枢面板.lnk` 的 Hotkey 字段。

Windows 的 .lnk 自带一个「快捷键」字段，写上 `CTRL+ALT+L` 这类组合，系统就会在
**任何程序里**都响应它——这正是用户想要的"能自定义的快捷键"，不需要常驻热键服务、
不需要第三方库。

为什么用 PowerShell 子进程而不是 pywin32：
  · 灵枢是零依赖技能，不能要求装 pywin32；
  · `WScript.Shell` 的 `CreateShortcut()` 是 Windows 自带的 COM，PowerShell 一行就能调。

安全边界：只写 **Hotkey** 与 **IconLocation** 两个字段，其余（TargetPath/Arguments/
WorkingDirectory）原样保留——改错一个字段就会让快捷方式指向别处。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# .lnk 里允许的按键写法：CTRL/ALT/SHIFT/WIN + 一个按键
_MODS = ("CTRL", "ALT", "SHIFT", "WIN", "EXT")
_KEYS = (
    [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + [str(d) for d in range(10)]
    + [f"F{i}" for i in range(1, 25)]
    + ["INSERT", "DELETE", "HOME", "END", "PGUP", "PGDN",
       "LEFT", "RIGHT", "UP", "DOWN", "TAB", "SPACE", "ENTER", "BACK", "ESC"]
)
_ALIAS = {"CONTROL": "CTRL", "CTL": "CTRL", "WINKEY": "WIN", "WINDOWS": "WIN",
          "DEL": "DELETE", "INS": "INSERT", "ESCAPE": "ESC", "RETURN": "ENTER"}


class ShortcutError(RuntimeError):
    """快捷方式读写失败（找不到 .lnk / PowerShell 不可用 / 格式不对）。"""


def normalize_hotkey(raw: str) -> str:
    """把用户写法归一成 .lnk 认识的 `CTRL+ALT+L`。

    接受 `ctrl+alt+l` / `Ctrl + Alt + L` / `control-alt-l` 等写法；
    空字符串表示"清除快捷键"（合法）。
    """
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    parts = [p for p in re.split(r"[+\-]", s) if p]
    if not parts:
        return ""
    mods, keys = [], []
    for p in parts:
        p = _ALIAS.get(p, p)
        if p in _MODS:
            if p not in mods:
                mods.append(p)
        elif p in _KEYS:
            keys.append(p)
        else:
            raise ShortcutError(
                f"不认识的按键「{p}」。可用：CTRL / ALT / SHIFT / WIN + 一个字母、"
                f"数字或 F1–F24（例如 CTRL+ALT+L）")
    if len(keys) != 1:
        raise ShortcutError("需要且只能有一个主键，例如 CTRL+ALT+L")
    if not mods:
        raise ShortcutError("至少要有一个修饰键（CTRL / ALT / SHIFT / WIN），"
                            "否则会抢掉普通打字")
    # .lnk 要求固定顺序：CTRL、ALT、SHIFT、WIN
    order = [m for m in ("CTRL", "ALT", "SHIFT", "WIN", "EXT") if m in mods]
    return "+".join(order + keys)


def find_shortcut() -> Path:
    """按优先级找出面板快捷方式。"""
    home = Path.home()
    for p in (home / "Desktop" / "灵枢面板.lnk",
              Path(r"C:\Users\Public\Desktop") / "灵枢面板.lnk",
              home / "Desktop" / "lingshu-panel.lnk"):
        if p.is_file():
            return p
    raise ShortcutError("找不到桌面上的「灵枢面板.lnk」。"
                        "请先在面板「诊断 → 数据目录」旁点「创建桌面快捷方式」。")


_PS_READ = r"""
$ErrorActionPreference='Stop'
# 强制 UTF-8 输出：中文路径（灵枢面板启动器.vbs）否则会按 GBK 吐出来，
# Python 侧拿到乱码字符串，完整性比对（逐字段相等）就会假报"被改动"。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$sh = New-Object -ComObject WScript.Shell
$l = $sh.CreateShortcut('{lnk}')
$o = [ordered]@{{
  target=$l.TargetPath; args=$l.Arguments; wd=$l.WorkingDirectory;
  icon=$l.IconLocation; hotkey=$l.Hotkey; style=$l.WindowStyle
}}
$o | ConvertTo-Json -Compress
"""

_PS_WRITE = r"""
$ErrorActionPreference='Stop'
$sh = New-Object -ComObject WScript.Shell
$l = $sh.CreateShortcut('{lnk}')
$l.Hotkey = '{hotkey}'
$l.Save()
Write-Output 'saved'
"""

_PS_REPAIR = r"""
$ErrorActionPreference='Stop'
$sh = New-Object -ComObject WScript.Shell
$l = $sh.CreateShortcut('{lnk}')
$l.TargetPath = 'C:\WINDOWS\system32\wscript.exe'
$l.Arguments = '"{vbs}"'
$l.WorkingDirectory = '{wd}'
$l.IconLocation = '{icon}'
$l.Description = '{desc}'
$l.WindowStyle = 7
$l.Save()
Write-Output 'repaired'
"""

# 改图标 + 让资源管理器立刻重读。
# Explorer 会缓存图标：只写 .lnk 经常「没变化」，所以补一次 SHChangeNotify
# （SHCNE_ASSOCCHANGED / SHCNF_IDLIST），这是不重启 explorer 就能刷新的标准做法。
_PS_SET_ICON = r"""
$ErrorActionPreference='Stop'
$sh = New-Object -ComObject WScript.Shell
$l = $sh.CreateShortcut('{lnk}')
$l.IconLocation = '{icon}'
$l.Save()
try {{
  Add-Type -Namespace W32 -Name U -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr a, System.IntPtr b);
'@ -ErrorAction Stop | Out-Null
  [W32.U]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
}} catch {{ }}
Write-Output 'ok'
"""


def _run_ps(script: str, timeout: int = 60) -> str:
    """跑一段 PowerShell。

    为什么用 `-EncodedCommand`（base64/UTF-16LE）而不是 `-Command`：
    脚本里带**中文路径**（`灵枢面板启动器.vbs`、`灵枢面板.lnk`）。`-Command` 走命令行
    编码，中文容易在中途被按 ANSI 解释，`CreateShortcut()` 于是拿到一个错的路径、
    建出一个**空 shortcut 对象**，`Save()` 一写就把原来的字段全冲掉——实测就是这么
    把用户的桌面快捷方式清空的（图标变白、点了没反应）。EncodedCommand 是二进制安全的，
    彻底绕开这一层。

    `timeout` 给到 60s：首次 `New-Object -ComObject WScript.Shell` 冷启动实测能到 30s。
    超时**不做强杀**——强杀会把 `Save()` 卡在半途，留下写坏的 .lnk。
    """
    import base64
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", blob],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ShortcutError(
            f"PowerShell 超时（{timeout}s）。快捷方式可能已被改动，"
            f"请点「刷新」确认；若目标/图标丢了，点「修复快捷方式」还原。") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShortcutError(f"调用 PowerShell 失败：{exc}") from exc
    if r.returncode != 0:
        raise ShortcutError((r.stderr or r.stdout or "").strip()[:300] or "PowerShell 失败")
    return (r.stdout or "").strip()


def _same_hotkey(a: str, b: str) -> bool:
    """两个热键写法是否等价（**顺序无关**）。

    Windows 会把 `CTRL+ALT+SHIFT+F12` 存成 `Alt+Ctrl+Shift+F12`——直接比字符串
    会永远判"写入未生效"（实测踩过）。按 `+` 拆成集合比大小写无关的 token。
    """
    def toks(s):
        return {t for t in (s or "").upper().replace(" ", "").split("+") if t}
    return toks(a) == toks(b)


# 快捷方式必须完好的字段。只写 Hotkey 时这几个**一个字都不许变**——
# 变了就说明 Save() 写坏了一个空对象（真实事故：桌面图标变白、点了没反应）。
_INTEGRITY_FIELDS = ("target", "args", "wd", "icon")


def _backup_dir() -> Path:
    """快捷方式备份放数据目录下：更新技能不会覆盖它，也不会脏了桌面。"""
    d = Path.home() / ".brain-memory" / "shortcut-backup"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _backup(lnk: Path) -> Path | None:
    try:
        dst = _backup_dir() / (lnk.stem + ".lnk.bak")
        dst.write_bytes(lnk.read_bytes())
        return dst
    except OSError:
        return None


def _restore_from(backup: Path, lnk: Path) -> bool:
    try:
        lnk.write_bytes(backup.read_bytes())
        return True
    except OSError:
        return False


def read_shortcut(lnk: Path | None = None) -> dict:
    """读取快捷方式的全部关键字段（含当前 Hotkey）。"""
    lk = lnk or find_shortcut()
    raw = _run_ps(_PS_READ.format(lnk=str(lk).replace("'", "''")))
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    data["path"] = str(lk)
    data["hotkey"] = (data.get("hotkey") or "").strip()
    # 完整性：四个关键字段都在才算"没被写坏"
    data["intact"] = all(str(data.get(k) or "").strip() for k in _INTEGRITY_FIELDS)
    return data


def set_hotkey(hotkey: str, lnk: Path | None = None) -> dict:
    """设置（或清除）全局快捷键，**并保证不损坏快捷方式本身**。

    三步防线（都是这次真实事故换来的）：
      1. 写之前把 .lnk 原样备份到数据目录；
      2. 写完之后回读，逐个核对 TargetPath / Arguments / WorkingDirectory / IconLocation
         有没有变——只改 Hotkey 时它们一个字都不该动；
      3. 一旦发现丢了字段，**立刻用备份还原**并把错误报出去，绝不留下一个空白快捷方式。
    """
    norm = normalize_hotkey(hotkey)
    lk = lnk or find_shortcut()
    before = read_shortcut(lk)
    if not before.get("intact"):
        raise ShortcutError(
            "当前快捷方式本身已经不完整（目标/参数/图标有缺失），先点「修复快捷方式」再说，"
            "否则改热键只会把它写得更坏。")
    bak = _backup(lk)
    _run_ps(_PS_WRITE.format(lnk=str(lk).replace("'", "''"), hotkey=norm))
    after = read_shortcut(lk)

    # —— 完整性核对 ——
    broken = [k for k in _INTEGRITY_FIELDS
              if str(before.get(k) or "") != str(after.get(k) or "")]
    if broken:
        restored = _restore_from(bak, lk) if bak else False
        brain_note = ("已用备份还原" if restored else "备份还原失败，请手动修复")
        raise ShortcutError(
            f"写入热键时破坏了快捷方式（字段变化：{'、'.join(broken)}）。{brain_note}。")

    applied = (after.get("hotkey") or "").strip()
    return {
        "ok": True,
        "requested": norm,
        "applied": applied,
        "before": before.get("hotkey", ""),
        "path": str(lk),
        "intact": after.get("intact", False),
        "backup": str(bak) if bak else "",
        # 写不进 Hotkey 时 Windows 会给回空串——如实报告，别假装成功。
        "verified": (not norm and not applied) or _same_hotkey(norm, applied),
    }


def set_icon_location(icon_value: str, lnk: Path | None = None) -> dict:
    """只改 `.lnk` 的 IconLocation，其余字段一个字不动。

    `icon_value` 形如 `C:\\path\\x.ico,0`（.ico 用 0；.exe/.dll 用资源序号）。
    沿用 set_hotkey 的三步防线：备份 → 写 → 逐字段核对 → 坏了自动还原。
    """
    lk = lnk or find_shortcut()
    before = read_shortcut(lk)
    if not before.get("intact"):
        raise ShortcutError(
            "当前快捷方式本身已经不完整（目标/参数/图标有缺失），先点「修复快捷方式」再说。")
    bak = _backup(lk)
    _run_ps(_PS_SET_ICON.format(lnk=str(lk).replace("'", "''"),
                                icon=icon_value.replace("'", "''")))
    after = read_shortcut(lk)
    # 只有 icon 允许变，target/args/wd 必须原样
    broken = [k for k in ("target", "args", "wd")
              if str(before.get(k) or "") != str(after.get(k) or "")]
    if broken:
        restored = _restore_from(bak, lk) if bak else False
        raise ShortcutError(
            f"写图标时破坏了快捷方式（字段变化：{'、'.join(broken)}）。"
            f"{'已用备份还原' if restored else '备份还原失败，请点「修复快捷方式」'}。")
    return {
        "ok": True,
        "icon": after.get("icon", ""),
        "previous": before.get("icon", ""),
        "path": str(lk),
        "intact": after.get("intact", False),
        "backup": str(bak) if bak else "",
    }


def repair_shortcut(lnk: Path | None = None) -> dict:
    """把被写坏的快捷方式重建回来（目标/参数/图标/窗口样式按灵枢的标准值）。

    真实事故：热键写入把 .lnk 清空 → 桌面图标变白、双击没反应。
    这里的值来自灵枢自己生成的启动器约定（wscript + 启动器.vbs + lingshu.ico）。
    """
    lk = lnk or (Path.home() / "Desktop" / "灵枢面板.lnk")
    home = Path.home()
    vbs = home / ".brain-memory" / "灵枢面板启动器.vbs"
    ico = home / ".brain-memory" / "lingshu.ico"
    if not vbs.is_file():
        raise ShortcutError(f"启动器不存在：{vbs}（面板「诊断」页可重新生成）")
    script = _PS_REPAIR.format(
        lnk=str(lk).replace("'", "''"),
        vbs=str(vbs).replace("'", "''"),
        wd=str(home / ".brain-memory").replace("'", "''"),
        icon=(str(ico).replace("'", "''") + ",0") if ico.is_file() else "",
        desc="灵枢 · 类脑记忆面板",
    )
    _run_ps(script)
    after = read_shortcut(lk)
    return {"ok": bool(after.get("intact")), **after,
            "vbs_exists": vbs.is_file(), "icon_exists": ico.is_file()}
