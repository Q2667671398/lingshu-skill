# -*- coding: utf-8 -*-
"""灵枢面板启动器：单实例 + 已在跑就只开浏览器 + 出错有可见提示。

解决两个真实困扰（用户反馈）：
  1. 再点一次快捷方式 → 端口被占 → 隐藏的 pythonw 里 raise SystemExit(1)，
     **用户什么都看不到**，还以为没反应；
  2. 关了网页后服务仍在后台跑，只能去任务管理器杀。

行为：
  · 先探测 `GET /api/ping`：
      - 已是我们 → **只打开浏览器**，退出 0（绝不起第二个进程）；
      - 端口有响应但不是灵枢 → 弹窗说清"端口被别的程序占用了"，退出 1；
      - 没响应 → 起服务（serve 里带空闲自退）。
  · 隐藏启动（pythonw）没有控制台，所以出错用 Windows MessageBox 弹出来——
    用 ctypes 调 user32，**零依赖**。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402,F401  ← 合并命名空间
import core  # noqa: E402


def _msgbox(text: str, title: str = "灵枢面板", error: bool = True) -> None:
    """隐藏进程里没有控制台，用系统弹窗把话说出去（ctypes 零依赖）。"""
    try:
        import ctypes
        icon = 0x10 if error else 0x40          # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), icon)
    except Exception:
        # 非 Windows / 弹窗失败：至少写 stderr（有控制台时能看到）
        print(f"[{title}] {text}", file=sys.stderr)


def _port() -> int:
    """与 panel_server 同口径：config.panel_port，退回 8765。"""
    try:
        p = int(core.load_config().get("panel_port") or 8765)
        return p if 1 <= p <= 65535 else 8765
    except Exception:
        return 8765


def _ping(port: int, timeout: float = 1.5) -> dict | None:
    """探测本机面板。返回 {ok:True,...} 表示是灵枢；None 表示没响应。"""
    url = f"http://127.0.0.1:{port}/api/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("app") == "lingshu-panel":
            return data
        return {"ok": True, "app": str(data.get("app") or "unknown")}
    except Exception:
        return None


def _port_occupied(port: int) -> bool:
    """端口有没有人在听（不关心是谁）。"""
    import socket
    s = socket.socket()
    s.settimeout(0.8)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main() -> int:
    port = _port()
    url = f"http://127.0.0.1:{port}"

    info = _ping(port)
    if info and info.get("app") == "lingshu-panel":
        # 已经在跑：只把浏览器叫到前面来，不起第二个进程
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return 0
    if _port_occupied(port):
        _msgbox(
            f"端口 {port} 已被别的程序占用，面板起不来。\n\n"
            f"两种办法：\n"
            f"  1) 关掉占用该端口的程序；\n"
            f"  2) 用 brain.py config set panel_port 8766 换个端口。",
            "灵枢面板 · 端口被占用")
        return 1

    # 起服务（serve 内部带空闲自退 + 打开浏览器）
    try:
        import panel_server
        panel_server.serve(port, open_browser=True)
        return 0
    except SystemExit as exc:                    # serve 里端口占用会 SystemExit
        _msgbox(f"面板启动失败：端口 {port} 不可用。\n"
                f"换个端口：brain.py config set panel_port 8766",
                "灵枢面板 · 启动失败")
        return int(getattr(exc, "code", 1) or 1)
    except Exception as exc:                     # noqa: BLE001
        _msgbox(f"面板启动失败：\n{type(exc).__name__}: {exc}", "灵枢面板 · 启动失败")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
