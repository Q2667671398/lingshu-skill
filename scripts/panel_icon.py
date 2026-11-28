# -*- coding: utf-8 -*-
"""自定义桌面快捷方式图标：选图 → 转多尺寸 .ico → 写进 .lnk。

调研结论（为什么这么做）：

1. **.lnk 存的不是图标本身，而是一个「IconLocation」字符串**
   （`C:\\path\\x.ico,0` 或 `C:\\path\\app.exe,3`）。所以换图标 = 生成一个 .ico 文件 +
   改这一个字段。不能把图片直接塞进 .lnk。

2. **.ico 必须是多尺寸的**。Windows 在不同位置取不同尺寸：任务栏 32、桌面大图标 48/
   96、Alt+Tab 与高分屏 256。只放一张 256 的话，小尺寸会被临时缩放，糊成一团。
   灵枢原来那个 lingshu.ico 就是 7 档（16/24/32/48/64/128/256），这里对齐它。

3. **改完要让资源管理器重新读**。Explorer 会缓存图标，光改 .lnk 常常「没反应」。
   标准做法是 `SHChangeNotify(SHCNE_ASSOCCHANGED)` + 通知 shell 刷新；
   更狠的是删 IconCache.db（要重启 explorer，代价大，不做）。

4. **图片转 ico 用 Pillow**（本机 12.3 可用）。没有 Pillow 时降级：只接受已经是
   .ico 的文件，并如实告诉用户「要 PNG 转 ICO 需要 Pillow」。

安全边界沿用 panel_shortcut 的那套：只写 IconLocation，写前备份 .lnk，
写后逐字段核对其它字段没被破坏，坏了立刻还原。
"""
from __future__ import annotations

import base64
import io
import subprocess
from pathlib import Path

# 与 lingshu.ico 对齐的尺寸档位
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# 支持的输入格式
INPUT_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".ico")


class IconError(RuntimeError):
    """图标相关操作失败。"""


def _data_dir() -> Path:
    d = Path.home() / ".brain-memory"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def default_icon() -> Path:
    return _data_dir() / "lingshu.ico"


def custom_icon() -> Path:
    """用户自定义图标的落地位置（固定名，方便来回替换且不堆垃圾）。"""
    return _data_dir() / "lingshu-custom.ico"


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 选文件（原生对话框）

_PS_PICK = r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = '选择图标图片（建议正方形，≥256×256）'
$d.Filter = '图片或图标|*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif;*.ico|所有文件|*.*'
$d.CheckFileExists = $true
$d.Multiselect = $false
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  Write-Output $d.FileName
}
"""


def pick_image_file() -> str | None:
    """弹出 Windows 原生文件选择框，返回选中的路径（取消则 None）。

    为什么由服务端弹框而不是网页 `<input type=file>`：
    面板跑在本机 `127.0.0.1`，浏览器出于安全**不会**把真实路径交给页面。
    而写 .lnk 需要真实路径，所以这个框必须在服务端弹。
    `-STA` 是 WinForms 对话框的硬要求。
    """
    blob_src = _PS_PICK
    blob = base64.b64encode(blob_src.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", blob],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,                      # 用户可能慢慢挑，给足时间
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise IconError("选择文件超时（3 分钟）") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise IconError(f"打开文件选择框失败：{exc}") from exc
    out = (r.stdout or "").strip()
    return out or None


# ---------------------------------------------------------------- 图片 → 多尺寸 ico

def to_ico(src: str | Path, dst: str | Path | None = None) -> Path:
    """把图片转成多尺寸 .ico。已经是 .ico 且含足够尺寸时直接复制。"""
    src = Path(src)
    if not src.is_file():
        raise IconError(f"文件不存在：{src}")
    if src.suffix.lower() not in INPUT_EXTS:
        raise IconError(f"不支持的文件类型「{src.suffix}」，"
                        f"可用：{'、'.join(INPUT_EXTS)}")
    out = Path(dst) if dst else custom_icon()
    out.parent.mkdir(parents=True, exist_ok=True)

    # 已经是 .ico：直接拿来用（但它可能只有单尺寸，下面会检查）
    if src.suffix.lower() == ".ico":
        try:
            sizes = _ico_sizes(src)
        except Exception:
            sizes = []
        if len(sizes) >= 4:
            out.write_bytes(src.read_bytes())
            return out
        # 尺寸太少 → 用 Pillow 重新打包（借它的第一帧当源）
        if not pillow_available():
            out.write_bytes(src.read_bytes())
            return out

    if not pillow_available():
        raise IconError("要把 PNG/JPG 转成图标需要 Pillow（pip install pillow）；"
                        "或者你直接准备一个 .ico 文件再选。")
    from PIL import Image
    try:
        im = Image.open(src)
        im.load()
    except Exception as exc:
        raise IconError(f"读不了这张图：{exc}") from exc

    # 统一成正方形 RGBA：非正方形先裁中心，避免被拉变形
    im = im.convert("RGBA")
    w, h = im.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
    if im.size[0] < 256:
        # 小图直接放大到 256，让最大档不虚（放大会略软，但比"没有大图标"好）
        im = im.resize((256, 256), Image.LANCZOS)
    try:
        im.save(out, format="ICO", sizes=ICO_SIZES)
    except Exception as exc:
        raise IconError(f"生成 .ico 失败：{exc}") from exc
    if not out.is_file() or out.stat().st_size == 0:
        raise IconError("生成的 .ico 是空的，请换一张图片试试")
    return out


def _ico_sizes(p: Path) -> list[tuple[int, int]]:
    """读 .ico 里有哪些尺寸（纯标准库，不依赖 Pillow 做判断）。"""
    import struct
    b = p.read_bytes()
    if len(b) < 6:
        return []
    _res, typ, n = struct.unpack("<HHH", b[:6])
    if typ != 1:
        return []
    out = []
    for i in range(n):
        off = 6 + i * 16
        if off + 16 > len(b):
            break
        w, h = struct.unpack("<BB", b[off:off + 2])
        out.append((w or 256, h or 256))
    return out


def ico_info(p: str | Path) -> dict:
    p = Path(p)
    if not p.is_file():
        return {"exists": False}
    try:
        sizes = _ico_sizes(p)
    except Exception:
        sizes = []
    return {
        "exists": True,
        "path": str(p),
        "bytes": p.stat().st_size,
        "sizes": sorted({s[0] for s in sizes}),
    }


def preview_data_url(p: str | Path, size: int = 64) -> str:
    """把 .ico 转成 PNG data URL，供网页 `<img>` 直接显示（无需额外鉴权）。

    网页 `<img src>` 带不了 Authorization 头，所以走 data URL 最省事。
    """
    p = Path(p)
    if not p.is_file():
        return ""
    if not pillow_available():
        return ""
    from PIL import Image
    try:
        im = Image.open(p)
        im = im.convert("RGBA")
        im = im.copy()
        im.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""
