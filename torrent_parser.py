#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · torrent 解析器（v3.1.0）
最小化集成：优先调用已内置的 aria2c --show-files 解析 .torrent，
输出 info_hash、名称、总大小、文件列表、磁力链、内置 trackers。

不引入 libtorrent/transmission 等重型依赖，保持安装包体积最小。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _app_root() -> str:
    """与 dlcore.app_root() 保持一致，但避免必须 import dlcore。"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        internal = os.path.join(exe_dir, "_internal")
        if os.path.isdir(internal):
            return internal
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))


def aria2c_exe() -> str:
    """定位 aria2c.exe（兼容源码与 PyInstaller 打包）。"""
    candidates = [
        os.path.join(_app_root(), "third_party", "aria2", "aria2c.exe"),
        os.path.join(_app_root(), "..", "third_party", "aria2", "aria2c.exe"),
        "aria2c",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[0]


def parse_torrent(path: str, timeout: int = 15) -> dict:
    """
    解析本地 .torrent 文件，返回结构化信息。

    返回字段：
        - info_hash (str): 40 位十六进制 info hash
        - name (str): 种子名称
        - total_length (int): 总字节数
        - magnet (str): 自动生成的 magnet URI
        - trackers (list[str]): 种子内置 announce trackers
        - files (list[dict]): 每个文件 {path, length}
        - raw (str): aria2c 原始输出（调试排错用）
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到种子文件：{path}")

    exe = aria2c_exe()
    cmd = [exe, "--show-files", path]
    try:
        out = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        ).stdout
    except FileNotFoundError as e:
        raise RuntimeError(f"aria2c 未找到：{exe}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("aria2c 解析种子超时") from e

    return _parse_show_files(out)


_RE_HASH = re.compile(r"Info Hash:\s*([0-9a-fA-F]{40})")
_RE_NAME = re.compile(r"Name:\s*(.+)\n")
_RE_TOTAL = re.compile(r"Total Length:\s*[\d.]+\w+\s*\(([\d,]+)\)")
_RE_MAGNET = re.compile(r"Magnet URI:\s*(.+)\n")
_RE_FILE_HEAD = re.compile(r"^\s*(\d+)\|\.?(/?.+)$")
_RE_FILE_SIZE = re.compile(r"^\s*\|?\s*([\d.]+)(\w+)\s*\(([\d,]+)\)")


def _parse_show_files(text: str) -> dict:
    result = {
        "info_hash": "",
        "name": "",
        "total_length": 0,
        "magnet": "",
        "trackers": [],
        "files": [],
        "raw": text,
    }

    m = _RE_HASH.search(text)
    if m:
        result["info_hash"] = m.group(1).lower()

    m = _RE_NAME.search(text)
    if m:
        result["name"] = m.group(1).strip()

    m = _RE_TOTAL.search(text)
    if m:
        result["total_length"] = int(m.group(1).replace(",", ""))

    m = _RE_MAGNET.search(text)
    if m:
        result["magnet"] = m.group(1).strip()

    # 解析 trackers：在 Announce: 与下一个空行/标题之间
    in_announce = False
    trackers: list[str] = []
    for line in text.splitlines():
        if line.startswith("Announce:"):
            in_announce = True
            rest = line[len("Announce:"):].strip()
            if rest:
                trackers.append(rest)
            continue
        if in_announce:
            if not line.strip() or line.startswith(("Info Hash", "Mode:", "Piece Length", "Total Length", "Files:")):
                in_announce = False
            else:
                trackers.append(line.strip())
    result["trackers"] = [t for t in trackers if t]

    # 解析文件列表
    files: list[dict] = []
    pending: Optional[dict] = None
    for line in text.splitlines():
        m = _RE_FILE_HEAD.match(line)
        if m:
            if pending:
                files.append(pending)
            pending = {"path": m.group(2).lstrip("./"), "length": 0}
            continue
        if pending is not None:
            m = _RE_FILE_SIZE.match(line)
            if m:
                pending["length"] = int(m.group(3).replace(",", ""))
                files.append(pending)
                pending = None
    if pending:
        files.append(pending)
    result["files"] = files

    return result


def parse_magnet(magnet: str) -> dict:
    """
    解析 magnet URI，提取 info_hash、display name、trackers。
    纯字符串处理，无需 aria2c。
    """
    from urllib.parse import parse_qs, urlparse
    u = urlparse(magnet)
    qs = parse_qs(u.query)
    xt = qs.get("xt", [""])[0]
    info_hash = ""
    if xt.startswith("urn:btih:"):
        info_hash = xt[len("urn:btih:"):].lower()
    return {
        "info_hash": info_hash,
        "name": qs.get("dn", [""])[0],
        "trackers": qs.get("tr", []),
        "magnet": magnet,
    }


if __name__ == "__main__":
    import json
    p = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Sunny_yang\AppData\Local\Temp\sintel.torrent"
    info = parse_torrent(p)
    print(json.dumps(info, indent=2, ensure_ascii=False))
