#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bt_trackers.py · 小助下载管理器 XZDM · BT Trackers 自动更新器
================================================================
行为：
  · 启动时异步从订阅地址拉取最新 tracker 列表（默认 cf.trackerslist.com/best.txt）
  · 本地缓存到 data/trackers.txt，网络失败时回退到缓存
  · 完全离线兜底：内置 20+ 条高可用 tracker，确保零网络也能解析 magnet

注意：仅依赖标准库，可与 dlcore 解耦单独 import。
"""

from __future__ import annotations

import os
import ssl
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# 订阅源（按优先级排序，全部走 https）
# ---------------------------------------------------------------------------
TRACKER_SOURCES = [
    "https://cf.trackerslist.com/best.txt",
    "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/best.txt",
    "https://cdn.jsdelivr.net/gh/XIU2/TrackersListCollection@master/best.txt",
]

# 离线兜底 trackers（2026-10 实测高可用）
FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "https://tracker.opentrackr.org:443/announce",
    "https://tracker.torrent.eu.org:443/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.dump.cl:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://tracker.breizh.pm:6969/announce",
    "udp://bt.ktrackers.com:6666/announce",
    "udp://tracker.edkj.club:6969/announce",
]

_UA = "XZDM/3.0 (+https://github.com/yangg27/xiaozhu-downloader)"


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    """trackers.txt 存放目录。优先走 APPDATA/XZDM，源码模式回落到 ./data。"""
    env = os.environ.get("XZD_DATA_DIR")
    if env:
        d = os.path.abspath(env)
    elif getattr(sys, "frozen", False):
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "小助下载器")
    else:
        d = os.path.join(_app_root(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def cache_path() -> str:
    return os.path.join(_data_dir(), "trackers.txt")


def meta_path() -> str:
    return os.path.join(_data_dir(), "trackers.meta")


# ---------------------------------------------------------------------------
# 解析 / 拉取
# ---------------------------------------------------------------------------
def parse_trackers(text: str) -> list[str]:
    """从 best.txt 内容解析 tracker 列表。每行一条，空行分隔段落。"""
    out: list[str] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def fetch_one(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("latin-1", errors="replace")


def fetch_trackers(sources: list[str] | None = None,
                   timeout: float = 8.0) -> tuple[list[str], str]:
    """依次尝试订阅源，返回 (trackers, source_url)。全失败时返回 ([], '')。"""
    for url in (sources or TRACKER_SOURCES):
        try:
            body = fetch_one(url, timeout=timeout)
            lst = parse_trackers(body)
            if lst:
                return lst, url
        except Exception:
            continue
    return [], ""


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------
def load_cached() -> list[str]:
    p = cache_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return parse_trackers(f.read())
    except Exception:
        return []


def save_cache(trackers: list[str], source: str) -> None:
    try:
        tmp = cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(trackers) + "\n")
        os.replace(tmp, cache_path())
        with open(meta_path(), "w", encoding="utf-8") as f:
            f.write(f"source={source}\nupdated={int(time.time())}\ncount={len(trackers)}\n")
    except Exception:
        pass


def meta_info() -> dict:
    p = meta_path()
    info: dict = {"source": "", "updated": 0, "count": 0}
    if not os.path.isfile(p):
        return info
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k == "updated":
                        try:
                            info[k] = int(v)
                        except ValueError:
                            pass
                    elif k == "count":
                        try:
                            info[k] = int(v)
                        except ValueError:
                            pass
                    else:
                        info[k] = v
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# 主入口：同步与异步
# ---------------------------------------------------------------------------
def get_trackers(*, prefer_cache: bool = True,
                 allow_fetch: bool = True,
                 timeout: float = 8.0) -> tuple[list[str], str]:
    """获取 tracker 列表。
    返回 (trackers, source_desc)。
    策略：缓存（新鲜）→ 网络拉取 → 缓存（陈旧）→ 内置兜底
    """
    cached = load_cached()
    meta = meta_info()
    fresh = bool(cached) and (time.time() - meta.get("updated", 0) < 24 * 3600)

    if prefer_cache and fresh:
        return cached, f"cache<{meta.get('source','')}>"

    if allow_fetch:
        lst, src = fetch_trackers(timeout=timeout)
        if lst:
            save_cache(lst, src)
            return lst, src

    if cached:
        return cached, "cache-stale"
    return list(FALLBACK_TRACKERS), "builtin-fallback"


def update_async(callback=None, timeout: float = 8.0) -> threading.Thread:
    """启动后台线程更新 trackers。完成后回调 callback(trackers, source)。"""
    def work():
        lst, src = fetch_trackers(timeout=timeout)
        if lst:
            save_cache(lst, src)
        if callback:
            try:
                callback(lst or load_cached() or list(FALLBACK_TRACKERS),
                         src or "cache|fallback")
            except Exception:
                pass
    t = threading.Thread(target=work, daemon=True, name="xzd-trackers")
    t.start()
    return t


# ---------------------------------------------------------------------------
# CLI 调试
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    lst, src = get_trackers(prefer_cache=False)
    print(f"source: {src}")
    print(f"count : {len(lst)}")
    for t in lst[:20]:
        print("  ", t)
