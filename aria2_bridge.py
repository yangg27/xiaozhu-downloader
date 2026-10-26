#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aria2_bridge.py · 小助下载器 · aria2c 备用下载通道
=====================================================
定位：作为内置「极速备用引擎」，与引擎主任务解耦，以子进程方式调用打包内嵌的 aria2c。

行为：
  1. 只在任务失败/超时/或用户显式选择「aria2c 极速下载」时启动。
  2. 自带 aria2c.exe / N_m3u8DL-RE.exe，无需用户安装。
  3. 普通 HTTP/HTTPS 大文件 → aria2c。
  4. .m3u8/.mpd → N_m3u8DL-RE（本机 aria2c 官方构建未编译 HLS）。
  5. 输出与原任务共享同目录，完成后回写任务状态；失败保留可读错误。

注意：
  · 本子模块不依赖外部 PATH，exe 路径基于 dlcore.app_root() 的 third_party/aria2/。
  · 为避免打包体积失控，本模块只 import 标准库。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

# 延迟导入 dlcore，避免循环依赖；调用时通过参数传入或运行时 import

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 路径/环境
# ---------------------------------------------------------------------------
def _app_root() -> str:
    """应用根目录。frozen 时返回 _internal（资源都在那），否则返回脚本所在目录。"""
    if getattr(sys, "frozen", False):
        # PyInstaller one-dir：sys.executable 在 <root>/小助下载器.exe，
        # 数据资源（含 third_party）在 <root>/_internal/ 下
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        internal = os.path.join(exe_dir, "_internal")
        if os.path.isdir(internal):
            return internal
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))


def aria2_dir() -> str:
    return os.path.join(_app_root(), "third_party", "aria2")


def aria2c_exe() -> str:
    return os.path.join(aria2_dir(), "aria2c.exe")


def nm3u8_exe() -> str:
    return os.path.join(aria2_dir(), "N_m3u8DL-RE.exe")


def is_bundled() -> bool:
    return os.path.isfile(aria2c_exe()) and os.path.isfile(nm3u8_exe())


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _human(n: int | None) -> str:
    if n is None or n < 0:
        return "-"
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(f) < 1024.0:
            return f"{f:.2f} {unit}"
        f /= 1024.0
    return f"{f:.2f} TiB"


def _sha256(path: str, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _guess_filename(url: str) -> str:
    try:
        path = urllib.parse.urlsplit(url).path
        name = urllib.parse.unquote(os.path.basename(path.rstrip("/")))
        name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
        if name:
            return name
    except Exception:
        pass
    return "download"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base}({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _normalize_win_path(p: str) -> str:
    """确保传给 Windows 子进程的是 C:\\ 风格路径（处理 Git Bash / MSYS 的 /c/ 前缀）。"""
    if not p:
        return p
    p = os.path.abspath(p)
    if os.sep == "/" and len(p) >= 3 and p[0] == "/" and p[2] == "/":
        # /c/Users/... → C:/Users/...
        drive = p[1].upper()
        p = f"{drive}:{p[2:]}"
    return os.path.normpath(p)


# ---------------------------------------------------------------------------
# 公共进度回调接口
# ---------------------------------------------------------------------------
class ProgressSink:
    """供外部任务对象注入，让 aria2 子进程下载期间也能刷新 UI 进度。"""

    def __init__(self, task):
        self.task = task
        self._last_t = time.time()
        self._lock = threading.Lock()

    def on_stdout(self, line: str) -> None:
        """解析 aria2c 进度行 [#xxxx 1.2MiB/10MiB(12%) CN:1 DL:3.4MiB ETA:2s]。"""
        try:
            m = re.search(
                r"\[#?[0-9a-fA-F]*\s+([0-9.]+[KMGTiB]*)/([0-9.]+[KMGTiB]*)\s*\((\d+)%\)",
                line)
            if m:
                pct = int(m.group(3)) / 100.0
                self._update(pct=pct)
                return
            # N_m3u8DL-RE 格式：Vid 1920x1080 | 6221 Kbps ---- 12/64 18.75% ...
            m2 = re.search(r"(\d+)/(\d+)\s+(\d+\.?\d*)%", line)
            if m2:
                pct = float(m2.group(3)) / 100.0
                self._update(pct=pct)
        except Exception:
            pass

    def _update(self, pct: float):
        with self._lock:
            t = time.time()
            if t - self._last_t < 0.3:
                return
            self._last_t = t
        try:
            self.task.progress = max(0.0, min(1.0, pct))
            self.task.phase = f"aria2c 备用通道 {pct*100:.1f}%"
            # 如果任务有 size，则更新 done_bytes 以让速度计算可用
            if getattr(self.task, "size", None):
                self.task.done_bytes = int(self.task.size * pct)
            self.task.mgr.notify()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------
class Aria2Bridge:
    """封装 aria2c / N_m3u8DL-RE 作为备用下载通道。"""

    def __init__(self, url: str, out_dir: str, filename: str = "",
                 referer: str = "", cookie: str = "", headers: dict | None = None,
                 user_agent: str = "", proxy: str = "", connections: int = 16,
                 task=None, trackers: list[str] | None = None,
                 on_refetch_trackers=None):
        self.url = url
        self.out_dir = _normalize_win_path(out_dir)
        self.filename = filename or _guess_filename(url)
        self.referer = referer or ""
        self.cookie = cookie or ""
        self.headers = dict(headers or {})
        self.user_agent = user_agent or self.headers.pop("User-Agent", UA)
        self.proxy = proxy or ""
        self.connections = max(1, min(32, int(connections)))
        self.task = task
        self.trackers = list(trackers) if trackers else []
        self.on_refetch_trackers = on_refetch_trackers
        self._proc: subprocess.Popen | None = None
        self._stop = False
        self._retried_with_fresh_trackers = False

    # ---- 控制 ----
    def stop(self):
        self._stop = True
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    # ---- 入口 ----
    def run(self) -> dict:
        if not is_bundled():
            return self._fail("aria2c 组件未打包（缺少 third_party/aria2/）")
        monitor_stop = threading.Event()
        monitor_thread = threading.Thread(
            target=self._stall_monitor, args=(monitor_stop,), daemon=True)
        monitor_thread.start()
        try:
            if self._is_bt(self.url):
                # v3.1.2: 若 _stall_monitor 触发 tracker 刷新，需要循环重试一次
                result = self._run_bt()
                if (callable(self.on_refetch_trackers)
                        and self._retried_with_fresh_trackers
                        and self.task
                        and "已换 trackers 重试" in str(getattr(self.task, "phase", ""))):
                    try:
                        if self._proc and self._proc.poll() is None:
                            self._proc.wait(timeout=10)
                    except Exception:
                        pass
                    result = self._run_bt()
                return result
            if self._is_hls(self.url):
                return self._run_hls()
            return self._run_http()
        finally:
            monitor_stop.set()

    def _stall_monitor(self, stop_evt: threading.Event):
        """BT/磁力任务死任务检测：长时间无进展则终止子进程；支持自动重拉 tracker 重试一次。"""
        if not self._is_bt(self.url):
            return
        stall_limit = 120.0
        check_interval = 20.0
        last_progress = -1.0
        last_bytes = -1
        last_t = time.time()
        while not stop_evt.wait(check_interval):
            if self._proc is None or self._proc.poll() is not None:
                return
            task = self.task
            if task is None:
                continue
            progress = getattr(task, "progress", 0.0) or 0.0
            done_bytes = getattr(task, "done_bytes", 0) or 0
            changed = (progress != last_progress or done_bytes != last_bytes)
            if changed:
                last_progress = progress
                last_bytes = done_bytes
                last_t = time.time()
                continue
            if time.time() - last_t > stall_limit:
                # v3.1.2: 首次停滞 → 换一批最新 tracker 重试一次；第二次停滞 → 真正停止
                if (callable(self.on_refetch_trackers)
                        and not self._retried_with_fresh_trackers):
                    self._retried_with_fresh_trackers = True
                    refetched = self.on_refetch_trackers()
                    if refetched:
                        new_trackers, src = refetched
                        self.trackers = list(new_trackers)
                        if task:
                            task.phase = f"已换 trackers 重试（{src}）"
                            task.err = ""
                            try:
                                task.mgr.notify()
                            except Exception:
                                pass
                        try:
                            self._proc.terminate()
                        except Exception:
                            pass
                        return
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                if task:
                    task.err = ("BT/磁力任务 120 秒无进展，已自动停止。"
                                "可尝试更换 trackers 后重试。")
                    task.phase = "已停止（无响应）"
                    try:
                        task.mgr.notify()
                    except Exception:
                        pass
                return

    @staticmethod
    def _is_hls(url: str) -> bool:
        try:
            p = urllib.parse.urlsplit(url).path.lower()
            return p.endswith((".m3u8", ".mpd"))
        except Exception:
            return False

    @staticmethod
    def _is_bt(url: str) -> bool:
        if not url:
            return False
        u = url.strip().lower()
        if u.startswith("magnet:"):
            return True
        try:
            p = urllib.parse.urlsplit(u).path
            if p.endswith(".torrent"):
                return True
        except Exception:
            pass
        # 本地 .torrent 文件路径
        if u.endswith(".torrent") and os.path.exists(url):
            return True
        return False

    def _fail(self, msg: str) -> dict:
        return {"ok": False, "error": msg, "path": "", "bytes": 0}

    # ---- HTTP via aria2c ----
    def _run_http(self) -> dict:
        out_dir = self.out_dir
        os.makedirs(out_dir, exist_ok=True)
        # 如果用户给了明确文件名，aria2 的 --out 只用于单文件；否则让 aria2 自己取
        base, ext = os.path.splitext(self.filename)
        if not ext:
            ext = ".bin"
        out_name = self.filename if self.filename else _guess_filename(self.url)
        final_path = _unique_path(os.path.join(out_dir, out_name))
        # 最终名若被 unique 加了 (1)，需要让 aria2 先落到临时名再 rename
        aria_out = out_name

        cmd = [
            aria2c_exe(),
            "--no-conf",
            "--continue=true",
            "--auto-file-renaming=false",
            "--content-disposition-default-utf8=true",
            "--file-allocation=none",
            f"--max-connection-per-server={self.connections}",
            f"--split={self.connections}",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=30",
            "--timeout=30",
            "--summary-interval=1",
            "--disk-cache=64M",
            "--http-accept-gzip=true",
            "--check-certificate=true",
            f"--dir={out_dir}",
            f"--out={aria_out}",
            "--show-console-readout=true",
            "--console-log-level=notice",
        ]
        if self.user_agent:
            cmd.append(f"--user-agent={self.user_agent}")
        if self.referer:
            cmd.append(f"--referer={self.referer}")
        if self.cookie:
            cmd.append(f"--header=Cookie: {self.cookie}")
        if self.proxy:
            cmd.append(f"--all-proxy={self.proxy}")
        for k, v in self.headers.items():
            cmd.append(f"--header={k}: {v}")
        cmd.append(self.url)

        # 如果 final_path 被 unique 改名了，需要让 aria2 落到 aria_out 后再 rename
        tmp_aria = os.path.join(out_dir, aria_out)

        sink = ProgressSink(self.task) if self.task else None
        t0 = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace")
            if self._proc.stdout:
                for line in self._proc.stdout:
                    if sink:
                        sink.on_stdout(line)
                    if self._stop:
                        self._proc.terminate()
                        return self._fail("用户取消")
            rc = self._proc.wait()
        except Exception as e:
            return self._fail(f"启动 aria2c 失败：{e}")
        elapsed = time.time() - t0

        # 找实际输出文件（aria2 可能用 Content-Disposition 改名）
        real_path = self._find_output(tmp_aria, out_dir, t0)
        if not real_path or not os.path.exists(real_path):
            return self._fail(f"aria2c 结束但文件未生成（exit={rc}）")

        # 若 unique 改名：把 aria_out 重命名为 final_path
        if final_path != tmp_aria and os.path.abspath(real_path) == os.path.abspath(tmp_aria):
            try:
                os.replace(real_path, final_path)
                real_path = final_path
            except OSError:
                pass

        size = os.path.getsize(real_path)
        result = {
            "ok": True,
            "error": "",
            "path": real_path,
            "bytes": size,
            "human": _human(size),
            "elapsed_sec": round(elapsed, 2),
            "downloader": "aria2c",
            "sha256": _sha256(real_path) if size < (2 << 30) else "",
        }
        if self.task:
            self.task.created_final = real_path
            self.task.filename = os.path.basename(real_path)
            self.task.size = size
            self.task.done_bytes = size
            self.task.progress = 1.0
            self.task._complete(real_path, "已完成（aria2c 备用）")
        return result

    def _find_output(self, expected: str, out_dir: str, since: float):
        """优先返回 expected；若不存在则返回目录 newest 文件。"""
        if os.path.exists(expected):
            return expected
        try:
            files = [(p, os.path.getmtime(p)) for p in glob.glob(os.path.join(out_dir, "*"))
                     if os.path.isfile(p) and os.path.getmtime(p) >= since - 2]
            if files:
                return max(files, key=lambda x: x[1])[0]
        except Exception:
            pass
        return ""

    # ---- BT/磁力 via aria2c ----
    def _run_bt(self) -> dict:
        """BT 种子 / 磁力链下载。aria2c 原生支持，输出一个目录或单文件。"""
        out_dir = self.out_dir
        os.makedirs(out_dir, exist_ok=True)

        # trackers 来源：实例属性（run_aria2 注入）或 headers 中的 _trackers
        trackers = getattr(self, "trackers", None) or []
        if not trackers and isinstance(self.headers, dict):
            trackers = self.headers.pop("_trackers", None) or []

        # DHT 路由表持久化目录：优先用任务级 data_dir，否则用输出目录
        data_dir = (getattr(self, "data_dir", None)
                    or (self.task and getattr(self.task, "mgr", None)
                        and getattr(self.task.mgr, "data_dir", None))
                    or out_dir)
        os.makedirs(data_dir, exist_ok=True)

        cmd = [
            aria2c_exe(),
            "--no-conf",
            "--continue=true",
            "--auto-file-renaming=true",  # BT 文件已存在时自动加 (1) 后缀
            "--allow-overwrite=true",     # 配合 continue=true：已有 .aria2 控制文件时续传
            # ---- DHT / PEX / LPD：最大化发现 peer ----
            "--enable-dht=true",
            "--enable-dht6=true",
            "--enable-peer-exchange=true",
            "--bt-enable-lpd=true",
            f"--dht-file-path={os.path.join(data_dir, 'aria2_dht.dat')}",
            f"--dht-file-path6={os.path.join(data_dir, 'aria2_dht6.dat')}",
            # ---- BT 连接策略 ----
            "--bt-require-crypto=false",
            "--bt-save-metadata=true",
            "--bt-load-saved-metadata=true",
            "--bt-metadata-only=false",
            "--seed-time=0",
            "--bt-max-peers=128",              # 默认 55，适当提高连接数
            "--bt-request-peer-speed-limit=50K",  # 低于 50KB/s 的 peer 不再请求
            "--bt-stop-timeout=600",           # 10 分钟无数据则停止（磁力 metadata 阶段更省资源）
            "--bt-min-crypto-level=plain",     # 兼容更多老旧客户端
            "--summary-interval=1",
            "--disk-cache=64M",
            "--file-allocation=none",
            "--follow-torrent=true",
            "--follow-metalink=true",
            f"--dir={out_dir}",
            "--show-console-readout=true",
            "--console-log-level=notice",
        ]
        if trackers:
            # tracker 选择策略：高可用知名 tracker 固定置顶 + UDP 优先 + 数量限制
            PRIORITY_TRACKERS = [
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
                "udp://tracker.torrent.eu.org:451/announce",
                "udp://tracker.openbittorrent.com:6969/announce",
                "udp://exodus.desync.com:6969/announce",
            ]
            seen = set()
            udp_tr = []
            http_tr = []
            for t in trackers:
                t = (t or "").strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                tl = t.lower()
                if tl.startswith("udp://"):
                    udp_tr.append(t)
                elif tl.startswith(("http://", "https://", "wss://", "ws://")):
                    http_tr.append(t)
            # 优先 tracker 置顶（如果在订阅源里没出现也强行加），然后 UDP，再 HTTP
            tr = list(PRIORITY_TRACKERS)
            for t in udp_tr:
                if t not in tr:
                    tr.append(t)
                if len(tr) >= 25: break
            for t in http_tr:
                if t not in tr:
                    tr.append(t)
                if len(tr) >= 30: break
            if tr:
                cmd.append("--bt-tracker=" + ",".join(tr))
        if self.user_agent:
            cmd.append(f"--user-agent={self.user_agent}")
        # BT peer 标识：使用 aria2 标准前缀，避免被反吸血/屏蔽
        cmd.append("--peer-agent=aria2/1.37.0")
        cmd.append("--peer-id-prefix=-AR1370-")
        if self.proxy:
            # BT 流量一般不走代理，但 tracker 请求走代理很有用
            cmd.append(f"--all-proxy={self.proxy}")
        cmd.append(self.url)

        sink = ProgressSink(self.task) if self.task else None
        t0 = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace")
            if self._proc.stdout:
                for line in self._proc.stdout:
                    if sink:
                        sink.on_stdout(line)
                    if self._stop:
                        self._proc.terminate()
                        return self._fail("用户取消")
            rc = self._proc.wait()
        except Exception as e:
            return self._fail(f"启动 aria2c (BT) 失败：{e}")
        elapsed = time.time() - t0

        # BT 输出可能是目录或多文件；扫 out_dir 内 t0 之后的新增/修改文件
        phase = getattr(self.task, "phase", "")
        if phase == "已停止（无响应）":
            return self._fail("BT/磁力任务长时间无进展，已自动停止。可更换 trackers 后重试")
        # v3.1.2: _stall_monitor 已触发 tracker 刷新并终止旧进程时，返回特殊标记由 run() 外层重试
        if self._retried_with_fresh_trackers and "已换 trackers 重试" in str(phase):
            return self._fail("__RETRY_WITH_FRESH_TRACKERS__")
        produced = self._collect_bt_outputs(out_dir, t0)
        if not produced:
            return self._fail(f"aria2c (BT) 结束但未产出文件（exit={rc}）")

        # 主产物：如果只有一个，就用它；多个文件用公共顶层目录作为 path
        if len(produced) == 1:
            real_path = produced[0]
        else:
            # 找最长公共前缀目录
            real_path = os.path.commonpath([os.path.dirname(p) for p in produced])
        try:
            size = 0
            for p in produced:
                if os.path.isfile(p):
                    size += os.path.getsize(p)
        except Exception:
            size = 0
        result = {
            "ok": True,
            "error": "",
            "path": real_path,
            "bytes": size,
            "human": _human(size),
            "elapsed_sec": round(elapsed, 2),
            "downloader": "aria2c-bt",
            "files": produced,
            "file_count": len(produced),
        }
        if self.task:
            self.task.created_final = real_path
            self.task.filename = os.path.basename(real_path)
            self.task.size = size
            self.task.done_bytes = size
            self.task.progress = 1.0
            self.task.extra["bt_files"] = produced
            self.task.extra["bt_file_count"] = len(produced)
            self.task._complete(real_path, "已完成（BT/磁力）")
        return result

    @staticmethod
    def _collect_bt_outputs(out_dir: str, since: float) -> list:
        """收集 BT 任务在 out_dir 下 t0 之后产生/修改的所有文件（不含 .aria2 控制文件）。"""
        produced = []
        try:
            for root, _, files in os.walk(out_dir):
                for fn in files:
                    if fn.endswith(".aria2"):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        if os.path.getmtime(full) >= since - 2:
                            produced.append(full)
                    except OSError:
                        continue
        except Exception:
            pass
        produced.sort()
        return produced

    # ---- HLS via N_m3u8DL-RE ----
    def _run_hls(self) -> dict:
        out_dir = self.out_dir
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(self.filename or "stream")[0]
        if base.lower().endswith((".mp4", ".ts", ".mkv")):
            base = os.path.splitext(base)[0]
        final_path = _unique_path(os.path.join(out_dir, base + ".mp4"))
        save_name = os.path.splitext(os.path.basename(final_path))[0]
        tmp_dir = os.path.join(out_dir, ".nm3u8-tmp")

        cmd = [
            nm3u8_exe(),
            self.url,
            "--save-dir", out_dir,
            "--save-name", save_name,
            "--auto-select",
            f"--thread-count={max(1, self.connections)}",
            "--del-after-done",
            "--no-log",
            "--tmp-dir", tmp_dir,
            "--no-ansi-color",
            "--ui-language", "en-US",
            "--log-level", "WARN",
        ]
        if self.referer:
            cmd.extend(["-H", f"Referer: {self.referer}"])
        if self.user_agent:
            cmd.extend(["-H", f"User-Agent: {self.user_agent}"])
        if self.cookie:
            cmd.extend(["-H", f"Cookie: {self.cookie}"])
        for k, v in self.headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        ff = shutil.which("ffmpeg") or os.path.join(_app_root(), "third_party", "ffmpeg.exe")
        if ff and os.path.exists(ff):
            cmd.extend(["--ffmpeg-binary-path", ff])

        sink = ProgressSink(self.task) if self.task else None
        t0 = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace")
            if self._proc.stdout:
                for line in self._proc.stdout:
                    if sink:
                        sink.on_stdout(line)
                    if self._stop:
                        self._proc.terminate()
                        return self._fail("用户取消")
            rc = self._proc.wait()
        except Exception as e:
            return self._fail(f"启动 N_m3u8DL-RE 失败：{e}")
        elapsed = time.time() - t0

        # N_m3u8DL-RE 输出 save_name.mp4 / save_name.ts
        candidates = [
            final_path,
            os.path.join(out_dir, save_name + ".mp4"),
            os.path.join(out_dir, save_name + ".ts"),
        ]
        real_path = ""
        for c in candidates:
            if os.path.exists(c):
                real_path = c
                break
        if not real_path:
            return self._fail(f"N_m3u8DL-RE 结束但视频未生成（exit={rc}）")

        size = os.path.getsize(real_path)
        result = {
            "ok": True,
            "error": "",
            "path": real_path,
            "bytes": size,
            "human": _human(size),
            "elapsed_sec": round(elapsed, 2),
            "downloader": "N_m3u8DL-RE",
            "sha256": _sha256(real_path) if size < (2 << 30) else "",
        }
        if self.task:
            self.task.created_final = real_path
            self.task.filename = os.path.basename(real_path)
            self.task.size = size
            self.task.done_bytes = size
            self.task.progress = 1.0
            self.task._complete(real_path, "已完成（aria2c HLS 备用）")
        return result


# ---------------------------------------------------------------------------
# 便捷接口
# ---------------------------------------------------------------------------
def run_aria2(url: str, out_dir: str, filename: str = "", *,
              referer: str = "", cookie: str = "", headers: dict | None = None,
              user_agent: str = "", proxy: str = "", connections: int = 16,
              trackers: list | None = None,
              task=None,
              on_refetch_trackers=None) -> dict:
    """一键调用 aria2c 备用通道；不依赖外部安装。trackers 用于 BT/磁力。"""
    bridge = Aria2Bridge(url, out_dir, filename=filename,
                         referer=referer, cookie=cookie, headers=headers,
                         user_agent=user_agent, proxy=proxy,
                         connections=connections, task=task,
                         trackers=trackers,
                         on_refetch_trackers=on_refetch_trackers)
    return bridge.run()