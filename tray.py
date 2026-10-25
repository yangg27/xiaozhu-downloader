#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 系统托盘  tray.py
==============================
零依赖实现（纯 ctypes 调 Windows Shell_NotifyIconW），不引入 pystray / pywin32。

功能：
  · 托盘图标常驻（后台静默运行时的唯一存在感）
  · 右键菜单：显示主窗口 / 暂停全部 / 继续全部 / 打开下载目录 / 退出
  · 左键双击：显示主窗口
  · 悬浮提示：实时显示下载速度与任务数（每 2 秒刷新）
  · 图标根据状态变化（空闲 / 下载中为蓝色，出错为红色）

用法：
    tray = Tray(on_show=..., on_pause=..., on_resume=..., on_open_dir=..., on_exit=...,
                get_status=lambda: "12.3 MB/s · 2 个任务")
    tray.start()      # 在后台线程创建隐藏窗口并挂图标
    tray.update(tip)  # 更新悬浮提示
    tray.stop()       # 移除图标
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  强制 PyInstaller 打包 ctypes.wintypes
from ctypes import wintypes as wt
import os
import sys
import threading
import time


def _res_dir() -> str:
    """资源目录：源码运行 = 本文件目录；冻结运行 = exe 目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _icon_file(name: str = "tray.ico") -> str:
    """按候选位置找图标：_internal/ → 打包临时目录 → _assets/ → exe 同级 → 源码目录"""
    exe_dir = _res_dir()
    cands = [os.path.join(exe_dir, "_internal", name)]
    mei = getattr(sys, "_MEIPASS", "")
    if mei:
        cands.append(os.path.join(mei, name))
    src_dir = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(src_dir, "_assets", name))
    cands.append(os.path.join(exe_dir, name))
    cands.append(os.path.join(src_dir, name))
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# ---- 显式声明关键 API 的签名（64 位下必须，否则指针会被截断成 int） ----
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

user32.CreateWindowExW.argtypes = [
    ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = ctypes.c_void_p

user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = ctypes.c_ushort

user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.LoadImageW.restype = ctypes.c_void_p

user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
user32.LoadIconW.restype = ctypes.c_void_p

user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                            ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t

user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]

user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int

user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = ctypes.c_ssize_t

user32.TranslateMessage.argtypes = [ctypes.c_void_p]

user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_wchar_p]
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_ssize_t]

shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

# ---- 常量 ----
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
MF_CHECKED = 0x00000008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

# 菜单命令 ID
ID_SHOW = 1001
ID_PAUSE = 1002
ID_RESUME = 1003
ID_OPENDIR = 1004
ID_EXIT = 1005

class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", ctypes.c_long * 2),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)
# 说明：64 位下 LPARAM 是有符号指针宽度，用 c_ssize_t 表示，避免 OverflowError


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class Tray:
    """Windows 系统托盘（纯 ctypes，零依赖）"""

    def __init__(self, on_show=None, on_pause=None, on_resume=None,
                 on_open_dir=None, on_exit=None, get_status=None,
                 app_name="小助下载器", icon_path="", idle_hint=None):
        self.on_show = on_show
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_open_dir = on_open_dir
        self.on_exit = on_exit
        self.get_status = get_status          # 返回一句话状态（悬浮提示）
        self.app_name = app_name
        self.icon_path = icon_path or _icon_file()
        self.idle_hint = idle_hint or f"{app_name} · 后台运行中"

        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._thread = None
        self._ready = threading.Event()
        self._running = False
        self._wndproc_ref = None
        self._class_atom = None
        self._last_tip = ""
        self._pause_state = False

    # ---------------- 图标 ----------------
    def _load_icons(self):
        """加载三态图标：正常 / 下载中 / 出错（着色区分）"""
        icons = {}
        base = None
        if os.path.exists(self.icon_path):
            h = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                base = h
        if not base:
            base = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        icons["normal"] = base

        # 尝试用 PIL 生成不同颜色的变体（有 PIL 就用，没有就用同一图标）
        try:
            import tempfile
            from PIL import Image
            ico = Image.open(self.icon_path).convert("RGBA")
            for name, color in (("busy", (15, 157, 88, 255)),
                                ("error", (217, 48, 37, 255)),
                                ("paused", (242, 153, 0, 255))):
                img = ico.copy()
                px = img.load()
                w, h = img.size
                for y in range(h):
                    for x in range(w):
                        r, g, b, a = px[x, y]
                        if a > 40 and not (r > 230 and g > 230 and b > 230):
                            px[x, y] = color
                tmp = os.path.join(tempfile.gettempdir(), f"_xzd_tray_{name}.ico")
                img.save(tmp, format="ICO",
                         sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48)])
                hh = user32.LoadImageW(None, tmp, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                icons[name] = hh or base
        except Exception:
            icons["busy"] = base
            icons["error"] = base
            icons["paused"] = base
        self._icons = icons
        return icons

    def set_state(self, state: str):
        """state: normal / busy / error / paused"""
        if not self._running or not self._nid:
            return
        h = (self._icons or {}).get(state) or (self._icons or {}).get("normal")
        if h:
            self._nid.hIcon = h
            self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    # ---------------- 窗口过程 ----------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._safe(self.on_show)
                elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == ID_SHOW:
                    self._safe(self.on_show)
                elif cmd == ID_PAUSE:
                    self._safe(self.on_pause)
                elif cmd == ID_RESUME:
                    self._safe(self.on_resume)
                elif cmd == ID_OPENDIR:
                    self._safe(self.on_open_dir)
                elif cmd == ID_EXIT:
                    self._safe(self.on_exit)
                return 0
            if msg == WM_TIMER:
                self._refresh_tip()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == 0x0001:  # WM_CREATE
                return 0
        except Exception:
            # 静默是有意为之：窗口消息处理：任何异常都不能让消息循环崩掉，否则托盘图标会「假死」
            pass
        # 64 位下必须显式转换为有符号/无符号整型，否则 ctypes 报 OverflowError
        return user32.DefWindowProcW(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_uint(int(msg)),
            ctypes.c_size_t(int(wparam) & 0xFFFFFFFFFFFFFFFF),
            ctypes.c_ssize_t(int(lparam)))

    @staticmethod
    def _safe(fn):
        if fn:
            try:
                fn()
            except Exception:
                # 静默是有意为之：菜单回调包装器：单个回调失败不应连累其他菜单项
                pass

    # ---------------- 菜单 ----------------
    def _show_menu(self):
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, ID_SHOW, "显示主窗口")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_PAUSE, "暂停全部任务")
            user32.AppendMenuW(hmenu, MF_STRING, ID_RESUME, "继续全部任务")
            user32.AppendMenuW(hmenu, MF_STRING, ID_OPENDIR, "打开下载目录")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_EXIT, "退出")

            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须有前台窗口，菜单才可靠弹出（Windows 的已知行为）
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                pt.x, pt.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, 0, 0, 0)
            if cmd:
                self._wnd_proc(self.hwnd, WM_COMMAND, cmd, 0)
        finally:
            user32.DestroyMenu(hmenu)

    # ---------------- 提示文本 ----------------
    def _refresh_tip(self):
        tip = self.idle_hint
        if self.get_status:
            try:
                s = self.get_status()
                if s:
                    tip = f"{self.app_name} · {s}"
            except Exception:
                # 静默是有意为之：取状态文本失败则退回默认提示，不值得打断 UI
                pass
        tip = tip[:120]
        if tip != self._last_tip:
            self.update(tip)

    def update(self, tip: str):
        if not self._running or not self._nid:
            return
        try:
            self._nid.szTip = tip[:127]
            self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
            self._last_tip = tip
        except Exception:
            # 静默是有意为之：更新托盘提示失败不影响主流程（下载照常）
            pass

    def notify(self, title: str, text: str):
        """气泡通知（用于"已开始下载"之类提示）"""
        if not self._running or not self._nid:
            return
        try:
            self._nid.uFlags = NIF_INFO | NIF_ICON | NIF_MESSAGE | NIF_TIP
            self._nid.szInfoTitle = (title or "")[:63]
            self._nid.szInfo = (text or "")[:255]
            self._nid.dwInfoFlags = 0x00000001  # NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            # 静默是有意为之：气泡通知可能被系统策略禁用，失败不影响下载
            pass

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        if not sys_is_windows():
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="xzd-tray")
        self._thread.start()
        self._ready.wait(timeout=6)
        return self._running

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = f"XZDTrayWnd_{os.getpid()}"
            self._wndproc_ref = WNDPROC(self._wnd_proc)
            wc = WNDCLASSW()
            wc.style = CS_HREDRAW | CS_VREDRAW
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            self._class_atom = user32.RegisterClassW(ctypes.byref(wc))
            if not self._class_atom:
                # 已注册过则直接创建窗口
                pass
            self.hwnd = user32.CreateWindowExW(
                0, cls_name, self.app_name, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                self._ready.set()
                return

            icons = self._load_icons()
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = icons.get("normal")
            nid.szTip = self.idle_hint[:127]
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            if not ok:
                # 任务栏可能刚启动，重试一次
                time.sleep(1.0)
                ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            if not ok:
                self._ready.set()
                return
            self._nid = nid
            self._running = True
            self._ready.set()
            user32.SetTimer(self.hwnd, 1, 2000, None)   # 每 2 秒刷新提示

            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ready.set()
        finally:
            self._running = False

    def stop(self):
        try:
            if self._nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            # 静默是有意为之：退出清理：删除托盘图标，失败也无所谓（进程即将结束）
            pass
        try:
            if self.hwnd:
                user32.KillTimer(self.hwnd, 1)
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            # 静默是有意为之：退出清理：销毁隐藏窗口/定时器，失败无副作用
            pass
        self._running = False


def sys_is_windows() -> bool:
    return os.name == "nt"


if __name__ == "__main__":
    # 自测：挂一个托盘，5 秒后退出
    print("托盘自测：图标应出现在任务栏右下角，5 秒后自动退出")
    t = Tray(on_show=lambda: print("  [菜单] 显示主窗口"),
             on_pause=lambda: print("  [菜单] 暂停全部"),
             on_resume=lambda: print("  [菜单] 继续全部"),
             on_open_dir=lambda: print("  [菜单] 打开目录"),
             on_exit=lambda: print("  [菜单] 退出"),
             get_status=lambda: "自测中 · 12.3 MB/s")
    ok = t.start()
    print("托盘启动:", "成功" if ok else "失败")
    for i in range(5):
        time.sleep(1)
        t.update(f"小助下载器 · 自测 {5 - i} 秒后退出")
        t.set_state("busy" if i % 2 == 0 else "normal")
    t.stop()
    print("托盘已移除")
