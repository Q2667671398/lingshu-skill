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
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402,F401  ← 合并命名空间
import core  # noqa: E402


def _msgbox(text: str, title: str = "灵枢面板", error: bool = True) -> None:
    """隐藏进程里没有控制台，用系统弹窗把话说出去（ctypes 零依赖）。

    `LINGSHU_NO_MSGBOX=1` 时只写 stderr——自动化/测试里弹窗会**阻塞等点击**，
    把进程永久挂住。
    """
    if os.environ.get("LINGSHU_NO_MSGBOX", "").strip() in ("1", "true", "yes"):
        print(f"[{title}] {text}", file=sys.stderr)
        return
    try:
        import ctypes
        icon = 0x10 if error else 0x40          # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), icon)
    except Exception:
        # 非 Windows / 弹窗失败：至少写 stderr（有控制台时能看到）
        print(f"[{title}] {text}", file=sys.stderr)


def _open_browser(url: str, timeout: float = 3.0) -> None:
    """打开浏览器，但**绝不拖住进程**。

    实测：没有默认浏览器关联时，`webbrowser.open()` 会一直阻塞——
    启动器就永久留在进程表里（用户看到的是"点了没反应、又多了个进程"）。
    所以放 daemon 线程、最多等 `timeout` 秒，然后照常退出。
    """
    import threading
    done = threading.Event()

    def _go():
        try:
            webbrowser.open(url)
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_go, name="open-browser", daemon=True).start()
    done.wait(timeout)


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


def _wait_for_app(port: int, tries: int = 8, gap: float = 0.35) -> dict | None:
    """带重试地探测：连点时另一个实例可能正在 bind，稍等就能看到它。"""
    for _ in range(max(1, tries)):
        info = _ping(port)
        if info and info.get("app") == "lingshu-panel":
            return info
        time.sleep(gap)
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

    info = _wait_for_app(port, tries=3, gap=0.2)
    if info and info.get("app") == "lingshu-panel":
        # 已经在跑：只把浏览器叫到前面来，不起第二个进程
        _open_browser(url)
        return 0
    if _port_occupied(port):
        # 竞态：连点时端口可能刚被**同伴**抢到，而我们第一轮探测太早没看到。
        # 先多等一会儿再看——只有确认不是我们自己的面板，才报"被别的程序占用"。
        again = _wait_for_app(port, tries=12, gap=0.3)
        if again and again.get("app") == "lingshu-panel":
            _open_browser(url)
            return 0
        _msgbox(
            f"端口 {port} 已被别的程序占用，面板起不来。\n\n"
            f"两种办法：\n"
            f"  1) 关掉占用该端口的程序；\n"
            f"  2) 用 brain.py config set panel_port 8766 换个端口。",
            "灵枢面板 · 端口被占用")
        return 1

    # 起服务（serve 内部带空闲自退 + 打开浏览器）
    # 竞态下 bind 失败是预期行为（另一个同伴刚抢到），让 serve 别往 stderr 吐假警报。
    os.environ["LINGSHU_QUIET_PORT"] = "1"
    try:
        import panel_server
        panel_server.serve(port, open_browser=True)
        return 0
    except (SystemExit, OSError) as exc:
        # **竞态**：连点两次时两边都会 bind 失败一次。先别急着报错——
        # 带重试再探一下，如果这会儿是我们自己的面板在服务，就静默开浏览器退出。
        # （直接弹窗会让用户看到假的"启动失败"，而且 MessageBox 会阻塞进程。）
        again = _wait_for_app(port, tries=10, gap=0.35)
        if again and again.get("app") == "lingshu-panel":
            _open_browser(url)
            return 0
        if _port_occupied(port):
            _msgbox(
                f"端口 {port} 已被别的程序占用，面板起不来。\n\n"
                f"两种办法：\n"
                f"  1) 关掉占用该端口的程序；\n"
                f"  2) 用 brain.py config set panel_port 8766 换个端口。",
                "灵枢面板 · 端口被占用")
            return 1
        _msgbox(f"面板启动失败：\n{type(exc).__name__}: {exc}", "灵枢面板 · 启动失败")
        return int(getattr(exc, "code", 1) or 2)
    except Exception as exc:                     # noqa: BLE001
        _msgbox(f"面板启动失败：\n{type(exc).__name__}: {exc}", "灵枢面板 · 启动失败")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
