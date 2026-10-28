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
  GET  /status                      活动任务概览（**不含已完成/已取消**）
  GET  /history?limit=N             已完成下载记录（含文件真实路径与是否还在）
  POST /open    {tid, mode}         打开已完成文件（file）或其所在目录（folder）
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dlcore

MAX_BODY = 2 * 1024 * 1024


class LocalAPI:
    def __init__(self, mgr: dlcore.Manager, port: int = 8763, token: str = "",
                 on_wake=None, on_help=None):
        self.mgr = mgr
        self.port = int(port)
        self.token = token or secrets.token_hex(16)
        self._httpd = None
        self._thread = None
        self.actual_port = self.port
        # 协议唤起回调：接收 xzd:// 链接，交给 GUI 主线程处理
        # （单实例保护的关键——已在运行时由本回调接管，不再启动新进程）
        self.on_wake = on_wake
        # 帮助页面回调：安装器或扩展请求打开主窗口并显示帮助
        self.on_help = on_help

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
            def _allowed_origin(self) -> str:
                """返回严格校验后的 Origin；拒绝伪装扩展域名、端口和用户信息。"""
                origin = (self.headers.get("Origin") or "").strip()
                if not origin:
                    return "*"
                try:
                    parsed = urllib.parse.urlsplit(origin)
                    host = parsed.hostname or ""
                    if (parsed.scheme in ("chrome-extension", "moz-extension",
                                         "extension", "safari-web-extension")
                            and parsed.netloc and not parsed.username and not parsed.password
                            and parsed.path in ("", "/") and not parsed.query
                            and not parsed.fragment and parsed.port is None):
                        return origin
                    if (parsed.scheme in ("http", "https")
                            and host in ("127.0.0.1", "localhost", "::1")
                            and not parsed.username and not parsed.password
                            and parsed.path in ("", "/") and not parsed.query
                            and not parsed.fragment):
                        return origin
                except ValueError:
                    pass
                return ""

            def _origin_ok(self) -> bool:
                return bool(self._allowed_origin())

            def _is_extension(self) -> bool:
                origin = self._allowed_origin()
                return origin.startswith(("chrome-extension://", "moz-extension://",
                                         "extension://", "safari-web-extension://"))

            def _json(self, obj, code: int = 200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                origin = self._allowed_origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
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
                """只接受有界 JSON 对象；坏请求由 HTTP 400 表示，不打断连接。"""
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise ValueError("Content-Length 无效")
                if n <= 0:
                    raise ValueError("请求体不能为空")
                if n > MAX_BODY:
                    raise ValueError("请求体超过 2MB 限制")
                try:
                    raw = self.rfile.read(n)
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("请求体必须是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError("JSON 根必须是对象")
                return value

            def log_message(self, *a):  # 静音，避免污染控制台
                return

            # --- 路由 ---
            def do_OPTIONS(self):
                # 预检不读取或创建任务，因此无需令牌；Origin 仍需遵守白名单。
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "origin 不允许"}, 403)
                self._json({"ok": True})

            def handle_one_request(self):
                # 浏览器探测端口或主动取消请求时，空连接/重置是正常噪声，
                # 不应让 BaseHTTPRequestHandler 打出整段看似崩溃的堆栈。
                try:
                    super().handle_one_request()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    self.close_connection = True

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
                    # ★ v3.1.9 缺陷修复：只返回**活动任务**。
                    #   旧版把已完成的也一并返回，扩展「下载中」列表里
                    #   会一直留着下完的任务，用户以为还在下载。
                    #   （GUI 侧仍展示全部，这里只影响扩展视图。）
                    active = [s for s in snap
                              if s["state"] not in ("done", "cancelled")]
                    return self._json({
                        "ok": True,
                        "tasks": [{"tid": s["tid"], "filename": s["filename"],
                                   "state": s["state"], "state_cn": s["state_cn"],
                                   "progress": s["progress"], "speed": s["speed"],
                                   "size": s["size"], "done_bytes": s["done_bytes"],
                                   "eta": s["eta"], "phase": s["phase"], "err": s["err"]}
                                  for s in active],
                        "total_speed": api.mgr.total_speed(),
                        "active_count": len(active),
                        "total_count": len(snap),
                    })
                if path == "/history":
                    if not self._token_ok():
                        return self._json({"ok": False, "error": "令牌无效"}, 401)
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    try:
                        limit = max(1, min(500, int((q.get("limit") or ["100"])[0])))
                    except (TypeError, ValueError):
                        return self._json({"ok": False, "error": "limit 必须是 1-500 的整数"}, 400)
                    items = api.mgr.history(limit=limit)
                    # 补上 out_dir / exists，扩展点「打开/目录」需要且能提前提示失效
                    out = []
                    for h in items:
                        it = dict(h)
                        fin = it.get("final") or ""
                        if not fin:
                            od = it.get("out_dir") or ""
                            fn = it.get("filename") or ""
                            if od and fn:
                                fin = os.path.join(od, fn)
                                it["final"] = fin
                        it["exists"] = bool(fin) and os.path.exists(fin)
                        out.append(it)
                    return self._json({"ok": True, "items": out})
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
                if path == "/help":
                    # 打开主窗口并显示帮助页；本机调用，无需令牌
                    if api.on_help is None:
                        return self._json({"ok": False, "error": "不支持帮助页"}, 501)
                    try:
                        api.on_help()
                        return self._json({"ok": True})
                    except Exception as e:  # noqa
                        return self._json({"ok": False,
                                           "error": f"{type(e).__name__}: {e}"}, 500)
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
                try:
                    body = self._body()
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)

                def add_one(item, page: str = "") -> dict:
                    if not isinstance(item, dict):
                        return {"ok": False, "error": "批次元素必须是对象", "item": item}
                    url = item.get("url") or ""
                    if not isinstance(url, str):
                        return {"ok": False, "error": "url 必须是字符串", "url": str(url)}
                    url = url.strip()
                    # v3.0.0: 接受 magnet: 与 .torrent（本地路径或 http 链接）
                    u_lower = url.lower()
                    is_bt = u_lower.startswith("magnet:") or u_lower.endswith(".torrent")
                    if not (url.startswith(("http://", "https://")) or is_bt):
                        return {"ok": False, "error": "URL 无效", "url": url}
                    # 本地 .torrent 文件路径：仅当真实存在时
                    if is_bt and not (u_lower.startswith("magnet:") or url.startswith(("http://","https://"))):
                        if not os.path.exists(url):
                            return {"ok": False, "error": "种子文件不存在", "url": url}
                    for key in ("referer", "cookie", "out_dir", "filename", "kind"):
                        if not isinstance(item.get(key, ""), str):
                            return {"ok": False, "error": f"{key} 必须是字符串", "url": url}
                    headers = item.get("headers") or {}
                    if not isinstance(headers, dict) or not all(
                            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                        return {"ok": False, "error": "headers 必须是字符串键值对象", "url": url}
                    if not isinstance(page, str):
                        return {"ok": False, "error": "page 必须是字符串", "url": url}
                    referer = item.get("referer") or page or ""
                    conns = item.get("conns")
                    if conns is not None and (isinstance(conns, bool) or not isinstance(conns, int)):
                        return {"ok": False, "error": "conns 必须是整数", "url": url}
                    try:
                        t = api.mgr.add_task(
                            url, out_dir=item.get("out_dir") or None,
                            filename=item.get("filename") or "",
                            referer=referer, cookie=item.get("cookie") or "",
                            headers=headers, kind=item.get("kind") or "auto", conns=conns)
                        return {"ok": True, "tid": t.tid, "filename": t.filename,
                                "kind": t.kind, "url": url}
                    except Exception as e:  # noqa
                        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}

                if path == "/add":
                    r = add_one(body)
                    return self._json(r, 200 if r.get("ok") else 400)
                if path == "/batch":
                    page = body.get("page") or ""
                    items = body.get("items")
                    if not isinstance(page, str):
                        return self._json({"ok": False, "error": "page 必须是字符串"}, 400)
                    if not isinstance(items, list):
                        return self._json({"ok": False, "error": "items 必须是数组"}, 400)
                    if not items:
                        return self._json({"ok": False, "error": "批次不能为空"}, 400)
                    if len(items) > 100:
                        return self._json({"ok": False, "error": "单批次最多 100 项，请拆分提交"}, 400)
                    results = [add_one(it, page) for it in items]
                    api.mgr.add_sniff_items(page, [{"url": it.get("url", ""),
                                                    "filename": it.get("filename", "")}
                                                   for it in items if isinstance(it, dict) and it.get("url")])
                    return self._json({"ok": True, "results": results})
                if path == "/sniff":
                    items = body.get("items")
                    page = body.get("page") or ""
                    if not isinstance(page, str) or not isinstance(items, list):
                        return self._json({"ok": False, "error": "page 必须是字符串，items 必须是数组"}, 400)
                    if not items:
                        return self._json({"ok": False, "error": "嗅探批次不能为空"}, 400)
                    if len(items) > 100:
                        return self._json({"ok": False, "error": "单批次最多 100 项，请拆分提交"}, 400)
                    if any(not isinstance(it, dict) for it in items):
                        return self._json({"ok": False, "error": "嗅探元素必须是对象"}, 400)
                    if any(not isinstance(it.get("url", ""), str) or
                           not isinstance(it.get("filename", ""), str) for it in items):
                        return self._json({"ok": False, "error": "url/filename 必须是字符串"}, 400)
                    n = api.mgr.add_sniff_items(page, items)
                    return self._json({"ok": True, "added": n})
                if path == "/open":
                    tid = body.get("tid") or ""
                    mode = body.get("mode") or "file"
                    if not isinstance(tid, str) or mode not in ("file", "folder"):
                        return self._json({"ok": False, "error": "tid/mode 参数无效"}, 400)
                    target = ""
                    task = api.mgr.get(tid)
                    if task:
                        target = task.created_final or task.final_path()
                    else:
                        # ★ v3.1.9 缺陷修复：任务可能已不在活动列表里
                        #   （软件重启后活动列表清空、或用户清理过任务），
                        #   此时必须回历史记录里找，否则扩展「已完成」列表
                        #   点「打开 / 目录」必然 404「任务不存在」——
                        #   用户看到的就是"点了没反应"。
                        for h in reversed(api.mgr.history(limit=500)):
                            if h.get("tid") == tid:
                                target = h.get("final") or ""
                                if not target:
                                    od = h.get("out_dir") or ""
                                    fn = h.get("filename") or ""
                                    if od and fn:
                                        target = os.path.join(od, fn)
                                break
                    if not target:
                        return self._json({"ok": False,
                                           "error": "找不到该任务的下载文件记录"}, 404)
                    exists = os.path.exists(target)
                    is_directory = os.path.isdir(target)
                    folder = target if is_directory else os.path.dirname(target)
                    try:
                        if mode == "folder":
                            # 文件已不在但目录还在 → 仍然打开目录（比报错有用）
                            if exists:
                                cmd = (["explorer", os.path.normpath(target)] if is_directory else
                                       ["explorer", "/select,", os.path.normpath(target)])
                                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
                                return self._json({"ok": True, "opened": folder})
                            if os.path.isdir(folder):
                                subprocess.Popen(
                                    ["explorer", os.path.normpath(folder)],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                                return self._json({"ok": True, "opened": folder,
                                                   "note": "文件已不在，已打开所在目录"})
                            return self._json({"ok": False,
                                               "error": "文件与所在目录都不存在"}, 404)
                        else:
                            if not exists:
                                return self._json(
                                    {"ok": False,
                                     "error": "文件不存在（可能已被移动或删除）"}, 404)
                            os.startfile(target)  # noqa
                            return self._json({"ok": True, "opened": target})
                    except Exception as e:
                        return self._json({"ok": False,
                                           "error": f"{type(e).__name__}: {e}"}, 500)
                if path == "/history/delete":
                    tid = (body or {}).get("tid", "").strip()
                    if not tid:
                        return self._json({"ok": False, "error": "缺少 tid 参数"}, 400)
                    found = api.mgr.delete_history(tid)
                    return self._json({"ok": True, "deleted": found})
                if path == "/history/clear":
                    n = api.mgr.clear_history()
                    return self._json({"ok": True, "cleared": n})
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
