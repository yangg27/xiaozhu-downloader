#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 图形界面  gui.py   v2.6.0
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

import os
import queue
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
    def __init__(self, master, title: str, w: int, h: int):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.geometry(f"{w}x{h}")
        self._center(w, h)
        self.bind("<Escape>", lambda e: self.destroy())
        # 主窗口可能在托盘/最小化状态，强制显示并置顶
        self.deiconify()
        self.lift()
        self.focus_force()
        self.attributes("-topmost", True)
        self.after(100, lambda: self.attributes("-topmost", False))

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
    def __init__(self, master, mgr, preset: dict | None = None):
        super().__init__(master, "新建下载", 560, 520)
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
        tk.Label(f, text="下载链接（支持多行，每行一个）", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        # 多行 URL 输入
        url_frame = tk.Frame(f, bg=BG)
        url_frame.grid(row=1, column=1, sticky="we", pady=(0, 8))
        self.t_url = tk.Text(url_frame, height=6, font=(FONT, 9), bg="#fff", fg=FG,
                             relief="solid", bd=1, wrap="none")
        self.t_url.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(url_frame, orient="vertical", command=self.t_url.yview)
        sb.pack(side="right", fill="y")
        self.t_url.configure(yscrollcommand=sb.set)
        self.t_url.insert("1.0", self.v_url.get())
        tk.Label(f, text="下载地址", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=1, column=0, sticky="nw", pady=(0, 8))
        fr = tk.Frame(f, bg=BG)
        fr.grid(row=2, column=1, sticky="we", pady=(0, 8))
        tk.Entry(fr, textvariable=self.v_dir, font=(FONT, 9), bg="#fff", fg=FG,
                 relief="solid", bd=1, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT).pack(
            side="left", fill="x", expand=True, ipady=4)
        tk.Button(fr, text="浏览", command=self._pick_dir, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=10, cursor="hand2").pack(side="left", padx=(6, 0))
        tk.Label(f, text="保存目录", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=2, column=0, sticky="w", pady=(0, 8))
        self._row(f, "文件名称", self.v_name, 3)
        tk.Label(f, text="线程数", bg=BG, fg=FG_DIM, font=(FONT, 9)).grid(
            row=4, column=0, sticky="w", pady=(0, 8))
        conn_row = tk.Frame(f, bg=BG)
        conn_row.grid(row=4, column=1, sticky="we", pady=(0, 8))
        # 输入框（不用 _row，避免 grid/pack 混用）
        e_conn = tk.Entry(conn_row, textvariable=self.v_conns, font=(FONT, 9),
                          bg="#fff", fg=FG, relief="solid", bd=1,
                          highlightthickness=1, highlightbackground=BORDER,
                          highlightcolor=ACCENT, width=8)
        e_conn.pack(side="left", ipady=4)
        tk.Label(conn_row, text="线程（1-32，越多越快，部分服务器限制）", bg=BG,
                 fg=FG_DIM, font=(FONT, 8)).pack(side="left", padx=(8, 0))
        self._row(f, "Referer", self.v_ref, 5)
        self._row(f, "Cookie", self.v_cookie, 6)
        tip = tk.Label(f, text="提示：视频/音频站点通常需要 Referer；不填则自动带上来源页",
                       bg=BG, fg=FG_DIM, font=(FONT, 8))
        tip.grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 10))
        btns = tk.Frame(f, bg=BG)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
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

    def _ok(self):
        raw = self.t_url.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("提示", "请填写下载地址", parent=self)
            return
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请填写下载地址", parent=self)
            return
        # 自动补协议
        fixed = []
        for u in urls:
            if not u.lower().startswith(("http://", "https://", "ftp://")):
                u = "http://" + u
            fixed.append(u)
        self.result = {
            "urls": fixed,  # 多链接
            "url": fixed[0],  # 兼容单链接
            "out_dir": self.v_dir.get().strip(), "filename": self.v_name.get().strip(),
            "conns": max(1, min(32, int(self.v_conns.get() or 24))),
            "referer": self.v_ref.get().strip(), "cookie": self.v_cookie.get().strip(),
        }
        self.destroy()


class SettingsDialog(Dialog):
    def __init__(self, master, mgr, app=None):
        super().__init__(master, "设置", 580, 700)
        self.mgr = mgr
        self.app = app
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
            row=r, column=0, sticky="w", pady=(0, 9))

    def _ent(self, p, var, r, w=30):
        e = tk.Entry(p, textvariable=var, font=(FONT, 9), bg="#fff", fg=FG, width=w,
                     relief="solid", bd=1, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT)
        e.grid(row=r, column=1, sticky="we", pady=(0, 9), ipady=4)
        return e

    def _build(self):
        f = tk.Frame(self, bg=BG, padx=18, pady=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        tk.Label(f, text="下载设置", bg=BG, fg=FG, font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fr = tk.Frame(f, bg=BG)
        fr.grid(row=1, column=1, sticky="we", pady=(0, 9))
        tk.Entry(fr, textvariable=self.v_dir, font=(FONT, 9), bg="#fff", fg=FG,
                 relief="solid", bd=1, highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", fill="x", expand=True, ipady=4)
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
                 font=(FONT, 8)).grid(row=4, column=2, sticky="w", padx=(8, 0), pady=(0, 9))
        self._lbl(f, "全局限速", 4)
        self._ent(f, self.v_proxy, 5)
        self._lbl(f, "代理服务器", 5)
        self._ent(f, self.v_ua, 6)
        self._lbl(f, "User-Agent", 6)
        self._ent(f, self.v_port, 7, 8)
        tk.Label(f, text="浏览器扩展对接端口", bg=BG, fg=FG_DIM,
                 font=(FONT, 8)).grid(row=7, column=2, sticky="w", padx=(8, 0), pady=(0, 9))
        self._lbl(f, "本地接口端口", 7)
        cb1 = tk.Checkbutton(f, text="任务完成后自动清理临时分片", variable=self.v_tmp, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb1.grid(row=8, column=1, sticky="w", pady=(0, 6))
        cb2 = tk.Checkbutton(f, text="忽略 HTTPS 证书错误（不推荐）", variable=self.v_insecure, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb2.grid(row=9, column=1, sticky="w", pady=(0, 6))
        cb5 = tk.Checkbutton(f, text="剪贴板监听（复制链接自动弹新建下载）",
                             variable=self.v_clipboard, bg=BG,
                             fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb5.grid(row=10, column=1, sticky="w", pady=(0, 6))

        # ---- v3.0.0: 引擎选择 + BT 设置 ----
        tk.Frame(f, bg=BORDER, height=1).grid(row=11, column=0, columnspan=3,
                                              sticky="we", pady=(10, 8))
        tk.Label(f, text="下载引擎 / BT 磁力", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(row=12, column=0, columnspan=3,
                                               sticky="w", pady=(0, 8))
        # 引擎单选
        eng_frame = tk.Frame(f, bg=BG)
        eng_frame.grid(row=13, column=1, sticky="w", pady=(0, 6))
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
                 font=(FONT, 8)).grid(row=14, column=2, sticky="w", padx=(8, 0), pady=(0, 9))
        # 自动更新开关
        cb_bt = tk.Checkbutton(f, text="启动时自动更新 BT trackers（推荐）",
                               variable=self.v_bt_autoupdate, bg=BG,
                               fg=FG, font=(FONT, 9), activebackground=BG, selectcolor="#fff")
        cb_bt.grid(row=15, column=1, sticky="w", pady=(0, 6))

        # ---- 系统集成 ----
        tk.Frame(f, bg=BORDER, height=1).grid(row=16, column=0, columnspan=3,
                                              sticky="we", pady=(10, 8))
        tk.Label(f, text="系统集成（让浏览器点击下载自动唤起）", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).grid(row=17, column=0, columnspan=3,
                                               sticky="w", pady=(0, 8))
        cb3 = tk.Checkbutton(f, text="开机自启（后台静默运行，不弹窗）",
                             variable=self.v_autostart, bg=BG, fg=FG, font=(FONT, 9),
                             activebackground=BG, selectcolor="#fff")
        cb3.grid(row=18, column=0, columnspan=2, sticky="w", pady=(0, 5))
        cb4 = tk.Checkbutton(f, text="注册 xzd:// 协议（扩展可唤起下载器）",
                             variable=self.v_protocol, bg=BG, fg=FG, font=(FONT, 9),
                             activebackground=BG, selectcolor="#fff")
        cb4.grid(row=19, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.lbl_si = tk.Label(f, text="", bg=BG, fg=FG_DIM, font=(FONT, 8),
                               justify="left", wraplength=470)
        self.lbl_si.grid(row=20, column=0, columnspan=3, sticky="w", pady=(2, 6))
        if self.si is None:
            cb3.config(state="disabled")
            cb4.config(state="disabled")
            self.lbl_si.config(text="当前环境不支持系统集成（仅 Windows 可用）", fg=ERR)
        else:
            self._refresh_si_label()

        tk.Label(f, text="全程离线：不检查更新、不联网校验、不上报任何数据",
                 bg=BG, fg=OK, font=(FONT, 8)).grid(row=21, column=0, columnspan=3,
                                                      sticky="w", pady=(6, 12))
        btns = tk.Frame(f, bg=BG)
        btns.grid(row=22, column=0, columnspan=3, sticky="e")
        tk.Button(btns, text="取消", command=self.destroy, font=(FONT, 9), bg="#eef1f6",
                  fg=FG, relief="flat", padx=18, pady=5, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="保存", command=self._ok, font=(FONT, 10, "bold"), bg=ACCENT,
                  fg="#fff", relief="flat", padx=18, pady=5, activebackground=ACCENT_DARK,
                  cursor="hand2").pack(side="left")

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
        )
        # 剪贴板监听开关立即生效
        if self.app:
            self.app._clip_enabled = bool(self.v_clipboard.get())
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
        if self._clip_enabled:
            threading.Thread(target=self._clipboard_watch, daemon=True).start()

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

        # v3.0.0：两行紧凑布局。第一行核心输入，第二行批量操作与工具
        row1 = tk.Frame(inner, bg=CARD)
        row1.pack(fill="x")
        btn(row1, "+ 新建下载", self.new_download, primary=True)
        btn(row1, "+ 新建 BT/磁力", self.new_bt_download)
        btn(row1, "粘贴链接", self._paste_url, compact=True)
        btn(row1, "分析网页", lambda: WebScanDialog(self.root, self), compact=True)
        btn(row1, "音视频合并", lambda: MergeAVDialog(self.root, self), compact=True)

        row2 = tk.Frame(inner, bg=CARD)
        row2.pack(fill="x", pady=(6, 0))
        btn(row2, "全部继续", lambda: (self.mgr.resume_all(), self.refresh_now()), compact=True)
        btn(row2, "全部暂停", lambda: (self.mgr.pause_all(), self.refresh_now()), compact=True)
        btn(row2, "清除已完成", self.clear_done, compact=True)
        btn(row2, "⚡ aria2c 重试", self.retry_selected_with_aria2, compact=True)
        right = tk.Frame(row2, bg=CARD)
        right.pack(side="right")
        btn(right, "设置", self._open_settings_panel, compact=True)
        btn(right, "打开目录", lambda: open_in_explorer(self.mgr.cfg.get("download_dir", "")), compact=True)

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
        self.ctx.add_command(label="继续下载", command=self.resume_selected)
        self.ctx.add_command(label="暂停", command=self.pause_selected)
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
        d = NewDownloadDialog(self.root, self.mgr, preset)
        self.root.wait_window(d)
        if d.result:
            r = d.result
            urls = r.get("urls", [r["url"]])
            for u in urls:
                self.mgr.add_task(u, out_dir=r["out_dir"], filename=r["filename"],
                                  conns=r["conns"], referer=r["referer"], cookie=r["cookie"],
                                  kind="auto")
            self.refresh_now()

    def new_bt_download(self):
        """v3.0.0：新建 BT/磁力任务。支持 magnet: URI 和本地 .torrent 文件。"""
        # 优先尝试剪贴板里的 magnet
        preset_url = ""
        try:
            clip = self.root.clipboard_get().strip()
            if clip.startswith("magnet:") or clip.endswith(".torrent"):
                preset_url = clip
        except Exception:
            pass
        d = NewDownloadDialog(self.root, self.mgr, {"url": preset_url})
        self.root.wait_window(d)
        if not d.result:
            return
        r = d.result
        url = r["url"].strip()
        if not (url.startswith("magnet:") or url.endswith(".torrent") or
                url.startswith("http") or os.path.exists(url)):
            messagebox.showwarning("格式不对", "请粘贴 magnet:?xt=... 磁力链，或选择 .torrent 种子文件")
            return
        # 本地 .torrent 文件直接传路径；http(s) 种子文件 aria2c 也支持
        self.mgr.add_task(url, out_dir=r["out_dir"], filename=r["filename"],
                          conns=r["conns"], referer=r["referer"], cookie=r["cookie"],
                          kind="bt")
        self.refresh_now()

    def _paste_url(self):
        try:
            txt = self.root.clipboard_get().strip()
        except Exception:
            txt = ""
        m = _find_url(txt) if txt else ""
        if not m:
            messagebox.showinfo("提示", "剪贴板里没有找到 http/https 链接", parent=self.root)
            return
        self.new_download({"url": m})

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

    # ---- 剪贴板监听 ----
    def _clipboard_watch(self):
        """后台线程：每 1.5s 读一次剪贴板，发现新 URL 就弹新建下载。"""
        import re
        url_re = re.compile(r"^https?://", re.I)
        while True:
            time.sleep(1.5)
            try:
                if not self._clip_enabled:
                    continue
                txt = self.root.clipboard_get().strip()
                if txt == self._clip_last or not txt:
                    continue
                self._clip_last = txt
                if url_re.match(txt):
                    # 主线程弹窗
                    self.root.after(0, lambda t=txt: self._on_clip_url(t))
            except Exception:
                continue

    def _on_clip_url(self, url: str):
        """剪贴板发现 URL，弹新建下载（自动填好链接）。"""
        # 已在下载列表里的不重复弹
        for s in self.mgr.snapshots():
            if s.get("url") == url and s.get("state") not in (dlcore.STATE_DONE, dlcore.STATE_CANCELLED, dlcore.STATE_ERROR):
                return
        self.new_download({"url": url})


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
