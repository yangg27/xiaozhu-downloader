#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_settings_shot.py · 生成「默认设置截图」
==========================================
用途：帮助页里的「默认设置截图」区域需要一张 default-settings.png。
本脚本真实渲染 SettingsDialog 并截图，输出到 _assets/default-settings.png，
由 build_app.py 拷贝到安装目录（exe 同级）。

截图方式：PrintWindow(PW_RENDERFULLCONTENT)
  比 ImageGrab 可靠 —— 后者依赖窗口可见且未被遮挡，
  在无交互桌面 / 窗口被覆盖时会抓到纯色画面。

为什么不用固定图片：设置页会随版本改版，截图必须与**当前实现**一致，
否则帮助页显示的是过期界面，误导用户。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

from ctypes import wintypes


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


def _setup_win32():
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.UINT]
    return user32, gdi32


def capture_hwnd(hwnd, crop_border=8):
    """抓取 hwnd 的窗口位图，返回 PIL.Image（已裁掉标题栏与边框）。"""
    from PIL import Image
    user32, gdi32 = _setup_win32()

    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)

    ok = user32.PrintWindow(hwnd, hdc_mem, 2)     # PW_RENDERFULLCONTENT
    if not ok:
        ok = user32.PrintWindow(hwnd, hdc_mem, 0)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h                   # 负数 = top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    if got == 0:
        return None

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    # 裁掉标题栏与边框（Tk 窗口约 8px 边框 + 31px 标题栏），只留客户区
    if crop_border and w > 40 and h > 60:
        img = img.crop((crop_border, 31, w - crop_border, h - crop_border))
    return img


def generate(out_path: str) -> bool:
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    os.chdir(root_dir)

    try:
        import tkinter as tk
    except Exception as e:
        print(f"[gen_settings_shot] 无 tkinter，跳过：{e}", file=sys.stderr)
        return False
    try:
        import PIL  # noqa
        from PIL import ImageGrab  # noqa
    except Exception as e:
        print(f"[gen_settings_shot] 无 Pillow，跳过：{e}", file=sys.stderr)
        return False

    import tempfile
    import dlcore
    import gui

    tmp = tempfile.mkdtemp(prefix="xzd_shot_")
    root = tk.Tk()
    root.geometry("320x240+40+40")
    root.update()
    mgr = dlcore.Manager(data_dir=tmp)
    dlg = None
    try:
        dlg = gui.SettingsDialog(root, mgr, None)
        dlg.deiconify()
        dlg.lift()
        for _ in range(70):
            root.update()
            dlg.update()
            time.sleep(0.03)
        time.sleep(0.4)

        try:
            user32, _ = _setup_win32()
            hwnd = user32.GetAncestor(dlg.winfo_id(), 2)   # GA_ROOT
        except Exception:
            hwnd = None

        img = capture_hwnd(hwnd) if hwnd else None
        if img is None:
            # 退路：整屏抓取后按窗口坐标裁剪
            from PIL import ImageGrab
            x, y = dlg.winfo_rootx(), dlg.winfo_rooty()
            w, h = dlg.winfo_width(), dlg.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)

        colors = img.getcolors(maxcolors=300000)
        ncols = len(colors) if colors else 999999
        if ncols < 12:
            print(f"[gen_settings_shot] 画面疑似纯色（{ncols} 种颜色），"
                  f"截图失败", file=sys.stderr)
            return False

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        img.save(out_path)
        print(f"[gen_settings_shot] 已生成: {out_path}  "
              f"({img.size[0]}x{img.size[1]}, {ncols} 色)")
        return True
    except Exception as e:
        import traceback
        print(f"[gen_settings_shot] 失败: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False
    finally:
        try:
            if dlg is not None:
                dlg.destroy()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_out = os.path.join(root_dir, "_assets", "default-settings.png")
    out = sys.argv[1] if len(sys.argv) > 1 else default_out
    ok = generate(out)
    sys.exit(0 if ok else 1)