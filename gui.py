#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 图形界面  gui.py   v3.0.8
=============================
基于 tkinter 的本地下载管理器界面，零依赖、纯离线。

  · 多线程下载，实时速度 / 进度 / 剩余时间
  · 任务队列：暂停 / 继续 / 删除 / 动态加速（增加连接数）
  · 内置 aria2c 备用极速通道（v2.6.0），失败或手动一键切换
  · 嗅探面板：承接浏览器扩展推来的媒体链接，一键下载
  · 设置：连接数 / 限速 / 并发任务数 / 代理 / 保存目录
  · 全程不联网校验、不检查更新、不弹注册框
  · 可冻结为免 Python 独立程序 + 双击安装包（v1.13.0）：
      one-dir 形态启动 0.7s，适配开机自启常驻托盘
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ------------------------------ 启动打点 ------------------------------
# 设 XZD_BOOT_TRACE=1 时，把"进程开始 → 各阶段完成"的时间落到 data/boot_trace.txt。
# 用于量化启动优化收益（默认关闭，对正常运行零影响）。
_T0 = time.time()
_BOOT_TRACE = os.environ.get("XZD_BOOT_TRACE") == "1"
_BOOT_MARKS: list[tuple[str, float]] = []


def boot_mark(name: str):
    if _BOOT_TRACE:
        _BOOT_MARKS.append((name, time.time() - _T0))


def boot_flush():
    if not _BOOT_TRACE:
        return
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "boot_trace.txt"), "w", encoding="utf-8") as f:
            for n, t in _BOOT_MARKS:
                f.write(f"{n}\t{t:.4f}\n")
    except Exception:
        dlcore.LOG.debug('忽略异常：d = os.path.join(os.path.dirname(os.path.abspath(__file__))...')


boot_mark("script_start")

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
# 冻结（打包）后：资源被展开到 _MEIPASS，需要把它也加进搜索路径
_MEI = getattr(sys, "_MEIPASS", "")
if _MEI and _MEI not in sys.path:
    sys.path.insert(0, _MEI)
import dlcore  # noqa: E402
import localserver  # noqa: E402
import sysintegration  # noqa: E402

boot_mark("imports_done")

# ------------------------------ 视觉常量 ------------------------------
FONT = "Microsoft YaHei UI"
FONT_FB = "Microsoft YaHei"
BG = "#f5f6f8"
CARD = "#ffffff"
FG = "#1f2329"
FG_DIM = "#7a828e"
ACCENT = "#2f6fed"
ACCENT_DARK = "#1f5bd4"
OK = "#0f9d58"
WARN = "#f29900"
ERR = "#d93025"
BORDER = "#e3e6eb"
FONT_FAMILY = None

STATE_COLORS = {
    dlcore.STATE_DOWNLOADING: ACCENT,
    dlcore.STATE_DONE: OK,
    dlcore.STATE_ERROR: ERR,
    dlcore.STATE_PAUSED: WARN,
    dlcore.STATE_WAIT: FG_DIM,
    dlcore.STATE_PENDING: FG_DIM,
}

# v1.12.0：进度条改用 1/8 精度字符（"█▉▊▋▌▍▎▏"），
# 相比原来的 1/12 整数格，同宽度下平滑度提升 8 倍 —— 进度看起来"在流动"
# 而不是一格一格地跳。同时按状态换用不同填充字符，一眼能分辨运行/出错。
BAR_FULL, BAR_EMPTY = "█", "░"

# v1.12.0：状态徽章 —— 状态列前加一个小图标，
# 在一屏 20 行里比纯文字更快分辨（尤其"已完成/出错"需要一眼扫出来）。
# 用基础几何符号而非 emoji：emoji 在部分 Windows 字体下会渲染成方块或彩色怪图。
STATE_BADGE = {
    dlcore.STATE_DOWNLOADING: "▶",   # 运行中
    dlcore.STATE_DONE: "✔",          # 已完成
    dlcore.STATE_ERROR: "✖",         # 出错
    dlcore.STATE_PAUSED: "❙❙",       # 已暂停
    dlcore.STATE_WAIT: "◷",          # 等待中
    dlcore.STATE_PENDING: "◷",
    dlcore.STATE_CANCELLED: "⊘",     # 已取消
}


def badge(state: str) -> str:
    return STATE_BADGE.get(state, "·")
_BAR_STEPS = " ▏▎▍▌▋▊▉█"          # 0/8 ~ 8/8


def bar(frac: float, width: int = 12, partial: bool = True) -> str:
    """把进度渲染成块状条。

    partial=True 时用 1/8 精度的中间字符（视觉平滑），
    False 则退回整数格（用于等宽对齐要求严格的场景）。
    """
    frac = max(0.0, min(1.0, frac or 0.0))
    if not partial:
        n = int(round(frac * width))
        return BAR_FULL * n + BAR_EMPTY * (width - n)
    total = frac * width                # 总格数（含小数）
    full = int(total)
    rest = total - full
    idx = int(round(rest * 8))
    out = BAR_FULL * full
    if idx >= 8:                        # 舍入进位
        out += BAR_FULL
        full += 1
        idx = 0
    if full < width and idx > 0:
        out += _BAR_STEPS[idx]
    pad = width - len(out)
    if pad > 0:
        out += BAR_EMPTY * pad
    return out[:width]


def open_in_explorer(path: str):
    if not path:
        return
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif os.path.isdir(path):
            os.startfile(os.path.normpath(path))  # noqa
        else:
            os.startfile(os.path.dirname(os.path.normpath(path)))  # noqa
    except Exception:
        try:
            os.startfile(os.path.dirname(os.path.normpath(path)))  # noqa
        except Exception:
            dlcore.LOG.debug('忽略异常：os.startfile(os.path.dirname(os.path.normpath(path)))')


def open_file(path: str):
    try:
        os.startfile(os.path.normpath(path))  # noqa
    except Exception:
        dlcore.LOG.debug('忽略异常：os.startfile(os.path.normpath(path))')


# ------------------------------ 通用对话框基类 ------------------------------

class Dialog(tk.Toplevel):
    def __init__(self, master, title: str, w: int, h: int, *,
                 resizable: bool = False, scrollable: bool = False):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(resizable, resizable)
        self.transient(master)
        self.result = None
        self._dlg_w = w
        self._dlg_h = h
        self._scrollable = scrollable
        self.geometry(f"{w}x{h}")
        self._center(w, h)
        self.bind("<Escape>", lambda e: self.destroy())
        # 主窗口可能在托盘/最小化状态，强制显示并置顶
        self.deiconify()
        self.lift()
        self.focus_force()
        self.attributes("-topmost", True)
        self.after(100, lambda: self.attributes("-topmost", False))
        if scrollable:
            self.bind("<Configure>", self._on_dlg_configure)

    def _on_dlg_configure(self, event=None):
        """可滚动对话框在尺寸变化时同步 canvas 可视区域。"""
        if not self._scrollable or not hasattr(self, "_canvas"):
            return
        try:
            self._canvas.configure(width=max(320, self.winfo_width() - 24))
            self._canvas.itemconfig(self._canvas_window,
                                    width=max(300, self.winfo_width() - 48))
        except Exception:
            pass

    def _center(self, w, h):
        self.update_idletasks()
        try:
            mx = self.master.winfo_rootx() + self.master.winfo_width() // 2
            my = self.master.winfo_rooty() + self.master.winfo_height() // 2
            x = max(0, mx - w // 2)
            y = max(0, my - h // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            dlcore.LOG.debug('忽略异常：mx = master.winfo_rootx() + master.winfo_width() // 2')


class MergeAVDialog(Dialog):
    """手动选择视频+音频文件，FFmpeg 合并为完整 MP4。"""
    def __init__(self, master, app):
        super().__init__(master, "音视频合并", 560, 330)
        self.app = app
        self.v_video = tk.StringVar()
        self.v_audio = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_status = tk.StringVar(value="选择已下载的视频（无声）和音频文件，合并为完整 MP4")
        self._build()

    def _file_row(self, parent, label, var, r, ftypes):
        tk.Label(parent, text=label, bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", pady=(0, 10))
        e = tk.Entry(parent, textvariable=var, font=(FONT, 9), bg="#fff", fg=FG,
                     relief="solid", bd=1, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT, width=44)
        e.grid(row=r, column=1, sticky="we", pady=(0, 10), ipady=4)
        tk.Button(parent, text="浏览", font=(FONT, 9), bg="#eef1f6", fg=FG,
                  relief="flat", padx=10, cursor="hand2",
                  command=lambda: self._pick(var, ftypes)).grid(
            row=r, column=2, padx=(8, 0), pady=(0, 10))

    def _pick(self, var, ftypes):
        p = filedialog.askopenfilename(parent=self, filetypes=ftypes)
        if p:
            var.set(p)
            if var is self.v_video and not self.v_out.get():
                self.v_out.set(os.path.splitext(p)[0] + "_merged.mp4")

    def _build(self):
        f = tk.Frame(self, bg=BG, padx=18, pady=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        tk.Label(f, text="手动合并音视频（适用于音视频分离的文件，如 B站 m4s）",
                 bg=BG, fg=FG, font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        self._file_row(f, "视频文件", self.v_video, 1,
                       [("视频文件", "*.mp4 *.m4s *.flv *.mkv *.ts *.webm"), ("全部文件", "*.*")])
        self._file_row(f, "音频文件", self.v_audio, 2,
                       [("音频文件", "*.m4a *.m4s *.mp3 *.aac *.wav *.flac"), ("全部文件", "*.*")])
        self._file_row(f, "输出路径", self.v_out, 3, [("MP4 视频", "*.mp4")])
        tk.Label(f, textvariable=self.v_status, bg=BG, fg=FG_DIM, font=(FONT, 9),
                 wraplength=500, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 10))
        btns = tk.Frame(f, bg=BG)
        btns.grid(row=5, column=0, columnspan=3, sticky="e")
        tk.Button(btns, text="取消", command=self.destroy, font=(FONT, 9),
                  bg="#eef1f6", fg=FG, relief="flat", padx=16, pady=5,
                  cursor="hand2").pack(side="left", padx=(0, 8))
        self.btn_merge = tk.Button(btns, text="开始合并", command=self._merge,
                                   font=(FONT, 10, "bold"), bg=ACCENT, fg="#fff",
                                   relief="flat", padx=16, pady=5, cursor="hand2")
        self.btn_merge.pack(side="left")

    def _merge(self):
        v, a, o = self.v_video.get().strip(), self.v_audio.get().strip(), self.v_out.get().strip()
        if not v or not os.path.exists(v):
            messagebox.showwarning("提示", "请选择视频文件", parent=self)
            return
        if not a or not os.path.exists(a):
            messagebox.showwarning("提示", "请选择音频文件", parent=self)
            return
        if not o:
            o = os.path.splitext(v)[0] + "_merged.mp4"
            self.v_out.set(o)
        self.btn_merge.config(state="disabled")
        self.v_status.set("正在合并…（大文件可能需要几十秒）")

        def work():
            ok, msg = dlcore.merge_av(v, a, o)
            def done():
                self.btn_merge.config(state="normal")
                if ok:
                    self.v_status.set(f"合并成功：{msg}")
                    messagebox.showinfo("完成", f"合并成功！\n{msg}", parent=self)
                    self.destroy()
                else:
                    self.v_status.set(f"合并失败：{msg}")
                    messagebox.showerror("失败", f"合并失败：\n{msg}", parent=self)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()


class NewDownloadDialog(Dialog):
    """v3.1.0 统一新建下载入口：支持 HTTP/HTTPS、磁力链 magnet:、本地/远程 .torrent 种子。"""

    def __init__(self, master, mgr, preset: dict | None = None):
        super().__init__(master, "新建下载", 560, 560)
        self.mgr = mgr
        preset = preset or {}
        self.v_url = tk.StringVar(value=preset.get("url", ""))
        self.v_dir = tk.StringVar(value=preset.get("out_dir") or mgr.cfg.get("download_dir"))
        self.v_name = tk.StringVar(value=preset.get("filename", ""))
        self.v_conns = tk.IntVar(value=int(preset.get("conns") or mgr.cfg.get("connections", 24)))
        self.v_ref = tk.StringVar(value=preset.get("referer", ""))
        self.v_cookie = tk.StringVar(value=preset.get("cookie", ""))
        self._build()

    def _row(self, parent, label, var, r, width=46, show=None):
        tk.Label(parent, text=label, bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", pady=(0, 8))
        e = tk.Entry(parent, textvariable=var, font=(FONT, 9), show=show,
                     bg="#fff", fg=FG, relief="solid", bd=1,
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT, width=width)
        e.grid(row=r, column=1, sticky="we", pady=(0, 8), ipady=4)
        return e

    def _build(self):
        f = tk.Frame(self, bg=BG, padx=18, pady=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        tk.Label(f, text="下载链接 / 磁力链 / 种子文件", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(f, text="支持每行一个 HTTP/HTTPS 链接、magnet:?xt=... 磁力链，或点击下方按钮选择 .torrent 种子文件",
                 bg=BG, fg=FG_DIM, font=(FONT, 8)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        # 多行 URL 输入
        url_frame = tk.Frame(f, bg=BG)
        url_frame.grid(row=2, column=1, sticky="we", pady=(0, 8))
        self.t_url = tk.Text(url_frame, height=5, font=(FONT, 9), bg="#fff", fg=FG,
                             relief="solid", bd=1, wrap="none")
        self.t_url.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(url_frame, orient="vertical", command=self.t_url.yview)
        sb.pack(side="right", fill="y")
        self.t_url.configure(yscrollcommand=sb.set)
        self.t_url.insert("1.0", self.v_url.get())
        tk.Label(f, text="下载地址", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=2, column=0, sticky="nw", pady=(0, 8))

        # 种子文件选择
        torrent_row = tk.Frame(f, bg=BG)
        torrent_row.grid(row=3, column=1, sticky="we", pady=(0, 12))
        self.v_torrent = tk.StringVar(value="")
        tk.Entry(torrent_row, textvariable=self.v_torrent, font=(FONT, 9), bg="#fff", fg=FG,
                 relief="solid", bd=1, highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT, state="readonly").pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(torrent_row, text="选择种子文件", command=self._pick_torrent, font=(FONT, 9),
                  bg="#eef1f6", fg=FG, relief="flat", padx=10, cursor="hand2").pack(side="left", padx=(6, 0))
        tk.Label(f, text="种子文件", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=3, column=0, sticky="w", pady=(0, 12))

        fr = tk.Frame(f, bg=BG)
        fr.grid(row=4, column=1, sticky="we", pady=(0, 8))
        tk.Entry(fr, textvariable=self.v_dir, font=(FONT, 9), bg="#fff", fg=FG,
                 relief="solid", bd=1, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT).pack(
            side="left", fill="x", expand=True, ipady=4)
        tk.Button(fr, text="浏览", command=self._pick_dir, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=10, cursor="hand2").pack(side="left", padx=(6, 0))
        tk.Label(f, text="保存目录", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=4, column=0, sticky="w", pady=(0, 8))
        self._row(f, "文件名称", self.v_name, 5)
        tk.Label(f, text="线程数", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=6, column=0, sticky="w", pady=(0, 8))
        conn_row = tk.Frame(f, bg=BG)
        conn_row.grid(row=6, column=1, sticky="we", pady=(0, 8))
        e_conn = tk.Entry(conn_row, textvariable=self.v_conns, font=(FONT, 9),
                          bg="#fff", fg=FG, relief="solid", bd=1,
                          highlightthickness=1, highlightbackground=BORDER,
                          highlightcolor=ACCENT, width=8)
        e_conn.pack(side="left", ipady=4)
        tk.Label(conn_row, text="线程（1-32，越多越快，部分服务器限制）", bg=BG,
                 fg=FG_DIM, font=(FONT, 8)).pack(side="left", padx=(8, 0))
        self._row(f, "Referer", self.v_ref, 7)
        self._row(f, "Cookie", self.v_cookie, 8)
        tip = tk.Label(f, text="提示：视频/音频站点通常需要 Referer；不填则自动带上来源页",
                       bg=BG, fg=FG_DIM, font=(FONT, 8))
        tip.grid(row=9, column=0, columnspan=2, sticky="w", pady=(2, 10))
        btns = tk.Frame(f, bg=BG)
        btns.grid(row=10, column=0, columnspan=2, sticky="e", pady=(12, 0))
        tk.Button(btns, text="取消", command=self.destroy, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=18, pady=5, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="开始下载", command=self._ok, font=(FONT, 10, "bold"),
                  bg=ACCENT, fg="#fff", relief="flat", padx=18, pady=5,
                  activebackground=ACCENT_DARK, activeforeground="#fff",
                  cursor="hand2").pack(side="left")
        self.t_url.focus_set()
        self.bind("<Return>", lambda e: self._ok())

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.v_dir.get() or os.path.expanduser("~"))
        if d:
            self.v_dir.set(os.path.normpath(d))

    def _pick_torrent(self):
        p = filedialog.askopenfilename(
            title="选择 BT 种子文件",
            filetypes=[("BT 种子", "*.torrent"), ("所有文件", "*.*")],
            initialdir=os.path.expanduser("~"))
        if not p:
            return
        p = os.path.normpath(p)
        self.v_torrent.set(p)
        self.t_url.delete("1.0", "end")
        self.t_url.insert("1.0", p)
        # v3.1.0：解析种子并显示摘要
        try:
            import torrent_parser
            info = torrent_parser.parse_torrent(p)
            files = info.get("files", [])
            total = info.get("total_length", 0)
            name = info.get("name") or os.path.basename(p)
            if files:
                summary = f"种子：{name} | 大小：{dlcore.human_size(total)} | 文件数：{len(files)}"
            else:
                summary = f"种子：{name} | 大小：{dlcore.human_size(total)}"
            messagebox.showinfo("种子信息", summary, parent=self)
        except Exception as e:
            messagebox.showwarning("种子解析失败", f"无法解析该种子文件：{e}", parent=self)

    def _ok(self):
        raw = self.t_url.get("1.0", "end").strip()
        torrent_path = self.v_torrent.get().strip()
        if not raw and not torrent_path:
            messagebox.showwarning("提示", "请填写下载地址或选择种子文件", parent=self)
            return
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if torrent_path and os.path.exists(torrent_path):
            urls.insert(0, torrent_path)
        if not urls:
            messagebox.showwarning("提示", "请填写下载地址或选择种子文件", parent=self)
            return
        fixed = []
        for u in urls:
            if os.path.exists(u):
                # 本地种子文件保持原样
                fixed.append(u)
            elif u.startswith("magnet:"):
                fixed.append(u)
            elif u.lower().startswith(("http://", "https://", "ftp://")):
                fixed.append(u)
            else:
                fixed.append("http://" + u)
        self.result = {
            "urls": fixed,
            "url": fixed[0],
            "out_dir": self.v_dir.get().strip(), "filename": self.v_name.get().strip(),
            "conns": max(1, min(32, int(self.v_conns.get() or 24))),
            "referer": self.v_ref.get().strip(), "cookie": self.v_cookie.get().strip(),
        }
        self.destroy()


class SettingsDialog(Dialog):
    def __init__(self, master, mgr, app=None):
        super().__init__(master, "设置", 560, 540, resizable=True, scrollable=True)
        self.mgr = mgr
        self.app = app
        self._entries: dict[str, tk.Entry] = {}
        self._errors: dict[str, tk.Label] = {}
        c = mgr.cfg
        self.v_dir = tk.StringVar(value=c.get("download_dir", ""))
        self.v_conns = tk.IntVar(value=int(c.get("connections", 24)))
        self.v_par = tk.IntVar(value=int(c.get("max_parallel_tasks", 3)))
        self.v_limit = tk.IntVar(value=int(c.get("speed_limit", 0)) // 1024)
        self.v_proxy = tk.StringVar(value=c.get("proxy", ""))
        self.v_ua = tk.StringVar(value=c.get("user_agent", dlcore.DEFAULT_UA))
        self.v_tmp = tk.BooleanVar(value=bool(c.get("delete_tmp_after_done", True)))
        self.v_insecure = tk.BooleanVar(value=bool(c.get("insecure", False)))
        self.v_port = tk.IntVar(value=int(c.get("port", 8763)))
        self.v_clipboard = tk.BooleanVar(value=bool(c.get("clipboard_watch", True)))
        # v3.0.0: 引擎与 BT 设置
        self.v_engine = tk.StringVar(value=str(c.get("engine", "builtin")))
        self.v_bt_source = tk.StringVar(value=str(c.get("bt_tracker_source", "https://cf.trackerslist.com/best.txt")))
        self.v_bt_autoupdate = tk.BooleanVar(value=bool(c.get("bt_trackers_autoupdate", True)))
        self.v_bt_probe_alive = tk.BooleanVar(value=bool(c.get("bt_tracker_probe_alive", True)))
        # 系统集成状态
        self.si = None
        self.v_autostart = tk.BooleanVar(value=False)
        self.v_protocol = tk.BooleanVar(value=False)
        self._load_sys_status()
        self._build()

    def _load_sys_status(self):
        try:
            import sysintegration as si
            self.si = si
            self.v_autostart.set(si.is_autostart_enabled())
            self.v_protocol.set(si.is_protocol_registered())
        except Exception:
            self.si = None

    def _lbl(self, p, t, r):
        tk.Label(p, text=t, bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", pady=(0, 7))

    def _ent(self, p, var, r, w=30, name=""):
        """v3.0.8: 带实时校验的输入框，错误时边框变红并在下方显示提示。"""
        fr = tk.Frame(p, bg=BG)
        fr.grid(row=r, column=1, sticky="we", pady=(0, 7))
        e = tk.Entry(fr, textvariable=var, font=(FONT, 9), bg="#fff", fg=FG, width=w,
                     relief="solid", bd=1, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT)
        e.pack(side="top", fill="x", ipady=3)
        err = tk.Label(fr, text="", bg=BG, fg=ERR, font=(FONT, 8))
        err.pack(side="top", anchor="w")
        key = name or f"row{r}"
        self._entries[key] = e
        self._errors[key] = err
        e.bind("<KeyRelease>", lambda ev, k=key: self._validate(k))
        e.bind("<FocusOut>", lambda ev, k=key: self._validate(k))
        return e

    def _validate(self, key: str) -> bool:
        """实时校验单个输入框，返回是否通过。"""
        e = self._entries.get(key)
        err = self._errors.get(key)
        if e is None or err is None:
            return True
        txt = e.get().strip()
        ok, msg = True, ""
        if key == "row2":
            try:
                v = int(txt)
                if not (1 <= v <= 32):
                    raise ValueError
            except ValueError:
                ok, msg = False, "默认线程数必须是 1-32 的整数"
        elif key == "row3":
            try:
                v = int(txt)
                if not (1 <= v <= 20):
                    raise ValueError
            except ValueError:
                ok, msg = False, "同时下载任务数必须是 1-20 的整数"
        elif key == "row4":
            try:
                v = int(txt)
                if v < 0:
                    raise ValueError
            except ValueError:
                ok, msg = False, "全局限速必须是大于等于 0 的整数（KB/s）"
        elif key == "row7":
            try:
                v = int(txt)
                if not (1024 <= v <= 65535):
                    raise ValueError
            except ValueError:
                ok, msg = False, "本地接口端口必须是 1024-65535 的整数"
        elif key == "row5":
            if txt and not re.match(r"^(https?|socks5?)://", txt, re.I):
                ok, msg = False, "代理格式形如 http://127.0.0.1:7890 或留空"
        elif key == "row6":
            if not txt:
                ok, msg = False, "User-Agent 不能为空"
        elif key == "row14":
            if txt and not txt.lower().startswith(("http://", "https://")):
                ok, msg = False, "Tracker 源必须是 http:// 或 https:// 链接"
        color = ERR if not ok else BORDER
        e.config(highlightbackground=color)
        err.config(text=msg)
        return ok

    def _build(self):
        # 外层：固定尺寸容器，底部固定按钮栏，中间为可滚动 canvas
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        # Canvas + 滚动条
        self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        vsb.grid(row=0, column=1, sticky="ns", pady=(8, 0))

        f = tk.Frame(self._canvas, bg=BG, padx=14, pady=12)
        self._canvas_window = self._canvas.create_window((0, 0), window=f, anchor="nw",
                                                         width=self._dlg_w - 48)
        f.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._on_dlg_configure())
        # 鼠标滚轮绑定
        def _on_mousewheel(event):
            try:
                self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass
        self.bind_all("<MouseWheel>", _on_mousewheel)
        # v3.0.8: 截图快捷键 Ctrl+Alt+P，保存默认设置截图到安装目录
        self.bind("<Control-Alt-p>", lambda e: self._save_screenshot())
        self.bind("<Control-Alt-P>", lambda e: self._save_screenshot())

        f.columnconfigure(1, weight=1)
        tk.Label(f, text="下载设置", bg=BG, fg=FG, font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        fr = tk.Frame(f, bg=BG)
        fr.grid(row=1, column=1, sticky="we", pady=(0, 7))
        tk.Entry(fr, textvariable=self.v_dir, font=(FONT, 9), bg="#fff", fg=FG,
                 relief="solid", bd=1, highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", fill="x", expand=True, ipady=3)
        tk.Button(fr, text="浏览", command=lambda: self._pick(self.v_dir),
                  font=(FONT, 9), bg="#eef1f6", fg=FG, relief="flat", padx=10,
                  cursor="hand2").pack(side="left", padx=(6, 0))
        self._lbl(f, "默认保存目录", 1)
        self._ent(f, self.v_conns, 2, 8)
        self._lbl(f, "默认线程数", 2)
        self._ent(f, self.v_par, 3, 8)
        self._lbl(f, "同时下载任务数", 3)
        self._ent(f, self.v_limit, 4, 8)
        tk.Label(f, text="= 全局限速（KB/s，0 表示不限速）", bg=BG, fg=FG_DIM,
                 font=(FONT, 8)).grid(row=4, column=2, sticky="w", padx=(8, 0), pady=(0, 7))
        self._lbl(f, "全局限速", 4)
        self._ent(f, self.v_proxy, 5)
        self._lbl(f, "代理服务器", 5)
        self._ent(f, self.v_ua, 6)
        self._lbl(f, "User-Agent", 6)
        self._ent(f, self.v_port, 7, 8)
        tk.Label(f, text="浏览器扩展对接端口", bg=BG, fg=FG_DIM,
                 font=(FONT, 8)).grid(row=7, column=2, sticky="w", padx=(8, 0), pady=(0, 7))
        self._lbl(f, "本地接口端口", 7)
        cb1 = tk.Checkbutton(f, text="任务完成后自动清理临时分片", variable=self.v_tmp, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb1.grid(row=8, column=1, sticky="w", pady=(0, 4))
        cb2 = tk.Checkbutton(f, text="忽略 HTTPS 证书错误（不推荐）", variable=self.v_insecure, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb2.grid(row=9, column=1, sticky="w", pady=(0, 4))
        cb5 = tk.Checkbutton(f, text="剪贴板监听（复制链接自动弹新建下载）",
                             variable=self.v_clipboard, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb5.grid(row=10, column=1, sticky="w", pady=(0, 4))

        # ---- v3.0.0: 引擎选择 + BT 设置 ----
        tk.Frame(f, bg=BORDER, height=1).grid(row=11, column=0, columnspan=3,
                                              sticky="we", pady=(8, 6))
        tk.Label(f, text="下载引擎 / BT 磁力", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(row=12, column=0, columnspan=3,
                                               sticky="w", pady=(0, 6))
        # 引擎单选
        eng_frame = tk.Frame(f, bg=BG)
        eng_frame.grid(row=13, column=1, sticky="w", pady=(0, 4))
        tk.Radiobutton(eng_frame, text="内置多线程引擎（默认，最快）",
                       variable=self.v_engine, value="builtin",
                       bg=BG, fg=FG, font=(FONT, 9),
                       activebackground=BG, selectcolor="#fff").pack(side="left", padx=(0, 12))
        tk.Radiobutton(eng_frame, text="aria2c 引擎（兼容性最强）",
                       variable=self.v_engine, value="aria2",
                       bg=BG, fg=FG, font=(FONT, 9),
                       activebackground=BG, selectcolor="#fff").pack(side="left")
        self._lbl(f, "默认下载引擎", 13)
        # BT trackers 源
        self._ent(f, self.v_bt_source, 14, 38)
        self._lbl(f, "BT Trackers 源", 14)
        tk.Label(f, text="每次启动时自动从此地址拉取最新 trackers", bg=BG, fg=FG_DIM,
                 font=(FONT, 8)).grid(row=14, column=2, sticky="w", padx=(8, 0), pady=(0, 7))
        # 自动更新开关
        cb_bt = tk.Checkbutton(f, text="启动时自动更新 BT trackers（推荐）",
                               variable=self.v_bt_autoupdate, bg=BG,
                               fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb_bt.grid(row=15, column=1, sticky="w", pady=(0, 4))
        # v3.0.2: tracker 测速过滤开关（默认开启）
        cb_bt_probe = tk.Checkbutton(f, text="BT Tracker 测速过滤（只保留可达 tracker，推荐开启）",
                                     variable=self.v_bt_probe_alive, bg=BG,
                                     fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb_bt_probe.grid(row=16, column=1, sticky="w", pady=(0, 4))

        # ---- 系统集成 ----
        tk.Frame(f, bg=BORDER, height=1).grid(row=17, column=0, columnspan=3,
                                              sticky="we", pady=(8, 6))
        tk.Label(f, text="系统集成（让浏览器点击下载自动唤起）", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(row=18, column=0, columnspan=3,
                                               sticky="w", pady=(0, 6))
        cb3 = tk.Checkbutton(f, text="开机自启（后台静默运行，不弹窗）",
                             variable=self.v_autostart, bg=BG, fg=FG, font=(FONT, 9),
                             activebackground=BG, selectcolor="#fff")
        cb3.grid(row=19, column=0, columnspan=2, sticky="w", pady=(0, 4))
        cb4 = tk.Checkbutton(f, text="注册 xzd:// 协议（扩展可唤起下载器）",
                             variable=self.v_protocol, bg=BG, fg=FG, font=(FONT, 9),
                             activebackground=BG, selectcolor="#fff")
        cb4.grid(row=20, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.lbl_si = tk.Label(f, text="", bg=BG, fg=FG_DIM, font=(FONT, 8),
                               justify="left", wraplength=420)
        self.lbl_si.grid(row=21, column=0, columnspan=3, sticky="w", pady=(2, 4))
        if self.si is None:
            cb3.config(state="disabled")
            cb4.config(state="disabled")
            self.lbl_si.config(text="当前环境不支持系统集成（仅 Windows 可用）", fg=ERR)
        else:
            self._refresh_si_label()

        tk.Label(f, text="全程离线：不检查更新、不联网校验、不上报任何数据",
                 bg=BG, fg=OK, font=(FONT, 8)).grid(row=22, column=0, columnspan=3,
                                                      sticky="w", pady=(4, 8))

        # 底部按钮栏（固定，不随内容滚动）
        btns = tk.Frame(outer, bg=BG)
        btns.grid(row=1, column=0, columnspan=2, sticky="we", padx=18, pady=(8, 12))
        self._reset_hint = tk.Label(btns, text="", bg=BG, fg=OK, font=(FONT, 9))
        self._reset_hint.pack(side="left", padx=(0, 12))
        tk.Button(btns, text="恢复默认设置", command=self._reset_defaults, font=(FONT, 9),
                  bg="#fff0f0", fg="#c5221f", relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left", padx=(0, 12))
        tk.Button(btns, text="取消", command=self.destroy, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=18, pady=4, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="保存", command=self._ok, font=(FONT, 10, "bold"), bg=ACCENT,
                  fg="#fff", relief="flat", padx=18, pady=4, activebackground=ACCENT_DARK,
                  cursor="hand2").pack(side="left")

    def _reset_defaults(self):
        if not messagebox.askyesno("恢复默认设置",
                                   "确定将所有设置恢复为默认值吗？\n"
                                   "已下载的文件和任务记录不会被删除。",
                                   icon="warning"):
            return
        # 保留当前安装相关状态不在这里处理；Manager 负责把配置写回
        defaults = dict(dlcore.DEFAULT_CFG)
        self.mgr.set_cfg(**defaults)
        # 刷新界面变量
        self.v_dir.set(defaults.get("download_dir", ""))
        self.v_conns.set(int(defaults.get("connections", 24)))
        self.v_par.set(int(defaults.get("max_parallel_tasks", 3)))
        self.v_limit.set(int(defaults.get("speed_limit", 0)) // 1024)
        self.v_proxy.set(defaults.get("proxy", ""))
        self.v_ua.set(defaults.get("user_agent", dlcore.DEFAULT_UA))
        self.v_tmp.set(bool(defaults.get("delete_tmp_after_done", True)))
        self.v_insecure.set(bool(defaults.get("insecure", False)))
        self.v_port.set(int(defaults.get("port", 8763)))
        self.v_clipboard.set(bool(defaults.get("clipboard_watch", True)))
        self.v_engine.set(defaults.get("engine", "builtin"))
        self.v_bt_source.set(defaults.get("bt_tracker_source",
                                          "https://cf.trackerslist.com/best.txt"))
        self.v_bt_autoupdate.set(bool(defaults.get("bt_trackers_autoupdate", True)))
        self.v_bt_probe_alive.set(bool(defaults.get("bt_tracker_probe_alive", True)))
        # 剪贴板监听立即生效
        if self.app:
            self.app._clip_enabled = bool(self.v_clipboard.get())
        # 系统集成保持现状（用户可能只是想重置下载设置）
        messagebox.showinfo("恢复默认设置", "已恢复默认设置，点击“保存”后生效。")
        try:
            self._reset_hint.config(text="已恢复默认设置，点击“保存”后生效。", fg=OK)
        except Exception:
            pass

    def _save_screenshot(self):
        """把当前设置窗口截图保存为安装目录下的 default-settings.png。"""
        try:
            from PIL import ImageGrab
        except Exception as e:
            messagebox.showwarning("截图失败", f"缺少 PIL 库：{e}\n请安装 pillow 后重试。",
                                   parent=self)
            return
        try:
            self.update_idletasks()
            x = self.winfo_rootx()
            y = self.winfo_rooty()
            w = self.winfo_width()
            h = self.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            if dlcore.is_frozen():
                root_dir = os.path.dirname(os.path.abspath(sys.executable))
            else:
                root_dir = dlcore.app_root()
            path = os.path.join(root_dir, "default-settings.png")
            img.save(path)
            messagebox.showinfo("截图已保存", f"默认设置截图已保存到：\n{path}", parent=self)
        except Exception as e:
            messagebox.showwarning("截图失败", f"保存截图时出错：{e}", parent=self)

    def _refresh_si_label(self):
        if not self.si:
            return
        try:
            st = self.si.status()
            running = "运行中" if st["running"] else "未运行"
            self.lbl_si.config(
                text=f"状态：{running} · 接口端口 {st['port']}\n"
                     f"启动器：{os.path.basename(st['python'])}（静默无窗口）",
                fg=FG_DIM)
        except Exception:
            dlcore.LOG.debug('忽略异常：st = si.status()')

    def _pick(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or os.path.expanduser("~"))
        if d:
            var.set(os.path.normpath(d))

    def _ok(self):
        all_ok = all(self._validate(k) for k in self._entries)
        if not all_ok:
            messagebox.showwarning("输入有误", "请修正标红的设置项后再保存。", parent=self)
            return
        try:
            limit = max(0, int(self.v_limit.get() or 0)) * 1024
        except Exception:
            limit = 0
        self.mgr.set_cfg(
            download_dir=self.v_dir.get().strip() or dlcore.DEFAULT_CFG["download_dir"],
            connections=max(1, min(32, int(self.v_conns.get() or 24))),
            max_parallel_tasks=max(1, min(20, int(self.v_par.get() or 3))),
            speed_limit=limit,
            proxy=self.v_proxy.get().strip(),
            user_agent=self.v_ua.get().strip() or dlcore.DEFAULT_UA,
            delete_tmp_after_done=bool(self.v_tmp.get()),
            insecure=bool(self.v_insecure.get()),
            port=max(1024, min(65535, int(self.v_port.get() or 8763))),
            clipboard_watch=bool(self.v_clipboard.get()),
            # v3.0.0: 引擎与 BT
            engine=self.v_engine.get() or "builtin",
            bt_tracker_source=self.v_bt_source.get().strip() or "https://cf.trackerslist.com/best.txt",
            bt_trackers_autoupdate=bool(self.v_bt_autoupdate.get()),
            bt_tracker_probe_alive=bool(self.v_bt_probe_alive.get()),
        )
        # 剪贴板监听开关立即生效
        if self.app:
            self.app._toggle_clipboard(bool(self.v_clipboard.get()))
        # 限速立即应用于进行中的任务
        for t in self.mgr.tasks.values():
            if t.speed_limit or limit:
                t.set_speed_limit(limit)
        # 应用系统集成开关
        msgs = []
        if self.si:
            try:
                want_auto = bool(self.v_autostart.get())
                if want_auto != self.si.is_autostart_enabled():
                    ok, info = (self.si.enable_autostart(True) if want_auto
                                else self.si.disable_autostart())
                    msgs.append("开机自启已" + ("开启" if want_auto else "关闭")
                                if ok else f"自启设置失败：{info}")
                want_proto = bool(self.v_protocol.get())
                if want_proto != self.si.is_protocol_registered():
                    ok, info = (self.si.register_protocol() if want_proto
                                else self.si.unregister_protocol())
                    msgs.append("xzd:// 协议已" + ("注册" if want_proto else "注销")
                                if ok else f"协议设置失败：{info}")
            except Exception as e:  # noqa
                msgs.append(f"系统集成应用失败：{e}")
        self.destroy()
        if msgs and self.app:
            self.app.lbl_status.config(text=" · ".join(msgs))


class HelpDialog(Dialog):
    """帮助页面：版本、功能、扩展安装、设置含义、默认设置截图。"""

    HELP_TEXT = """小助下载器 XZDM 使用帮助

【当前版本】v{version}

【软件功能】
· 多线程满速下载（内置引擎 / aria2c 备用）
· 浏览器下载接管（Chrome / Edge 通用扩展）
· 网页媒体嗅探（m3u8 / mp4 / mp3 / flv…）
· BT / 磁力链下载（aria2c + 自动 tracker 更新）
· 剪贴板监听（复制链接自动弹新建下载）
· 断点续传与任务恢复
· 开机自启 + 后台静默运行
· HLS 视频下载与合并
· 音视频合并（FFmpeg）

【浏览器扩展安装】
1. 打开 Chrome：chrome://extensions 或 Edge：edge://extensions
2. 打开右上角「开发者模式」
3. 点「加载已解压的扩展程序」
4. 选择安装目录下的 browser-extension 文件夹
5. 建议把扩展图标固定到工具栏

【主要功能使用】
· 新建下载：点工具栏「+ 新建下载」粘贴链接；或直接 Ctrl+V
· 动态加速：右键下载任务 →「增加 8/16 个线程」
· aria2c 重试：任务失败或卡住 → 工具栏「⚡ aria2c 重试」
· 分析网页：点「分析网页」粘贴网页地址，列出媒体资源
· 托盘：双击显示窗口 / 右键暂停继续退出

【设置项含义】
· 默认保存目录：新任务默认落盘位置
· 默认线程数：每个任务连接数（1-32，默认 24）
· 同时下载任务数：超过后任务排队
· 全局限速：0 = 不限速，单位 KB/s
· 代理服务器：形如 http://127.0.0.1:7890
· User-Agent：默认 Chrome UA
· 本地接口端口：与浏览器扩展通信，默认 8763
· 任务完成后自动清理临时分片：删除 .xzd 临时目录
· 忽略 HTTPS 证书错误：仅特殊环境使用
· 剪贴板监听：复制链接自动弹新建下载
· 默认下载引擎：builtin 内置 / aria2c 备用
· BT Trackers 源：启动时自动拉取 tracker 列表
· 启动时自动更新 BT trackers：建议开启
· BT Tracker 测速过滤：只保留可达 tracker
· 开机自启：后台静默运行
· 注册 xzd:// 协议：浏览器点击下载自动唤起
"""

    def __init__(self, master, mgr):
        super().__init__(master, "帮助", 620, 520, resizable=True, scrollable=True)
        self.mgr = mgr
        self._build()

    def _build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        vsb.grid(row=0, column=1, sticky="ns", pady=(8, 0))

        f = tk.Frame(self._canvas, bg=BG, padx=18, pady=14)
        self._canvas_window = self._canvas.create_window((0, 0), window=f, anchor="nw",
                                                         width=self._dlg_w - 48)
        f.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._on_dlg_configure())

        def _on_mousewheel(event):
            try:
                self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass
        self.bind_all("<MouseWheel>", _on_mousewheel)

        # 标题 + 版本
        tk.Label(f, text="小助下载器 XZDM", bg=BG, fg=FG,
                 font=(FONT, 13, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(f, text=f"当前版本：v{dlcore.ENGINE_VERSION}", bg=BG, fg=ACCENT,
                 font=(FONT, 9)).pack(anchor="w", pady=(0, 12))

        # 功能列表
        tk.Label(f, text="软件功能", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(0, 8))
        features = [
            "多线程满速下载（内置引擎 / aria2c 备用）",
            "浏览器下载接管（Chrome / Edge 通用扩展）",
            "网页媒体嗅探（m3u8 / mp4 / mp3 / flv…）",
            "BT / 磁力链下载（aria2c + 自动 tracker 更新）",
            "剪贴板监听（复制链接自动弹新建下载）",
            "断点续传与任务恢复",
            "开机自启 + 后台静默运行",
            "HLS 视频下载与合并",
            "音视频合并（FFmpeg）",
        ]
        for ft in features:
            tk.Label(f, text="· " + ft, bg=BG, fg=FG, font=(FONT, 9)).pack(anchor="w", pady=(2, 2))

        # 浏览器扩展安装
        tk.Label(f, text="浏览器扩展安装", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 8))
        steps = [
            "打开 Chrome：chrome://extensions 或 Edge：edge://extensions",
            "打开右上角「开发者模式」",
            "点「加载已解压的扩展程序」",
            f"选择安装目录下的 browser-extension 文件夹（{dlcore.app_root() if not dlcore.is_frozen() else '安装目录'}\\browser-extension）",
            "建议把扩展图标固定到工具栏",
        ]
        for i, st in enumerate(steps, 1):
            tk.Label(f, text=f"{i}. {st}", bg=BG, fg=FG, font=(FONT, 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(2, 2))

        # 功能使用说明
        tk.Label(f, text="主要功能使用", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 8))
        usage = [
            "新建下载：点工具栏「+ 新建下载」粘贴链接；或直接 Ctrl+V",
            "动态加速：右键下载任务 →「增加 8/16 个线程」",
            "aria2c 重试：任务失败或卡住 → 工具栏「⚡ aria2c 重试」",
            "分析网页：点「分析网页」粘贴网页地址，列出媒体资源",
            "托盘：双击显示窗口 / 右键暂停继续退出",
        ]
        for u in usage:
            tk.Label(f, text="· " + u, bg=BG, fg=FG, font=(FONT, 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(2, 2))

        # 设置项含义
        tk.Label(f, text="设置项含义", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 8))
        settings = [
            ("默认保存目录", "新任务默认落盘位置"),
            ("默认线程数", "每个任务连接数（1-32，默认 24）"),
            ("同时下载任务数", "超过后任务排队"),
            ("全局限速", "0 = 不限速，单位 KB/s"),
            ("代理服务器", "形如 http://127.0.0.1:7890"),
            ("User-Agent", "默认 Chrome UA"),
            ("本地接口端口", "与浏览器扩展通信，默认 8763"),
            ("任务完成后自动清理临时分片", "删除 .xzd 临时目录"),
            ("忽略 HTTPS 证书错误", "仅特殊环境使用"),
            ("剪贴板监听", "复制链接自动弹新建下载"),
            ("默认下载引擎", "builtin 内置 / aria2c 备用"),
            ("BT Trackers 源", "启动时自动拉取 tracker 列表"),
            ("启动时自动更新 BT trackers", "建议开启"),
            ("BT Tracker 测速过滤", "只保留可达 tracker"),
            ("开机自启", "后台静默运行"),
            ("注册 xzd:// 协议", "浏览器点击下载自动唤起"),
        ]
        for name, desc in settings:
            tk.Label(f, text=f"· {name}：{desc}", bg=BG, fg=FG, font=(FONT, 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(2, 2))

        # 默认设置截图占位
        tk.Label(f, text="默认设置截图", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 8))
        tk.Label(f, text="在设置页面按 Ctrl+Alt+P 可保存当前截图到安装目录。",
                 bg=BG, fg=FG_DIM, font=(FONT, 8)).pack(anchor="w", pady=(0, 6))
        self._img_lbl = tk.Label(f, text="未找到默认设置截图", bg="#eef1f6", fg=FG_DIM,
                                 font=(FONT, 9), width=60, height=12, relief="flat")
        self._img_lbl.pack(anchor="w", pady=(0, 8))
        self._load_default_settings_image()

        # 关闭按钮
        btns = tk.Frame(outer, bg=BG)
        btns.grid(row=1, column=0, columnspan=2, sticky="e", padx=18, pady=(8, 12))
        tk.Button(btns, text="关闭", command=self.destroy, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=18, pady=5, cursor="hand2").pack(side="left")

    def _load_default_settings_image(self):
        """加载安装目录下的 default-settings.png，若不存在则显示提示。"""
        candidates = [
            os.path.join(dlcore.app_root(), "default-settings.png"),
        ]
        if dlcore.is_frozen():
            candidates.insert(0, os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                              "default-settings.png"))
        candidates.append(os.path.join(dlcore.app_root(), "browser-extension", "default-settings.png"))
        path = ""
        for c in candidates:
            if os.path.exists(c):
                path = c
                break
        if not path:
            self._img_lbl.config(text="未找到默认设置截图（default-settings.png）\n"
                                     "可在设置页面按 Ctrl+Alt+P 保存截图到安装目录。")
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            # 等比缩放到宽度 520
            w, h = img.size
            max_w = 520
            if w > max_w:
                ratio = max_w / w
                img = img.resize((max_w, int(h * ratio)), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._img_lbl.config(image=photo, text="", width=0, height=0, bg=BG)
            self._img_lbl.image = photo  # 保持引用
        except Exception:
            self._img_lbl.config(text="默认设置截图加载失败")


class WebScanDialog(Dialog):
    """分析网页：由下载器代为抓取页面，列出所有媒体资源"""

    def __init__(self, master, app, preset_url: str = ""):
        super().__init__(master, "分析网页媒体", 720, 460)
        self.app = app
        self.v_url = tk.StringVar(value=preset_url)
        self.items: list[dict] = []
        self._build()
        if preset_url:
            self.after(200, self._scan)

    def _build(self):
        top = tk.Frame(self, bg=BG, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="网页地址", bg=BG, fg=FG_DIM, font=(FONT, 9)).pack(side="left")
        e = tk.Entry(top, textvariable=self.v_url, font=(FONT, 9), bg="#fff", fg=FG,
                     relief="solid", bd=1, highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        e.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        tk.Button(top, text="分析", command=self._scan, font=(FONT, 10, "bold"), bg=ACCENT,
                  fg="#fff", relief="flat", padx=16, pady=4, activebackground=ACCENT_DARK,
                  cursor="hand2").pack(side="left")
        mid = tk.Frame(self, bg=BG, padx=16)
        mid.pack(fill="both", expand=True)
        cols = ("type", "name", "url")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12,
                                 selectmode="extended")
        for c, t, w in (("type", "类型", 70), ("name", "文件名", 200), ("url", "地址", 400)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c != "type" else "center")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        bot = tk.Frame(self, bg=BG, padx=16, pady=12)
        bot.pack(fill="x")
        self.lbl = tk.Label(bot, text="输入网页地址后点击「分析」，自动提取 m3u8 / mp4 / 音频等资源",
                            bg=BG, fg=FG_DIM, font=(FONT, 9))
        self.lbl.pack(side="left")
        tk.Button(bot, text="全部下载", command=self._dl_all, font=(FONT, 10, "bold"),
                  bg=OK, fg="#fff", relief="flat", padx=14, pady=4, cursor="hand2").pack(side="right")
        tk.Button(bot, text="仅音频", command=lambda: self._dl_sel(audio_only=True),
                  font=(FONT, 9), bg="#e67e22", fg="#fff", relief="flat", padx=14, pady=4,
                  cursor="hand2").pack(side="right", padx=6)
        tk.Button(bot, text="下载视频", command=lambda: self._dl_sel(audio_only=False),
                  font=(FONT, 9), bg=ACCENT, fg="#fff", relief="flat", padx=14, pady=4,
                  cursor="hand2").pack(side="right", padx=6)
        self.tree.bind("<Double-1>", lambda e: self._dl_sel(audio_only=False))
        e.focus_set()
        self.bind("<Return>", lambda ev: self._scan())

    def _scan(self):
        url = self.v_url.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("提示", "请填写以 http:// 或 https:// 开头的网页地址", parent=self)
            return
        self.lbl.config(text="正在分析…", fg=ACCENT)
        self.update_idletasks()

        def work():
            try:
                res = dlcore.scan_webpage(url)
                self.after(0, lambda: self._show(res))
            except Exception as e:  # noqa
                self.after(0, lambda: self.lbl.config(
                    text=f"分析失败：{type(e).__name__}: {e}", fg=ERR))

        threading.Thread(target=work, daemon=True).start()

    def _show(self, res: dict):
        self.items = res.get("items", [])
        for i in self.tree.get_children():
            self.tree.delete(i)
        for it in self.items:
            self.tree.insert("", "end", values=(it["type"], it["filename"], it["url"]))
        title = res.get("title") or ""
        self.lbl.config(text=f"发现 {len(self.items)} 个媒体资源" +
                             (f" · 页面：{title}" if title else ""), fg=OK if self.items else FG_DIM)
        if self.items and self.app:
            page = res.get("page", "")
            self.app.mgr.add_sniff_items(page, self.items)

    def _dl_sel(self, audio_only: bool = False):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要下载的资源", parent=self)
            return
        items = [self.items[self.tree.index(i)] for i in sel]
        self._add(items, audio_only=audio_only)

    def _dl_all(self):
        if not self.items:
            messagebox.showinfo("提示", "没有可下载的资源", parent=self)
            return
        self._add(self.items)

    def _add(self, items: list[dict], audio_only: bool = False):
        page = self.v_url.get().strip()
        for it in items:
            kind = it.get("type") if it.get("type") == "hls" else "auto"
            if audio_only:
                # 音频模式：文件名加 .mp3 后缀，dlcore 下载后 FFmpeg 抽音轨
                fn = it.get("filename", "")
                if not fn.lower().endswith(".mp3"):
                    fn = os.path.splitext(fn)[0] + ".mp3"
                self.app.mgr.add_task(it["url"], referer=page, filename=fn,
                                      kind=kind, extra={"audio_only": True})
            else:
                # 视频模式：检查是否有配对音频，有则标记合并
                extra = {}
                if it.get("paired_audio"):
                    extra["paired_audio"] = it["paired_audio"]
                self.app.mgr.add_task(it["url"], referer=page, filename=it.get("filename", ""),
                                      kind=kind, extra=extra if extra else None)
        self.app.refresh_now()
        mode = "音频" if audio_only else "视频"
        self.lbl.config(text=f"已加入下载队列：{len(items)} 个{mode}任务", fg=OK)
        self.after(600, self.destroy if len(items) > 3 else lambda: None)


# ------------------------------ 主窗口 ------------------------------

class App:
    def __init__(self, silent: bool = False, protocol_url: str = ""):
        self.silent = silent
        self.protocol_url = protocol_url or ""
        # v3.0.8: 开启 DPI 感知，解决 125%/150% 缩放下字体图标模糊
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        boot_mark("tk_ready")
        self.root.title(f"{dlcore.APP_NAME} v{dlcore.ENGINE_VERSION}")
        self.root.geometry("1100x620")
        self.root.minsize(880, 480)
        self.root.configure(bg=BG)

        self._setup_style()
        boot_mark("style_done")
        self.mgr = dlcore.Manager(on_notify=self._schedule_refresh)
        self.mgr.on_task_done = self._on_task_done
        # 端口：配置优先，其次 sysintegration.api_port()（支持 XZD_API_PORT 环境覆盖，
        # 供启动计时/并行测试时多实例互不干扰；正常运行取默认 8763）
        try:
            _port = sysintegration.api_port(int(self.mgr.cfg.get("port", 8763)))
        except Exception:
            _port = int(self.mgr.cfg.get("port", 8763))
        self.api = localserver.LocalAPI(self.mgr, port=_port,
                                        on_wake=self._on_api_wake)
        self.api_ok = self.api.start()
        boot_mark("api_started")

        self._ui_q: queue.Queue = queue.Queue()
        # 刷新缓存（v1.9.0）：只在内容真正变化时才触发 Tk 重绘
        self._row_cache: dict = {}
        self._sniff_sig = None
        self._hover_row = None          # 悬停高亮行（v1.12.0）
        self._last_speed_txt = "\u0000"
        self._last_status_txt = "\u0000"
        self._last_tab_txt = ("\u0000", "\u0000")
        # 托盘相关先占位，避免刷新回调提前访问
        self.tray = None
        self._tray_state = "normal"
        self._build()
        boot_mark("ui_built")
        self.restored = self.mgr.load_jobs(auto_start=False)
        boot_mark("jobs_loaded")
        self.refresh_now()
        self.root.after(600, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-v>", lambda e: self._paste_url())
        self.root.bind("<Delete>", lambda e: self.del_selected())
        # 记录自己的 PID：便于外部（安装器/体检脚本）判断是否已运行，
        # 也用于异常退出后的残留识别
        self._write_pid()

        # ---- 系统托盘（后台静默运行的关键）----
        # v1.6.0：托盘改为**异步**创建 —— 托盘要走 Shell_NotifyIconW + 建隐藏窗口，
        # 实测要 0.3~0.4s。它跟界面首帧没有任何依赖，放在关键路径上白白拖慢启动。
        # 现在用 after(1) 让主线程先把窗口画出来，再创建托盘。
        self.root.after(1, self._init_tray_async)
        boot_mark("app_init_done")

        # 静默启动：不显示窗口，只留托盘
        self._window_shown = True          # 是否处于"窗口可见"状态
        if self.silent:
            self.root.withdraw()
            self._window_shown = False
        # 处理协议唤起（xzd://add?url=...）
        if self.protocol_url:
            self.root.after(800, lambda: self._handle_protocol(self.protocol_url))
        boot_mark("app_init_done")
        # 首帧真正画出之后再打一个点（用户看到界面 = 这个时刻）
        self.root.after(0, lambda: (boot_mark("first_paint"), boot_flush()))

        # ---- 剪贴板监听（复制 URL 自动弹新建下载）----
        self._clip_last = ""
        self._clip_enabled = self.mgr.cfg.get("clipboard_watch", True)
        self._clip_native = None
        self._clip_fallback_thread = None
        if self._clip_enabled:
            self._start_clipboard_listener()

    def _write_pid(self):
        try:
            p = os.path.join(self.mgr.data_dir, "gui.pid")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            os.replace(tmp, p)
        except Exception:
            dlcore.LOG.debug('忽略异常：p = os.path.join(mgr.data_dir, "gui.pid")')

    def _clear_pid(self):
        try:
            p = os.path.join(self.mgr.data_dir, "gui.pid")
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            dlcore.LOG.debug('忽略异常：p = os.path.join(mgr.data_dir, "gui.pid")')

    # ---------- 托盘 ----------
    def _init_tray_async(self):
        """异步创建托盘：不阻塞首帧；顺带把"托盘就绪"打点补上"""
        self._init_tray()
        boot_mark("tray_done")
        if _BOOT_TRACE and "first_paint" in dict(_BOOT_MARKS):
            boot_flush()

    def _init_tray(self):
        try:
            import tray as traymod
            self.tray = traymod.Tray(
                on_show=lambda: self.root.after(0, self.show_window),
                on_pause=lambda: self.root.after(0, lambda: (self.mgr.pause_all(),
                                                             self.refresh_now())),
                on_resume=lambda: self.root.after(0, lambda: (self.mgr.resume_all(),
                                                              self.refresh_now())),
                on_open_dir=lambda: self.root.after(
                    0, lambda: open_in_explorer(self.mgr.cfg.get("download_dir", ""))),
                on_exit=lambda: self.root.after(0, self._quit_app),
                get_status=self._tray_status,
                idle_hint=f"{dlcore.APP_NAME} · 后台运行中",
            )
            if not self.tray.start():
                self.tray = None
        except Exception:
            self.tray = None

    def _tray_status(self) -> str:
        """托盘悬浮提示：实时速度 + 任务数"""
        try:
            snaps = self.mgr.snapshots()
            act = [s for s in snaps if s["state"] == dlcore.STATE_DOWNLOADING]
            done = sum(1 for s in snaps if s["state"] == dlcore.STATE_DONE)
            if act:
                total = self.mgr.total_speed()
                return (f"下载中 {len(act)} 个 · {dlcore.human_speed(total)}"
                        + (f" · 已完成 {done}" if done else ""))
            if done:
                return f"空闲 · 已完成 {done} 个任务"
            wait = sum(1 for s in snaps if s["state"] in
                       (dlcore.STATE_WAIT, dlcore.STATE_PAUSED))
            if wait:
                return f"{wait} 个任务等待中"
            return "空闲中 · 等待下载任务"
        except Exception:
            return "后台运行中"

    def _update_tray_icon(self, snaps=None):
        tray = getattr(self, "tray", None)
        if not tray:
            return
        try:
            if snaps is None:
                snaps = self.mgr.snapshots()
            if any(s["state"] == dlcore.STATE_ERROR for s in snaps):
                st = "error"
            elif any(s["state"] == dlcore.STATE_DOWNLOADING for s in snaps):
                st = "busy"
            elif snaps and all(s["state"] == dlcore.STATE_PAUSED for s in snaps):
                st = "paused"
            else:
                st = "normal"
            if st != self._tray_state:
                self._tray_state = st
                tray.set_state(st)
        except Exception:
            dlcore.LOG.debug('忽略异常：if snaps is None: snaps = mgr.snapshots()')

    def show_window(self):
        """从托盘恢复主窗口"""
        try:
            self.root.deiconify()
            self._window_shown = True
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
            self.root.after(20, self.refresh_now)   # 立刻补一次刷新，别等 tick
        except Exception:
            dlcore.LOG.debug('忽略异常：root.deiconify()')

    def _on_api_wake(self, link: str):
        """接口 /wake 回调（来自另一个进程的协议唤起）——必须在主线程处理"""
        try:
            self.root.after(0, lambda: self._handle_protocol(link))
        except Exception:
            dlcore.LOG.debug('忽略异常：root.after(0, lambda: _handle_protocol(link))')

    def _handle_protocol(self, url: str):
        """处理 xzd:// 协议唤起：add?url=xxx 或 show"""
        try:
            import sysintegration as si
            r = si.wake_from_protocol(url)
            if isinstance(r, tuple) and r and r[0] == "__ADD__":
                _, target, ref, name = r
                self.mgr.add_task(target, referer=ref, filename=name, kind="auto")
                self.refresh_now()
                if self.tray:
                    self.tray.notify("已开始下载", dlcore.filename_from_url(target))
                return
            if r is True:
                self.show_window()
        except Exception:
            # 协议唤起失败 = 用户点了下载链接却"毫无反应"，必须留痕
            dlcore.LOG.once_exc("protocol", "协议唤起处理失败 url=%r", url)

    # ---------- 样式 ----------
    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            dlcore.LOG.debug('忽略异常：st.theme_use("clam")')
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                     rowheight=30, borderwidth=0, font=(FONT, 9))
        st.configure("Treeview.Heading", background="#eef1f6", foreground=FG_DIM,
                     relief="flat", font=(FONT, 9, "bold"), padding=(6, 6))
        st.map("Treeview", background=[("selected", "#dbe7ff")],
               foreground=[("selected", FG)])
        st.map("Treeview.Heading", background=[("active", "#e3e8f0")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#e9ecf1", foreground=FG_DIM,
                     padding=(18, 8), font=(FONT, 9), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", CARD)],
               foreground=[("selected", ACCENT)])
        st.configure("TSeparator", background=BORDER)

    # ---------- 界面 ----------
    def _build(self):
        # 顶部工具栏
        tb = tk.Frame(self.root, bg=CARD, height=54)
        tb.pack(fill="x", side="top")
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="top")
        inner = tk.Frame(tb, bg=CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=9)

        def btn(parent, text, cmd, primary=False, width=None, compact=False):
            b = tk.Button(parent, text=text, command=cmd,
                          font=(FONT, 8 if compact else 9),
                          bg=ACCENT if primary else "#eef1f6",
                          fg="#fff" if primary else FG, relief="flat",
                          padx=10 if compact else 14, pady=4 if compact else 5,
                          cursor="hand2",
                          activebackground=ACCENT_DARK if primary else "#e2e7ee",
                          activeforeground="#fff" if primary else FG)
            if width:
                b.config(width=width)
            b.pack(side="left", padx=(0, 6))
            return b

        # v3.1.0：单行紧凑工具栏。新建下载统一入口支持 HTTP/磁力/种子
        row1 = tk.Frame(inner, bg=CARD)
        row1.pack(fill="x")
        btn(row1, "+ 新建下载", self.new_download, primary=True)
        btn(row1, "分析网页", lambda: WebScanDialog(self.root, self), compact=True)
        btn(row1, "音视频合并", lambda: MergeAVDialog(self.root, self), compact=True)
        btn(row1, "清除已完成", self.clear_done, compact=True)
        btn(row1, "⚡ aria2c 重试", self.retry_selected_with_aria2, compact=True)
        right = tk.Frame(row1, bg=CARD)
        right.pack(side="right")
        btn(right, "帮助", self._open_help, compact=True)
        btn(right, "设置", self._open_settings_panel, compact=True)
        btn(right, "打开目录", lambda: open_in_explorer(self.mgr.cfg.get("download_dir", "")), compact=True)

    def _open_help(self):
        HelpDialog(self.root, self.mgr)

        # 主体：两个标签页
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self.tab_tasks = tk.Frame(self.nb, bg=CARD)
        self.tab_sniff = tk.Frame(self.nb, bg=CARD)
        self.nb.add(self.tab_tasks, text="  下载任务  ")
        self.nb.add(self.tab_sniff, text="  嗅探到的媒体  ")
        self._build_tasks()
        self._build_sniff()

        # 状态栏
        sb = tk.Frame(self.root, bg=BG, height=28)
        sb.pack(fill="x", side="bottom", padx=14, pady=(0, 6))
        self.lbl_status = tk.Label(sb, text="", bg=BG, fg=FG_DIM, font=(FONT, 8))
        self.lbl_status.pack(side="left")
        self.lbl_speed = tk.Label(sb, text="", bg=BG, fg=ACCENT, font=(FONT, 9, "bold"))
        self.lbl_speed.pack(side="right")

    def _build_tasks(self):
        wrap = tk.Frame(self.tab_tasks, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=2, pady=2)
        cols = ("name", "size", "progress", "speed", "eta", "state", "conns", "dir")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        heads = (("name", "文件名", 300, "w"), ("size", "大小", 90, "e"),
                 ("progress", "进度", 150, "center"), ("speed", "速度", 100, "e"),
                 ("eta", "剩余", 80, "e"), ("state", "状态", 130, "w"),
                 ("conns", "线程", 55, "center"), ("dir", "保存位置", 200, "w"))
        for c, t, w, a in heads:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c in ("name", "dir")))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground=OK)
        self.tree.tag_configure("error", foreground=ERR)
        self.tree.tag_configure("paused", foreground=WARN)
        self.tree.tag_configure("dl", foreground=FG)
        # v1.12.0：新增"等待中/已取消"两色，避免所有非活动状态都是灰色一片
        self.tree.tag_configure("wait", foreground=FG_DIM)
        self.tree.tag_configure("cancelled", foreground=FG_DIM)
        # 悬停行背景（v1.12.0）。
        # 与状态 tag 无冲突：状态 tag 只设 foreground，这个只设 background。
        self.tree.tag_configure("hover", background="#eef4ff")
        self.tree.bind("<Double-1>", self._open_selected_file)
        self.tree.bind("<Button-3>", self._task_menu)
        # v1.12.0：鼠标悬停高亮。20 行以上的表格里，行间错位是常见的"看错行"来源。
        # 实现上只改 tag、不重算内容，开销可忽略。
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._clear_hover())
        self._hover_row = None

        self.ctx = tk.Menu(self.root, tearoff=0, font=(FONT, 9))
        # v3.0.2：右键菜单置顶状态控制按钮（替代工具栏的“全部继续/全部暂停”）
        self.ctx.add_command(label="▶ 继续下载", command=self.resume_selected,
                             font=(FONT, 9, "bold"))
        self.ctx.add_command(label="⏸ 暂停", command=self.pause_selected,
                             font=(FONT, 9, "bold"))
        self.ctx.add_separator()
        # v3.0.8: 插队能力
        self.ctx.add_command(label="⏫ 插队优先", command=self.jump_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="增加 8 个线程", command=lambda: self.boost_selected(8))
        self.ctx.add_command(label="增加 16 个线程", command=lambda: self.boost_selected(16))
        self.ctx.add_separator()
        self.ctx.add_command(label="⚡ 用 aria2c 极速重试", command=self.retry_selected_with_aria2)
        self.ctx.add_separator()
        self.ctx.add_command(label="打开文件", command=self._open_selected_file)
        self.ctx.add_command(label="打开所在文件夹", command=self._open_selected)
        self.ctx.add_command(label="复制下载链接", command=self.copy_selected_url)
        self.ctx.add_separator()
        self.ctx.add_command(label="删除任务（保留文件）", command=lambda: self.del_selected(False))
        self.ctx.add_command(label="删除任务和临时文件", command=lambda: self.del_selected(True))

    def _build_sniff(self):
        top = tk.Frame(self.tab_sniff, bg=CARD)
        top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(top, text="浏览器扩展嗅探到的媒体会自动出现在这里", bg=CARD, fg=FG_DIM,
                 font=(FONT, 9)).pack(side="left")
        tk.Button(top, text="清空", command=lambda: (self.mgr.clear_sniff(), self.refresh_now()),
                  font=(FONT, 9), bg="#eef1f6", fg=FG, relief="flat", padx=12,
                  cursor="hand2").pack(side="right")
        tk.Button(top, text="下载选中", command=self._sniff_download, font=(FONT, 9, "bold"),
                  bg=ACCENT, fg="#fff", relief="flat", padx=12, cursor="hand2").pack(side="right", padx=6)
        wrap = tk.Frame(self.tab_sniff, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ("type", "name", "size", "url", "page")
        self.stree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("type", "类型", 70), ("name", "文件名", 220), ("size", "大小", 90),
                        ("url", "地址", 420), ("page", "来源页面", 260)):
            self.stree.heading(c, text=t)
            self.stree.column(c, width=w, anchor="center" if c in ("type", "size") else "w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.stree.yview)
        self.stree.configure(yscrollcommand=vs.set)
        self.stree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.stree.bind("<Double-1>", lambda e: self._sniff_download())

    # ---------- 刷新 ----------
    def _schedule_refresh(self):
        try:
            self._ui_q.put_nowait("refresh")
        except Exception:
            dlcore.LOG.debug('忽略异常：_ui_q.put_nowait("refresh")')

    def refresh_now(self):
        self._do_refresh()

    def _do_refresh(self):
        # v1.9.0：窗口不可见（纯托盘后台）时，跳过一切"画界面"的工作。
        #   为什么要跳过：Treeview 的 item/insert/delete 会触发 Tk 的几何计算与
        #   重绘请求，即使窗口隐藏也要走一遍 Tcl 解释器 —— 后台常驻时纯属白烧 CPU。
        #   但进度/完成事件仍要处理（托盘图标 + 状态栏文字），否则用户
        #   展开窗口会看到陈旧数据。
        visible = getattr(self, "_window_shown", True)
        if not visible:
            self._refresh_bg_only()
            return
        snaps = self.mgr.snapshots()
        self._refresh_common(snaps)
        sel = set(self.tree.selection())
        existing = set(self.tree.get_children())
        wanted = {s["tid"] for s in snaps}
        for tid in existing - wanted:
            self.tree.delete(tid)
        # v1.9.0：只更新**内容真正变化**的行。
        # 原来每轮无条件 tree.item(values=...) —— Tk 会重建该行所有单元格并重绘，
        # 20 个任务时实测每轮多花几毫秒，且会打断用户的选中/滚动交互。
        prev = self._row_cache
        hover_row = getattr(self, "_hover_row", None)
        for s in snaps:
            tag = ("done" if s["state"] == dlcore.STATE_DONE else
                   "error" if s["state"] == dlcore.STATE_ERROR else
                   "paused" if s["state"] == dlcore.STATE_PAUSED else
                   "cancelled" if s["state"] == dlcore.STATE_CANCELLED else
                   "wait" if s["state"] in (dlcore.STATE_WAIT, dlcore.STATE_PENDING)
                   else "dl")
            # 保留悬停态：否则每轮刷新都会把鼠标下那行的高亮抹掉（闪烁感）
            tags = (tag, "hover") if s["tid"] == hover_row else (tag,)
            vals = (
                s["filename"] or "(未命名)",
                dlcore.human_size(s["size"]),
                f"{bar(s['progress'])} {s['progress'] * 100:5.1f}%",
                dlcore.human_speed(s["speed"]) if s["state"] == dlcore.STATE_DOWNLOADING else "",
                dlcore.human_eta(s["eta"]) if s["state"] == dlcore.STATE_DOWNLOADING else "",
                f"{badge(s['state'])} {s['state_cn']}"
                + (f"：{s['err'][:40]}" if s["err"] else ""),
                s["conns"] if s["kind"] != "hls" else "-",
                s["out_dir"],
            )
            # 缓存键只含状态 tag（不含 hover）—— 悬停变化不触发内容重写，
            # 由 _on_tree_motion 单独处理，避免鼠标一动就重绘整行。
            key = (vals, tag)
            if s["tid"] in existing:
                if prev.get(s["tid"]) != key:
                    self.tree.item(s["tid"], values=vals, tags=tags)
                    prev[s["tid"]] = key
            else:
                self.tree.insert("", "end", iid=s["tid"], values=vals, tags=tags)
                prev[s["tid"]] = key
        # 清掉已删除行的缓存，避免长跑时字典无限增长
        if len(prev) > len(wanted):
            for tid in [k for k in prev if k not in wanted]:
                prev.pop(tid, None)
        for tid in sel:
            if tid in wanted:
                self.tree.selection_add(tid)

        # 嗅探标签：仅在**条目集合**变化时重建（原来每轮 400 行全量 delete+insert）
        items = self.mgr.sniff_items[:400]
        sig = (len(items), items[0]["url"] if items else "", items[-1]["url"] if items else "")
        if self._sniff_sig != sig or len(self.stree.get_children()) != len(items):
            self._sniff_sig = sig
            self.stree.delete(*self.stree.get_children())
            for i, it in enumerate(items):
                self.stree.insert("", "end", iid=str(i),
                                  values=(it["type"], it["name"],
                                          dlcore.human_size(it.get("size")),
                                          it["url"], it.get("page", "")))

    def _refresh_bg_only(self):
        """窗口不可见时的轻量刷新：只做托盘图标 + 状态文字（不碰任何表格）"""
        snaps = self.mgr.snapshots()
        self._refresh_common(snaps)

    @staticmethod
    def _overall_progress(snaps) -> str:
        """所有"未完成任务"的加权总进度（按字节加权）。

        为什么按字节而不是按任务数：
          10 个任务里 9 个是 1MB 小文件、1 个是 5GB 大文件，若按任务数加权，
          小文件下完就显示 90%，但实际上还有 5GB 要下 —— 完全误导。
        体积未知的任务按 0 计算（无法预估，不参与加权），
        若全都没有已知体积则返回空串（不显示）。
        """
        total = 0
        done = 0
        for s in snaps:
            if s["state"] in (dlcore.STATE_DONE, dlcore.STATE_CANCELLED):
                continue
            size = s.get("size") or 0
            if size <= 0:
                continue
            total += size
            done += min(size, max(0, int(size * (s.get("progress") or 0))))
        if total <= 0:
            return ""
        return f"{done / total * 100:.1f}%"

    def _refresh_common(self, snaps):
        """与"窗口是否可见"无关的刷新：托盘图标 + 状态栏 + 标签标题。

        全部走"变化才写"策略：Tk 的 config/text 即使内容相同也会
        触发一次 Tcl 调用与几何重算，对后台常驻进程是不必要的开销。
        """
        # 托盘图标状态
        self._update_tray_icon(snaps)
        # 状态栏
        ts = self.mgr.total_speed()
        act = sum(1 for s in snaps if s["state"] == dlcore.STATE_DOWNLOADING)
        wait = sum(1 for s in snaps if s["state"] == dlcore.STATE_WAIT)
        # v1.12.0：速度前加一个方向符号，与状态列的徽章形成视觉呼应，
        # 也让"总速度"在状态栏里更容易被扫到（原先只有数字）。
        t_speed = f"▼ {dlcore.human_speed(ts)}" if ts else ""
        if t_speed != self._last_speed_txt:
            self._last_speed_txt = t_speed
            self.lbl_speed.config(text=t_speed)
        api_txt = (f"扩展接口：127.0.0.1:{self.api.actual_port} 就绪"
                   if self.api_ok else "扩展接口：未启动")
        # v1.12.0：加"总体进度"—— 多任务同时下载时，
        # 单看每行的百分比无法回答"到底还剩多少"。按字节加权更贴近真实等待感。
        overall = self._overall_progress(snaps)
        t_status = (f"任务 {len(snaps)} 个 · 下载中 {act} · 排队 {wait}"
                    + (f" · 总进度 {overall}" if overall else "")
                    + f" · {api_txt} · 完全离线运行")
        if t_status != self._last_status_txt:
            self._last_status_txt = t_status
            self.lbl_status.config(text=t_status)
        # 标签页标题带数量
        pend = sum(1 for s in snaps if s["state"] not in (dlcore.STATE_DONE,))
        t0 = f"  下载任务 ({pend})  "
        t1 = f"  嗅探到的媒体 ({len(self.mgr.sniff_items)})  "
        if (t0, t1) != self._last_tab_txt:
            self._last_tab_txt = (t0, t1)
            try:
                self.nb.tab(0, text=t0)
                self.nb.tab(1, text=t1)
            except Exception:
                dlcore.LOG.debug('忽略异常：nb.tab(0, text=t0)')

    def _tick(self):
        try:
            while True:
                self._ui_q.get_nowait()
        except queue.Empty:
            pass                              # 队列排空是正常控制流，非异常
        except Exception:
            dlcore.LOG.once_exc("tick_queue", "刷新队列异常")
        try:
            self._do_refresh()
        except Exception:
            # 刷新异常会让界面"卡住不动"且无任何提示，是最难排查的一类问题
            dlcore.LOG.once_exc("tick_refresh", "界面刷新失败")
        # v1.9.0：自适应刷新间隔（原生 600ms → 按状态动态调整）
        #   · 有任务在下载：600ms（视觉流畅，够用）
        #   · 静默后台 / 无下载：放慢到 3 秒
        #     —— 后台驻留时不需要频繁重绘 400 行嗅探表与状态栏
        # ⚠ 不要用 winfo_viewable() 判断窗口是否隐藏：
        #   实测它每次都会触发 Tk 的窗口状态查询，静默后台下每秒轮询多次
        #   会让空闲 CPU 从 0.00% 涨到 2.16%（v1.9.0 实测踩坑）。
        #   改用自己维护的布尔标记（_window_shown），零成本。
        self.root.after(self._next_interval(), self._tick)

    def _next_interval(self) -> int:
        try:
            downloading = any(t.state == dlcore.STATE_DOWNLOADING
                              for t in self.mgr.tasks.values())
            if not self._window_shown:
                return 900 if downloading else 3000
            return 600 if downloading else 1200
        except Exception:
            return 600

    # ---------- 动作 ----------
    def new_download(self, preset=None):
        """v3.1.0：统一新建下载入口（HTTP/磁力/种子）。"""
        d = NewDownloadDialog(self.root, self.mgr, preset)
        self.root.wait_window(d)
        if not d.result:
            return
        r = d.result
        urls = r.get("urls", [r["url"]])
        for u in urls:
            kind = dlcore.kind_of_url(u)
            # 磁力和本地种子直接走 bt；http .torrent 先交给 aria2c 自动处理
            if kind == "bt":
                self.mgr.add_task(u, out_dir=r["out_dir"], filename=r["filename"],
                                  conns=r["conns"], referer=r["referer"], cookie=r["cookie"],
                                  kind="bt")
            else:
                self.mgr.add_task(u, out_dir=r["out_dir"], filename=r["filename"],
                                  conns=r["conns"], referer=r["referer"], cookie=r["cookie"],
                                  kind="auto")
        self.refresh_now()

    def _paste_url(self):
        """v3.1.0：粘贴链接 → 打开统一新建对话框并自动填入。"""
        try:
            txt = self.root.clipboard_get().strip()
        except Exception:
            txt = ""
        if not txt:
            messagebox.showinfo("提示", "剪贴板是空的", parent=self.root)
            return
        self.new_download({"url": txt})

    def _selected_tids(self) -> list[str]:
        return list(self.tree.selection())

    def resume_selected(self):
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if t:
                t.start()
        self.refresh_now()

    def pause_selected(self):
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if t:
                t.pause()
        self.refresh_now()

    def boost_selected(self, k: int = 8):
        n = 0
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if t and isinstance(t, dlcore.FileDownload) and t.state == dlcore.STATE_DOWNLOADING:
                if self.mgr.add_connections(t, k):
                    n += 1
        if n:
            self.lbl_status.config(text=f"已为 {n} 个任务增加 {k} 个线程")
        else:
            messagebox.showinfo("提示", "请选中正在下载的任务（HLS 已完成分片并行，无需加速）",
                                parent=self.root)
        self.refresh_now()

    def retry_selected_with_aria2(self):
        """v2.6.0：选中失败/暂停任务 → 切到内置 aria2c 备用通道重试。"""
        n = 0
        skipped = 0
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if not t:
                continue
            if t.state in (dlcore.STATE_ERROR, dlcore.STATE_PAUSED):
                if t.retry_with_aria2():
                    n += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        if n:
            self.lbl_status.config(text=f"已用 aria2c 重试 {n} 个任务")
        else:
            messagebox.showinfo("提示", "请选中出错或已暂停的任务后再点「aria2c 极速重试」\n"
                                "该通道使用软件内置的 aria2c，无需额外安装。",
                                parent=self.root)
        self.refresh_now()

    def del_selected(self, delete_tmp: bool = False):
        tids = self._selected_tids()
        if not tids:
            return
        for tid in tids:
            t = self.mgr.get(tid)
            if t:
                t.remove(delete_files=delete_tmp)
        self.refresh_now()

    def clear_done(self):
        n = 0
        for s in self.mgr.snapshots():
            if s["state"] == dlcore.STATE_DONE:
                self.mgr.remove_task(s["tid"])
                n += 1
        self.lbl_status.config(text=f"已清除 {n} 个已完成任务（文件保留）")
        self.refresh_now()

    def copy_selected_url(self):
        tids = self._selected_tids()
        if not tids:
            return
        t = self.mgr.get(tids[0])
        if t:
            self.root.clipboard_clear()
            self.root.clipboard_append(t.url)
            self.lbl_status.config(text="下载链接已复制到剪贴板")

    def jump_selected(self):
        """v3.0.8: 把选中任务提到队列最前（插队）。"""
        tids = self._selected_tids()
        if not tids:
            return
        n = 0
        for tid in tids:
            if self.mgr.jump_queue(tid):
                n += 1
        if n:
            self.lbl_status.config(text=f"已插队 {n} 个任务到最前")
        self.refresh_now()

    def _open_selected(self):
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if t:
                open_in_explorer(t.created_final or t.out_dir)
                return

    def _open_selected_file(self):
        for tid in self._selected_tids():
            t = self.mgr.get(tid)
            if t and t.created_final and os.path.exists(t.created_final):
                open_file(t.created_final)
                return
        messagebox.showinfo("提示", "文件尚未下载完成", parent=self.root)

    def _task_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            # v3.0.2：根据选中任务状态动态启用/禁用“继续/暂停”
            sel = self._selected_tids()
            can_resume = can_pause = False
            for tid in sel:
                t = self.mgr.get(tid)
                if not t:
                    continue
                if t.state in (dlcore.STATE_PAUSED, dlcore.STATE_ERROR):
                    can_resume = True
                if t.state == dlcore.STATE_DOWNLOADING:
                    can_pause = True
            try:
                self.ctx.entryconfig("▶ 继续下载", state="normal" if can_resume else "disabled")
                self.ctx.entryconfig("⏸ 暂停", state="normal" if can_pause else "disabled")
            except Exception:
                pass
            self.ctx.tk_popup(event.x_root, event.y_root)

    # ---------- 悬停高亮（v1.12.0）----------
    def _on_tree_motion(self, ev):
        """鼠标移过行时高亮该行。

        性能：identify_row 是 Tk 的 C 层命中测试（微秒级），且先用
        "行号没变就直接返回"短路 —— 鼠标在同一行内移动不做任何事。
        """
        try:
            row = self.tree.identify_row(ev.y)
        except Exception:
            return
        if row == self._hover_row:
            return                        # 同一行内移动 → 无事可做
        self._clear_hover()
        self._hover_row = row
        if not row:
            return
        try:
            # 悬停 tag 放到最前：Tk 取**第一个**匹配到的 tag 决定前景/背景，
            # 放在前面既保证背景变色，也不会覆盖原有状态配色。
            tags = list(self.tree.item(row, "tags") or ())
            if "hover" not in tags:
                self.tree.item(row, tags=["hover"] + tags)
        except Exception:
            pass

    def _clear_hover(self):
        row, self._hover_row = getattr(self, "_hover_row", None), None
        if not row:
            return
        try:
            if not self.tree.exists(row):
                return
            tags = [t for t in (self.tree.item(row, "tags") or ()) if t != "hover"]
            self.tree.item(row, tags=tags or ("dl",))
        except Exception:
            pass

    def _sniff_download(self):
        sel = self.stree.selection()
        if not sel:
            return
        n = 0
        for i in sel:
            try:
                it = self.mgr.sniff_items[int(i)]
            except Exception:
                continue
            self.mgr.add_task(it["url"], filename=it.get("name", ""),
                              referer=it.get("referer", ""), cookie=it.get("cookie", ""),
                              kind="hls" if it.get("type") == "hls" else "auto")
            n += 1
        self.nb.select(0)
        self.refresh_now()
        self.lbl_status.config(text=f"已加入下载队列：{n} 个任务")

    def open_settings(self):
        SettingsDialog(self.root, self.mgr)
        self.refresh_now()

    def _open_settings_panel(self):
        d = SettingsDialog(self.root, self.mgr, app=self)
        self.root.wait_window(d)
        self.refresh_now()

    def _on_task_done(self, task):
        """任务完成：界面提示（不做系统通知，保持安静）"""
        pass

    def _on_close(self):
        """点关闭 → 最小化到托盘（后台继续下载），真正退出走托盘菜单"""
        if getattr(self, "_really_quit", False):
            self._quit_app()
            return
        if self.tray:
            try:
                self.root.withdraw()
                self._window_shown = False
                self.tray.notify("仍在后台运行",
                                 "下载继续进行。双击托盘图标可重新打开窗口。")
                return
            except Exception:
                dlcore.LOG.debug('忽略异常：root.withdraw()')
        # 没有托盘才真的退出
        self._quit_app()

    def _quit_app(self):
        self._really_quit = True
        try:
            if any(s["state"] == dlcore.STATE_DOWNLOADING for s in self.mgr.snapshots()):
                if not messagebox.askokcancel(
                        "退出", "还有任务正在下载，退出后可断点续传。确定退出？",
                        parent=self.root):
                    self._really_quit = False
                    return
            self.mgr.save_jobs()
            self.mgr.pause_all()
            if self.tray:
                self.tray.stop()
            self.api.stop()
            self._clear_pid()
        except Exception:
            dlcore.LOG.debug('忽略异常：if any(s["state"] == dlcore.STATE_DOWNLOADING for s in mgr....')
        try:
            self.root.destroy()
        except Exception:
            dlcore.LOG.debug('忽略异常：root.destroy()')
        # 确保进程彻底退出（托盘窗口线程为 daemon，不阻塞）
        os._exit(0)

    def run(self):
        # 注意：用 pythonw / 打包成无控制台程序时 stdout 可能为 None 或已关闭，
        # 直接 print 会抛异常。这里统一走安全输出。
        if self.api_ok and not self.silent:
            _safe_console(f"[{dlcore.APP_NAME}] 浏览器扩展接口就绪："
                          f"http://127.0.0.1:{self.api.actual_port}")
            _safe_console(f"[{dlcore.APP_NAME}] 令牌文件：{self.api.token_path}")
        if self.restored and not self.silent:
            _safe_console(f"[{dlcore.APP_NAME}] 已恢复 {self.restored} 个未完成任务（可继续下载）")
        self.root.mainloop()

    def _start_clipboard_listener(self):
        """v3.0.8: 优先使用 Windows 原生剪贴板事件，失败则回退到轮询。"""
        try:
            listener = ClipboardListener(self._on_clipboard_event)
            if listener.start():
                self._clip_native = listener
                self._poll_clipboard_queue()
                return
        except Exception:
            dlcore.LOG.debug("原生剪贴板监听启动失败，将使用轮询兜底")
        # 兜底：后台线程轮询
        self._clip_fallback_thread = threading.Thread(
            target=self._clipboard_watch, daemon=True)
        self._clip_fallback_thread.start()

    def _on_clipboard_event(self):
        """原生事件回调（在后台线程被调用）：调度到主线程读取剪贴板。"""
        try:
            if self._clip_enabled and self.root:
                self.root.after(0, lambda: self._clipboard_poll(force=True))
        except Exception:
            pass

    def _poll_clipboard_queue(self):
        """原生监听模式下不再需要此轮询，但保留一次兜底检查。"""
        if self._clip_enabled and self._clip_native:
            self.root.after(2000, self._poll_clipboard_queue)

    def _toggle_clipboard(self, enabled: bool):
        """v3.0.8: 根据设置启停剪贴板监听。"""
        self._clip_enabled = enabled
        if enabled:
            if (self._clip_native is None and
                (self._clip_fallback_thread is None or not self._clip_fallback_thread.is_alive())):
                self._start_clipboard_listener()
        else:
            if self._clip_native:
                try:
                    self._clip_native.stop()
                except Exception:
                    pass
                self._clip_native = None
            self._clip_fallback_thread = None

    # ---- 剪贴板监听 ----
    def _clipboard_watch(self):
        """后台线程：每 1.5s 提醒主线程检查一次剪贴板。

        Tkinter 的 clipboard_get() 必须在主线程调用，否则在部分环境下
        会抛 Tcl_AsyncDelete 或返回空。因此后台线程只做定时触发，
        实际读取、判断、弹窗全部通过 after() 调度到主线程完成。
        """
        while True:
            time.sleep(1.5)
            try:
                if not self._clip_enabled or not self.root:
                    continue
                self.root.after(0, self._clipboard_poll)
            except Exception:
                continue

    def _clipboard_poll(self, force: bool = False):
        """主线程：读取剪贴板，发现新 URL 且未在下载列表中则弹新建下载。

        去重规则：同一 URL 只有在已存在“未完成 / 下载中 / 等待中 / 已暂停”
        任务时才跳过；已取消/已失败/已完成的 URL 再次复制仍会触发弹窗，
        让用户重新确认是否下载。
        """
        try:
            txt = self.root.clipboard_get().strip()
        except Exception:
            return
        if not txt:
            return
        if not force and txt == self._clip_last:
            return
        self._clip_last = txt
        if not self._is_clipboard_download_url(txt):
            return
        if self._clip_url_already_active(txt):
            return
        self._on_clip_url(txt)

    @staticmethod
    def _is_clipboard_download_url(txt: str) -> bool:
        """区分普通网页地址和下载链接。"""
        if not txt:
            return False
        low = txt.lower()
        if low.startswith("magnet:?"):
            return True
        if low.endswith(".torrent"):
            return True
        # 简单 URL 解析：带查询或路径以可下载扩展名结尾
        try:
            from urllib.parse import urlsplit
            path = urlsplit(txt).path.lower().split("?")[0]
        except Exception:
            path = ""
        download_exts = (
            ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".iso",
            ".exe", ".msi", ".apk", ".ipa", ".dmg", ".pkg", ".bin", ".dat",
            ".mp4", ".webm", ".mkv", ".flv", ".mov", ".avi", ".ts", ".m3u8", ".mpd",
            ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".wma",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".csv", ".json", ".xml", ".html", ".htm",
        )
        if path.endswith(download_exts):
            return True
        # 常见下载特征
        if "/download" in low or "/attachment" in low or "?download=1" in low:
            return True
        return False

    def _on_clip_url(self, url: str):
        """剪贴板发现 URL，弹新建下载（自动填好链接）。"""
        if self._clip_url_already_active(url):
            return
        self.new_download({"url": url})

    def _clip_url_already_active(self, url: str) -> bool:
        """URL 是否已存在未完成的任务（下载中/等待/暂停/准备中）。"""
        active_states = {
            dlcore.STATE_PENDING, dlcore.STATE_WAIT,
            dlcore.STATE_DOWNLOADING, dlcore.STATE_PAUSED,
        }
        for s in self.mgr.snapshots():
            if s.get("url") == url and s.get("state") in active_states:
                return True
        return False


class ClipboardListener:
    """v3.0.8: Windows 原生剪贴板事件监听（零 CPU 轮询）。"""

    WM_CLIPBOARDUPDATE = 0x031D
    WM_DESTROY = 0x0002

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.wintypes.LRESULT,
        ctypes.wintypes.HWND,
        ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM)

    def __init__(self, on_change):
        self.on_change = on_change
        self._hwnd = None
        self._thread = None
        self._running = threading.Event()
        self._proc_ref = self._WNDPROC(self._wndproc)

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._running.wait(timeout=2.0)

    def stop(self):
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, self.WM_DESTROY, 0, 0)
            except Exception:
                pass

    def _run(self):
        try:
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc = ctypes.wintypes.WNDCLASSW()
            wc.lpfnWndProc = self._proc_ref
            wc.lpszClassName = "XZDClipboardListener"
            wc.hInstance = hinst
            atom = ctypes.windll.user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                return
            hwnd = ctypes.windll.user32.CreateWindowExW(
                0, "XZDClipboardListener", "XZD Clipboard", 0,
                0, 0, 0, 0, 0, 0, hinst, None)
            if not hwnd:
                return
            self._hwnd = hwnd
            if not ctypes.windll.user32.AddClipboardFormatListener(hwnd):
                ctypes.windll.user32.DestroyWindow(hwnd)
                self._hwnd = None
                return
            self._running.set()
            msg = ctypes.wintypes.MSG()
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            try:
                if self._hwnd:
                    ctypes.windll.user32.RemoveClipboardFormatListener(self._hwnd)
                    ctypes.windll.user32.DestroyWindow(self._hwnd)
                    self._hwnd = None
            except Exception:
                pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_CLIPBOARDUPDATE:
            try:
                self.on_change()
            except Exception:
                pass
            return 0
        if msg == self.WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _safe_console(msg: str):
    """向控制台输出诊断信息；无控制台（pythonw/打包无 console）时静默跳过"""
    try:
        if sys.stdout is not None and not getattr(sys.stdout, "closed", False):
            print(msg)
    except Exception:
        # 静默是有意为之：无控制台（pythonw/打包）时 stdout 可能是 None 或已关闭，
        # 输出失败不应影响任何功能
        pass


_URL_RE = None


def _find_url(text: str) -> str:
    import re
    global _URL_RE
    if _URL_RE is None:
        _URL_RE = re.compile(r"https?://[^\s\"'<>（）()【】\[\]，。；！]+", re.I)
    m = _URL_RE.search(text or "")
    if not m:
        return ""
    u = m.group(0).rstrip(".,;:")
    # 去掉被一并选中的中文/尾部标点
    return u


def _parse_args(argv: list[str]):
    """解析命令行：--silent 静默启动；xzd://xxx 协议唤起"""
    silent = False
    protocol = ""
    for a in argv[1:]:
        low = a.lower()
        if low in ("--silent", "-s", "/silent"):
            silent = True
        elif low.startswith("xzd:"):
            protocol = a
    return silent, protocol


def _install_crash_guards():
    """安装全局异常兜底（v1.11.0）。

    背景：后台线程崩溃时 Python 默认只在 stderr 打一行字，而打包成无控制台
    程序后**连这行字都看不见** —— 用户看到的现象是"界面卡住不动/托盘图标还在
    但下载没反应"，排查时毫无痕迹。这里把三类未处理异常统一落到日志。

    注意：钩子本身绝不能再抛异常，否则会掩盖原始错误。
    """
    import traceback                      # 惰性导入：只在真出异常时才付出代价
    prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            if issubclass(exc_type, KeyboardInterrupt):
                return prev_hook(exc_type, exc, tb)
            dlcore.LOG.error("主线程未捕获异常：%s: %s\n%s", exc_type.__name__, exc,
                             "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            # 静默是有意为之：钩子自身绝不能再抛异常，否则会掩盖原始错误
            pass
        try:
            prev_hook(exc_type, exc, tb)
        except Exception:
            # 静默是有意为之：同上，前一个钩子失败也不能连累程序
            pass

    sys.excepthook = _hook

    # 后台线程（下载线程/写盘线程/托盘线程）异常 ← 这是"假死"的主要来源
    def _thook(args):
        try:
            if issubclass(args.exc_type, SystemExit):
                return
            dlcore.LOG.error("后台线程未捕获异常 [%s]：%s: %s\n%s",
                             getattr(args.thread, "name", "?"),
                             args.exc_type.__name__, args.exc_value,
                             "".join(traceback.format_exception(
                                 args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            # 静默是有意为之：同上，钩子自身不能抛异常
            pass

    if hasattr(threading, "excepthook"):
        threading.excepthook = _thook
        return True
    return False


def main():
    silent, protocol = _parse_args(sys.argv)
    boot_mark("main_enter")
    _install_crash_guards()
    boot_mark("crash_guards")
    # 单实例保护：已在后台运行时，把本次请求转交给它，不启动第二个进程
    # （否则点两次下载链接就会出现两个托盘图标、两份任务列表）
    try:
        if sysintegration.handoff(silent, protocol):
            boot_mark("handoff_exit")
            return
    except Exception:
        dlcore.LOG.debug('忽略异常：if sysintegration.handoff(silent, protocol): boot_mark("han...')
    app = App(silent=silent, protocol_url=protocol)
    app.run()


if __name__ == "__main__":
    main()
