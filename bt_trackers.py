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
import re
import socket
import ssl
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 订阅源（按优先级排序，多个源聚合去重）
# ---------------------------------------------------------------------------
# 主源：trackerslist 的精选/全量/协议分类
TRACKER_SOURCES = [
    "https://cf.trackerslist.com/best.txt",
    "https://cf.trackerslist.com/all.txt",
    "https://cf.trackerslist.com/http.txt",
    "https://cf.trackerslist.com/udp.txt",
]
# 备用镜像（当主源不可达时）
TRACKER_SOURCES_MIRROR = [
    "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/best.txt",
    "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/all.txt",
    "https://cdn.jsdelivr.net/gh/XIU2/TrackersListCollection@master/best.txt",
    "https://cdn.jsdelivr.net/gh/XIU2/TrackersListCollection@master/all.txt",
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
_TRACKER_SCHEMES = ("udp://", "http://", "https://", "ws://", "wss://")

# v3.1.8：一个订阅源至少给出这么多条才算"可用"，
# 低于此数一律当作没拉到（防止 HTML 页面/半截响应污染列表）。
MIN_VIABLE_TRACKERS = 5

# v3.1.8 修复：只接受真正的 tracker URL。
# 事故背景：订阅源 cf.trackerslist.com 有时会返回一个 **HTML 页面**
# （CDN 拦了直连、或返回了站点首页），而旧 parse_trackers 只按行切分、
# 不做任何校验，于是 <html> <script> 等整页源码全被当成 tracker 存进缓存。
# 实测污染后 153 条里 130+ 条是网页源码，BT 任务拿着这些垃圾去 announce，
# 既浪费建连时间也让 tracker 列表彻底失效。
_TRACKER_URL_RE = re.compile(
    r"^(?:udp|http|https|ws|wss)://[^\s<>\"'\\]+$", re.I)


def parse_trackers(text: str) -> list[str]:
    """从 best.txt 内容解析 tracker 列表。

    只保留形如 <scheme>://<host>[:port][/path] 的合法 tracker URL：
      · 丢弃 HTML/JS/CSS 等非 URL 行（订阅源返回网页时的自我保护）
      · 丢弃带空格、引号、尖括号等 URL 非法字符的行
      · 去重且保持原顺序
    """
    out: list[str] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")          # 去 BOM
        if not line or line.startswith("#"):
            continue
        if not _TRACKER_URL_RE.match(line):
            continue                                  # ← v3.1.8：非法项直接丢弃
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


def _protocol_rank(t: str) -> int:
    """协议优先级：UDP 优先（穿透强），HTTPS 其次，HTTP 兜底，WSS 最后。"""
    tl = t.lower()
    if tl.startswith("udp://"):
        return 0
    if tl.startswith("https://"):
        return 1
    if tl.startswith("http://"):
        return 2
    if tl.startswith(("wss://", "ws://")):
        return 3
    return 4


_PRIORITY_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
]


def _prioritize_trackers(trackers: list[str]) -> list[str]:
    """知名 tracker 置顶，同优先级按协议排序。"""
    seen = set()
    out = []
    for t in _PRIORITY_TRACKERS:
        if t in trackers and t not in seen:
            seen.add(t)
            out.append(t)
    for t in sorted(trackers, key=_protocol_rank):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _split_sources(text: str) -> list[str]:
    """把用户输入的源字符串（逗号/空格/换行分隔）拆成 URL 列表。"""
    out = []
    for raw in (text or "").replace(",", "\n").splitlines():
        t = raw.strip()
        if t.startswith(("http://", "https://")):
            out.append(t)
    return out


def fetch_trackers(sources: list[str] | None = None,
                   timeout: float = 8.0,
                   extra_sources: list[str] | None = None,
                   custom_source: str = "") -> tuple[list[str], str]:
    """
    依次尝试订阅源并聚合去重。
    返回 (trackers, source_url)。全失败时返回 ([], '')。
    策略：优先源逐个尝试直到拿到有效列表；如全部失败再尝试备用镜像。
    最终按协议优先级排序并去重。
    """
    all_sources = list(sources or TRACKER_SOURCES)
    if custom_source:
        all_sources = _split_sources(custom_source) + all_sources
    if extra_sources:
        all_sources.extend(extra_sources)

    collected: list[str] = []
    seen = set()
    first_source = ""

    for url in all_sources:
        try:
            body = fetch_one(url, timeout=timeout)
            lst = parse_trackers(body)
            # v3.1.8：低于 MIN_VIABLE 视为"该源没给出可用列表"而跳过。
            # 配合 parse_trackers 的 URL 校验，双保险拦住 HTML 页面污染：
            # 就算页面里正好含 1-2 个 tracker 链接，也不会拿它当成功源。
            if len(lst) < MIN_VIABLE_TRACKERS:
                continue
            if not first_source:
                first_source = url
            for t in lst:
                if t not in seen:
                    seen.add(t)
                    collected.append(t)
            # 拿到主源即可继续尝试更多源以扩充列表，但不强制中断
        except Exception:
            continue

    if not collected and not sources:
        # 主源全失败，尝试备用镜像
        return fetch_trackers(sources=TRACKER_SOURCES_MIRROR, timeout=timeout)

    if collected:
        # 按协议优先级排序，同协议内保持原顺序
        collected.sort(key=_protocol_rank)
        return collected, first_source or "aggregated"
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
# Tracker 可用性探测（可选，启动后后台执行）
# ---------------------------------------------------------------------------
def _tracker_host_port(t: str) -> tuple[str, int] | None:
    """从 tracker URL 提取主机和端口。"""
    try:
        u = urlparse(t)
        host = u.hostname
        port = u.port
        if not host or not port:
            return None
        return host, port
    except Exception:
        return None


def probe_tracker(t: str, timeout: float = 3.0) -> bool:
    """
    快速探测 tracker 是否可达。
    UDP tracker：connect 阶段无法完成完整握手，只能做 DNS + 端口 connect 初筛；
    HTTP/HTTPS tracker：解析并尝试 TCP 连接。
    返回 True 表示至少主机端口可解析并连接成功。
    """
    hp = _tracker_host_port(t)
    if not hp:
        return False
    host, port = hp
    try:
        # 先解析 DNS（顺带验证域名有效）
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not addr:
            return False
    except Exception:
        return False

    # UDP tracker 只做端口 connect（UDP connect 不真正发包，但可验证路由可达）
    if t.lower().startswith("udp://"):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.connect((addr[0][4][0], port))
            sock.close()
            return True
        except Exception:
            return False

    # HTTP/HTTPS 做 TCP 连接
    is_https = t.lower().startswith("https://")
    try:
        sock = socket.socket(addr[0][0], socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((addr[0][4][0], port))
        if is_https:
            # 简单 TLS 握手探测
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as _:
                return True
        sock.close()
        return True
    except Exception:
        return False


def filter_alive(trackers: list[str], max_workers: int = 32,
                 timeout: float = 3.0) -> tuple[list[str], list[tuple[str, bool]]]:
    """
    多线程探测 tracker 可用性。返回 (alive_list, full_results)。
    注意：这是端口层可达性，不等价于 tracker 真能 announce，但可剔除大量无效节点。
    """
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def check(t: str):
        ok = probe_tracker(t, timeout=timeout)
        with lock:
            results.append((t, ok))

    threads = []
    for t in trackers:
        while len(threads) >= max_workers:
            threads[0].join(timeout=0.1)
            threads = [th for th in threads if th.is_alive()]
        th = threading.Thread(target=check, args=(t,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()

    alive = [t for t, ok in results if ok]
    return alive, results


# ---------------------------------------------------------------------------
# 主入口：同步与异步
# ---------------------------------------------------------------------------
def get_trackers(*, prefer_cache: bool = True,
                 allow_fetch: bool = True,
                 timeout: float = 8.0,
                 custom_source: str = "",
                 probe_alive: bool = False,
                 probe_timeout: float = 3.0,
                 probe_workers: int = 32) -> tuple[list[str], str]:
    """获取 tracker 列表。
    返回 (trackers, source_desc)。
    策略：缓存（新鲜）→ 网络拉取 → 缓存（陈旧）→ 内置兜底
    如果 probe_alive=True，则对结果进行端口层可达性探测，只保留存活的 tracker。
    """
    cached = load_cached()
    meta = meta_info()
    fresh = bool(cached) and (time.time() - meta.get("updated", 0) < 24 * 3600)

    lst: list[str] = []
    src = ""
    if prefer_cache and fresh:
        lst, src = cached, f"cache<{meta.get('source','')}>"
    elif allow_fetch:
        lst, src = fetch_trackers(timeout=timeout, custom_source=custom_source)
        if lst:
            save_cache(lst, src)
    elif cached:
        lst, src = cached, "cache-stale"
    else:
        lst, src = list(FALLBACK_TRACKERS), "builtin-fallback"

    if probe_alive and lst:
        alive, _ = filter_alive(lst, max_workers=probe_workers, timeout=probe_timeout)
        if alive:
            # 过滤后：知名 tracker 置顶 → UDP 优先 → HTTP/HTTPS/WSS
            alive = _prioritize_trackers(alive)
            return alive, f"{src}|alive:{len(alive)}/{len(lst)}"
    return _prioritize_trackers(lst), src


def update_async(callback=None, timeout: float = 8.0,
                 custom_source: str = "",
                 probe_alive: bool = False,
                 probe_timeout: float = 3.0) -> threading.Thread:
    """启动后台线程更新 trackers。完成后回调 callback(trackers, source)。"""
    def work():
        lst, src = get_trackers(
            prefer_cache=False, allow_fetch=True, timeout=timeout,
            custom_source=custom_source,
            probe_alive=probe_alive, probe_timeout=probe_timeout)
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
