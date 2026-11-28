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


def _looks_like_panel(port: int, timeout: float = 2.0) -> bool:
    """回退识别：`GET /` 取首页，判断是不是灵枢面板。

    用于**升级过渡期**——升级前起的面板进程没有 `/api/ping`（或被 token 闸挡住），
    只认 ping 就会把"自己的旧面板"误判成"别的程序占端口"并弹窗。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
    except Exception:
        return False
    if 'name="lingshu-panel"' in body:
        return True
    low = body.lower()
    return "<html" in low and "灵枢" in body


def _wait_for_app(port: int, tries: int = 8, gap: float = 0.35) -> dict | None:
    """带重试地探测：连点时另一个实例可能正在 bind，稍等就能看到它。"""
    for _ in range(max(1, tries)):
        info = _ping(port)
        if info and info.get("app") == "lingshu-panel":
            return info
        time.sleep(gap)
    # 兜底：老版本面板（没有 /api/ping）
    if _looks_like_panel(port):
        return {"ok": True, "app": "lingshu-panel", "legacy": True}
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


def _port_bindable(port: int) -> bool:
    """端口现在能不能被我们 bind（瞬时判断，不发任何 HTTP 请求）。

    为什么不用 `_ping` 来判断"有没有在跑"：本机实测——**连接一个没有监听的本地端口
    不是立刻 ECONNREFUSED，而是一直挂到超时**（安全软件/防火墙丢 SYN）。
    于是 `_ping` 每次都烧满 1.5s、`_looks_like_panel` 烧 2s，启动前那套
    "先探测三轮再起服务"白白花掉 **7.1 秒**（实测 8.3s 里的大头）。
    bind 探测是纯本地内核操作，几毫秒返回。

    注意**不设 SO_REUSEADDR**：Windows 上设了它可以让两个 socket 绑同一端口，
    那样探测就永远"成功"，等于没测。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main() -> int:
    port = _port()
    url = f"http://127.0.0.1:{port}"

    # 一次性、瞬时地判断端口状态；只有确实有人在听时，才去做（较贵的）HTTP 探测。
    if not _port_bindable(port):
        info = _wait_for_app(port, tries=8, gap=0.25)
        if info and info.get("app") == "lingshu-panel":
            # 已经在跑：只把浏览器叫到前面来，不起第二个进程
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

    # ---------------------------------------------------------------- 立刻给反馈
    # 从这里到浏览器出画面还有好几秒（进程创建 + 导入 + 绑端口 + 浏览器渲染），
    # 全程没有可见输出 → 用户以为没点上。所以**秒开一枚启动光晕**，服务就绪即关。
    # tkinter 跑在主线程，真正的服务启动放后台线程（Windows 上 tkinter 必须占主线程）。
    splash = None
    if os.environ.get("LINGSHU_NO_SPLASH", "").strip() not in ("1", "true", "yes"):
        try:
            import panel_splash
            if panel_splash.available():
                splash = panel_splash.Splash("正在唤醒记忆…")
        except Exception:
            splash = None                   # 拿不到就不装，退化成原来的静默启动

    import threading
    state = {"ready": False, "err": None, "serving": False}

    def _boot():
        """后台线程：起服务。成功后会一直阻塞在 serve_forever（预期行为）。"""
        try:
            import panel_server
            panel_server.serve(port, open_browser=False)   # 浏览器由主线程开，顺序可控
            state["serving"] = True
        except (SystemExit, OSError) as exc:
            state["err"] = exc
        except Exception as exc:                            # noqa: BLE001
            state["err"] = exc

    if splash is not None:
        th = threading.Thread(target=_boot, name="panel-boot", daemon=True)
        th.start()

        def _watch():
            """轮询：服务通了就关光晕 + 开浏览器。"""
            if state["err"] is not None:
                splash.close()
                return
            info2 = _ping(port, timeout=0.6)
            if info2 and info2.get("app") == "lingshu-panel":
                splash.set_text("已就绪 · 正在打开面板…")
                splash.close()
                _open_browser(url)
                return
            if splash.root is not None and not splash._closed:
                splash.root.after(140, _watch)

        # 超时兜底：9 秒还没通就换文案，17 秒仍未通就关窗——不许永久挂在屏幕上
        def _slow_note():
            if not splash._closed:
                splash.set_text("启动比预期慢，仍在尝试…")

        def _hard_stop():
            splash.close()

        if splash.root is not None:
            splash.root.after(9000, _slow_note)
            splash.root.after(17000, _hard_stop)
            splash.root.after(140, _watch)
        splash.run()                    # 主线程跑界面直到 close()

        # 光晕关掉后：服务线程若已失败，按原来的方式报错（不能静默退出）
        if state["err"] is not None:
            exc = state["err"]
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
        # 正常路径：服务在后台线程里继续跑，主线程到此结束（daemon 线程随之结束前
        # 需要保持进程活着——所以这里改为阻塞等待该线程）。
        th.join()
        return 0

    # ---- 没有 tkinter：退回原来的同步启动（行为与旧版一致）----
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
