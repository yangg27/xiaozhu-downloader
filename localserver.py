#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 本地接口  localserver.py
=====================================
浏览器扩展（嗅探到媒体后）通过本接口把链接推送给下载器，同时提供"分析网页"能力。

安全设计：
  · 只监听 127.0.0.1（外部网络无法访问）
  · 每次启动生成随机令牌（写入 data/token.txt），扩展需带令牌才能调用
  · 严格校验 Origin / 来源，只允许浏览器扩展与本机页面
  · 不访问互联网、不上报任何数据

接口一览（全部 HTTP，JSON）：
  GET  /ping                        健康检查，返回令牌与版本
  POST /add     {url, referer, filename, cookie, kind, headers}
  POST /batch   {items:[{url,referer,...}], page}
  GET  /scan?url=...                分析网页媒体（由下载器代为请求）
  GET  /sniff?page=...              取最新嗅探结果（扩展轮询）
  GET  /status                      活动任务概览
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dlcore

MAX_BODY = 2 * 1024 * 1024


class LocalAPI:
    def __init__(self, mgr: dlcore.Manager, port: int = 8763, token: str = "",
                 on_wake=None):
        self.mgr = mgr
        self.port = int(port)
        self.token = token or secrets.token_hex(16)
        self._httpd = None
        self._thread = None
        self.actual_port = self.port
        # 协议唤起回调：接收 xzd:// 链接，交给 GUI 主线程处理
        # （单实例保护的关键——已在运行时由本回调接管，不再启动新进程）
        self.on_wake = on_wake

    # ---- 令牌 ----
    @property
    def token_path(self) -> str:
        return os.path.join(self.mgr.data_dir, "token.txt")

    def load_token(self):
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    self.token = t
        except Exception:
            self.save_token()

    def save_token(self):
        try:
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(self.token)
        except Exception:
            dlcore.LOG.debug('忽略异常：with open(token_path, "w", encoding="utf-8") as f: f.write(...')

    # ---- 生命周期 ----
    def start(self) -> bool:
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "xiaozhu-dl/1.0"

            # --- 工具 ---
            def _origin_ok(self) -> bool:
                o = (self.headers.get("Origin") or "").strip()
                if not o:
                    return True  # 本地进程（curl/脚本）无 Origin
                if o.startswith(("chrome-extension://", "moz-extension://",
                                 "extension://", "safari-web-extension://")):
                    return True
                host = urllib.parse.urlsplit(o).hostname or ""
                return host in ("127.0.0.1", "localhost", "::1")

            def _is_extension(self) -> bool:
                o = (self.headers.get("Origin") or "").strip()
                return o.startswith(("chrome-extension://", "moz-extension://",
                                     "extension://", "safari-web-extension://"))

            def _json(self, obj, code: int = 200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-XZD-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    dlcore.LOG.debug('忽略异常：wfile.write(body)')

            def _token_ok(self) -> bool:
                t = (self.headers.get("X-XZD-Token") or "").strip()
                if not t:
                    q = urllib.parse.urlsplit(self.path).query
                    t = (urllib.parse.parse_qs(q).get("token") or [""])[0]
                return bool(t) and hmac.compare_digest(t, api.token)

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > MAX_BODY:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8", "replace")) or {}
                except Exception:
                    return {}

            def log_message(self, *a):  # 静音，避免污染控制台
                return

            # --- 路由 ---
            def do_OPTIONS(self):
                self._json({"ok": True})

            def do_GET(self):
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "origin 不允许"}, 403)
                path = urllib.parse.urlsplit(self.path).path
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if path == "/ping":
                    # 安全：令牌只发给浏览器扩展或本机进程，
                    # 网页（Origin 为 http/https）拿不到令牌，防止任意网站往里塞下载任务
                    info = {"ok": True, "app": dlcore.APP_NAME,
                            "version": dlcore.ENGINE_VERSION,
                            "port": api.actual_port}
                    if self._is_extension() or not self.headers.get("Origin"):
                        info["token"] = api.token
                    return self._json(info)
                if path == "/status":
                    if not self._token_ok():
                        return self._json({"ok": False, "error": "令牌无效"}, 401)
                    snap = api.mgr.snapshots()
                    return self._json({
                        "ok": True,
                        "tasks": [{"tid": s["tid"], "filename": s["filename"],
                                   "state": s["state"], "state_cn": s["state_cn"],
                                   "progress": s["progress"], "speed": s["speed"]}
                                  for s in snap],
                        "total_speed": api.mgr.total_speed(),
                    })
                if path == "/sniff":
                    if not self._token_ok():
                        return self._json({"ok": False, "error": "令牌无效"}, 401)
                    return self._json({"ok": True, "items": api.mgr.sniff_items})
                if path == "/scan":
                    if not self._token_ok():
                        return self._json({"ok": False, "error": "令牌无效"}, 401)
                    page = (q.get("url") or [""])[0]
                    if not page.startswith(("http://", "https://")):
                        return self._json({"ok": False, "error": "URL 无效"}, 400)
                    try:
                        res = dlcore.scan_webpage(page)
                        api.mgr.add_sniff_items(page, res.get("items", []))
                        return self._json({"ok": True, **res})
                    except Exception as e:  # noqa
                        return self._json({"ok": False,
                                           "error": f"{type(e).__name__}: {e}"}, 500)
                if path == "/":
                    return self._json({"ok": True, "app": dlcore.APP_NAME})
                if path == "/wake":
                    # 协议唤起：把 xzd:// 链接转给已运行的实例处理（单实例保护）
                    # 本机进程无 Origin，浏览器直接访问会带 http Origin —— 都放行，
                    # 但只有能提供正确令牌的调用才会真正执行（防止网页滥用）
                    link = (q.get("link") or [""])[0]
                    if not link:
                        return self._json({"ok": False, "error": "缺少 link"}, 400)
                    if not self._token_ok():
                        return self._json({"ok": False, "error": "令牌无效"}, 401)
                    if api.on_wake is None:
                        return self._json({"ok": False, "error": "不支持唤起"}, 501)
                    try:
                        api.on_wake(link)
                        return self._json({"ok": True, "handled": True})
                    except Exception as e:  # noqa
                        return self._json({"ok": False,
                                           "error": f"{type(e).__name__}: {e}"}, 500)
                return self._json({"ok": False, "error": "未知接口"}, 404)

            def do_POST(self):
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "origin 不允许"}, 403)
                if not self._token_ok():
                    return self._json({"ok": False, "error": "令牌无效"}, 401)
                path = urllib.parse.urlsplit(self.path).path
                body = self._body()

                def add_one(item: dict, page: str = "") -> dict:
                    url = (item.get("url") or "").strip()
                    # v3.0.0: 接受 magnet: 与 .torrent（本地路径或 http 链接）
                    u_lower = url.lower()
                    is_bt = u_lower.startswith("magnet:") or u_lower.endswith(".torrent")
                    if not (url.startswith(("http://", "https://")) or is_bt):
                        return {"ok": False, "error": "URL 无效", "url": url}
                    # 本地 .torrent 文件路径：仅当真实存在时
                    if is_bt and not (u_lower.startswith("magnet:") or url.startswith(("http://","https://"))):
                        if not os.path.exists(url):
                            return {"ok": False, "error": "种子文件不存在", "url": url}
                    referer = item.get("referer") or page or ""
                    headers = item.get("headers") or {}
                    if not isinstance(headers, dict):
                        headers = {}
                    try:
                        t = api.mgr.add_task(
                            url,
                            filename=(item.get("filename") or "").strip(),
                            referer=referer,
                            cookie=(item.get("cookie") or ""),
                            headers=headers,
                            kind=(item.get("kind") or "auto"),
                            conns=item.get("conns"),
                        )
                        return {"ok": True, "tid": t.tid, "filename": t.filename,
                                "kind": t.kind, "url": url}
                    except Exception as e:  # noqa
                        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}

                if path == "/add":
                    r = add_one(body)
                    return self._json(r, 200 if r.get("ok") else 400)
                if path == "/batch":
                    page = body.get("page") or ""
                    items = body.get("items") or []
                    if not isinstance(items, list):
                        return self._json({"ok": False, "error": "items 必须是数组"}, 400)
                    results = [add_one(it, page) for it in items[:100]]
                    api.mgr.add_sniff_items(page, [{"url": it.get("url", ""),
                                                    "filename": it.get("filename", "")}
                                                   for it in items[:100] if it.get("url")])
                    return self._json({"ok": True, "results": results})
                if path == "/sniff":
                    items = body.get("items") or []
                    page = body.get("page") or ""
                    n = api.mgr.add_sniff_items(page, items)
                    return self._json({"ok": True, "added": n})
                return self._json({"ok": False, "error": "未知接口"}, 404)

        # 只绑定 127.0.0.1
        if not self._bind(Handler):
            return False
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="xzd-api")
        self._thread.start()
        self.save_token()
        # 写端口提示：让下次启动（单实例探测）一次 open 就知道该探哪个端口，
        # 不必盲扫 13 个端口（本机空端口不回 RST，盲扫要熬 4.6 秒）
        try:
            import sysintegration as _si
            _si.write_port_hint(self.actual_port)
        except Exception:
            dlcore.LOG.debug('忽略异常：import sysintegration as _si')
        return True

    def _bind(self, handler_cls) -> bool:
        """在 127.0.0.1 上绑定端口；首选 self.port，被占用则顺延 11 个。

        仅监听回环地址 —— 局域网内其它机器无法访问本接口。
        """
        last_err = None
        for p in [self.port] + list(range(self.port + 1, self.port + 12)):
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", p), handler_cls)
                self.actual_port = self._httpd.server_address[1]
                return True
            except OSError as e:
                last_err = e
                continue
        dlcore.LOG.once("api_bind_fail",
                        "接口端口绑定失败（%d..%d 全被占用），浏览器扩展接口不可用：%s",
                        self.port, self.port + 11, last_err)
        return False

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            dlcore.LOG.debug('忽略异常：if _httpd: _httpd.shutdown() _httpd.server_close()')
        self._httpd = None
        # 清掉端口提示，避免下次启动拿一个死端口去探（白等一次超时）
        try:
            import sysintegration as _si
            _si.clear_port_hint()
        except Exception:
            dlcore.LOG.debug('忽略异常：import sysintegration as _si')

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.actual_port}"
