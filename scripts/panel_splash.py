# -*- coding: utf-8 -*-
"""灵枢启动光晕：点开快捷方式后**立刻**出现的加载态。

为什么要有（用户反馈）：快捷方式走 wscript → pythonw（隐藏进程），从点击到浏览器
出画面这几秒里屏幕上什么都没发生，用户以为"点了没作用"，会连点好几次。

设计取向「符合灵枢」：不是一个转圈的进度条，而是一枚**呼吸的星核**——三层同心环
以不同速率明灭，中心一颗暖金色 orb，下面一行小字说明现在在做什么。
和面板里的星图、人格星核是同一套视觉语言。

实现约束：
  · 只用标准库 tkinter（零依赖），**拿不到就静默跳过**，绝不让启动失败；
  · 无边框 + 置顶 + 居中，尺寸小（400×190），不抢焦点；
  · 主线程跑 tkinter 事件循环，真实工作在别的线程做（Windows 上 tkinter 必须在
    主线程才能稳定响应，所以不能反过来）。
"""
from __future__ import annotations

import tkinter as tk

# 与面板同一套色：--bg #0a0c0f / --amber #d9a441 / --faint #5d646e
BG = "#0a0c0f"
AMBER = "#d9a441"
AMBER_DIM = "#8a6423"
TEXT = "#e9e6df"
FAINT = "#5d646e"

W, H = 400, 190


def _center(win: tk.Tk) -> None:
    win.update_idletasks()
    try:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - W) // 2
        y = (sh - H) // 2 - 40          # 略微偏上，视觉重心更舒服
        win.geometry(f"{W}x{H}+{x}+{y}")
    except Exception:
        win.geometry(f"{W}x{H}")


class Splash:
    """三环呼吸 + 中心星核的可关闭加载窗。

    用法：
        sp = Splash("正在唤醒记忆…")
        sp.start()          # 非阻塞，装上事件循环的定时器
        ...做正事...
        sp.set_text("正在打开面板…")
        sp.close()
        sp.run()            # 真正驱动界面（阻塞到 close 之后）
    """

    def __init__(self, text: str = "正在唤醒记忆…", sub: str = "灵枢 · 类脑记忆"):
        self._closed = False
        self._phase = 0.0
        self._text = text
        self._sub = sub
        self.root = None
        self.canvas = None
        try:
            self.root = tk.Tk()
            # 先隐藏再搭界面：Tk 建窗后默认是系统白底，若先显示再画布上色，
            # 会闪一下白框（实测用户会以为"白屏/没反应"）。先 withdraw →
            # 全部画好 → update → 再显示，观感是"直接出现一枚黑底光晕"。
            self.root.withdraw()
            self.root.overrideredirect(True)          # 无边框
            self.root.attributes("-topmost", True)
            try:
                self.root.attributes("-alpha", 0.0)   # 显示瞬间也不透明白闪
            except Exception:
                pass
            self.root.configure(bg=BG)
            _center(self.root)
            self.canvas = tk.Canvas(self.root, width=W, height=H, bg=BG,
                                    highlightthickness=0, bd=0)
            self.canvas.pack(fill="both", expand=True)
            self._draw_static()
            self._animate()
            # 强制先把这一帧画完，再露出来
            self.root.update_idletasks()
            self.root.update()
            self.root.deiconify()
            try:
                self.root.attributes("-alpha", 0.97)
            except Exception:
                pass
        except Exception:
            # 没有 tkinter / 没有显示环境：静默降级，绝不阻断启动
            self.root = None

    # ---------------------------------------------------------------- 绘制
    def _draw_static(self) -> None:
        c = self.canvas
        c.create_text(W // 2, 44, text="灵 枢", fill=AMBER,
                      font=("Microsoft YaHei UI", 19, "bold"))
        c.create_text(W // 2, 68, text=self._sub, fill=FAINT,
                      font=("Microsoft YaHei UI", 9))
        self.txt = c.create_text(W // 2, 152, text=self._text, fill=TEXT,
                                 font=("Microsoft YaHei UI", 10))
        # 三个环的 id 留着按相位改颜色/宽度
        self.rings = [c.create_oval(0, 0, 0, 0, outline=AMBER_DIM, width=1)
                      for _ in range(3)]
        self.core = c.create_oval(0, 0, 0, 0, fill=AMBER, outline="")

    def _animate(self) -> None:
        if self._closed or self.canvas is None:
            return
        import math
        c = self.canvas
        cx, cy = W // 2, 108
        self._phase += 0.07
        # 星核呼吸：半径在 5~7 之间起伏
        cr = 5.0 + 1.6 * math.sin(self._phase * 1.6)
        c.coords(self.core, cx - cr, cy - cr, cx + cr, cy + cr)
        # 三环：相位错开 2π/3，越靠外的环越淡、扩张越慢
        for i, rid in enumerate(self.rings):
            ph = self._phase * 0.9 - i * (2 * math.pi / 3)
            t = (math.sin(ph) + 1) / 2                  # 0..1
            r = 13 + i * 11 + t * 5
            c.coords(rid, cx - r, cy - r, cx + r, cy + r)
            # 亮度和线宽随呼吸变化：外环最淡，形成层次
            base = (0.95, 0.6, 0.35)[i]
            a = base * (0.25 + 0.75 * t)
            c.itemconfigure(rid, outline=self._blend(AMBER, BG, a),
                            width=1 if i else 2)
        self.root.after(45, self._animate)

    @staticmethod
    def _blend(fg: str, bg: str, a: float) -> str:
        """把 fg 按 alpha 混到 bg 上（tkinter 不支持 rgba，只能算成实色）。"""
        def rgb(h):
            h = h.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        f, b = rgb(fg), rgb(bg)
        out = tuple(round(b[i] + (f[i] - b[i]) * max(0.0, min(1.0, a))) for i in range(3))
        return "#%02x%02x%02x" % out

    # ---------------------------------------------------------------- 外部控制
    def set_text(self, text: str) -> None:
        """换一句状态说明（例如"面板已就绪，正在打开浏览器…"）。"""
        self._text = text
        try:
            if self.canvas is not None:
                self.canvas.itemconfigure(self.txt, text=text)
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        try:
            if self.root is not None:
                self.root.after(0, self._destroy)
        except Exception:
            pass

    def _destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        """跑界面事件循环，直到 close() 把窗口销毁。没有 tk 时立即返回。"""
        if self.root is None:
            return
        try:
            self.root.mainloop()
        except Exception:
            pass


def available() -> bool:
    """tkinter 能不能用（拿不到就不装加载窗，静默退化成原来的行为）。"""
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False
