# -*- coding: utf-8 -*-
"""生成「灵枢面板」图标（多尺寸 .ico + 一张预览 png）。

设计：灵枢 = 类脑记忆的"枢"——中心一个琥珀色核，四周神经节点用弧线相连，
在深色圆角底上发微光。要求：
  · 16px 也要认得出（所以是粗线条 + 少元素，不堆细节）
  · 配色跟面板一致（bg #0a0c0f / amber #d9a441 / text #e9e6df）
  · 用 4 倍超采样再缩小，边缘才干净（Pillow 不自动抗锯齿）

用法：python scripts/make_icon.py [--out 路径.ico]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BG = (10, 12, 15, 255)          # --bg
BG2 = (21, 26, 33, 255)         # --surface2（底部渐深，做出体积感）
AMBER = (217, 164, 65, 255)     # --amber
AMBER_HI = (240, 196, 112, 255)  # 高光
TEAL = (159, 210, 203, 255)     # --teal-text（点缀冷色，避免整张太"黄"）

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4                          # 超采样倍数


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return m


def _vertical_gradient(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(int(BG[i] + (BG2[i] - BG[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (size, y)], fill=c)
    return img


def draw_icon(size: int) -> Image.Image:
    """在 size×size 上画图标（内部按 SS 倍超采样）。"""
    S = size * SS
    img = _vertical_gradient(S)

    # 中心暖光（径向渐晕），让琥珀核"发亮"而不是贴上去
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx = cy = S / 2
    maxr = S * 0.46
    steps = 26
    for i in range(steps, 0, -1):
        r = maxr * i / steps
        a = int(46 * (1 - i / steps) ** 1.6)
        if a <= 0:
            continue
        gd.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(AMBER[0], AMBER[1], AMBER[2], a))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.02))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)

    # ── 神经节点：中心 + 一圈卫星 ──
    R = S * 0.30                      # 卫星环半径
    node_r = S * 0.052                # 卫星点半径
    n_nodes = 6
    nodes = []
    for k in range(n_nodes):
        ang = -math.pi / 2 + k * 2 * math.pi / n_nodes
        nodes.append((cx + R * math.cos(ang), cy + R * math.sin(ang)))

    lw = max(1, int(S * 0.028))       # 连线粗细（16px 时也够粗）

    # 连线：中心→卫星（内圈）+ 相邻卫星（外圈弧感，用直线近似但短）
    for (x, y) in nodes:
        d.line([(cx, cy), (x, y)], fill=(AMBER[0], AMBER[1], AMBER[2], 150), width=lw)
    for k in range(n_nodes):
        x1, y1 = nodes[k]
        x2, y2 = nodes[(k + 1) % n_nodes]
        # 只连一部分，避免太密
        if k % 2 == 0:
            d.line([(x1, y1), (x2, y2)], fill=(TEAL[0], TEAL[1], TEAL[2], 110), width=lw)

    # 卫星点：琥珀描边 + 深色心（显得是"节点"不是"圆点"）
    for (x, y) in nodes:
        d.ellipse([x - node_r, y - node_r, x + node_r, y + node_r],
                  fill=(11, 14, 18, 255), outline=AMBER, width=max(1, int(S * 0.016)))

    # 中心核：实心琥珀 + 高光
    core_r = S * 0.115
    d.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=AMBER)
    hi_r = core_r * 0.46
    d.ellipse([cx - hi_r - core_r * 0.18, cy - hi_r - core_r * 0.22,
               cx + hi_r - core_r * 0.18, cy + hi_r - core_r * 0.22], fill=AMBER_HI)

    # 圆角裁切
    img.putalpha(_rounded_mask(S))
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--out", default=str(root / "assets" / "lingshu.ico"))
    ap.add_argument("--preview", default=str(root / "assets" / "lingshu-icon-preview.png"))
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    frames = [draw_icon(s) for s in SIZES]
    # .ico 里塞多尺寸，Windows 会按需挑
    frames[-1].save(out, format="ICO",
                    sizes=[(s, s) for s in SIZES])
    print(f"已生成 {out}（{out.stat().st_size} bytes，尺寸 {SIZES}）")

    # 预览图：把 256 和 32 并排放大，方便人眼检查
    prev = Image.new("RGBA", (256 + 32 * 4 + 60, 300), (18, 22, 28, 255))
    big = frames[-1]
    prev.alpha_composite(big, (20, 22))
    x = 20 + 256 + 20
    for s in (16, 24, 32, 48):
        im = draw_icon(s).resize((s * 3, s * 3), Image.NEAREST)  # 放大看清像素
        prev.alpha_composite(im, (x, 22))
        x += s * 3 + 10
    pp = Path(a.preview)
    prev.save(pp, format="PNG")
    print(f"已生成预览 {pp}")

    # 面板左上角 brand 用的 PNG（直接按 128px 绘制，不是把 256 缩下来，边缘更锐）
    brand_png = Path(a.out).with_name("lingshu-icon.png")
    draw_icon(128).save(brand_png, format="PNG")
    print(f"已生成面板图标 {brand_png}（{brand_png.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
