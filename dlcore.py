#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 核心引擎  dlcore.py   v2.5.0
=========================================
零依赖（纯 Python 标准库）下载引擎，提供与 IDM 同款的核心能力：

  1. 多线程分片下载 —— 区块位图 + 随机写入，速度跑满带宽
  2. 动态加速       —— 运行中随时增加连接数（不重启、不重下）
  3. 精确断点续传   —— 1MB 粒度位图，任何时刻中断都能续
  4. HLS(m3u8) 下载 —— 分片并行抓取 + AES-128 解密 + 自动合并
  5. 网页媒体嗅探   —— 分析网页中的 m3u8 / mp4 / mp3 / flv 等资源
  6. 完全离线       —— 不联网校验、不更新、不注册、不弹窗
  7. 长期稳定运行   —— 全局异常兜底 + 僵尸任务自愈 + 磁盘空间预检 + 失败有上限
  8. 免 Python 分发 —— 可冻结为独立 exe + 双击安装包（v1.13.0）
  9. 共享 SSLContext —— 并发 TLS 建连不再串行化（v2.3.0，启动提速 6.3s）
 10. aria2c 备用极速通道 —— 内置 aria2c，失败/手动一键切换（v2.5.0）

设计要点（aria2 同款思路）：
  · 目标文件一次性预分配，多个工作线程各持独立文件句柄，随机写入
    各自负责的区块（互不重叠），无需合并、无碎片。
  · 区块完成状态记录在位图文件 bitmap.bin 中，1 字节/区块，
    每 2 秒 + 暂停/退出时落盘 → 断电/强杀也能精确续传。
  · 连接数调度采用"工作队列 + 目标线程数"，增加连接 = 直接加线程，
    立即生效，无任何数据竞争。

CLI 用法：
  python dlcore.py -u URL -o 保存目录 [-c 16] [--limit KB/s] [--json]
  python dlcore.py --scan PAGE_URL        # 嗅探网页媒体
  python dlcore.py --help
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_NAME = "小助下载器"
APP_ID = "xiaozhu-downloader"
ENGINE_VERSION = "2.5.0"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

MEDIA_EXTS = (
    ".m3u8", ".mpd", ".mp4", ".webm", ".mkv", ".flv", ".mov", ".avi", ".ts",
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg",
)
STREAM_EXTS = (".m3u8", ".mpd")
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".iso",
                ".exe", ".msi", ".apk", ".pdf", ".dmg", ".pkg", ".bin", ".dat")

READ_SIZE = 256 * 1024            # 网络读取块
FILE_READ_SIZE = 512 * 1024       # 合并/搬运块
BLOCK_SIZE = 1024 * 1024          # 续传粒度区块 = 1MB
MIN_SPLIT_SIZE = 4 * 1024 * 1024  # 小于 4MB 不分块
MIN_SEG_SIZE = 1024 * 1024        # 小于 1MB 干脆单线程
MAX_RETRY = 8                     # 单个区块最大重试
MAX_BLOCK_FAILS = 24              # 同一区块累计失败数上限
SINGLE_MAX_CONSEC_FAIL = 6        # v1.11.0：单流模式下连续失败上限（防永久空转）
SOCKET_TIMEOUT = 40
BITMAP_SAVE_INTERVAL = 2.0

# ---- 限速/过载响应处理（v1.7.0）--------------------------------------------
# 实测华为云镜像在 24 并发下会对部分请求回 429 Too Many Requests。
# 旧行为：算作普通失败，重试间隔 1s/2s/3s… 且 8 次就放弃该区块，
#         结果 24 块里有 8 块直接失败、整体重试风暴。
# 新行为：识别 429/503/509 → 走**专门的退避**（指数 + 抖动），
#         并让同任务的所有线程一起"收敛节奏"，避免继续加压。
RATE_LIMIT_CODES = (429, 503, 509)
RATE_LIMIT_BASE_BACKOFF = 1.5     # 首次退避 1.5s，之后 3s / 6s / 12s…
RATE_LIMIT_MAX_BACKOFF = 20.0
# 全局"服务器压力"信号：最后被限流的时刻。所有线程看到它都会放慢。
_LAST_THROTTLE_TS = [0.0]
_THROTTLE_LOCK = threading.Lock()


def note_throttle():
    """记录一次被限流，供所有线程参考（收敛节奏）"""
    with _THROTTLE_LOCK:
        _LAST_THROTTLE_TS[0] = time.time()


def throttle_pressure() -> float:
    """返回"最近被限流的紧迫度"：0=无压力，越大越该慢"""
    with _THROTTLE_LOCK:
        dt = time.time() - _LAST_THROTTLE_TS[0]
    if dt > 30:
        return 0.0
    return max(0.0, 1.0 - dt / 30.0)


class _RateLimited(Exception):
    """服务器限速（429/503/509）—— 与网络故障区分开，走专门退避策略"""

    def __init__(self, status: int = 429):
        super().__init__(f"HTTP {status} 限速")
        self.status = status


# --------------------------------------------------------------------------
# 原子定位写（等价 POSIX pwrite）
# 定位写：把数据写到文件的指定偏移。
#
# ⚠ 重要教训（v1.5.0 踩坑记录）
# Windows 上**不能**往同步句柄传 OVERLAPPED 结构体做"定位写"。
# 官方文档明确警告：对未带 FILE_FLAG_OVERLAPPED 打开的句柄，
# 传入 OVERLAPPED 属于未定义行为，"the function can incorrectly
# report that the write is complete"（可能错误地报告写入已完成）。
#
# 实测后果：24 线程并发下载 512MB，每块前 4KB 正确、其余是旧数据，
# 且每次运行 MD5 都不同 —— 典型的静默数据损坏。原因是 WriteFile
# 提前返回后，上层立即复用了同一块缓冲（内存优化引入的缓冲复用），
# 数据在真正落盘前就被下一块覆盖。早期"每块新建 bytearray"掩盖了它。
#
# 正确做法：每线程持有**独立文件句柄** + seek + write。
#   · 文件指针是每句柄私有的 → 线程间无竞争，无需加锁
#   · write 返回即已同步落盘，语义明确
#   · FileIO.write 对 memoryview 零拷贝（指针直接交给内核），性能不受损
# --------------------------------------------------------------------------
def _make_write_at():
    """返回 (fileobj, buf, off) -> bool 的定位写函数"""
    if hasattr(os, "pwrite"):                     # POSIX：pwrite 无需 seek
        def _posix(f, buf, off: int) -> bool:
            try:
                return os.pwrite(f.fileno(), buf, off) == len(buf)
            except Exception:
                return False
        return _posix

    def _sync(f, buf, off: int) -> bool:
        """线程本地句柄上的 seek + write（同步语义明确，零拷贝）"""
        try:
            f.seek(off)
            return f.write(buf) == len(buf)
        except Exception:
            return False
    return _sync


_WRITE_AT = _make_write_at()


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# 运行时路径解析（源码运行 / 打包冻结 两种形态都能正确工作）
# --------------------------------------------------------------------------

def is_frozen() -> bool:
    """是否运行在 PyInstaller 等冻结环境中（已打包为 exe）"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """程序资源所在目录。
    · 源码运行：本文件所在目录
    · 冻结运行：exe 所在目录（resources 已随 exe 同级展开）
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def merge_av(video_path: str, audio_path: str, out_path: str = "") -> tuple:
    """用 FFmpeg 合并本地视频与音频文件（视频流 copy，音频转 AAC）。
    返回 (是否成功, 输出路径 或 错误信息)。"""
    ffmpeg = os.path.join(app_root(), "third_party", "ffmpeg.exe")
    if not os.path.exists(ffmpeg):
        return False, "未找到 third_party/ffmpeg.exe"
    if not video_path or not os.path.exists(video_path):
        return False, "视频文件不存在"
    if not audio_path or not os.path.exists(audio_path):
        return False, "音频文件不存在"
    if not out_path:
        out_path = os.path.splitext(video_path)[0] + "_merged.mp4"
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-i", audio_path,
             "-c:v", "copy", "-c:a", "aac", "-strict", "experimental", out_path],
            capture_output=True, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True, out_path
        err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        return False, (err[-1] if err else "FFmpeg 合并失败")
    except Exception as e:
        return False, str(e)


def data_root() -> str:
    """可写数据目录。
    打包后程序可能装在 Program Files 等**只读位置**，
    因此数据必须放到用户可写的 %APPDATA%；源码运行仍用项目内 data/，
    方便开发调试时一眼看到配置文件。

    可用环境变量 XZD_DATA_DIR 覆盖（便于多实例测试）。
    """
    env = os.environ.get("XZD_DATA_DIR")
    if env:
        return ensure_dir(os.path.abspath(env))
    if is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return ensure_dir(os.path.join(base, APP_NAME))
    return ensure_dir(os.path.join(app_root(), "data"))


def ensure_dir(p: str) -> str:
    if p and not os.path.isdir(p):
        os.makedirs(p, exist_ok=True)
    return p


# --------------------------------------------------------------------------
# 本地日志（v1.10.0）
#
# 背景：项目里原本有 60+ 处 `except Exception: pass`。绝大多数是"清理路径
# 失败无所谓"（关句柄、删临时文件），但其中有十几处一旦失败会**静默破坏
# 核心能力**——最典型的是位图落盘失败：断点续传从此静默失效，用户只看到
# "怎么每次都从头下"，开发者拿不到任何线索。
#
# 取舍：
#   · 只写本地文件、绝不上报（延续"完全离线"承诺）
#   · 惰性打开：第一次真要写日志时才建目录/文件 → 启动路径零开销
#   · 失败即永久降级：磁盘满/无权限就直接放弃，绝不因日志拖慢下载
#   · 按大小滚动，磁盘占用上限约 1.5MB
# 级别：默认 WARNING（只记真问题）；排查时设 XZD_LOG_LEVEL=DEBUG 放开
# --------------------------------------------------------------------------
LOG_DIRNAME = "logs"
LOG_FILENAME = "xzd.log"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 2
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_LOG_NAMES = {10: "DEBUG", 20: "INFO ", 30: "WARN ", 40: "ERROR"}


class _FileLogger:
    """极简本地文件日志：惰性打开、按大小滚动、线程安全、失败即静默降级。

    没有直接用 stdlib `logging`，因为它 import 就要 ~2ms，且会带来
    handler/formatter 一套配置负担；本项目只需要"能落盘、能滚动、不添乱"。
    """

    def __init__(self, path_fn, level_name: str = "WARNING"):
        self._path_fn = path_fn          # 惰性求值，避免 import 期建目录
        self.level = _LOG_LEVELS.get(str(level_name).upper(), 30)
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        self._broken = False             # 一旦写不进去就永久放弃
        self._once_lock = threading.Lock()
        self._once_seen = set()          # 已记过的"只记一次"键

    # ---- 级别控制 ----
    def set_level(self, name):
        self.level = _LOG_LEVELS.get(str(name).upper(), self.level)

    @property
    def enabled(self) -> bool:
        return self.level <= 40 and not self._broken

    # ---- 内部：打开 / 滚动（调用方必须持锁）----
    def _path(self) -> str:
        return os.path.join(ensure_dir(os.path.join(self._path_fn(), LOG_DIRNAME)),
                            LOG_FILENAME)

    def _open(self):
        if self._broken:
            return None
        if self._fh is not None:
            return self._fh
        try:
            p = self._path()
            self._size = os.path.getsize(p) if os.path.exists(p) else 0
            self._fh = open(p, "a", encoding="utf-8", errors="replace")
            return self._fh
        except Exception:
            self._broken = True          # 磁盘满/权限不足：不再重试
            return None

    def _rotate(self):
        # ⚠ 本方法由 _emit 在**持锁状态下**调用，且日志记录器自身也在滚动失败时
        #   可能再次回调 → 这里绝不能调用 self.debug/self.warn（threading.Lock
        #   不可重入，会直接死锁）。失败一律静默，下次写入继续尝试即可。
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            p = self._path()
            if os.path.exists(p + ".%d" % LOG_BACKUP_COUNT):
                os.remove(p + ".%d" % LOG_BACKUP_COUNT)
            for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                src, dst = p + ".%d" % i, p + ".%d" % (i + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(p):
                os.replace(p, p + ".1")
        except Exception:
            pass                          # 同上：不得在此记录日志
        self._size = 0

    def _fmt(self, msg, args):
        if not args:
            return str(msg)
        try:
            return str(msg) % args
        except Exception:
            return "%s %r" % (msg, args)

    def _emit(self, lvl: int, text: str):
        if lvl < self.level or self._broken:
            return
        with self._lock:
            if self._size > LOG_MAX_BYTES:
                self._rotate()
            fh = self._open()
            if fh is None:
                return
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                line = "%s [%s] T%-5d %s\n" % (
                    ts, _LOG_NAMES.get(lvl, "?"), threading.get_ident() % 100000, text)
                fh.write(line)
                fh.flush()                # 崩溃时也要能看到最后几条
                self._size += len(line)
            except Exception:
                self._broken = True

    # ---- 对外 ----
    def debug(self, msg, *args):
        self._emit(10, self._fmt(msg, args))

    def info(self, msg, *args):
        self._emit(20, self._fmt(msg, args))

    def warn(self, msg, *args):
        self._emit(30, self._fmt(msg, args))

    def error(self, msg, *args):
        self._emit(40, self._fmt(msg, args))

    def exc(self, msg, *args, level: int = 30):
        """记一条**带完整栈**的异常。

        ⚠ 只能用在 except 块内。在无异常上下文调用时，`format_exc()` 只会
        返回 "NoneType: None"（实测踩过），所以这里显式检测并提示改用 once()。
        """
        if level < self.level or self._broken:
            return
        try:
            import traceback                # 惰性导入：平时不付出代价
            if sys.exc_info()[0] is None:
                self._emit(level, self._fmt(msg, args)
                           + "\n(注意：exc() 在非异常上下文调用，无栈可记录 —— "
                             "正常路径请改用 once())")
                return
            tb = traceback.format_exc()
        except Exception:
            tb = "(traceback 不可用)"
        self._emit(level, self._fmt(msg, args) + "\n" + tb.rstrip())

    def once(self, key: str, msg, *args, level: int = 30):
        """同一个 key 只记第一次。

        用于**可能每 2 秒重复触发**的失败（如磁盘满导致位图存不下）。
        否则一次故障能在几分钟内把日志刷成几十 MB。
        """
        if level < self.level or self._broken:
            return
        with self._once_lock:
            if key in self._once_seen:
                return
            self._once_seen.add(key)
        self._emit(level, self._fmt(msg, args))

    def once_exc(self, key: str, msg, *args, level: int = 30):
        """同上，但带完整栈"""
        if level < self.level or self._broken:
            return
        with self._once_lock:
            if key in self._once_seen:
                return
            self._once_seen.add(key)
        self.exc(msg, *args, level=level)


LOG = _FileLogger(data_root, os.environ.get("XZD_LOG_LEVEL", "WARNING"))


def log_path() -> str:
    """日志文件路径（供界面/说明文档展示）"""
    return LOG._path()


def human_size(n) -> str:
    if n is None:
        return "未知"
    n = float(n)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024.0 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def human_speed(bps) -> str:
    return human_size(bps) + "/s" if bps else "0 B/s"


def human_eta(sec) -> str:
    if not sec or sec <= 0 or sec > 86400 * 30:
        return "--"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_filename(name: str, fallback: str = "download") -> str:
    name = urllib.parse.unquote(name or "").strip()
    name = _ILLEGAL.sub("_", name).strip(" .")
    name = name[:180]
    return name or fallback


def filename_from_url(url: str, fallback: str = "download") -> str:
    path = urllib.parse.urlsplit(url).path
    return safe_filename(os.path.basename(path), fallback)


def filename_from_disposition(value: str) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(2).strip())
        except Exception:
            LOG.debug('忽略异常：return urllib.parse.unquote(m.group(2).strip())')
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
    return m.group(1).strip() if m else None


_MIME_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/x-flv": ".flv",
    "video/quicktime": ".mov", "video/mp2t": ".ts",
    "application/vnd.apple.mpegurl": ".m3u8", "application/x-mpegurl": ".m3u8",
    "audio/mpegurl": ".m3u8", "audio/x-mpegurl": ".m3u8",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/flac": ".flac", "audio/wav": ".wav", "audio/ogg": ".ogg",
    "application/zip": ".zip", "application/x-msdownload": ".exe",
    "application/pdf": ".pdf", "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar", "application/vnd.rar": ".rar",
    "application/gzip": ".gz", "application/x-tar": ".tar",
}


def guess_ext_from_mime(mime: str) -> str:
    return _MIME_EXT.get((mime or "").split(";")[0].strip().lower(), "")


def kind_of_url(url: str) -> str:
    p = urllib.parse.urlsplit(url).path.lower()
    if p.endswith(STREAM_EXTS):
        return "hls"
    if p.endswith(MEDIA_EXTS) or p.endswith(ARCHIVE_EXTS):
        return "file"
    return "page"


def unique_path(path: str) -> str:
    """若文件已存在，自动加 (1)(2)… 后缀"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base}({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


# --------------------------------------------------------------------------
# 共享 SSLContext（v2.3.0 ★ 关键性能修复）
#
# ⚠ 血泪教训：原先 `_new_conn()` 里写的是
#       ctx = self._ssl_ctx or ssl.create_default_context()
#   看起来无害，实则是**并发下载的头号杀手**：
#
#   实测（24 线程、华为云镜像、2026-10-12）：
#     · create_default_context() 单次耗时 **0.491s**
#       （其中 load_default_certs() 占 0.257s —— 要读 Windows 证书存储、
#        解码 PEM、构建 X509_STORE，全程持有 GIL）
#     · 24 线程各自 create 一次 + TLS 握手：**6.339s**（等于串行！）
#     · 共享一个预建 context：            **0.403s**（快 15.7×）
#
#   后果：每次下载启动时，24 个工作线程排队加载 CA 证书，
#   把本该并行的建连硬生生串行化 —— 178MB 档启动阶段白等 7 秒，
#   直接输给 IDM（实测第 1 组耗时 0.66x）。
#
#   修复：模块级共享一个 context，**首次调用时才创建**（懒加载，
#   不影响 CLI 解析、GUI 启动等不下载的场景），之后所有线程复用。
#   SSLContext 在 CPython 中是可安全跨线程共享的（wrap_socket 无状态）。
# --------------------------------------------------------------------------
_SHARED_SSL_CTX = None
_SHARED_SSL_LOCK = threading.Lock()


def shared_ssl_context() -> ssl.SSLContext:
    """返回全局唯一的默认 SSLContext（线程安全、懒加载）"""
    global _SHARED_SSL_CTX
    ctx = _SHARED_SSL_CTX
    if ctx is not None:
        return ctx
    with _SHARED_SSL_LOCK:
        if _SHARED_SSL_CTX is None:
            _SHARED_SSL_CTX = ssl.create_default_context()
        return _SHARED_SSL_CTX


def insecure_ssl_context() -> ssl.SSLContext:
    """基于共享 context 派生的独立宽松校验 context。

    我们曾在旧代码里直接调用 ssl.create_default_context() 再改
    check_hostname / verify_mode —— 这样又会让并发 TLS 建连退化成串行。
    正确做法是：以共享 context 为模板，构造一个独立的轻量副本，
    既保留 CA 证书链缓存，又能单独关闭校验，同时不污染全局共享对象。
    """
    base = shared_ssl_context()
    # SSLContext 没有 copy()，但可通过 new + copy options 达到同样效果：
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.options = base.options
    ctx.minimum_version = base.minimum_version
    ctx.maximum_version = base.maximum_version
    ctx.set_default_verify_paths()
    # 这是关键：关闭校验（但只影响这个独立 ctx）
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def warmup_ssl_context():
    """提前在后台把 context 建好，避免首个连接线程承担 0.5s 延迟。

    在任务启动（探测阶段）调用即可 —— 探测本身也要建 TLS 连接，
    所以这次预热是真·零成本（总要有一次 create）。
    """
    try:
        shared_ssl_context()
    except Exception:
        LOG.debug('SSL context 预热失败，忽略（后续仍会按需创建）')


# --------------------------------------------------------------------------
# HTTP 封装
# --------------------------------------------------------------------------

class HttpHelper:
    def __init__(self, ua: str = DEFAULT_UA, referer: str = "", cookie: str = "",
                 proxy: str = "", insecure: bool = False, extra: dict | None = None):
        self.ua = ua or DEFAULT_UA
        self.referer = referer or ""
        self.cookie = cookie or ""
        self.extra = dict(extra or {})
        self._proxy = (proxy or "").strip()
        handlers = [urllib.request.ProxyHandler(
            {"http": self._proxy, "https": self._proxy} if self._proxy else {})]
        if insecure:
            # 宽松校验：从共享 context 派生独立实例，复用证书缓存但不污染全局对象
            ctx = insecure_ssl_context()
            self._ssl_ctx = ctx
            self._ctx_override = ctx
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        else:
            # ★ 正常路径：把共享 context 挂给 urllib 的 HTTPSHandler。
            # 不这么做的话，urllib 的降级路径（代理/异常 URL）会走
            # ssl._create_default_https_context() 每次重新加载 CA 证书，
            # 同样会把并发建连拖成串行（详见模块顶部说明）。
            self._ssl_ctx = None
            self._ctx_override = None
            handlers.append(urllib.request.HTTPSHandler(
                context=shared_ssl_context()))
        self._opener = urllib.request.build_opener(*handlers)
        # 连接池按**线程**分（见 _tl_pool），这里只留一个占位说明
        self._tl = threading.local()

    def headers(self, extra: dict | None = None) -> dict:
        h = {"User-Agent": self.ua, "Accept": "*/*",
             "Accept-Encoding": "identity", "Connection": "keep-alive"}
        if self.referer:
            h["Referer"] = self.referer
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(self.extra)
        if extra:
            h.update(extra)
        return h

    def open(self, url: str, extra: dict | None = None, method: str = "GET",
             timeout: int = SOCKET_TIMEOUT):
        req = urllib.request.Request(url, headers=self.headers(extra), method=method)
        return self._opener.open(req, timeout=timeout)

    # ------------------------------------------------------------------
    # 连接复用（v1.7.0）
    # ------------------------------------------------------------------
    # 为什么需要它：
    #   原先每个区块都走 urllib 新建 TCP 连接。实测单块建连开销：
    #     · 本机回环：2.1ms/块   → 128MB 档 128 块 = 0.19s 纯建连
    #     · 外网 HTTPS：178ms/块 → 同一档要 20+ 秒！(TLS 握手尤其贵)
    #   实测 12 块 × 1MB 对比：
    #     · 本机：0.047s → 0.013s (3.69×)
    #     · 外网：6.109s → 3.551s (1.72×)
    # 做法：
    #   每个工作线程长期持有**一条** http.client 连接，连续发 Range 请求。
    #   服务器返回 Connection: close / 连接被对端关闭时，自动重连一次。
    # 兼容性：
    #   代理、SSRF 防护、异常 url 一律退回原 urllib 路径（_conn_get 返回 None）。
    # ------------------------------------------------------------------
    def _url_key(self, url: str):
        """把 URL 拆成连接池 key：(scheme, host, port, path+q, 是否可用)

        返回 None 表示该 URL 不适合走连接池（交由 urllib 处理）。
        """
        try:
            u = urllib.parse.urlsplit(url)
        except Exception:
            return None
        scheme = (u.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = u.hostname
        if not host:
            return None
        default_port = 443 if scheme == "https" else 80
        port = u.port or default_port
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        if self._proxy or self._ctx_override is not None:
            return None          # 代理 / 自定义 SSL 上下文：交给 urllib 更稳妥
        return (scheme, host, port, path)

    def _host_key(self, url: str):
        """把 URL 拆成**主机级**连接池 key：(scheme, host, port)。

        用于 HLS 这种同一服务器上请求大量不同分片 URL 的场景：
        分片 URL 的 path 各不相同，但主机相同，完全可以在一条长连接上
        串行发多个 GET（HTTP/1.1 keep-alive），避免每个分片都重新 TCP/TLS 握手。
        返回 None 表示该 URL 不适合走连接池（交由 urllib 处理）。
        """
        try:
            u = urllib.parse.urlsplit(url)
        except Exception:
            return None
        scheme = (u.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = u.hostname
        if not host:
            return None
        default_port = 443 if scheme == "https" else 80
        port = u.port or default_port
        if self._proxy or self._ctx_override is not None:
            return None
        return (scheme, host, port)

    def _new_conn(self, scheme: str, host: str, port: int, timeout: float):
        import http.client
        if scheme == "https":
            # ⚠ 绝不要在这里调 ssl.create_default_context()！
            # 每次调用要 0.49s 且持 GIL → 24 线程并发时退化成串行（详见
            # 模块顶部 _SHARED_SSL_CTX 的说明）。这里只做一次极轻量的取出。
            ctx = self._ssl_ctx or shared_ssl_context()
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _tl_pool(self) -> dict:
        """当前线程专属的连接池（线程本地 dict）。

        为什么按线程分池（v1.7.0 设计要点）：
          http.client 的连接**不是线程安全的**（一个连接同时只能有一个
          请求在途）。若多线程共享一个池，就必须加锁串行化 → 又退化成
          "一次只能一个块在建连"。按线程分池后：
            · 每线程 1 条长连接，24 线程 = 24 条并发长连接（正是我们要的）
            · 线程只碰自己的池 → 无锁、无竞争、无竞态
            · 预分配按 1MB 区块对齐，连接数天然受限（不会雪崩）
        """
        d = getattr(self._tl, "pool", None)
        if d is None:
            d = {}
            self._tl.pool = d
        return d

    def _conn_request(self, pool: dict, key, url: str, path: str,
                      extra: dict | None, timeout: float):
        """连接池 GET 的公共实现。返回 (resp, conn, key) 或 (None,None,None)。

        key 的前三个元素必须是 (scheme, host, port)，用于建连；
        后续元素（如 path）只影响池 key，不影响连接目标。
        """
        scheme, host, port = key[:3]
        conn = pool.get(key)
        for attempt in (0, 1):
            try:
                if conn is None:
                    conn = self._new_conn(scheme, host, port, timeout)
                conn.request("GET", path, headers=self.headers(extra))
                resp = conn.getresponse()
                return resp, conn, key
            except Exception:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    LOG.debug('忽略异常：if conn is not None: conn.close()')
                conn = None
                if attempt:          # 第二次仍失败 → 放弃，交由 urllib
                    break
        return None, None, None

    def conn_get(self, url: str, extra: dict | None = None,
                 timeout: float = SOCKET_TIMEOUT):
        """用连接池发一个 GET，返回 (response, conn, key)。

        key 为完整 URL（含 path），适用于普通多线程分块下载：同文件、
        同 URL、不同 Range，一条连接串行服务多个 Range 请求。
        """
        key = self._url_key(url)
        if key is None:
            return None, None, None
        scheme, host, port, path = key
        pool = self._tl_pool()
        return self._conn_request(pool, key, url, path, extra, timeout)

    def conn_get_host(self, url: str, extra: dict | None = None,
                      timeout: float = SOCKET_TIMEOUT):
        """用**主机级**连接池发一个 GET，返回 (response, conn, key)。

        key 只到 (scheme, host, port)，适用于 HLS 分片下载：同一主机的
        不同分片 URL 复用同一条长连接，省掉每个分片一次的 TCP/TLS 握手。
        """
        key = self._host_key(url)
        if key is None:
            return None, None, None
        try:
            path = urllib.parse.urlsplit(url).path or "/"
            if urllib.parse.urlsplit(url).query:
                path += "?" + urllib.parse.urlsplit(url).query
        except Exception:
            return None, None, None
        pool = self._tl_pool()
        return self._conn_request(pool, key, url, path, extra, timeout)

    def conn_release(self, key, conn, keep: bool):
        """归还连接：keep=True 且服务器允许 → 存回线程专属池；否则关闭。"""
        if conn is None:
            return
        pool = self._tl_pool()
        if key is not None and keep:
            pool[key] = conn
        else:
            try:
                conn.close()
            except Exception:
                LOG.debug('忽略异常：conn.close()')
            if key is not None and pool.get(key) is conn:
                pool.pop(key, None)

    @staticmethod
    def is_keepalive(resp) -> bool:
        """服务器是否允许复用这条连接（HTTP/1.1 默认允许）"""
        try:
            c = (resp.getheader("Connection") or "").lower()
        except Exception:
            return False
        if "close" in c:
            return False
        ver = getattr(resp, "version", 11)
        return ver >= 11 or "keep-alive" in c

    def pool_close_all(self):
        """关闭**当前线程**池内连接（线程退出前调用，防句柄泄漏）"""
        pool = self._tl_pool()
        for k, c in list(pool.items()):
            try:
                c.close()
            except Exception:
                LOG.debug('忽略异常：c.close()')
        pool.clear()

    def get_text(self, url: str, limit: int = 8 * 1024 * 1024,
                 extra: dict | None = None) -> tuple[str, dict, int]:
        with self.open(url, extra=extra) as r:
            raw = r.read(limit)
            return raw.decode("utf-8", "replace"), dict(r.headers), getattr(r, "status", 200)

    def get_bytes(self, url: str, limit: int = 4 * 1024 * 1024,
                  extra: dict | None = None) -> bytes:
        """读取原始字节（用于 AES 密钥等二进制内容，绝不能走文本解码）"""
        with self.open(url, extra=extra) as r:
            return r.read(limit)


# --------------------------------------------------------------------------
# 限速器（令牌桶）
# --------------------------------------------------------------------------

class RateLimiter:
    # 单块单次消费至少 READ_SIZE（256KB），令牌桶上限不得低于该值，
    # 否则 consume(n) 永远无法满足，出现死循环（限速 1~127 KB/s 时卡死）。
    _MIN_BURST = READ_SIZE

    def __init__(self, rate_bps: int = 0):
        self.rate = max(0, int(rate_bps or 0))
        # 初始令牌直接装满到 burst，避免低速时第一次 consume 要等几十秒。
        self._tokens = float(max(self._MIN_BURST, self.rate))
        self._ts = now()
        self._lock = threading.Lock()

    def set_rate(self, rate_bps: int):
        with self._lock:
            self.rate = max(0, int(rate_bps or 0))
            burst = max(self._MIN_BURST, self.rate)
            self._tokens = float(min(burst, self._tokens))

    def consume(self, n: int):
        if self.rate <= 0:
            return
        n = max(1, int(n))
        # 小限速下仍允许一次性预支一个完整数据块，避免永久阻塞。
        # 这相当于在低速时把实际突发提到 READ_SIZE，保证功能可用。
        burst = max(self._MIN_BURST, self.rate)
        while True:
            with self._lock:
                t = now()
                self._tokens = min(burst,
                                   self._tokens + (t - self._ts) * self.rate)
                self._ts = t
                if self._tokens >= n:
                    self._tokens -= n
                    return
                # 要满足 n，需要等的时间；rate=0 上面已返回，不会除零。
                wait = (n - self._tokens) / float(self.rate)
            time.sleep(min(max(wait, 0.005), 0.25))


# --------------------------------------------------------------------------
# 状态常量
# --------------------------------------------------------------------------

STATE_PENDING = "pending"
STATE_WAIT = "waiting"
STATE_DOWNLOADING = "downloading"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"

STATE_CN = {
    STATE_PENDING: "准备中", STATE_WAIT: "排队中", STATE_DOWNLOADING: "下载中",
    STATE_PAUSED: "已暂停", STATE_DONE: "已完成", STATE_ERROR: "出错",
    STATE_CANCELLED: "已取消",
}


# ---- 路径安全 ----

def find_tmp_dir(out_dir: str, tid: str) -> str | None:
    """安全地定位续传目录：只接受 out_dir 下直接子目录 .xzd/<tid>，杜绝路径穿越"""
    safe_tid = re.sub(r"[^0-9a-fA-F]", "", str(tid or ""))[:16]
    if not safe_tid or safe_tid != str(tid):
        return None
    base = os.path.realpath(os.path.join(out_dir, ".xzd"))
    cand = os.path.realpath(os.path.join(base, safe_tid))
    if not cand.startswith(base + os.sep):
        return None
    return cand if os.path.isdir(cand) else None


# --------------------------------------------------------------------------
# 任务基类
# --------------------------------------------------------------------------

class DownloadTask:
    def __init__(self, mgr, url: str, out_dir: str, filename: str = "", *,
                 tid: str = "", conns: int = 24, referer: str = "", cookie: str = "",
                 headers: dict | None = None, speed_limit: int = 0, kind: str = "auto",
                 insecure: bool = False, proxy: str = "", group: str = "",
                 added_at: float | None = None):
        self.mgr = mgr
        self.url = url
        self.out_dir = os.path.abspath(out_dir)
        ensure_dir(self.out_dir)
        self.filename = safe_filename(filename) if filename else filename_from_url(url)
        self.tid = tid or hashlib.md5(f"{url}|{now()}".encode("utf-8")).hexdigest()[:12]
        self.requested_conns = max(1, min(32, int(conns)))
        self.kind = kind if kind in ("file", "hls") else kind_of_url(url)
        self.speed_limit = max(0, int(speed_limit or 0))
        self.group = group or ""
        self.added_at = added_at or now()
        self.extra_headers = dict(headers or {})

        self.http = HttpHelper(referer=referer, cookie=cookie, proxy=proxy,
                               insecure=insecure, extra=self.extra_headers)
        self.limiter = RateLimiter(self.speed_limit)

        self.state = STATE_PENDING
        self.err = ""
        self.size = None
        self.done_bytes = 0
        self.speed = 0.0
        self.eta = None
        self.progress = 0.0
        self.phase = "准备中"
        self.created_final = ""
        self.resumable = True

        self._stop = False
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._slot_held = False
        self._tick_t = now()
        self._tick_b = 0
        self._run_thread: threading.Thread | None = None
        self._zombie_hits = 0            # 僵尸巡检计数（v1.11.0，见 Manager._reap_dead_tasks）

    # ---- 控制 ----
    def start(self):
        if self._stop or self.state == STATE_DONE:
            return
        if self.state in (STATE_PAUSED, STATE_ERROR):
            self.err = "" if self.state == STATE_ERROR else self.err
            self.state = STATE_WAIT
            self.phase = "排队中"
            self._wake.set()
            self.mgr.notify()

    def pause(self):
        if self.state in (STATE_DOWNLOADING, STATE_WAIT, STATE_PENDING):
            self.state = STATE_PAUSED
            self.phase = "已暂停"
            self._wake.set()
            self.mgr.notify()

    def cancel(self, delete_files: bool = False):
        self.state = STATE_CANCELLED
        self._stop = True
        self._wake.set()
        if delete_files:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.mgr.notify()

    def remove(self, delete_files: bool = False):
        self.cancel(delete_files=delete_files)
        self.mgr.remove_task(self.tid)

    def add_connections(self, k: int = 8):
        return self.mgr.add_connections(self, k)

    def retry_with_aria2(self) -> bool:
        """任务失败后，切换为内置 aria2c 备用通道重试。

        返回 True 表示已发起重试（在后台线程跑），False 表示无法重试。
        """
        try:
            import aria2_bridge
        except Exception as exc:
            self.err = f"aria2c 备用通道不可用：{exc}"
            self.mgr.notify()
            return False
        if self.state not in (STATE_ERROR, STATE_PAUSED, STATE_WAIT):
            # 已完成/已取消的任务不重试
            return False
        self.state = STATE_WAIT
        self.err = ""
        self.phase = "排队中（aria2c 备用）"
        self.mgr.notify()

        def work():
            try:
                self._slot_held = False
                # 释放旧槽位（如果还占着）
                self.mgr.release_slot(self)
                # 占用新槽位
                if not self.mgr.acquire_slot(self):
                    self.state = STATE_ERROR
                    self.err = "aria2c 重试：无法获得并发槽位"
                    self.phase = "出错"
                    self.mgr.notify()
                    return
                self.state = STATE_DOWNLOADING
                self.phase = "启动 aria2c 备用通道"
                self.mgr.notify()
                result = aria2_bridge.run_aria2(
                    self.url, self.out_dir, filename=self.filename,
                    referer=self.http.referer, cookie=self.http.cookie,
                    headers=dict(self.http.extra),
                    user_agent=self.http.ua,
                    proxy=self.http._proxy,
                    connections=min(16, self.requested_conns),
                    task=self)
                if not result.get("ok"):
                    self.state = STATE_ERROR
                    self.err = f"aria2c 备用失败：{result.get('error', '未知')}"
                    self.phase = "出错"
                # 成功由 run_aria2 自动更新 task 状态
            except Exception as exc:
                self.state = STATE_ERROR
                self.err = f"aria2c 重试异常：{exc}"
                self.phase = "出错"
            finally:
                self.mgr.release_slot(self)
                self.mgr.save_jobs()
                self.mgr.notify()
        threading.Thread(target=work, daemon=True, name=f"xzd-aria2-{self.tid}").start()
        return True

    def set_speed_limit(self, bps: int):
        self.speed_limit = max(0, int(bps or 0))
        self.limiter.set_rate(self.speed_limit)
        self.mgr.notify()

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.out_dir, ".xzd", self.tid)

    def final_path(self) -> str:
        return os.path.join(self.out_dir, self.filename)

    def snapshot(self) -> dict:
        return {
            "tid": self.tid, "url": self.url, "filename": self.filename,
            "out_dir": self.out_dir, "state": self.state,
            "state_cn": STATE_CN.get(self.state, self.state),
            "kind": self.kind, "size": self.size, "done_bytes": self.done_bytes,
            "speed": round(self.speed, 1), "eta": self.eta,
            "progress": round(self.progress, 4), "phase": self.phase,
            "err": self.err, "conns": self.active_conns(),
            "target_conns": self.target_conns(),
            "speed_limit": self.speed_limit, "group": self.group,
            "final": self.created_final, "added_at": self.added_at,
            "resumable": self.resumable,
        }

    def active_conns(self) -> int:
        return 0

    def target_conns(self) -> int:
        return self.requested_conns

    # ---- 进度/速度 ----
    def _tick_speed(self):
        t = now()
        dt = t - self._tick_t
        if dt >= 0.6:
            self.speed = max(0.0, (self.done_bytes - self._tick_b) / dt)
            self._tick_t = t
            self._tick_b = self.done_bytes

    def _refresh_eta(self):
        if self.speed > 1:
            if self.size:
                self.eta = max(0.0, (self.size - self.done_bytes) / self.speed)
            else:
                self.eta = self._extra_eta()
        else:
            self.eta = None

    def _extra_eta(self):
        return None

    # ---- 生命周期（由子类实现会话）----
    def _session(self):
        raise NotImplementedError

    def run(self):
        try:
            while not self._stop:
                if self.state == STATE_CANCELLED:
                    return
                if self.state == STATE_ERROR:
                    return
                if self.state == STATE_PAUSED:
                    self._wake.wait(timeout=1.0)
                    self._wake.clear()
                    continue
                if self.state == STATE_DONE:
                    return
                # 需要执行：先抢并发槽位
                if not self.mgr.acquire_slot(self):
                    continue
                self.state = STATE_DOWNLOADING
                try:
                    self._session()
                except Exception as e:  # noqa
                    self.state = STATE_ERROR
                    self.err = f"{type(e).__name__}: {e}。可右键任务 → 用 aria2c 极速重试"
                    self.phase = "出错（可用 aria2c 重试）"
                finally:
                    self.mgr.release_slot(self)
                    self.mgr.save_jobs()
                    self.mgr.notify()
                if self.state == STATE_DONE:
                    self.phase = self.phase if self.phase else "已完成"
                    # 音频提取（extra.audio_only）
                    if getattr(self, 'extra', {}).get('audio_only'):
                        self._extract_audio()
                    # 音视频合并（extra.paired_audio）
                    if getattr(self, 'extra', {}).get('paired_audio'):
                        self._merge_with_audio()
                    if self.mgr.on_task_done:
                        try:
                            self.mgr.on_task_done(self)
                        except Exception:
                            LOG.debug('忽略异常：mgr.on_task_done(self)')
                    return
        except BaseException as e:
            # v1.11.0：任务主循环**最后一道防线**。
            try:
                if self.state not in (STATE_DONE, STATE_CANCELLED):
                    self.state = STATE_ERROR
                    self.err = f"内部异常 {type(e).__name__}: {e}。可右键任务 → 用 aria2c 极速重试"
                    self.phase = "内部错误（可用 aria2c 重试）"
            except Exception:
                pass
            LOG.exc("任务主循环未捕获异常 tid=%s（已强制置为出错态）", self.tid,
                    level=40)
        finally:
            # v1.11.0：收尾也必须有保护 —— save_jobs/notify 抛异常同样会让
            # 线程带着异常退出，把任务永久钉在"下载中"。
            try:
                self.mgr.release_slot(self)
            except Exception:
                LOG.debug('忽略异常：release_slot')
            try:
                self.mgr.on_task_end(self)
            except Exception:
                LOG.once_exc("on_task_end", "任务收尾处理失败 tid=%s", self.tid)

    def _extract_audio(self):
        """用 FFmpeg 把已下载的视频/音频文件抽音轨转 MP3。"""
        src = getattr(self, 'created_final', None)
        if not src or not os.path.exists(src):
            return
        ffmpeg = os.path.join(app_root(), "third_party", "ffmpeg.exe")
        if not os.path.exists(ffmpeg):
            return
        dst = os.path.splitext(src)[0] + ".mp3"
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", src, "-vn", "-acodec", "libmp3lame",
                 "-q:a", "2", dst],
                capture_output=True, timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and os.path.exists(dst):
                os.remove(src)
                self.created_final = dst
                self.filename = os.path.basename(dst)
                self.size = os.path.getsize(dst)
                self.done_bytes = self.size
        except Exception:
            pass

    def _merge_with_audio(self):
        """下载完成后，自动下载配对音频并用 FFmpeg 合并为完整视频。"""
        video_path = getattr(self, 'created_final', None)
        audio_url = self.extra.get('paired_audio') if self.extra else None
        if not video_path or not os.path.exists(video_path) or not audio_url:
            return
        ffmpeg = os.path.join(app_root(), "third_party", "ffmpeg.exe")
        if not os.path.exists(ffmpeg):
            return
        # 下载配对音频到同目录
        audio_path = os.path.splitext(video_path)[0] + "_audio.mp3"
        try:
            # 创建音频下载任务（内部走 Manager，避免重复造轮子）
            t = self.mgr.add_task(audio_url, out_dir=os.path.dirname(video_path),
                                  filename=os.path.basename(audio_path),
                                  referer=self.http.referer, cookie=self.http.cookie)
            # 等待音频下载完成（最多 10 分钟）
            wait = 0
            while t.state not in (STATE_DONE, STATE_ERROR, STATE_CANCELLED) and wait < 600:
                time.sleep(1)
                wait += 1
            if t.state != STATE_DONE or not os.path.exists(t.created_final):
                return
            audio_path = t.created_final
            # FFmpeg 合并：视频流 copy + 音频流 copy
            out_path = os.path.splitext(video_path)[0] + "_merged.mp4"
            r = subprocess.run(
                [ffmpeg, "-y", "-i", video_path, "-i", audio_path,
                 "-c:v", "copy", "-c:a", "aac", "-strict", "experimental", out_path],
                capture_output=True, timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and os.path.exists(out_path):
                # 合并成功：删除分离的视频和音频，保留合并后文件
                os.remove(video_path)
                os.remove(audio_path)
                self.created_final = out_path
                self.filename = os.path.basename(out_path)
                self.size = os.path.getsize(out_path)
                self.done_bytes = self.size
        except Exception:
            pass


# --------------------------------------------------------------------------
# 文件下载：区块位图 + 单文件随机写入（aria2 式）
# --------------------------------------------------------------------------

class FileDownload(DownloadTask):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.kind = "file"
        self.bitmap: bytearray | None = None
        self.nblocks = 0
        self.block_size = BLOCK_SIZE
        self.range_ok = False
        self.single_mode = False
        self.target = 1
        self._queue: list[int] = []
        self._qlock = threading.Lock()
        self._filelock = threading.Lock()
        # 位图更新专用轻量锁：与全局任务锁分开，16 线程高频标记时不再相互阻塞
        self._block_lock = threading.Lock()
        # "正在下载中"的区块集合（已从队列取出、尚未标记完成）。
        # 关键：队列为空时重建待下载列表必须排除这些区块，
        # 否则多个线程会同时抓同一个区块 → 重复传输（实测多耗 3~10% 流量）
        self._inflight: set[int] = set()
        # 线程本地存储：复用文件句柄，省掉每块 open/close 的系统调用
        self._tl = threading.local()
        # 所有存活的写句柄注册表（锁保护）。
        # 用途：工作线程可能正阻塞在网络读取上、来不及响应 _stop_workers，
        # join 超时后句柄仍被占用 → Windows 下 _finalize 的 os.replace 会失败。
        # 有了注册表，主线程可以在收尾前**强制关闭**所有残留句柄。
        self._fh_reg: set = set()
        self._fh_reg_lock = threading.Lock()
        # "刚好下完"事件：最后一块落盘时唤醒主循环，立即进入收尾
        self._done_evt = threading.Event()
        # 探测阶段顺手取回的首块数据（可为空）
        self._probe_seed = b""
        self._block_fails: dict[int, int] = {}
        self._fatal = ""
        self._workers: list[threading.Thread] = []
        self._data_f = None
        self._stop_workers = False
        self._last_progress_t = now()
        self._last_progress_b = 0
        # ---- 异步写盘池（v1.8.0）----
        self._wr_q: queue.Queue = queue.Queue(maxsize=self.WRITE_QUEUE_SIZE)
        self._writers: list[threading.Thread] = []
        self._wr_started = False
        self._wr_pending = 0
        self._wr_free = threading.Event()

    # ---------- 路径 ----------
    @property
    def data_path(self) -> str:
        return os.path.join(self.tmp_dir, "data.bin")

    @property
    def bitmap_path(self) -> str:
        return os.path.join(self.tmp_dir, "bitmap.bin")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.tmp_dir, "meta.json")

    # ---------- 探测 ----------
    def _probe(self):
        self.phase = "探测服务器"
        self.mgr.notify()
        # ★ v2.3.0：先把共享 SSLContext 预热好再发探测请求。
        # 探测本身就要建一条 TLS 连接 —— 这里创建 context 属于"本来就要付一次"，
        # 但它会把 0.49s 的证书加载成本从**每个工作线程**（24× = 串行 6.3s）
        # 收敛成全局一次。放在探测前做，后续 24 个线程全部直接复用。
        if self.url.lower().startswith("https"):
            warmup_ssl_context()
        # 一次请求同时完成两件事：拿到文件总大小 + 顺手把**第一个区块的数据**取回来。
        # 原先先 bytes=0-0 探测、再单独发一次 Range 请求拿首块，多花一次完整往返
        # （实测约 0.3s，对 100~200MB 档占比不可忽略）。合并后只需一次建连。
        #
        # v1.7.0：探测也要处理 429。实测镜像站在并发压测下会对探测请求回
        # 429 Too Many Requests，旧代码直接把异常抛到 _session → 任务瞬间变成
        # error（实测 0.39s 就失败）。现在探测阶段同样退避重试，且**逐次降低
        # 并发预感**（记入全局压力信号）让工作线程起步就收敛。
        want = BLOCK_SIZE
        extra = {"Range": f"bytes=0-{want - 1}"}
        seed = b""
        status = 200
        hdrs: dict = {}
        last_err = None
        for attempt in range(10):
            try:
                req = urllib.request.Request(self.url, headers=self.http.headers(extra))
                with self.http._opener.open(req, timeout=SOCKET_TIMEOUT) as r:
                    status = getattr(r, "status", 200)
                    hdrs = dict(r.headers)
                    if status == 206:
                        # 只读一个区块就断开；多余部分不会产生额外流量
                        try:
                            seed = r.read(want)
                        except Exception:
                            seed = b""
                if status in RATE_LIMIT_CODES:
                    note_throttle()
                    backoff = min(RATE_LIMIT_BASE_BACKOFF * (2 ** attempt),
                                  RATE_LIMIT_MAX_BACKOFF)
                    backoff *= (0.75 + 0.5 * random.random())
                    time.sleep(backoff)
                    continue
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if getattr(e, "code", 0) in RATE_LIMIT_CODES:
                    note_throttle()
                    backoff = min(RATE_LIMIT_BASE_BACKOFF * (2 ** attempt),
                                  RATE_LIMIT_MAX_BACKOFF)
                    backoff *= (0.75 + 0.5 * random.random())
                    time.sleep(backoff)
                    continue
                raise
            except Exception as e:      # noqa
                last_err = e
                if attempt >= 3:
                    raise
                time.sleep(min(1.0 * (attempt + 1), 4))
        else:
            # 10 次仍被限速：明确报错（而不是抛原始 HTTPError）
            raise IOError(f"服务器持续限速（HTTP {status}），请稍后重试")
        if status in RATE_LIMIT_CODES and last_err is not None:
            raise IOError(f"服务器持续限速（HTTP {status}），请稍后重试")
        cr = hdrs.get("Content-Range")
        if status == 206 and cr and "/" in cr:
            try:
                self.size = int(cr.rsplit("/", 1)[-1])
                self.range_ok = True
            except ValueError:
                self.size = None
        if self.size is None:
            cl = (hdrs.get("Content-Length") or "").strip()
            self.size = int(cl) if cl.isdigit() else None
        # 首块数据仅在"支持 Range 且长度吻合"时可用于预填，否则丢弃（宁慢勿错）
        if seed and self.range_ok and self.size:
            if len(seed) == min(want, self.size):
                self._probe_seed = bytes(seed)
            else:
                self._probe_seed = b""
        ctype = (hdrs.get("Content-Type") or "").lower()
        if "mpegurl" in ctype or self.url.lower().split("?")[0].endswith(".m3u8"):
            self.kind = "hls"
            return
        # 文件名
        fn = filename_from_disposition(hdrs.get("Content-Disposition", ""))
        if fn:
            self.filename = safe_filename(fn, self.filename)
        elif not os.path.splitext(self.filename)[1]:
            ext = guess_ext_from_mime(ctype)
            if ext:
                self.filename += ext

    # ---------- 断点数据 ----------
    def _load_state(self) -> bool:
        """尝试加载上次的续传数据，成功返回 True"""
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return False
        if not meta.get("size") or meta.get("size") != self.size:
            return False
        if not os.path.exists(self.data_path):
            return False
        try:
            blob = open(self.bitmap_path, "rb").read()
        except Exception:
            return False
        if len(blob) != meta.get("nblocks", -1):
            return False
        self.block_size = meta.get("block_size", BLOCK_SIZE)
        self.nblocks = meta["nblocks"]
        self.single_mode = bool(meta.get("single_mode", False))
        self.range_ok = bool(meta.get("range_ok", self.range_ok))
        self.bitmap = bytearray(blob)
        self.done_bytes = 0
        for i, b in enumerate(self.bitmap):
            if b:
                self.done_bytes += self._block_len(i)
        self.filename = meta.get("filename", self.filename)
        return True

    def _block_len(self, i: int) -> int:
        if self.size is None:
            return self.block_size
        start = i * self.block_size
        return min(self.block_size, self.size - start)

    def _save_state(self, force: bool = False):
        if self.kind == "hls" or self.bitmap is None:
            return
        try:
            meta = {
                "url": self.url, "filename": self.filename, "size": self.size,
                "nblocks": self.nblocks, "block_size": self.block_size,
                "single_mode": self.single_mode, "range_ok": self.range_ok,
                "done_bytes": self.done_bytes, "referer": self.http.referer,
                "host": urllib.parse.urlsplit(self.url).netloc,
            }
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(tmp, self.meta_path)
            btmp = self.bitmap_path + ".tmp"
            with open(btmp, "wb") as f:
                f.write(bytes(self.bitmap))
            os.replace(btmp, self.bitmap_path)
        except Exception:
            # ⚠ 高危静默点：这里落盘失败 = 断点续传从此失效，
            # 用户只会看到"怎么每次都从头下"，却拿不到任何线索。
            # 每 2 秒就会被调一次，所以只记首次（once），避免刷爆日志。
            LOG.once_exc("save_state", "位图/元数据落盘失败 tid=%s（续传将失效）", self.tid)
        else:
            LOG.info("状态已落盘 tid=%s done=%d/%s", self.tid, self.done_bytes, self.size)

    # ---------- 位图维护 ----------
    def _mark_block(self, i: int, ok: bool = True):
        # 用专用轻量锁，避免与全局任务锁争抢；位图标记是热路径
        with self._block_lock:
            if self.bitmap is None:
                return
            if ok:
                if not self.bitmap[i]:
                    self.bitmap[i] = 1
                    self.done_bytes += self._block_len(i)
            if self.size:
                self.progress = min(1.0, self.done_bytes / float(self.size))
            just_done = self.size is not None and self.done_bytes >= self.size
        if just_done:
            # 全部区块到手 → 通知主循环立刻收尾（不等到下一次轮询）
            self._done_evt.set()

    def _is_done(self) -> bool:
        if self.size is None:
            return False
        return self.done_bytes >= self.size

    def _missing_blocks(self) -> list[int]:
        if self.bitmap is None:
            return []
        return [i for i, b in enumerate(self.bitmap) if not b]

    # ---------- 会话 ----------
    def _check_disk_space(self, need: int):
        """开工前检查目标分区剩余空间（v1.11.0）。

        · 续传场景只需剩余未下载部分 → 用 done_bytes 抵扣，避免误判
        · 留 64MB 余量：位图/元数据/日志也要写盘，且碎片化会吃掉一些
        · 检查失败（拿不到磁盘信息）时**放行**，绝不因预检本身挡住下载；
          真正的写入失败仍由既有错误处理兜底
        """
        try:
            probe_dir = self.out_dir if os.path.isdir(self.out_dir) else os.path.dirname(self.data_path)
            while probe_dir and not os.path.isdir(probe_dir):
                parent = os.path.dirname(probe_dir)
                if parent == probe_dir:
                    break
                probe_dir = parent
            if not probe_dir or not os.path.isdir(probe_dir):
                return
            free = shutil.disk_usage(probe_dir).free
            remaining = max(0, int(need) - int(self.done_bytes or 0))
            if free < remaining + 64 * 1024 * 1024:
                raise OSError(
                    f"磁盘空间不足：需要约 {human_size(remaining + 64 * 1024 * 1024)}，"
                    f"可用仅 {human_size(free)}（{probe_dir}）")
            LOG.debug("磁盘空间检查通过：需要 %s，可用 %s",
                      human_size(remaining), human_size(free))
        except OSError:
            raise                              # 空间不足 → 让上层置为出错态
        except Exception:
            LOG.debug('磁盘空间检查本身失败，放行（不影响下载）')

    def _session(self):
        if self.size is None and not self._load_state():
            self._probe()
            if self.kind == "hls":
                self._run_hls_fallback()
                return
        if self.size is None or self.size < 0:
            # 未知大小 → 单流下载
            self._single_stream_loop()
            return
        # v1.11.0：磁盘空间预检。
        # 教训来源：v1.8.0 压测时磁盘写满（Errno 28），下载中途才炸，
        # 而且是在"已经写了一部分"之后 —— 既浪费时间又留下半成品。
        # 已知体积时先在开工前检查，宁可早失败也别下到一半才崩。
        self._check_disk_space(self.size)
        if self.size < MIN_SEG_SIZE or not self.range_ok:
            self.single_mode = True
        if self.single_mode:
            self._single_stream_loop()
            return

        # ---- 多线程区块模式 ----
        ensure_dir(self.tmp_dir)
        if not self._load_state():
            self.nblocks = (self.size + BLOCK_SIZE - 1) // BLOCK_SIZE
            self.block_size = BLOCK_SIZE
            self.bitmap = bytearray(self.nblocks)
            # 预分配改为**后台异步**执行：
            #   · 大文件首次把文件扩到完整大小在 NTFS 上要耗时
            #     （178MB≈0.4s，929MB≈3.3s），放在关键路径上纯属浪费；
            #   · 后台线程扩容与前台写入互不冲突（truncate 只增大、从不缩小，
            #     不影响已写入区间），因此可以安全并行；
            #   · 收尾/暂停前会 _await_prealloc() 汇合，避免句柄未释放导致
            #     文件无法移动（Windows 上打开的文件默认不可 rename）。
            if self.size:
                self._start_prealloc(self.size)
            self._save_state()
        else:
            # 续传：确保数据文件尺寸正确（同样放进后台）
            try:
                cur = os.path.getsize(self.data_path)
            except OSError:
                cur = -1
            if cur != self.size:
                if cur < 0:
                    with open(self.data_path, "wb"):
                        pass
                self._start_prealloc(self.size)

        if self._is_done():
            self._finalize()
            return

        self.target = max(1, min(32, self.requested_conns))
        self._stop_workers = False
        self._spawn_workers(self.target)
        last_save = now()
        self._last_progress_t = now()
        self._last_progress_b = self.done_bytes

        while not self._stop:
            # 用事件等待替代 sleep：最后一块落盘时会被立刻唤醒，
            # 省掉最多 0.4s 的"已完成却还在等轮询"的空转（小文件尤其明显）
            self._done_evt.wait(0.4)
            self._done_evt.clear()
            self._tick_speed()
            self._refresh_eta()
            if self.state == STATE_PAUSED:
                self._stop_workers = True
                self._join_workers(3)
                # v1.8.0：写线程必须**先汇合**（把已入队数据全部落盘），
                # 再存位图 —— 否则位图说"这块完成了"，实际数据还在队列里
                self._drain_writers(timeout=120.0)
                self._save_state()
                # 汇合后台预分配（幂等，通常已结束）并释放句柄，
                # 让暂停后文件不被占用，用户可自由移动/复制
                self._await_prealloc(timeout=10.0)
                self._shutdown_tl_handles()
                return
            if self.state == STATE_CANCELLED:
                self._stop_workers = True
                self._join_workers(3)
                self._drain_writers(timeout=120.0)
                self._await_prealloc(timeout=10.0)
                self._shutdown_tl_handles()
                return
            if self._fatal:
                self._stop_workers = True
                self._join_workers(3)
                self._drain_writers(timeout=120.0)
                self._save_state()
                self._await_prealloc(timeout=10.0)
                self._shutdown_tl_handles()
                self.state = STATE_ERROR
                self.err = f"{self._fatal}。可右键任务 → 用 aria2c 极速重试"
                self.phase = "出错（可用 aria2c 重试）"
                return
            if self._is_done():
                self._stop_workers = True
                self._join_workers(3)
                # 全部区块都已入队完成 → 等写线程把最后一块落盘再收尾
                self._drain_writers(timeout=300.0)
                self._save_state()
                self._finalize()
                return
            # 目标连接数变化 → 增/减线程
            alive = sum(1 for t in self._workers if t.is_alive())
            if self.target > alive:
                self._spawn_workers(self.target - alive)
            # 完成一批区块后可能有大量空闲线程 → 让线程自然退出
            if now() - last_save >= BITMAP_SAVE_INTERVAL:
                self._save_state()
                last_save = now()
            if self.done_bytes > self._last_progress_b + 64 * 1024:
                self._last_progress_t = now()
                self._last_progress_b = self.done_bytes
            elif now() - self._last_progress_t > 45 and alive == 0 and not self._is_done():
                # 全部线程退出但没下完 → 再补一轮
                self._spawn_workers(max(1, min(4, self.target)))
                if now() - self._last_progress_t > 120:
                    self.state = STATE_ERROR
                    self.err = "长时间无数据进展，已停止重试。可右键任务 → 用 aria2c 极速重试"
                    self.phase = "出错（可用 aria2c 重试）"
                    self._save_state()
                    return
            self.phase = f"{self.active_conns()} 线程 · 区块 {self.nblocks - len(self._missing_blocks())}/{self.nblocks}"
            self.mgr.notify()

    def active_conns(self) -> int:
        # 已完成的任务保留"最终使用线程数"，便于界面展示
        if self.state in (STATE_DONE, STATE_CANCELLED):
            return self.target
        if self.state == STATE_PAUSED:
            return 0
        return sum(1 for t in self._workers if t.is_alive())

    def target_conns(self) -> int:
        return self.target

    def _spawn_workers(self, k: int):
        for _ in range(int(k)):
            t = threading.Thread(target=self._worker, daemon=True,
                                 name=f"xzd-{self.tid}")
            self._workers.append(t)
            t.start()
        self.phase = f"{self.target} 线程"

    def _join_workers(self, timeout: float):
        t0 = now()
        for t in list(self._workers):
            remain = timeout - (now() - t0)
            if remain <= 0:
                break
            t.join(timeout=remain)

    def _close_tl_handles(self):
        """关闭所有复用文件句柄（含各工作线程缓存的）。

        必须在 _finalize / 暂停 / 取消 之前调用：
        Windows 上**被打开的文件无法 rename/replace**，
        缓存句柄不关会导致 os.replace 抛 PermissionError。

        注意：工作线程可能仍阻塞在网络读取中没退出，
        因此不能只依赖它们自己收尾——这里通过注册表**强制关闭**。
        """
        # 关主线程自己可能持有的句柄
        try:
            f = getattr(self._tl, "fh", None)
            if f is not None and not f.closed:
                f.close()
            self._tl.fh = None
        except Exception:
            LOG.debug('忽略异常：f = getattr(_tl, "fh", None)')
        # 强制关闭所有已注册的工作线程句柄
        with self._fh_reg_lock:
            reg = list(self._fh_reg)
            self._fh_reg.clear()
        for f in reg:
            try:
                if not f.closed:
                    f.close()
            except Exception:
                LOG.debug('忽略异常：if not f.closed: f.close()')
        self._shutdown_tl = True

    def _register_fh(self, f):
        with self._fh_reg_lock:
            self._fh_reg.add(f)

    def _unregister_fh(self, f):
        with self._fh_reg_lock:
            self._fh_reg.discard(f)

    def _shutdown_tl_handles(self):
        """工作线程退出前主动关闭自己的句柄（在 _worker 的 finally 中调用）"""
        try:
            f = getattr(self._tl, "fh", None)
            if f is not None and not f.closed:
                self._unregister_fh(f)
                f.close()
            self._tl.fh = None
        except Exception:
            LOG.debug('忽略异常：f = getattr(_tl, "fh", None)')

    def _claim_block(self) -> int | None:
        """取出一个待下载区块，并登记为"下载中"，避免被其他线程重复领取"""
        with self._qlock:
            while self._queue:
                i = self._queue.pop()
                if self.bitmap is None or not self.bitmap[i]:
                    self._inflight.add(i)
                    return i
            return None

    def _release_block(self, i: int, done: bool):
        """区块结束（成功或放弃）：从"下载中"移除；失败则重新入队"""
        with self._qlock:
            self._inflight.discard(i)
            if not done and self.bitmap is not None and not self.bitmap[i] \
                    and i not in self._queue:
                self._queue.append(i)

    def _requeue_block(self, i: int):
        with self._qlock:
            self._inflight.discard(i)
            if self.bitmap is not None and not self.bitmap[i] and i not in self._queue:
                self._queue.append(i)

    def _missing_now(self) -> list[int]:
        """当前真正待下载的区块 = 位图未完成 - 正在下载中"""
        with self._qlock:
            if self.bitmap is None:
                return []
            return [i for i, b in enumerate(self.bitmap)
                    if not b and i not in self._inflight]

    def _worker(self):
        try:
            while not self._stop_workers and not self._stop:
                if self.state in (STATE_PAUSED, STATE_CANCELLED):
                    return
                # 线程数超出目标 → 自行退出（等调度器再拉起）
                if self.active_conns() > self.target:
                    return
                with self._qlock:
                    if not self._queue:
                        # 重建队列时必须排除"正在下载中"的区块，
                        # 否则会造成重复下载（浪费带宽、拖慢整体）
                        missing = [i for i, b in enumerate(self.bitmap or [])
                                   if not b and i not in self._inflight]
                        if not missing:
                            return
                        # 从尾部开始（大偏移），与顺序读取互补
                        self._queue = sorted(missing, reverse=True)
                i = self._claim_block()
                if i is None:
                    return
                ok = self._download_block(i)
                if not ok:
                    # 暂停/取消导致的中止，不计入失败次数
                    if (self.state in (STATE_PAUSED, STATE_CANCELLED)
                            or self._stop_workers or self._stop):
                        self._requeue_block(i)
                        return
                    fails = self._block_fails.get(i, 0) + 1
                    self._block_fails[i] = fails
                    if fails > MAX_BLOCK_FAILS:
                        with self._qlock:
                            self._inflight.discard(i)
                        self._fatal = f"区块 {i} 连续 {fails} 次失败，已放弃"
                        return
                    self._requeue_block(i)
                    time.sleep(min(2.0 * fails, 15))
                else:
                    self._block_fails.pop(i, None)
                    with self._qlock:
                        self._inflight.discard(i)
        except Exception as e:  # noqa
            self._fatal = f"线程异常 {type(e).__name__}: {e}"
        finally:
            # 线程退出时释放自己缓存的写句柄
            # （Windows 下句柄不释放会导致收尾 os.replace 失败）
            self._shutdown_tl_handles()
            # v1.7.0：同时归还线程专属长连接（否则连接会随线程"漏"掉）
            try:
                self.http.pool_close_all()
            except Exception:
                LOG.debug('忽略异常：http.pool_close_all()')

    def _download_block(self, i: int) -> bool:
        start = i * self.block_size
        end = start + self._block_len(i) - 1
        want = self._block_len(i)
        # 探测阶段已经把首块数据取回来了 → 直接落盘，省掉一次完整建连往返
        seed = getattr(self, "_probe_seed", b"")
        if seed and i == 0 and self.block_size == BLOCK_SIZE and len(seed) == want:
            self._probe_seed = b""
            try:
                self._write_at(0, bytearray(seed))
                self._mark_block(0)
                return True
            except Exception:
                LOG.debug('忽略异常：_write_at(0, bytearray(seed))')
        attempt = 0
        rl_attempt = 0                     # 限流退避计数（与普通重试分开）
        while attempt < MAX_RETRY:
            if self._stop_workers or self._stop or self.state in (STATE_PAUSED, STATE_CANCELLED):
                return False
            conn = key = resp = None
            keep = False
            try:
                hdrs = {"Range": f"bytes={start}-{end}"}
                # 全局压力感知：别的线程刚被限流 → 自己也先缓一下，
                # 避免所有线程一起顶着服务器撞（这是 429 越撞越多的根源）
                p = throttle_pressure()
                if p > 0.55:
                    time.sleep(0.25 + 0.55 * p)
                # ---- v1.7.0：优先走线程专属长连接（省掉每块一次的 TCP/TLS 建连）----
                resp, conn, key = self.http.conn_get(self.url, extra=hdrs)
                if resp is not None:
                    status = getattr(resp, "status", 200)
                    if status in RATE_LIMIT_CODES:
                        # 服务器限速：这一块不算"失败重试"，走专门退避
                        try:
                            resp.read(65536)      # 读掉响应体，便于连接复用
                        except Exception:
                            LOG.debug('忽略异常：resp.read(65536)')
                        note_throttle()
                        raise _RateLimited(status)
                    if status == 200 and start > 0:
                        # 服务器忽略 Range → 跳过前缀
                        need_skip = start
                        while need_skip > 0:
                            d = resp.read(min(READ_SIZE, need_skip))
                            if not d:
                                raise IOError("skip 失败")
                            need_skip -= len(d)
                            self.limiter.consume(len(d))
                    elif status not in (200, 206):
                        raise IOError(f"HTTP {status}")
                    # 线程本地复用缓冲 + readinto 直接读入：
                    #   · 旧写法 bytearray() 从 0 增长，1MB 会触发多轮 realloc，
                    #     每轮都产生一份临时副本（24 线程下峰值多出几十 MB）
                    #   · 现在缓冲按线程复用一个，容量一次给足，只读不重建
                    #   · 用 readinto(mv) 直接写入目标内存，全程一次拷贝都没有
                    buf = self._get_buf(want)
                    got = 0
                    ro = resp.readinto
                    while got < want:
                        if self.state in (STATE_PAUSED, STATE_CANCELLED) or self._stop_workers:
                            return False
                        mv = memoryview(buf)[got:want]
                        try:
                            n = ro(mv)
                        except (AttributeError, TypeError):
                            n = None
                        finally:
                            mv.release()
                        if n is None:                 # 极少数响应对象不支持 readinto
                            d = resp.read(min(READ_SIZE, want - got))
                            if not d:
                                break
                            buf[got:got + len(d)] = d
                            n = len(d)
                        if not n:
                            break
                        got += n
                        self.limiter.consume(n)
                    keep = self.http.is_keepalive(resp)
                else:
                    # ---- 降级路径：连接池不可用（代理/异常 URL/建连两次失败）----
                    with self.http.open(self.url, extra=hdrs) as r:
                        status = getattr(r, "status", 200)
                        if status in RATE_LIMIT_CODES:
                            note_throttle()
                            raise _RateLimited(status)
                        if status == 200 and start > 0:
                            need_skip = start
                            while need_skip > 0:
                                d = r.read(min(READ_SIZE, need_skip))
                                if not d:
                                    raise IOError("skip 失败")
                                need_skip -= len(d)
                                self.limiter.consume(len(d))
                        elif status not in (200, 206):
                            raise IOError(f"HTTP {status}")
                        buf = self._get_buf(want)
                        got = 0
                        ro = r.readinto
                        while got < want:
                            if self.state in (STATE_PAUSED, STATE_CANCELLED) or self._stop_workers:
                                return False
                            mv = memoryview(buf)[got:want]
                            try:
                                n = ro(mv)
                            except (AttributeError, TypeError):
                                n = None
                            finally:
                                mv.release()
                            if n is None:
                                d = r.read(min(READ_SIZE, want - got))
                                if not d:
                                    break
                                buf[got:got + len(d)] = d
                                n = len(d)
                            if not n:
                                break
                            got += n
                            self.limiter.consume(n)
                if got != want:
                    raise IOError(f"数据不足 {got}/{want}")
                # 写入：复用常驻文件句柄 + 线程本地缓冲，零额外拷贝
                self._write_at(start, memoryview(buf)[:want])
                # ⚠ v1.8.0 关键：缓冲已随入队**交给写线程**，必须立刻换一块新的。
                # 若继续复用这块缓冲，写线程还在往外写、下载线程已经往里覆盖
                # 新数据 → 静默数据损坏（v1.5.0 踩过同样的坑）。
                self._drop_buf()
                self._mark_block(i)
                # 正常读完 → 连接归还池中复用（下一块省掉完整建连）
                self.http.conn_release(key, conn, keep)
                return True
            except _RateLimited as e:
                # ---- 限速：独立退避策略 ----
                # 与普通失败的区别：
                #   · 不计入 _block_fails（否则 24 次上限一到整块就被放弃）
                #   · 退避更久（1.5s → 3s → 6s → 12s，带抖动防同步）
                #   · 连接状态可能仍可用，但为稳妥仍关闭
                self.http.conn_release(key, conn, False)
                rl_attempt += 1
                if rl_attempt > 12:
                    return False              # 极端情况：12 次退避仍被限速
                backoff = min(RATE_LIMIT_BASE_BACKOFF * (2 ** (rl_attempt - 1)),
                              RATE_LIMIT_MAX_BACKOFF)
                backoff *= (0.75 + 0.5 * random.random())   # 抖动
                time.sleep(backoff)
            except Exception:
                # 出错路径：连接状态已不可信，必须关闭而非复用
                self.http.conn_release(key, conn, False)
                attempt += 1
                if attempt >= MAX_RETRY:
                    return False
                time.sleep(min(1.0 * attempt, 6))
        return False

    def _get_buf(self, want: int) -> bytearray:
        """取一个容量 ≥ want 的线程本地缓冲，避免频繁分配/回收造成的内存峰值。

        为什么不在每块下载里新建 bytearray：
          bytearray() 从 0 增长到 1MB 会经历多轮 realloc，每次 realloc
          都要复制已有数据 —— 24 线程并发时这片临时内存会瞬间叠加，
          实测能把峰值顶到 100MB 以上。复用后每线程常驻一份，稳定且快。

        v1.8.0 注意：缓冲一旦交给写线程（_write_at 入队），调用方必须
        紧接着调用 _drop_buf() 丢弃引用，下次取到的是全新一块 ——
        这是"异步写盘"与"缓冲复用"能共存的前提。
        """
        b = getattr(self._tl, "buf", None)
        if b is None or len(b) < want:
            # 一次给足（按 1MB 对齐），避免下次又要扩容
            cap = max(want, BLOCK_SIZE)
            b = bytearray(cap)
            self._tl.buf = b
        return b

    def _drop_buf(self):
        """丢弃线程本地缓冲（所有权已转移给写线程，不再复用这一块）"""
        self._tl.buf = None

    # ---------- 异步写盘池（v1.8.0）----------
    # 为什么要把写盘搬出下载线程：
    #   实测 512MB 任务里"写入"累计 34.3s（线程时）/ 24 线程 ≈ 1.43s 墙钟，
    #   占端到端 2.7s 的一半 —— 下载线程花在 f.seek+f.write 上的时间，
    #   本该用来收下一个区块。
    # 做法：
    #   · 下载线程只把 (偏移, 缓冲) 放进队列，立刻回头继续拿下一个区块
    #   · N 个专职写线程消费队列，串行 seek+write 到同一个文件
    #       - 每个写线程独占一个句柄 → 文件指针私有，无需加锁
    #       - 缓冲所有权随入队转移：下载线程入队后立刻换一块新缓冲，
    #         绝不会"边写边改"（这是 v1.5.0 数据损坏教训的直接应用）
    # 实测（512MB）：24 线程 1.94s → 0.98s（**1.97×**）；4 个写线程最优。
    WRITER_THREADS = 4
    # 队列槽位（v1.9.0 调优）：实测 8/16/32/96 对吞吐影响在噪声范围内
    #（512MB：1.95/2.06/1.96/2.06s），但内存占用与槽位成正比
    #（每槽 1MB 数据 + 缓冲对象）。取 32 → 每任务写缓冲上限 ~32MB，
    # 兼顾"不阻塞下载线程"与"后台常驻内存可控"。
    WRITE_QUEUE_SIZE = 32

    def _ensure_writers(self):
        if self._wr_started:
            return
        self._wr_started = True
        for k in range(self.WRITER_THREADS):
            t = threading.Thread(target=self._writer_loop, daemon=True,
                                 name=f"xzd-w{k}-{self.tid}")
            self._writers.append(t)
            t.start()

    def _writer_loop(self):
        f = None
        try:
            f = open(self.data_path, "r+b", buffering=0)
            # 登记句柄：收尾时若写线程卡住，主线程能强制关闭它
            # （Windows 上被打开的文件无法 rename）
            self._register_fh(f)
            while True:
                item = self._wr_q.get()
                if item is None:
                    break
                try:
                    off, buf = item
                    f.seek(off)
                    f.write(buf)
                except Exception as e:
                    # 单块写盘失败不要直接判整个任务死刑：
                    # 先登记失败状态，让 _write_at 降级到同步路径再试一次；
                    # 同步路径仍失败时，上层 download 循环会把任务置 error。
                    self._fatal = f"写盘线程异常 {type(e).__name__}: {e}"
                    LOG.error("写盘线程异常，将触发同步降级: %s", e)
                finally:
                    self._wr_pending -= 1
                    self._wr_free.set()
        except Exception as e:  # noqa
            self._fatal = f"写盘线程异常 {type(e).__name__}: {e}"
        finally:
            try:
                if f is not None:
                    self._unregister_fh(f)
                    f.close()
            except Exception:
                LOG.debug('忽略异常：if f is not None: _unregister_fh(f) f.close()')

    def _drain_writers(self, timeout: float = 60.0):
        """等待所有已入队数据落盘，然后关闭写线程（暂停/取消/出错时调用）"""
        if not self._wr_started:
            return
        t0 = now()
        while self._wr_pending > 0 and now() - t0 < timeout:
            self._wr_free.wait(0.05)
            self._wr_free.clear()
        for _ in self._writers:
            try:
                self._wr_q.put_nowait(None)
            except Exception:
                LOG.debug('忽略异常：_wr_q.put_nowait(None)')
        for t in list(self._writers):
            try:
                t.join(timeout=5)
            except Exception:
                LOG.debug('忽略异常：t.join(timeout=5)')
        self._writers.clear()
        self._wr_started = False

    def _write_at(self, start: int, buf):
        """把数据写到文件的指定偏移（线程安全）。

        实现要点：
          · **入队给专职写线程**（异步，下载线程不等落盘）—— v1.8.0 主优化
          · 缓冲所有权随入队转移：调用方（_download_block）交出这份缓冲后
            必须立刻换新的，绝不能继续改它（v1.5.0 数据损坏的根本教训）
          · 队列满 → 阻塞等待（背压），避免内存无上限堆积
          · 同时保留"预分配必须先完成"的硬前提（v1.5.0 静默损坏根因）
          · 任何异常都降级为"同步独立句柄写"，宁可慢也不能错
        """
        # 数据正确性硬前提：预分配（扩容填零）必须已完成。
        self._ensure_prealloc_done()
        self._ensure_writers()
        while True:
            if self._stop and not self._stop_workers:
                break                      # 取消：直接走同步降级，保证不丢数据
            try:
                self._wr_q.put((start, buf), timeout=0.5)
                self._wr_pending += 1
                return
            except Exception:
                # 队列满：等写线程消化（背压）。若写线程已死则走降级
                if self._fatal:
                    break
                self._wr_free.wait(0.05)
                self._wr_free.clear()
        # ---- 降级：同步写（保证正确性优先）----
        tl = self._tl
        f = getattr(tl, "fh", None)
        try:
            if f is None or f.closed:
                f = open(self.data_path, "r+b", buffering=0)
                tl.fh = f
                self._register_fh(f)
            if _WRITE_AT(f, buf, start):
                return
            raise OSError("定位写失败")
        except Exception:
            try:
                if f is not None and not f.closed:
                    self._unregister_fh(f)
                    f.close()
            except Exception:
                LOG.debug('忽略异常：if f is not None and not f.closed: _unregister_fh(f) f.close()')
            tl.fh = None
            with open(self.data_path, "r+b", buffering=0) as f2:
                f2.seek(start)
                f2.write(buf)

    # ---------- 单流模式（不支持 Range / 未知大小）----------
    def _single_stream_loop(self):
        path = os.path.join(self.tmp_dir, "single.bin")
        ensure_dir(self.tmp_dir)
        self.single_mode = True
        self.resumable = False
        self.phase = "单线程下载"
        # v1.11.0：连续失败计数。
        # 旧逻辑：异常 → sleep 2s → 无条件重试，**没有次数上限**。
        # 后果：服务器持续 500 / 一直断连时，会永久每 2 秒重试一次，
        # 界面显示"下载中"却永远不动，用户只能手动取消。
        # 新逻辑：连续失败到上限就置为出错态，并给出可读原因。
        consec_fail = 0
        last_err = ""
        while not self._stop:
            if self.state == STATE_PAUSED:
                self._save_single_state()
                return
            if self.state == STATE_CANCELLED:
                return
            try:
                cur = os.path.getsize(path) if os.path.exists(path) else 0
                hdrs = {}
                if cur > 0 and self.range_ok:
                    hdrs["Range"] = f"bytes={cur}-"
                with self.http.open(self.url, extra=hdrs) as r:
                    if getattr(r, "status", 200) == 200 and cur > 0:
                        cur = 0
                        open(path, "wb").close()
                    with open(path, "ab", buffering=0) as f:
                        while True:
                            if self._stop:
                                return
                            if self.state != STATE_DOWNLOADING:
                                self._save_single_state()
                                return
                            d = r.read(READ_SIZE)
                            if not d:
                                break
                            self.limiter.consume(len(d))
                            f.write(d)
                            cur += len(d)
                            self.done_bytes = cur
                            if self.size:
                                self.progress = min(1.0, cur / float(self.size))
                            self._tick_speed()
                            self._refresh_eta()
                            self.mgr.notify()
                            consec_fail = 0        # 有数据落地 → 重置失败计数
                if self.size and os.path.getsize(path) >= self.size:
                    break
                if not self.size:
                    break
                time.sleep(1.0)
            except Exception as e:  # noqa
                consec_fail += 1
                last_err = f"{type(e).__name__}: {e}"
                LOG.once("single_fail_%d" % min(consec_fail, 3),
                         "单流下载失败第 %d 次 tid=%s：%s", consec_fail, self.tid, last_err)
                if self.state == STATE_ERROR:
                    return
                if "HTTP 4" in str(e) or "HTTP 5" in str(e):
                    self.state = STATE_ERROR
                    self.err = f"{e}。可右键任务 → 用 aria2c 极速重试"
                    self.phase = "出错（可用 aria2c 重试）"
                    return
                if consec_fail >= SINGLE_MAX_CONSEC_FAIL:
                    self.state = STATE_ERROR
                    self.err = (f"连续 {consec_fail} 次连接失败，已停止：{last_err}。"
                                f"可右键任务 → 用 aria2c 极速重试")
                    LOG.once_exc("single_give_up",
                                 "单流下载放弃 tid=%s 连续失败 %d 次", self.tid, consec_fail,
                                 level=40)
                    return
                # 指数退避：2s → 4s → 8s …（上限 20s），别死磕同一个坏连接
                time.sleep(min(2.0 * consec_fail, 20.0))
        if os.path.exists(path):
            self._finish_single(path)

    def _save_single_state(self):
        try:
            p = os.path.join(self.tmp_dir, "single.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "filename": self.filename,
                           "size": self.size, "referer": self.http.referer}, f)
        except Exception:
            LOG.debug('忽略异常：p = os.path.join(tmp_dir, "single.json")')

    def _finish_single(self, path: str):
        final = unique_path(self.final_path())
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.move(path, final)
        self.created_final = final
        self.filename = os.path.basename(final)
        try:
            self.size = os.path.getsize(final)
            self.done_bytes = self.size
        except OSError:
            LOG.debug('忽略异常：size = os.path.getsize(final)')
        self.progress = 1.0
        self.state = STATE_DONE
        self.phase = "已完成"
        self._cleanup_tmp()

    # ---------- 收尾 ----------
    def _run_hls_fallback(self):
        """探测发现其实是 HLS → 转交 HLS 引擎"""
        self.kind = "hls"
        hls = HLSDownload(self.mgr, self.url, self.out_dir, self.filename,
                          tid=self.tid, conns=self.requested_conns,
                          referer=self.http.referer,
                          speed_limit=self.speed_limit, kind="hls",
                          added_at=self.added_at)
        hls._st = self._st if hasattr(self, "_st") else None
        hls.run_once(self)

    def _finalize(self):
        # 收尾前必须：
        #   ① 汇合异步写线程（最后一块必须真落盘，不能还躺在队列里）
        #   ② 汇合后台预分配
        #   ③ 关闭所有缓存句柄
        # 否则 Windows 上 os.replace 会因"文件被占用"失败，或者文件内容残缺
        self._drain_writers(timeout=300.0)
        self._await_prealloc()
        self._close_tl_handles()
        final = unique_path(self.final_path())
        os.makedirs(os.path.dirname(final), exist_ok=True)
        last = None
        for wait in (0.0, 0.3, 0.8, 1.5, 3.0):
            if wait:
                time.sleep(wait)
                self._close_tl_handles()
            try:
                os.replace(self.data_path, final)
                last = None
                break
            except PermissionError as e:
                last = e
            except OSError as e:
                # 跨盘移动等场景 os.replace 不支持 → 退化为复制+删除
                if getattr(e, "winerror", None) == 17 or e.errno == 18:
                    shutil.copyfile(self.data_path, final)
                    try:
                        os.remove(self.data_path)
                    except OSError:
                        LOG.debug('忽略异常：os.remove(data_path)')
                    last = None
                    break
                raise
        if last is not None:
            raise last
        self.created_final = final
        self.filename = os.path.basename(final)
        self.progress = 1.0
        self.state = STATE_DONE
        self.phase = "已完成"
        if self.size:
            self.done_bytes = self.size
        self._cleanup_tmp()

    def _start_prealloc(self, size: int):
        """后台异步把数据文件扩到完整大小（不阻塞下载启动）。

        ⚠ 重要约束（v1.5.0 踩坑记录）
        `truncate` 扩大文件的区域在 NTFS 上会被**填零**。若它与数据写入
        并发，会把已写入的数据清零 —— 实测表现为"每块前 4KB 正确、其余
        全为 0/旧数据"的静默损坏，且每次运行 MD5 都不同。
        （前 4KB 正确是因为后台线程启动+打开文件有少许延迟，此时已写入
         的那点数据侥幸没被覆盖。）

        因此：**必须保证预分配在任何写盘之前完成**。
        做法：启动时就同步建好文件（open+truncate 对空文件是秒级操作，
        仅在大文件时略慢），并把完成状态记录在事件里，写线程可等待。
        """
        if size <= 0:
            return
        # 已有预分配在跑 → 等它结束（幂等）
        th = getattr(self, "_prealloc_thr", None)
        if th is not None and th.is_alive():
            return
        st = getattr(self, "_prealloc_st", None)
        if st is not None and st.done:
            return

        class _St:
            __slots__ = ("_lock", "done", "evt")

            def __init__(self):
                self._lock = threading.Lock()
                self.done = False
                self.evt = threading.Event()

        st = _St()
        self._prealloc_st = st

        def _do():
            try:
                # 用"seek 到末尾 + 写 1 字节"代替 ftruncate 做扩容：
                #   · NTFS 上 ftruncate 会真实分配并置零整个区域，
                #     实测 128MB=0.56s / 512MB=2.84s / 1GB=6.26s
                #   · seek+write 走**稀疏文件**语义，只建元数据，
                #     实测同样大小仅需 0.01s（快 200~700 倍）
                #   · 两者得到的文件大小完全一致，后续随机写不受影响
                if not os.path.exists(self.data_path):
                    with open(self.data_path, "wb"):
                        pass
                fd = os.open(self.data_path,
                             os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o666)
                try:
                    cur = os.fstat(fd).st_size
                    if cur < size:
                        os.lseek(fd, size - 1, os.SEEK_SET)
                        os.write(fd, b"\0")
                finally:
                    os.close(fd)
            except OSError:
                LOG.debug('忽略异常：if not os.path.exists(data_path): with open(data_path, "wb"...')
            with st._lock:
                st.done = True
            st.evt.set()

        t = threading.Thread(target=_do, daemon=True, name=f"prealloc-{self.tid}")
        self._prealloc_thr = t
        t.start()
        # 关键：不阻塞返回，但**写线程在首次落盘前会等待此事件**
        # （见 _write_at 里的 _ensure_prealloc_done）

    def _ensure_prealloc_done(self):
        """确保预分配已完成 —— 任何写盘之前必须调用。

        这是数据正确性的硬前提：truncate 会把新增区域填零，
        若与写入并发就会静默清掉已写数据。
        """
        st = getattr(self, "_prealloc_st", None)
        if st is None:
            return
        if not st.done:
            st.evt.wait(timeout=120.0)

    def _await_prealloc(self, timeout: float = 90.0):
        """等预分配完成。收尾/暂停前必须调用：
        Windows 上文件被打开时无法 os.replace/rename。"""
        th = getattr(self, "_prealloc_thr", None)
        if th is not None and th.is_alive():
            th.join(timeout=timeout)

    def _cleanup_tmp(self):
        if self.mgr.cfg.get("delete_tmp_after_done", True):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# HLS (m3u8) 下载
# --------------------------------------------------------------------------

class HLSDownload(DownloadTask):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.kind = "hls"
        self.segments: list[str] = []
        self.key_uri = ""
        self.key_iv_hex: str | None = None
        self.init_uri = ""
        self.is_live = True
        self.duration = 0.0
        self._seq = 0
        self.total_segments = 0
        self.done_segments = 0
        self._seg_lock = threading.Lock()
        self._t_start = now()

    # ---- 路径 ----
    def _seg_file(self, i: int) -> str:
        return os.path.join(self.tmp_dir, f"seg_{i:05d}.ts")

    # ---- 解析 ----
    def _parse_playlist(self, url: str, depth: int = 0) -> None:
        text, _, _ = self.http.get_text(url)
        if "#EXT-X-STREAM-INF" in text and depth < 4:
            lines = [l.strip() for l in text.splitlines()]
            best = None
            for j, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"BANDWIDTH=(\d+)", ln)
                    bw = int(m.group(1)) if m else 0
                    for k in range(j + 1, len(lines)):
                        if lines[k] and not lines[k].startswith("#"):
                            if best is None or bw > best[0]:
                                best = (bw, urllib.parse.urljoin(url, lines[k]))
                            break
            if best:
                self.phase = "选择最高清晰度"
                self.mgr.notify()
                self._parse_playlist(best[1], depth + 1)
                return
        base = url
        self.segments = []
        self.key_uri = ""
        self.key_iv_hex = None
        self.init_uri = ""
        self.duration = 0.0
        seq = 0
        method = ""
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    seq = int(ln.split(":", 1)[1])
                except Exception:
                    seq = 0
            elif ln.startswith("#EXT-X-MAP"):
                m = re.search(r'URI="([^"]+)"', ln)
                if m:
                    self.init_uri = urllib.parse.urljoin(base, m.group(1))
            elif ln.startswith("#EXT-X-KEY"):
                m = re.search(r"METHOD=([^,]+)", ln)
                method = (m.group(1) if m else "").strip().upper()
                if method and method != "NONE":
                    mu = re.search(r'URI="([^"]+)"', ln)
                    self.key_uri = urllib.parse.urljoin(base, mu.group(1)) if mu else ""
                    miv = re.search(r"IV=0x([0-9A-Fa-f]+)", ln)
                    self.key_iv_hex = miv.group(1) if miv else None
                else:
                    self.key_uri = ""
            elif ln.startswith("#EXTINF"):
                try:
                    self.duration += float(ln.split(":", 1)[1].split(",")[0])
                except Exception:
                    LOG.debug('忽略异常：duration += float(ln.split(":", 1)[1].split(",")[0])')
            elif ln.startswith("#"):
                continue
            else:
                self.segments.append(urllib.parse.urljoin(base, ln))
        self._seq = seq
        self.is_live = "#EXT-X-ENDLIST" not in text
        if method and method not in ("NONE", "AES-128"):
            raise RuntimeError(f"暂不支持的加密方式 {method}（支持 AES-128）")

    # ---- AES ----
    def _load_key(self) -> bytes:
        if not self.key_uri:
            return b""
        key = self.http.get_bytes(self.key_uri)
        if len(key) not in (16, 24, 32):
            # 极少数站点会把密钥用 base64 文本返回
            try:
                decoded = base64.b64decode(key.strip(), validate=True)
                if len(decoded) in (16, 24, 32):
                    return decoded
            except Exception:
                LOG.debug('忽略异常：decoded = base64.b64decode(key.strip(), validate=True)')
        return key

    def _seg_iv(self, idx: int) -> bytes:
        if self.key_iv_hex:
            return bytes.fromhex(self.key_iv_hex.rjust(32, "0"))
        return (self._seq + idx).to_bytes(16, "big")

    @staticmethod
    def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            out = dec.update(data) + dec.finalize()
        except Exception:
            out = pure_aes_cbc_decrypt(data, key, iv)
            return out
        # HLS AES-128 加密分片**不做** PKCS7 padding（TS 分片长度已保证为
        # 16 字节整数倍）。这里一旦剥离 padding，当真实视频数据末尾恰好
        # 形成合法 padding 模式时就会静默丢失数据，导致花屏或音画不同步。
        # 因此直接返回原始解密数据。
        return out

    # ---- 会话 ----
    def _session(self):
        self.phase = "解析播放列表"
        self.mgr.notify()
        self._parse_playlist(self.url)
        ensure_dir(self.tmp_dir)
        total = len(self.segments)
        if total == 0:
            raise RuntimeError("播放列表中没有找到分片")

        # 初始化段（fmp4）
        if self.init_uri:
            ip = os.path.join(self.tmp_dir, "init.bin")
            if not (os.path.exists(ip) and os.path.getsize(ip) > 0):
                with self.http.open(self.init_uri) as r, open(ip, "wb") as f:
                    while True:
                        d = r.read(READ_SIZE)
                        if not d:
                            break
                        self.limiter.consume(len(d))
                        f.write(d)

        key = self._load_key() if self.key_uri else b""
        self.total_segments = total
        done_set = set()
        for i in range(total):
            p = self._seg_file(i)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                done_set.add(i)
        self.done_segments = len(done_set)
        self.done_bytes = sum(os.path.getsize(self._seg_file(i)) for i in done_set)
        self.progress = self.done_segments / float(total)
        self._t_start = now()
        self.phase = f"分片 {self.done_segments}/{total}"
        self.mgr.notify()
        if self.done_segments >= total:
            self._merge()
            return

        todo = [i for i in range(total) if i not in done_set]
        workers = max(1, min(16, self.requested_conns))
        fatal = {"err": ""}
        stop = threading.Event()

        def fetch(i: int):
            path = self._seg_file(i)
            conn_key = None
            conn = None
            for attempt in range(MAX_RETRY):
                if stop.is_set() or self._stop:
                    return
                if self.state in (STATE_PAUSED, STATE_CANCELLED):
                    return
                try:
                    # ---- v2.3.2：HLS 分片走主机级连接池 ----
                    # 同一 m3u8 的成百上千个分片通常落在同一主机，
                    # 按主机复用长连接可避免每个分片都重新 TCP/TLS 握手。
                    # 连接池失败（代理/异常 URL/两次建连失败）则降级到 urllib。
                    resp, conn, conn_key = self.http.conn_get_host(self.segments[i])
                    if resp is not None:
                        try:
                            status = getattr(resp, "status", 200)
                            if status not in (200, 206):
                                raise IOError(f"HTTP {status}")
                            buf = bytearray()
                            while True:
                                if stop.is_set() or self._stop:
                                    return
                                if self.state == STATE_PAUSED:
                                    return
                                d = resp.read(READ_SIZE)
                                if not d:
                                    break
                                self.limiter.consume(len(d))
                                buf.extend(d)
                        finally:
                            keep = conn is not None and self.http.is_keepalive(resp)
                            self.http.conn_release(conn_key, conn, keep)
                    else:
                        with self.http.open(self.segments[i]) as r:
                            status = getattr(r, "status", 200)
                            if status not in (200, 206):
                                raise IOError(f"HTTP {status}")
                            buf = bytearray()
                            while True:
                                if stop.is_set() or self._stop:
                                    return
                                if self.state == STATE_PAUSED:
                                    return
                                d = r.read(READ_SIZE)
                                if not d:
                                    break
                                self.limiter.consume(len(d))
                                buf.extend(d)
                    if key:
                        buf = bytearray(self._decrypt(bytes(buf), key, self._seg_iv(i)))
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(buf)
                    if stop.is_set():
                        return
                    os.replace(tmp, path)
                    with self._seg_lock:
                        self.done_segments += 1
                        self.done_bytes += len(buf)
                        self.progress = min(1.0, self.done_segments / float(total))
                        self.phase = f"分片 {self.done_segments}/{total}"
                    return
                except Exception as e:  # noqa
                    # 出错路径：若连接已建立则关闭（状态不可信），下次重试新建连接
                    if conn is not None:
                        try:
                            self.http.conn_release(conn_key, conn, False)
                        except Exception:
                            pass
                        conn = None
                    if attempt >= MAX_RETRY - 1:
                        with self._seg_lock:
                            fatal["err"] = f"分片 {i} 下载失败：{e}"
                        return
                    time.sleep(min(1.5 * (attempt + 1), 8))

        # 延迟导入 ThreadPoolExecutor：concurrent.futures 整包约 50ms 导入开销，
        # 而只有 HLS 任务才用得到。放进函数内 → GUI 冷启动省 50ms（v1.6.0）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch, i): i for i in todo}
            while futs:
                done = [f for f in futs if f.done()]
                for f in done:
                    futs.pop(f, None)
                self._tick_speed()
                self._refresh_eta()
                self.mgr.notify()
                if self.state == STATE_PAUSED:
                    stop.set()
                    for f in list(futs):
                        f.cancel()
                    futs.clear()
                    self._save_hls_state()
                    return
                if self.state == STATE_CANCELLED:
                    stop.set()
                    for f in list(futs):
                        f.cancel()
                    futs.clear()
                    return
                if not done:
                    time.sleep(0.15)
        if fatal["err"] and self.done_segments < total:
            raise RuntimeError(fatal["err"])
        self._merge()

    def _extra_eta(self):
        if self.done_segments > 0 and self.total_segments:
            elapsed = now() - self._t_start
            per = elapsed / self.done_segments
            return per * (self.total_segments - self.done_segments)
        return None

    def _save_hls_state(self):
        try:
            p = os.path.join(self.tmp_dir, "hls.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "filename": self.filename,
                           "referer": self.http.referer}, f)
        except Exception:
            LOG.debug('忽略异常：p = os.path.join(tmp_dir, "hls.json")')

    def _merge(self):
        ext = ".mp4" if self.init_uri else ".ts"
        if not self.filename.lower().endswith((".mp4", ".ts", ".mkv", ".flv")):
            self.filename = os.path.splitext(self.filename)[0] + ext
        final = unique_path(self.final_path())
        tmp_final = final + ".xzd-part"
        written = 0
        os.makedirs(os.path.dirname(final), exist_ok=True)
        with open(tmp_final, "wb") as out:
            ip = os.path.join(self.tmp_dir, "init.bin")
            if os.path.exists(ip):
                with open(ip, "rb") as f:
                    d = f.read()
                    out.write(d)
                    written += len(d)
            for i in range(len(self.segments)):
                p = self._seg_file(i)
                with open(p, "rb") as f:
                    while True:
                        d = f.read(FILE_READ_SIZE)
                        if not d:
                            break
                        out.write(d)
                        written += len(d)
        os.replace(tmp_final, final)
        self.created_final = final
        self.filename = os.path.basename(final)
        self.size = written
        self.done_bytes = written
        self.progress = 1.0
        self.state = STATE_DONE
        note = "（直播流：仅保存了当前可见分片）" if self.is_live else ""
        self.phase = f"已完成{note}"
        if self.mgr.cfg.get("delete_tmp_after_done", True):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # FFmpeg 无损转封装 ts → mp4
        self._remux_to_mp4(final)

    def _remux_to_mp4(self, src: str):
        """ts 无损转封装为 mp4。需要 third_party/ffmpeg.exe。"""
        ffmpeg = os.path.join(app_root(), "third_party", "ffmpeg.exe")
        if not os.path.exists(ffmpeg):
            return
        if not src.lower().endswith(".ts"):
            return
        dst = os.path.splitext(src)[0] + ".mp4"
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", src, "-c", "copy", "-bsf:a", "aac_adtstoasc", dst],
                capture_output=True, timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and os.path.exists(dst):
                os.remove(src)
                self.created_final = dst
                self.filename = os.path.basename(dst)
                self.size = os.path.getsize(dst)
                self.done_bytes = self.size
        except Exception:
            pass

    # ---- 供 FileDownload 内部转交使用（直接跑一轮会话）----
    def run_once(self, host: DownloadTask):
        """在宿主任的线程里完成一次 HLS 下载，并把结果回写到宿主任"""
        self._stop = host._stop
        try:
            self._session()
        except Exception as e:  # noqa
            self.state = STATE_ERROR
            self.err = f"{type(e).__name__}: {e}。可右键任务 → 用 aria2c 极速重试"
            self.phase = "出错（可用 aria2c 重试）"
        host.size = self.size
        host.done_bytes = self.done_bytes
        host.progress = self.progress
        host.speed = self.speed
        host.phase = self.phase
        host.state = self.state
        host.err = self.err
        host.created_final = self.created_final
        host.filename = self.filename


# --------------------------------------------------------------------------
# 纯 Python AES-CBC（无 cryptography 时的兜底，已通过 FIPS-197 向量验证）
# --------------------------------------------------------------------------

_FWD = None
_INV = None
_RCON = None


def _aes_init():
    global _FWD, _INV, _RCON
    if _FWD is not None:
        return
    p = q = 1
    fwd = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if (p & 0x80) else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        fwd[p] = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^
                  ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4)) ^ 0x63) & 0xFF
        if p == 1:
            break
    fwd[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(fwd):
        inv[v] = i
    rcon = [0] * 11
    rc = 1
    for i in range(1, 11):
        rcon[i] = rc
        rc = ((rc << 1) ^ (0x1B if (rc & 0x80) else 0)) & 0xFF
    _FWD, _INV, _RCON = fwd, inv, rcon


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes_expand(key: bytes):
    _aes_init()
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_FWD[b] for b in t]
            t[0] ^= _RCON[i // nk]
        elif nk > 6 and i % nk == 4:
            t = [_FWD[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w, nr


def _ark(s, w, rnd):
    for c in range(4):
        word = w[rnd * 4 + c]
        for r in range(4):
            s[r + 4 * c] ^= word[r]
    return s


def _inv_shift_rows(s):
    for r in range(1, 4):
        row = [s[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            s[r + 4 * c] = row[c]
    return s


def _inv_mix_columns(s):
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c], s[4 * c + 1], s[4 * c + 2], s[4 * c + 3]
        s[4 * c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        s[4 * c + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        s[4 * c + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        s[4 * c + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return s


def _aes_decrypt_block(block, w, nr):
    _aes_init()
    s = list(block)
    _ark(s, w, nr)
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(s)
        s = [_INV[b] for b in s]
        _ark(s, w, rnd)
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    s = [_INV[b] for b in s]
    _ark(s, w, 0)
    return bytes(s)


def pure_aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        data = data[: len(data) - (len(data) % 16)]
    if not data:
        return b""
    w, nr = _aes_expand(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _aes_decrypt_block(blk, w, nr)
        out.extend(bytes(dec[i] ^ prev[i] for i in range(16)))
        prev = blk
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return bytes(out)


# --------------------------------------------------------------------------
# 管理器
# --------------------------------------------------------------------------

DEFAULT_CFG = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "connections": 24,
    "speed_limit": 0,            # bytes/s，0 不限
    "max_parallel_tasks": 3,
    "delete_tmp_after_done": True,
    "proxy": "",
    "insecure": False,
    "user_agent": DEFAULT_UA,
    "port": 8763,
    "auto_start_tasks": True,
}


class Manager:
    def __init__(self, data_dir: str | None = None, on_notify=None,
                 on_task_done=None):
        self.data_dir = data_dir or data_root()
        ensure_dir(self.data_dir)
        self.cfg = dict(DEFAULT_CFG)
        self._load_cfg()
        self.on_notify = on_notify
        self.on_task_done = on_task_done
        self.tasks: dict[str, DownloadTask] = {}
        self.order: list[str] = []
        self.sniff_items: list[dict] = []
        self._lock = threading.RLock()
        self._cv = threading.Condition()
        self._slot_holders: set[str] = set()

    # ---- 配置 ----
    def cfg_path(self) -> str:
        return os.path.join(self.data_dir, "config.json")

    def jobs_path(self) -> str:
        return os.path.join(self.data_dir, "jobs.json")

    def _load_cfg(self):
        # v1.11.0：损坏的配置不能只是"静默回退默认值" —— 用户会觉得设置
        # 莫名其妙自己变了。这里把坏文件改名备份，留下可诊断线索。
        try:
            with open(self.cfg_path(), "r", encoding="utf-8") as f:
                self.cfg.update(json.load(f) or {})
        except FileNotFoundError:
            pass                          # 首次运行，正常
        except Exception:
            LOG.once_exc("cfg_corrupt", "配置文件损坏，已改用默认值（坏文件已备份）")
            try:
                bad = self.cfg_path() + ".bad"
                os.replace(self.cfg_path(), bad)
            except Exception:
                # 静默是有意为之：备份失败不影响"已回退默认值"这个结果，
                # 上面的 once_exc 已经把问题记进日志了
                pass
        ensure_dir(self.cfg.get("download_dir") or DEFAULT_CFG["download_dir"])

    def save_cfg(self):
        try:
            with open(self.cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            # 配置（连接数/保存目录/限速）存不下：下次启动会"退回默认值"
            LOG.once_exc("save_cfg", "配置保存失败（下次启动将回退默认值）")

    def set_cfg(self, **kw):
        self.cfg.update(kw)
        self.save_cfg()
        self.notify()

    def notify(self):
        if self.on_notify:
            try:
                self.on_notify()
            except Exception:
                LOG.debug('忽略异常：on_notify()')

    # ---- 并发槽位 ----
    def acquire_slot(self, task: DownloadTask) -> bool:
        with self._cv:
            while True:
                if task._stop or task.state == STATE_CANCELLED:
                    return False
                if task.tid in self._slot_holders:
                    return True
                if len(self._slot_holders) < max(1, int(self.cfg.get("max_parallel_tasks", 3))):
                    self._slot_holders.add(task.tid)
                    return True
                if task.state == STATE_PAUSED:
                    return False
                self._cv.wait(timeout=0.5)

    def release_slot(self, task: DownloadTask):
        with self._cv:
            self._slot_holders.discard(task.tid)
            self._cv.notify_all()

    def on_task_end(self, task: DownloadTask):
        self.release_slot(task)
        self.save_jobs()
        self.notify()

    # ---- 任务 ----
    def add_task(self, url: str, *, out_dir: str | None = None, filename: str = "",
                 conns: int | None = None, referer: str = "", cookie: str = "",
                 headers: dict | None = None, kind: str = "auto",
                 speed_limit: int | None = None, group: str = "",
                 start: bool = True, extra: dict | None = None) -> DownloadTask:
        conns = int(conns if conns is not None else self.cfg.get("connections", 16))
        speed_limit = int(speed_limit if speed_limit is not None else self.cfg.get("speed_limit", 0))
        out_dir = os.path.abspath(out_dir or self.cfg.get("download_dir"))
        k = kind if kind in ("file", "hls") else kind_of_url(url)
        headers = dict(headers or {})
        if "User-Agent" not in headers and self.cfg.get("user_agent"):
            headers["User-Agent"] = self.cfg["user_agent"]

        cls = HLSDownload if k == "hls" else FileDownload
        # 同名任务去重（同一 URL 未完成时直接复用）
        with self._lock:
            for tid in self.order:
                t = self.tasks.get(tid)
                if t and t.url == url and t.state not in (STATE_DONE, STATE_CANCELLED, STATE_ERROR):
                    return t
        task = cls(self, url, out_dir, filename, conns=conns,
                   referer=referer, cookie=cookie, headers=headers,
                   speed_limit=speed_limit, kind=k, group=group,
                   insecure=bool(self.cfg.get("insecure")),
                   proxy=self.cfg.get("proxy", ""))
        task.extra = extra or {}
        with self._lock:
            self.tasks[task.tid] = task
            self.order.insert(0, task.tid)
        task.state = STATE_WAIT
        t = threading.Thread(target=task.run, daemon=True, name=f"xzd-{task.tid}")
        task._run_thread = t
        t.start()
        if not start:
            task.pause()
        self.save_jobs()
        self.notify()
        return task

    def get(self, tid: str) -> DownloadTask | None:
        return self.tasks.get(tid)

    def add_connections(self, task: DownloadTask, k: int = 8) -> bool:
        if not isinstance(task, FileDownload) or task.state != STATE_DOWNLOADING:
            return False
        task.requested_conns = min(32, task.requested_conns + int(k))
        task.target = min(32, task.target + int(k))
        task.phase = f"动态加速 → {task.target} 线程"
        self.notify()
        return True

    def remove_task(self, tid: str, delete_files: bool = False):
        with self._lock:
            t = self.tasks.pop(tid, None)
            if tid in self.order:
                self.order.remove(tid)
        if t and delete_files:
            shutil.rmtree(t.tmp_dir, ignore_errors=True)
        self.release_slot(t) if t else None
        self.save_jobs()
        self.notify()

    def pause_all(self):
        for t in list(self.tasks.values()):
            t.pause()

    def resume_all(self):
        for t in list(self.tasks.values()):
            if t.state in (STATE_PAUSED, STATE_ERROR):
                t.start()

    def snapshots(self) -> list[dict]:
        with self._lock:
            snaps = [self.tasks[tid].snapshot() for tid in self.order if tid in self.tasks]
        self._reap_dead_tasks(snaps)
        return snaps

    # 僵尸判定：连续多少次巡检发现"线程已死但状态仍是活动态"才动手
    # （取 3 是为了容忍"线程刚提交完最后一笔状态、尚未退出"的极短窗口）
    ZOMBIE_STRIKES = 3

    def _reap_dead_tasks(self, snaps: list[dict]):
        """清理"僵尸任务"（v1.11.0）：线程已死但状态还停在下载中/等待中。

        为什么需要：即便有了全局异常钩子，仍可能出现线程被外部因素终结
        （例如依赖库里的 C 层异常、进程被调试器打断等）。此时任务会**永远**
        停在"下载中"，界面显示有速度有进度却再也不动 —— 用户只能重启软件。
        这里利用界面本来就每 600ms 调用一次的 snapshots() 做顺带巡检，
        零额外线程、零额外定时器。

        判定条件（三条同时满足才动手，避免误杀）：
          1. 状态属于活动态（下载中 / 等待中）
          2. `_run_thread` 存在且已 not alive()
          3. 连续 ZOMBIE_STRIKES 次巡检都如此
        """
        try:
            for i, s in enumerate(snaps):
                tid = s.get("tid")
                t = self.tasks.get(tid)
                if t is None:
                    continue
                if s.get("state") not in (STATE_DOWNLOADING, STATE_WAIT):
                    t._zombie_hits = 0
                    continue
                th = getattr(t, "_run_thread", None)
                if th is None:
                    continue
                try:
                    alive = th.is_alive()
                except Exception:
                    alive = True
                if alive:
                    t._zombie_hits = 0
                    continue
                hits = getattr(t, "_zombie_hits", 0) + 1
                t._zombie_hits = hits
                if hits < self.ZOMBIE_STRIKES:
                    continue
                # 动手：置为出错态，让"继续"按钮可用（可续传），而非死在那里
                t._zombie_hits = 0
                if t.state in (STATE_DONE, STATE_CANCELLED, STATE_ERROR):
                    continue
                t.state = STATE_ERROR
                t.err = "任务线程意外终止（已自动停止，可点『继续』续传）"
                t.phase = "意外终止"
                # 注意：这里是**正常代码路径**发现的僵尸（不在 except 块内），
                # 用 once 而非 once_exc —— 后者依赖 traceback.format_exc()，
                # 在无异常上下文中只会写出 "NoneType: None" 这种无用内容。
                LOG.once("zombie", "检测到僵尸任务并已修正 tid=%s（线程已死但状态仍为活动态）",
                         tid, level=40)
                snaps[i] = t.snapshot()
        except Exception:
            LOG.once_exc("reap", "僵尸任务巡检本身出错（不影响下载）")

    def total_speed(self) -> float:
        return sum(t.speed for t in self.tasks.values())

    # ---- 持久化 ----
    def save_jobs(self):
        # save_jobs 在多线程热路径被频繁调用（下载进度更新等），
        # 必须串行化，避免并发写同一个 .tmp 文件导致 PermissionError
        # 或写出损坏的 jobs.json（用户日志里已复现）。
        with self._lock:
            try:
                data = []
                for tid in self.order:
                    t = self.tasks.get(tid)
                    if not t:
                        continue
                    data.append({
                        "tid": t.tid, "url": t.url, "out_dir": t.out_dir,
                        "filename": t.filename, "kind": t.kind,
                        "conns": t.requested_conns, "state": t.state,
                        "referer": t.http.referer, "group": t.group,
                        "added_at": t.added_at, "speed_limit": t.speed_limit,
                        "size": t.size,
                    })
                tmp = self.jobs_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.jobs_path())
            except Exception:
                # 任务列表存不下 → 重启后任务"凭空消失"，用户会当成丢数据
                LOG.once_exc("save_jobs", "任务列表保存失败（重启后可能丢失）")

    def load_jobs(self, auto_start: bool = False) -> int:
        """恢复上次未完成的任务；返回恢复条数"""
        try:
            with open(self.jobs_path(), "r", encoding="utf-8") as f:
                data = json.load(f) or []
        except FileNotFoundError:
            return 0                      # 首次运行
        except Exception:
            # v1.11.0：损坏时备份坏文件 + 留日志。
            # 否则用户会看到"任务列表全没了"却完全不知道原因。
            LOG.once_exc("jobs_corrupt", "任务列表文件损坏，无法恢复（坏文件已备份）")
            try:
                os.replace(self.jobs_path(), self.jobs_path() + ".bad")
            except Exception:
                # 静默是有意为之：备份失败不影响"返回 0"的结果，
                # 上面的 once_exc 已经记了日志
                pass
            return 0
        n = 0
        for it in data:
            try:
                if it.get("state") in (STATE_DONE, STATE_CANCELLED):
                    continue
                task = self.add_task(
                    it["url"], out_dir=it.get("out_dir"), filename=it.get("filename", ""),
                    conns=it.get("conns"), referer=it.get("referer", ""),
                    kind=it.get("kind", "auto"), speed_limit=it.get("speed_limit"),
                    start=False)
                n += 1
            except Exception:
                continue
        return n

    # ---- 嗅探面板 ----
    def add_sniff_items(self, page_url: str, items: list[dict]) -> int:
        added = 0
        with self._lock:
            existing = {x["url"] for x in self.sniff_items}
            for it in items or []:
                u = (it.get("url") or "").strip()
                if not u or u in existing:
                    continue
                low = u.lower().split("?")[0]
                t = it.get("type") or ("hls" if low.endswith(STREAM_EXTS) else "media")
                row = {
                    "url": u, "type": t,
                    "name": it.get("filename") or filename_from_url(u),
                    "size": it.get("size"), "page": page_url,
                    "referer": it.get("referer") or page_url,
                    "cookie": it.get("cookie") or "",
                    "ts": now(),
                }
                self.sniff_items.insert(0, row)
                existing.add(u)
                added += 1
            if len(self.sniff_items) > 500:
                self.sniff_items = self.sniff_items[:500]
        if added:
            self.notify()
        return added

    def clear_sniff(self):
        with self._lock:
            self.sniff_items = []
        self.notify()


# --------------------------------------------------------------------------
# 网页媒体嗅探
# --------------------------------------------------------------------------

_MEDIA_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\]+?\.(?:m3u8|mp4|webm|mkv|flv|mov|ts|mpd|mp3|m4a|aac|flac|wav|ogg)(?:\?[^\s"'<>\\]*)?""",
    re.I)
_TAG_SRC_RE = re.compile(
    r"""<(?:video|audio|source|embed|iframe)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_TAG_ALT_RE = re.compile(
    r"""<(?:video|audio|source|embed|object)\b[^>]*?\b(?:data|file|href)\s*=\s*["']([^"']+)["']""",
    re.I)
_JSON_PLAY_RE = re.compile(
    r'"(?:url|src|file|playUrl|videoUrl|video_url|mediaUrl|sourceUrl|hlsUrl|m3u8)"\s*:\s*"([^"]+)"',
    re.I)
_QUOTED_STREAM = re.compile(r'''["']([^"']+\.(?:m3u8|mpd)(?:\?[^"']*)?)["']''', re.I)


def scan_webpage(url: str, http: HttpHelper | None = None) -> dict:
    http = http or HttpHelper()
    # 如果 URL 本身就是媒体/流直链，直接返回，不再抓 HTML
    low = url.lower().split("?")[0]
    if low.endswith(STREAM_EXTS) or low.endswith(MEDIA_EXTS):
        t = "hls" if low.endswith(STREAM_EXTS) else "media"
        return {"items": [{"url": url, "type": t,
                           "filename": filename_from_url(url), "source": "直链"}],
                "title": "", "page": url}
    html, _, _ = http.get_text(url, limit=8 * 1024 * 1024)
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    found: dict[str, dict] = {}

    def push(u: str, src: str):
        if not u:
            return
        u = u.strip().replace("&amp;", "&").replace("\\/", "/")
        if u.startswith("//"):
            u = urllib.parse.urlsplit(url).scheme + ":" + u
        u = urllib.parse.urljoin(url, u)
        if not u.lower().startswith(("http://", "https://")):
            return
        low = u.lower().split("?")[0]
        if low.endswith(STREAM_EXTS):
            t = "hls"
        elif low.endswith(MEDIA_EXTS):
            t = "media"
        else:
            return
        if u not in found:
            found[u] = {"url": u, "type": t,
                        "filename": filename_from_url(u), "source": src}

    for m2 in _MEDIA_URL_RE.finditer(html):
        push(m2.group(0), "页面文本")
    for m2 in _TAG_SRC_RE.finditer(html):
        push(m2.group(1), "媒体标签")
    for m2 in _TAG_ALT_RE.finditer(html):
        push(m2.group(1), "媒体标签")
    for m2 in _JSON_PLAY_RE.finditer(html):
        push(m2.group(1), "播放器配置")
    for m2 in _QUOTED_STREAM.finditer(html):
        push(m2.group(1), "播放器配置")

    items = sorted(found.values(), key=lambda x: (x["type"] != "hls", x["filename"]))
    # 音视频配对：B站 DASH 格式 视频ID_u1-1-编号.m4s
    # 规律：同视频ID下，编号小的（3xxxx）是音频，编号大的（1xxxxx）是视频
    import re as _re
    dash_groups: dict[str, list] = {}
    for i in items:
        fn = i["filename"]
        m = _re.match(r"^(\d+_u\d+-\d+)-(\d+)\.(m4s|mp4|m4a)$", fn, _re.I)
        if m:
            prefix, num, ext = m.groups()
            key = prefix
            if key not in dash_groups:
                dash_groups[key] = []
            dash_groups[key].append({"item": i, "num": int(num), "ext": ext.lower()})
    for prefix, group in dash_groups.items():
        if len(group) < 2:
            continue
        # 按编号排序：小编号=音频，大编号=视频
        group.sort(key=lambda x: x["num"])
        audio = group[0]["item"]   # 最小
        video = group[-1]["item"]  # 最大
        video["paired_audio"] = audio["url"]
        audio["paired_video"] = video["url"]
    # 通用配对：文件名含 video/audio 前缀
    videos = [i for i in items if "paired_audio" not in i and (i["type"] == "hls" or
              (i["type"] == "media" and not i["filename"].lower().endswith((".mp3",".m4a",".aac",".flac",".wav",".ogg"))))]
    audios = [i for i in items if "paired_video" not in i and i["type"] == "media" and
              i["filename"].lower().endswith((".mp3",".m4a",".aac",".flac",".wav",".ogg"))]
    for v in videos:
        vbase = os.path.splitext(v["filename"])[0].lower().replace("_video","").replace("-video","")
        for a in audios:
            abase = os.path.splitext(a["filename"])[0].lower().replace("_audio","").replace("-audio","")
            if vbase == abase or vbase in abase or abase in vbase:
                v["paired_audio"] = a["url"]
                a["paired_video"] = v["url"]
                break
    return {"items": items, "title": title, "page": url}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser(prog="dlcore",
                                description="小助下载器引擎（多线程 / HLS / 嗅探）")
    ap.add_argument("-u", "--url", help="下载地址")
    ap.add_argument("-o", "--out", help="保存目录")
    ap.add_argument("-f", "--filename", default="", help="保存文件名")
    ap.add_argument("-c", "--conns", type=int, default=16, help="连接数，默认 16")
    ap.add_argument("-r", "--referer", default="", help="Referer")
    ap.add_argument("--cookie", default="", help="Cookie")
    ap.add_argument("--limit", type=int, default=0, help="限速 KB/s，0 不限")
    ap.add_argument("--scan", metavar="PAGE_URL", help="分析网页中的媒体资源")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if args.scan:
        res = scan_webpage(args.scan)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"页面：{res['title']}  发现 {len(res['items'])} 个媒体资源")
            for it in res["items"]:
                print(f"  [{it['type']}] {it['filename']}\n      {it['url']}")
        return 0
    if not args.url:
        ap.print_help()
        return 2

    mgr = Manager()
    task = mgr.add_task(args.url, out_dir=args.out or os.getcwd(),
                        filename=args.filename, conns=args.conns,
                        referer=args.referer, cookie=args.cookie,
                        speed_limit=args.limit * 1024 if args.limit else 0)
    last = ""
    while task.state not in (STATE_DONE, STATE_ERROR, STATE_CANCELLED):
        time.sleep(0.5)
        line = (f"{STATE_CN.get(task.state, task.state)} "
                f"{task.progress * 100:5.1f}%  {human_speed(task.speed)}  "
                f"{human_size(task.done_bytes)}/{human_size(task.size)}  "
                f"ETA {human_eta(task.eta)}")
        if not args.json and line != last:
            sys.stdout.write("\r" + line.ljust(100))
            sys.stdout.flush()
            last = line
    if args.json:
        print(json.dumps(task.snapshot(), ensure_ascii=False, indent=2))
    else:
        print()
        if task.state == STATE_DONE:
            print(f"完成：{task.created_final}")
        else:
            print(f"结束：{STATE_CN.get(task.state)} {task.err}")
    return 0 if task.state == STATE_DONE else 1


if __name__ == "__main__":
    sys.exit(_cli())
