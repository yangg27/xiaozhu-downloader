#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/range_server.py · 支持 HTTP Range 的测试服务器（v3.1.8 新增）
=================================================================
背景：标准库 http.server **完全不支持 Range**（实测 `'Range' in
inspect.getsource(http.server)` 为 False）。此前 e2e / 全机测试用的都是
http.server，导致探测阶段只能拿到 status=200 → range_ok=False →
任务走 `_single_stream_loop()` 单流降级，**多线程分片核心路径从未被覆盖**。

本服务器实现：
  · 206 Partial Content + Content-Range（多线程分片必需）
  · HEAD / GET
  · Content-Length / Content-Disposition（测文件名解析）
  · 可配置限速（测限速器）
  · 可配置 429 限流（测退避重试）
  · 可配置"忽略 Range"（测单流兜底）
  · m3u8 / m4s 分片（测 HLS）
  · 可配置断连（测续传）

用法（测试内嵌线程启动）：
    srv = RangeServer(files={"a.bin": b"..."} )
    srv.start()
    url = srv.url("a.bin")
    ...
    srv.stop()
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class _GlobalBucket:
    """全局令牌桶：所有连接共享，用于让整个下载耗时可控。

    注意：`rate_bps` 是**每请求**限速，24 个并发分片会叠加成 24 倍带宽，
    因此测均衡/插队这类"需要下载持续足够久"的场景必须用本类的全局限速。
    """

    def __init__(self, rate_bps: int = 0, burst: int = 256 * 1024):
        self.rate = rate_bps
        self.burst = max(burst, rate_bps // 4 if rate_bps else burst)
        self._tokens = float(self.burst)
        self._last = time.time()
        self._lock = threading.Lock()

    def set_rate(self, rate_bps: int):
        with self._lock:
            self.rate = rate_bps
            self.burst = max(256 * 1024, rate_bps // 4 if rate_bps else 256 * 1024)
            self._tokens = min(self._tokens, float(self.burst))

    def consume(self, n: int):
        """阻塞直到取到 n 个令牌（rate<=0 直接返回）。"""
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(float(self.burst),
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                need = (n - self._tokens) / float(self.rate)
            time.sleep(max(0.002, min(need, 0.2)))


class RangeServer:
    """可编程的测试文件服务器，支持 Range 分片。"""

    def __init__(self, files: dict | None = None, *, chunk_delay: float = 0.0,
                 rate_bps: int = 0, global_rate_bps: int = 0,
                 ignore_range: bool = False,
                 rate_limit_status: int = 0, rate_limit_count: int = 0,
                 drop_after: int = 0,
                 chunk_size: int = 64 * 1024, host: str = "127.0.0.1", port: int = 0):
        """
        files            : {路径: 字节内容 或 (字节内容, headers字典)}
        chunk_delay      : 每个数据块送出后的 sleep（模拟慢速）
        rate_bps         : **每请求**限速（字节/秒），0 不限
        global_rate_bps  : **全局**限速（所有连接共享令牌桶），0 不限
        ignore_range     : True 时忽略 Range 头，始终返回 200 全量（测单流兜底）
        rate_limit_status: 限流时返回的**状态码**（默认取 429）
        rate_limit_count : 前 N 次请求返回限流码（测退避重试）；只给 status
                           不给 count 时，默认限流 3 次
        drop_after       : >0 时，送出 N 字节后直接断开连接（测断点续传）
        """
        self.files: dict[str, bytes] = {}
        self.headers_map: dict[str, dict] = {}
        for path, val in (files or {}).items():
            if isinstance(val, tuple):
                self.files[path] = val[0]
                self.headers_map[path] = val[1]
            else:
                self.files[path] = val
        self.chunk_delay = chunk_delay
        self.rate_bps = rate_bps
        self.bucket = _GlobalBucket(global_rate_bps)
        self.ignore_range = ignore_range
        self.rate_limit_status = rate_limit_status or 429
        self.drop_after = drop_after
        self.chunk_size = chunk_size
        self.host = host
        self._port = port

        # 运行期统计
        self.stats = {
            "requests": 0,
            "range_requests": 0,
            "full_requests": 0,
            "bytes_sent": 0,
            "by_status": {},
            "range_headers": [],
        }
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._rl_left = rate_limit_count or (3 if rate_limit_status else 0)
        self._drop_left = drop_after

    # ---------- 生命周期 ----------
    def start(self) -> "RangeServer":
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静音
                pass

            def _hit(self, status: int):
                with srv._lock:
                    srv.stats["requests"] += 1
                    srv.stats["by_status"][status] = srv.stats["by_status"].get(status, 0) + 1

            def _resolve(self):
                path = self.path.split("?", 1)[0]
                return srv.files.get(path), srv.headers_map.get(path, {})

            def _pre_checks(self):
                """返回 True 表示已处理（限流/忽略），调用方应直接返回。"""
                with srv._lock:
                    if srv._rl_left > 0:
                        srv._rl_left -= 1
                        do = True
                    else:
                        do = False
                if do:
                    code = srv.rate_limit_status or 429
                    self._hit(code)
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    return True
                return False

            def do_HEAD(self):
                data, extra = self._resolve()
                if data is None:
                    self._hit(404)
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._hit(200)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()

            def do_GET(self):
                if self._pre_checks():
                    return
                data, extra = self._resolve()
                if data is None:
                    self._hit(404)
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                total = len(data)
                rng = self.headers.get("Range")
                # 统计
                if rng and not srv.ignore_range:
                    with srv._lock:
                        srv.stats["range_requests"] += 1
                        if len(srv.stats["range_headers"]) < 200:
                            srv.stats["range_headers"].append(rng)
                else:
                    with srv._lock:
                        srv.stats["full_requests"] += 1

                if rng and not srv.ignore_range:
                    m = _RANGE_RE.match(rng.strip())
                    if not m:
                        self._hit(400)
                        self.send_response(400)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    s_s, e_s = m.group(1), m.group(2)
                    if s_s == "" and e_s:
                        # suffix-range: bytes=-N
                        n = int(e_s)
                        start = max(0, total - n)
                        end = total - 1
                    else:
                        start = int(s_s) if s_s else 0
                        end = int(e_s) if e_s else total - 1
                    if start >= total:
                        self._hit(416)
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, total - 1)
                    body = data[start:end + 1]
                    self._hit(206)
                    self.send_response(206)
                    self.send_header("Content-Type", extra.get("Content-Type",
                                                                "application/octet-stream"))
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Accept-Ranges", "bytes")
                    for k, v in extra.items():
                        if k != "Content-Type":
                            self.send_header(k, v)
                    self.end_headers()
                    self._stream(body)
                else:
                    self._hit(200)
                    self.send_response(200)
                    self.send_header("Content-Type", extra.get("Content-Type",
                                                                "application/octet-stream"))
                    self.send_header("Content-Length", str(total))
                    self.send_header("Accept-Ranges",
                                     "none" if srv.ignore_range else "bytes")
                    for k, v in extra.items():
                        if k != "Content-Type":
                            self.send_header(k, v)
                    self.end_headers()
                    self._stream(data)

            def _stream(self, body: bytes):
                sent = 0
                t0 = time.time()
                try:
                    for i in range(0, len(body), srv.chunk_size):
                        chunk = body[i:i + srv.chunk_size]
                        # 全局限速：所有连接共享令牌桶（先扣令牌再发送）
                        if srv.bucket.rate > 0:
                            srv.bucket.consume(len(chunk))
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        sent += len(chunk)
                        with srv._lock:
                            srv.stats["bytes_sent"] += len(chunk)
                        # 断连模拟：送出 N 字节后直接断开
                        with srv._lock:
                            if srv._drop_left > 0 and sent >= srv._drop_left:
                                srv._drop_left = 0
                                raise ConnectionAbortedError("drop_after")
                        if srv.rate_bps > 0:
                            expect = sent / srv.rate_bps
                            actual = time.time() - t0
                            if expect > actual:
                                time.sleep(min(expect - actual, 0.5))
                        elif srv.chunk_delay:
                            time.sleep(srv.chunk_delay)
                except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                    try:
                        self.close_connection = True
                    except Exception:
                        pass

        self._httpd = ThreadingHTTPServer((self.host, self._port), Handler)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="range-server", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass
        try:
            if self._thread:
                self._thread.join(timeout=3)
        except Exception:
            pass

    @property
    def port(self) -> int:
        return self._port

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{self.host}:{self._port}{path}"

    def reset_stats(self):
        with self._lock:
            self.stats = {"requests": 0, "range_requests": 0, "full_requests": 0,
                          "bytes_sent": 0, "by_status": {}, "range_headers": []}

    def set_rate_limit(self, n: int):
        """让接下来 n 次请求返回 429。"""
        with self._lock:
            self._rl_left = n

    def set_global_rate(self, rate_bps: int):
        """运行时调整全局限速。"""
        self.bucket.set_rate(rate_bps)

    def set_drop_after(self, n: int):
        with self._lock:
            self._drop_left = n

    # ---------- 便捷构造 ----------
    @staticmethod
    def aes128_cbc_encrypt(plain: bytes, key: bytes, iv: bytes,
                           pad: bool = True) -> bytes:
        """纯 Python AES-128-CBC 加密（零依赖，仅供测试构造加密分片）。

        cryptography 装不上时也能跑 AES-128 的 HLS 用例。
        实现取自 FIPS-197 标准流程，与本项目 dlcore 的纯 Python 解密
        实现互为逆运算（两侧均通过官方测试向量核对）。
        """
        SBOX = [
            0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
            0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
            0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
            0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
            0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
            0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
            0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
            0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
            0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
            0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
            0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
            0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
            0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
            0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
            0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
            0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
        ]
        RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]

        def xtime(a):
            a <<= 1
            if a & 0x100:
                a ^= 0x11b
            return a & 0xff

        def mul(a, b):
            r = 0
            while b:
                if b & 1:
                    r ^= a
                a = xtime(a)
                b >>= 1
            return r

        # 密钥扩展
        nk, nr = 4, 10
        w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
        for i in range(nk, 4 * (nr + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [SBOX[b] for b in t]
                t[0] ^= RCON[i // nk - 1]
            w.append([w[i - nk][j] ^ t[j] for j in range(4)])

        def add_round_key(s, rnd):
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= w[rnd * 4 + c][r]

        def sub_bytes(s):
            for r in range(4):
                for c in range(4):
                    s[r][c] = SBOX[s[r][c]]

        def shift_rows(s):
            for r in range(1, 4):
                s[r] = s[r][r:] + s[r][:r]

        def mix_columns(s):
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = mul(a[0], 2) ^ mul(a[1], 3) ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ mul(a[1], 2) ^ mul(a[2], 3) ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ mul(a[2], 2) ^ mul(a[3], 3)
                s[3][c] = mul(a[0], 3) ^ a[1] ^ a[2] ^ mul(a[3], 2)

        def encrypt_block(block):
            s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
            add_round_key(s, 0)
            for rnd in range(1, nr):
                sub_bytes(s)
                shift_rows(s)
                mix_columns(s)
                add_round_key(s, rnd)
            sub_bytes(s)
            shift_rows(s)
            add_round_key(s, nr)
            return bytes(s[r][c] for c in range(4) for r in range(4))

        data = bytearray(plain)
        if pad:
            p = 16 - (len(data) % 16)
            data += bytes([p]) * p
        elif len(data) % 16:
            raise ValueError("明文长度必须是 16 的倍数（pad=False）")
        out = bytearray()
        prev = bytes(iv)
        for i in range(0, len(data), 16):
            blk = bytes(x ^ y for x, y in zip(data[i:i + 16], prev))
            enc = encrypt_block(blk)
            out += enc
            prev = enc
        return bytes(out)

    @staticmethod
    def make_payload(size: int, seed: int = 20261026) -> bytes:
        """生成可复现的伪随机数据（用于校验完整性）。"""
        import random
        rnd = random.Random(seed)
        return bytes(rnd.getrandbits(8) for _ in range(size)) if size <= 1 << 20 else \
            RangeServer._fast_payload(size, seed)

    @staticmethod
    def _fast_payload(size: int, seed: int) -> bytes:
        """大文件用哈希链快速填充，避免纯 Python 逐字节慢。"""
        out = bytearray()
        h = hashlib.sha256(str(seed).encode()).digest()
        while len(out) < size:
            h = hashlib.sha256(h).digest()
            out += h
        return bytes(out[:size])

    @staticmethod
    def make_m3u8(segments: list[bytes], *, key: bytes | None = None,
                  iv: bytes | None = None, content_type: str = "application/vnd.apple.mpegurl",
                  init: bytes | None = None) -> tuple:
        """生成 HLS 播放列表 + 分片体，返回 (playlist_bytes, {path: body})。"""
        files = {}
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
                 "#EXT-X-MEDIA-SEQUENCE:0"]
        if init is not None:
            files["/init.mp4"] = init
            lines.append('#EXT-X-MAP:URI="init.mp4"')
        if key is not None:
            files["/enc.key"] = key
            ivhex = (iv or b"\x00" * 16).hex()
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x{ivhex}')
        for i, seg in enumerate(segments):
            files[f"/seg{i:05d}.ts"] = seg
            lines.append("#EXTINF:4.000,")
            lines.append(f"seg{i:05d}.ts")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode(), files


if __name__ == "__main__":
    # 自测：起服务并验证 206
    payload = RangeServer.make_payload(3 * 1024 * 1024)
    srv = RangeServer({"/data.bin": payload}).start()
    import urllib.request
    req = urllib.request.Request(srv.url("/data.bin"), headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req) as r:
        print("status:", r.status, "len:", len(r.read()),
              "Content-Range:", r.headers.get("Content-Range"))
    print("stats:", srv.stats)
    print("md5 一致:",
          hashlib.md5(urllib.request.urlopen(srv.url("/data.bin")).read()).hexdigest()
          == hashlib.md5(payload).hexdigest())
    srv.stop()