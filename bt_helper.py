"""
小助下载管理器 · BT/磁力辅助模块（v3.0.1）

职责：
1. 统一规范化 BT 输入（magnet / .torrent 本地路径 / .torrent HTTP URL）
2. 对磁力链执行 metadata-only 预抓取，尽量转换为 .torrent 种子文件
3. 对 HTTP 种子文件自动下载到本地缓存
4. 解析种子文件，返回摘要信息
5. 当 tracker 不可用时，自动提示启用 DHT/PEX/LPD（aria2c 参数已默认开启）

依赖：
- aria2c.exe（已内置）
- torrent_parser.py（已内置，基于 aria2c --show-files）
- 纯 Python 标准库
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Any

import torrent_parser as _tp


def _parse_torrent(path: str) -> dict:
    return _tp.parse_torrent(path)

_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)
_TORRENT_URL_RE = re.compile(r"^https?://.*\.torrent(\?.*)?$", re.IGNORECASE)


def is_magnet(url: str) -> bool:
    return bool(url and _MAGNET_RE.match(url.strip()))


def is_torrent_url(url: str) -> bool:
    return bool(url and _TORRENT_URL_RE.match(url.strip()))


def is_torrent_path(path: str) -> bool:
    return bool(path and os.path.isfile(path) and path.lower().endswith(".torrent"))


def _aria2c_exe() -> str:
    """定位 aria2c.exe：优先 _internal/third_party，再同级目录。"""
    candidates = []
    if getattr(sys := __import__("sys"), "frozen", False):
        base = os.path.dirname(sys.executable)
        candidates.append(os.path.join(base, "third_party", "aria2", "aria2c.exe"))
        candidates.append(os.path.join(base, "aria2c.exe"))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(base, "third_party", "aria2", "aria2c.exe"))
        candidates.append(os.path.join(base, "aria2c.exe"))
    for c in candidates:
        if os.path.exists(c):
            return os.path.normpath(c)
    found = shutil.which("aria2c") or shutil.which("aria2c.exe")
    if found:
        return found
    raise FileNotFoundError("aria2c.exe 未找到")


def _fetch_url(url: str, headers: dict | None = None, timeout: float = 30.0,
               proxy: str | None = None) -> bytes:
    """简单下载 URL 内容，返回 bytes。"""
    req = urllib.request.Request(url, headers=headers or {})
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def download_torrent_file(url: str, out_dir: str | None = None,
                          headers: dict | None = None,
                          timeout: float = 60.0,
                          proxy: str | None = None) -> str:
    """把 HTTP .torrent 文件下载到本地，返回本地路径。"""
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    data = _fetch_url(url, headers=headers, timeout=timeout, proxy=proxy)
    # 从 URL 取文件名
    parsed = urllib.request.urlparse(url)
    name = os.path.basename(parsed.path) or "download.torrent"
    name = re.sub(r"\?.*$", "", name)
    if not name.lower().endswith(".torrent"):
        name += ".torrent"
    local_path = os.path.join(out_dir, name)
    # 避免覆盖：加数字后缀
    local_path = _unique_path(local_path)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def magnet_to_torrent(magnet: str, out_dir: str | None = None,
                      trackers: list[str] | None = None,
                      timeout: float = 300.0,
                      proxy: str | None = None) -> str | None:
    """
    用 aria2c --bt-metadata-only=true 把磁力链转成 .torrent 种子文件。
    成功返回种子路径；失败返回 None。
    这比直接下载更稳，因为拿到 metadata 后可用 bt-load-saved-metadata 续传。
    """
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="xzdm_magnet_", dir=out_dir)

    cmd = [
        _aria2c_exe(),
        "--no-conf",
        "--continue=false",
        "--bt-metadata-only=true",
        "--enable-dht=true",
        "--enable-dht6=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        f"--dht-file-path={os.path.join(out_dir, 'aria2_dht.dat')}",
        f"--dht-file-path6={os.path.join(out_dir, 'aria2_dht6.dat')}",
        "--bt-save-metadata=true",
        "--seed-time=0",
        "--bt-stop-timeout=120",
        "--bt-max-peers=128",
        "--summary-interval=5",
        "--file-allocation=none",
        f"--dir={tmp_dir}",
        "--console-log-level=warn",
    ]
    if trackers:
        cmd.append("--bt-tracker=" + ",".join(trackers[:50]))
    if proxy:
        cmd.append(f"--all-proxy={proxy}")
    cmd.append(magnet)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            text=True, encoding="utf-8", errors="replace")
        t0 = time.time()
        while proc.poll() is None:
            if time.time() - t0 > timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                break
            time.sleep(0.5)
        rc = proc.poll()
        # aria2c metadata-only 成功后会保存 .torrent 文件到 tmp_dir
        torrents = []
        for root, _, files in os.walk(tmp_dir):
            for fn in files:
                if fn.lower().endswith(".torrent"):
                    torrents.append(os.path.join(root, fn))
        if torrents:
            # 按修改时间取最新的
            torrents.sort(key=os.path.getmtime, reverse=True)
            final = os.path.join(out_dir, os.path.basename(torrents[0]))
            final = _unique_path(final)
            shutil.move(torrents[0], final)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return final
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def normalize_bt_input(url_or_path: str,
                       cache_dir: str | None = None,
                       trackers: list[str] | None = None,
                       headers: dict | None = None,
                       proxy: str | None = None,
                       magnet_timeout: float = 300.0) -> dict[str, Any]:
    """
    统一规范化 BT 输入，返回字典：
    {
        "kind": "magnet" | "torrent",
        "url": 实际传给 aria2c 的 URL/路径,
        "info": torrent_parser.parse 结果（种子时）,
        "converted": bool,  # 磁力是否被转成种子
        "source": 原始输入,
    }
    """
    raw = (url_or_path or "").strip()
    if not raw:
        raise ValueError("BT 输入为空")

    cache_dir = cache_dir or tempfile.gettempdir()
    os.makedirs(cache_dir, exist_ok=True)

    # 1. 本地种子文件
    if is_torrent_path(raw):
        info = _parse_torrent(raw)
        return {
            "kind": "torrent",
            "url": raw,
            "info": info,
            "converted": False,
            "source": raw,
        }

    # 2. HTTP 种子链接 -> 下载到本地
    if is_torrent_url(raw):
        local = download_torrent_file(raw, out_dir=cache_dir, headers=headers,
                                      timeout=120.0, proxy=proxy)
        info = _parse_torrent(local)
        return {
            "kind": "torrent",
            "url": local,
            "info": info,
            "converted": False,
            "source": raw,
        }

    # 3. 磁力链 -> 尝试 metadata-only 转种子
    if is_magnet(raw):
        converted = magnet_to_torrent(raw, out_dir=cache_dir, trackers=trackers,
                                      timeout=magnet_timeout, proxy=proxy)
        if converted and os.path.exists(converted):
            info = _parse_torrent(converted)
            return {
                "kind": "torrent",
                "url": converted,
                "info": info,
                "converted": True,
                "source": raw,
            }
        # 转换失败也允许回退到直接用磁力链下载（aria2c 自己处理）
        return {
            "kind": "magnet",
            "url": raw,
            "info": None,
            "converted": False,
            "source": raw,
        }

    # 4. 未知：可能是本地相对路径或错误输入
    if os.path.isfile(raw):
        info = _parse_torrent(raw)
        return {
            "kind": "torrent",
            "url": raw,
            "info": info,
            "converted": False,
            "source": raw,
        }

    raise ValueError(f"无法识别的 BT 输入：{raw[:120]}")


def recommend_protocols(info: dict | None) -> list[str]:
    """根据种子/磁力信息推荐启用的协议。"""
    recs = ["DHT", "PEX", "LPD"]
    if info and info.get("trackers"):
        recs.append("Tracker")
    else:
        recs.append("DHT（Tracker 为空，将主要依靠 DHT/PEX）")
    return recs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python bt_helper.py <magnet|torrent-url|torrent-file>")
        raise SystemExit(1)
    sample = sys.argv[1]
    print("输入:", sample)
    result = normalize_bt_input(sample)
    print("kind     :", result["kind"])
    print("converted:", result["converted"])
    print("url      :", result["url"])
    print("recommend:", ", ".join(recommend_protocols(result["info"])))
    if result["info"]:
        print("info:")
        for k, v in result["info"].items():
            if k != "files":
                print(f"  {k}: {v}")
