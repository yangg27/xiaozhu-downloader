#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_extension_guide.py · 生成浏览器扩展安装指引图
===============================================
输出一张带箭头/步骤标注的 PNG 图，放在安装目录供用户参考。
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont


def _find_font(size: int):
    candidates = [
        "msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc",
        "arial.ttf", "segoeui.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            pass
    return ImageFont.load_default()


def draw_guide(out_path: str):
    W, H = 820, 560
    img = Image.new("RGB", (W, H), "#f5f6f8")
    draw = ImageDraw.Draw(img)
    title_font = _find_font(22)
    step_font = _find_font(18)
    desc_font = _find_font(14)

    # 标题
    draw.text((W // 2, 24), "小助下载器浏览器扩展安装指引", fill="#1f2329",
              font=title_font, anchor="mt")

    steps = [
        ("1", "打开扩展管理页", "Chrome 地址栏输入 chrome://extensions，Edge 输入 edge://extensions"),
        ("2", "开启「开发者模式」", "点击右上角开关，打开开发者模式"),
        ("3", "解压扩展文件", "把 browser-extension.crx 改后缀为 .zip 并解压"),
        ("4", "加载已解压的扩展", '点击「加载已解压的扩展程序」，选择解压后的 browser-extension 文件夹'),
    ]

    y = 70
    box_h = 95
    gap = 16
    for num, title, desc in steps:
        # 步骤卡片
        draw.rounded_rectangle([28, y, W - 28, y + box_h], radius=12,
                               fill="#ffffff", outline="#dbe7ff", width=2)
        # 编号圆圈
        cx, cy = 65, y + box_h // 2
        draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill="#2f6fed")
        draw.text((cx, cy), num, fill="#ffffff", font=step_font, anchor="mm")
        # 标题 + 说明
        draw.text((105, y + 24), title, fill="#1f2329", font=step_font)
        draw.text((105, y + 54), desc, fill="#7a828e", font=desc_font)
        y += box_h + gap

    # 底部提示
    draw.rounded_rectangle([28, H - 70, W - 28, H - 24], radius=8,
                           fill="#e8f5e9", outline="#0f9d58", width=1)
    draw.text((W // 2, H - 47), "安装完成后，点击工具栏扩展图标即可查看下载进度与历史记录",
              fill="#0f9d58", font=desc_font, anchor="mm")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    print(f"[gen_extension_guide] 已生成: {out_path}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "浏览器扩展安装指引.png"
    draw_guide(out)
