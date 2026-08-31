# -*- coding: utf-8 -*-
"""生成 CrabClaw 螃蟹图标：icon.png + icon.ico（多尺寸）。

设计：橙色圆角方块背景 + 白色简笔螃蟹（钳子 + 圆壳 + 眼柄 + 腿）。
坐标系基于 1024x1024，按比例缩放到目标尺寸。
"""
import os
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))

ORANGE = (242, 84, 27, 255)   # 螃蟹橙红
WHITE = (255, 255, 255, 255)
DARK = (43, 43, 43, 255)      # 瞳孔 / 嘴


def draw_crab(draw, S):
    f = S / 1024.0

    def P(x, y):
        return (x * f, y * f)

    def circ(cx, cy, r, fill):
        draw.ellipse([P(cx - r, cy - r), P(cx + r, cy + r)], fill=fill)

    def line(a, b, width, fill):
        draw.line([P(*a), P(*b)], fill=fill, width=max(1, int(width * f)))

    def poly(pts, fill):
        draw.polygon([P(*p) for p in pts], fill=fill)

    # 背景圆角方块
    draw.rounded_rectangle([P(0, 0), P(1024, 1024)], radius=230 * f, fill=ORANGE)

    # 腿（壳后方，先画）
    legw = 22
    for a, b in [((330, 690), (232, 768)), ((352, 714), (248, 822)), ((380, 730), (288, 866))]:
        line(a, b, legw, WHITE)
    for a, b in [((694, 690), (792, 768)), ((672, 714), (776, 822)), ((644, 730), (736, 866))]:
        line(a, b, legw, WHITE)

    # 钳臂
    line((345, 600), (250, 575), 50, WHITE)
    line((679, 600), (774, 575), 50, WHITE)

    # 钳子（圆掌 + 橙色缺口 = 钳口）
    circ(235, 560, 96, WHITE)
    circ(789, 560, 96, WHITE)
    poly([(235, 560), (132, 498), (176, 636)], ORANGE)
    poly([(789, 560), (892, 498), (848, 636)], ORANGE)

    # 圆壳
    draw.ellipse([P(302, 470), P(722, 770)], fill=WHITE)

    # 眼柄
    line((468, 502), (450, 402), 28, WHITE)
    line((556, 502), (574, 402), 28, WHITE)

    # 眼球 + 瞳孔
    circ(450, 386, 42, WHITE)
    circ(574, 386, 42, WHITE)
    circ(450, 386, 16, DARK)
    circ(574, 386, 16, DARK)

    # 嘴（微笑弧）
    draw.arc([P(470, 632), P(554, 704)], start=20, end=160, fill=DARK, width=max(2, int(9 * f)))


def render(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_crab(ImageDraw.Draw(img), size)
    return img


def main():
    # 预览 / 网页用 PNG（512）
    png = render(512)
    png_path = os.path.join(BASE, "icon.png")
    png.save(png_path)

    # ICO 多尺寸（从大图 LANCZOS 下采样，自带抗锯齿）
    master = render(1024)
    sizes = [256, 128, 48, 32, 16]
    frames = [master.resize((s, s), Image.LANCZOS) for s in sizes]
    ico_path = os.path.join(BASE, "icon.ico")
    # 额外补一张 1024 帧，保证大尺寸清晰
    frames.insert(0, master)
    master.save(ico_path, format="ICO", sizes=[(f.width, f.height) for f in frames])
    print(f"generated: {png_path} ({png.size}), {ico_path}")


if __name__ == "__main__":
    main()
