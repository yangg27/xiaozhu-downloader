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
    """与 dlcore.app_root() 保持一致，但避免必须 import dlcore。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
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
                 task=None):
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
        self._proc: subprocess.Popen | None = None
        self._stop = False

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
        if self._is_hls(self.url):
            return self._run_hls()
        return self._run_http()

    @staticmethod
    def _is_hls(url: str) -> bool:
        try:
            p = urllib.parse.urlsplit(url).path.lower()
            return p.endswith((".m3u8", ".mpd"))
        except Exception:
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
            self.task.state = "done"
            self.task.phase = "已完成（aria2c 备用）"
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
            self.task.state = "done"
            self.task.phase = "已完成（aria2c HLS 备用）"
        return result


# ---------------------------------------------------------------------------
# 便捷接口
# ---------------------------------------------------------------------------
def run_aria2(url: str, out_dir: str, filename: str = "", *,
              referer: str = "", cookie: str = "", headers: dict | None = None,
              user_agent: str = "", proxy: str = "", connections: int = 16,
              task=None) -> dict:
    """一键调用 aria2c 备用通道；不依赖外部安装。"""
    bridge = Aria2Bridge(url, out_dir, filename=filename,
                         referer=referer, cookie=cookie, headers=headers,
                         user_agent=user_agent, proxy=proxy,
                         connections=connections, task=task)
    return bridge.run()