# -*- coding: utf-8 -*-
"""frozen 端到端实测：扩展「已完成」列表点「打开/目录」是否真能打开
（对着打包好的 exe 跑，不是源码）"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist_app", "小助下载器", "小助下载器.exe")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

# ★ 本机环境注入了 HTTP(S)_PROXY（公司网关），urllib 会把 127.0.0.1 的请求
#   也交给代理 → 502 / 超时。测试必须直连：清掉代理环境变量 + 装直连 Opener。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import urllib.request  # noqa: E402

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

user32 = ctypes.windll.user32
CREATE_NO_WINDOW = 0x08000000


def cabinets():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        try:
            if user32.IsWindowVisible(h):
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cls, 256)
                if cls.value in ("CabinetWClass", "ExploreWClass"):
                    out.append(h)
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def close_all():
    for h in cabinets():
        user32.PostMessageW(h, 0x0010, 0, 0)


def get(url, token="", timeout=8):
    h = {"X-XZD-Token": token} if token else {}
    r = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except Exception as e:
        return 0, {"EXC": str(e)}


def post(url, body, token="", timeout=8):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if token:
        h["X-XZD-Token"] = token
    r = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except Exception as e:
        return 0, {"EXC": str(e)}


# 启动本机 HTTP 服务器提供一个可下载文件
import http.server
import socketserver
import threading

PAYLOAD = bytes(range(256)) * 2048       # 512KB
PORT = 18911


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        data = PAYLOAD
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split("-")
            a = int(spec[0]) if spec[0] else 0
            b = int(spec[1]) if len(spec) > 1 and spec[1] else len(data) - 1
            b = min(b, len(data) - 1)
            part = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(data)))
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            self.wfile.write(part)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("测试服务器: http://127.0.0.1:%d/f.bin" % PORT, flush=True)

# 用独立 APPDATA 跑，避免污染用户真实数据
sandbox = os.path.join(os.environ.get("TEMP", "."), "xzd_frozen_%s" % uuid.uuid4().hex[:8])
os.makedirs(sandbox, exist_ok=True)
env = dict(os.environ)
env["APPDATA"] = sandbox
dl_dir = os.path.join(sandbox, "Downloads")
os.makedirs(dl_dir, exist_ok=True)

# ★ 关键：必须显式指定 API 端口与数据目录。
#   否则 exe 走默认 8763，而用户机器上真实安装的旧版正占着它 ——
#   sysintegration.handoff() 的单实例保护会把本次启动直接"转交"给旧版
#   然后自己退出，本测试便永远等不到接口。
env["XZD_API_PORT"] = "18790"
env["XZD_DATA_DIR"] = os.path.join(sandbox, "data")

print("启动 exe …", flush=True)
p = subprocess.Popen([EXE, "--silent"], env=env, cwd=os.path.dirname(EXE),
                     creationflags=CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)

# 等接口就绪
base = None
token = ""
for i in range(60):
    time.sleep(0.5)
    for port in range(18790, 18820):
        st, j = get("http://127.0.0.1:%d/ping" % port, timeout=1.5)
        if st == 200 and j.get("ok"):
            base = "http://127.0.0.1:%d" % port
            token = j.get("token", "")
            print("接口就绪: %s  版本=%s" % (base, j.get("version")), flush=True)
            break
    if base:
        break

if not base:
    print("★ 接口未就绪，测试中止", flush=True)
    p.kill()
    sys.exit(1)

close_all()
base_wins = len(cabinets())
print("基线资源管理器窗口:", base_wins, flush=True)

# 加一个任务并等完成
st, j = post(base + "/add",
             {"url": "http://127.0.0.1:%d/f.bin" % PORT, "out_dir": dl_dir,
              "filename": "frozen_test.bin"}, token)
print("\n/add ->", st, j, flush=True)
tid = j.get("tid")

for i in range(60):
    time.sleep(0.5)
    st, js = get(base + "/status", token)
    active = [x for x in js.get("tasks", [])]
    if tid not in [x["tid"] for x in active]:
        break
print("任务已离开活动列表（下载完成）", flush=True)

# /status 不应含已完成
st, js = get(base + "/status", token)
print("\n① /status 活动任务数:", len(js.get("tasks", [])),
      " total_count:", js.get("total_count"), flush=True)
print("   已完成任务是否仍残留:", "是 ★问题" if any(
    x["tid"] == tid for x in js.get("tasks", [])) else "否 ✓", flush=True)

# /history 应含它且 exists=True
st, jh = get(base + "/history?limit=20", token)
hit = [h for h in jh.get("items", []) if h.get("tid") == tid]
print("\n② /history 找到该任务:", bool(hit), flush=True)
if hit:
    print("   final =", hit[-1].get("final"), flush=True)
    print("   exists =", hit[-1].get("exists"), flush=True)

# 点「目录」
print("\n③ 点「目录」→", flush=True)
# ★ 正确做法：先关掉本轮之前的所有资源管理器窗口（close_all 已做），
#   再数窗口数 —— 只看"相对增量"会被用户原有的窗口干扰（实测基线 3 个，
#   新窗口打开后旧的被关掉，总数反而变小，误判成"未打开"）。
close_all()
time.sleep(1.0)
base_wins = len(cabinets())
print("   操作前窗口数:", base_wins, flush=True)
st, j = post(base + "/open", {"tid": tid, "mode": "folder"}, token)
print("   返回:", st, j, flush=True)
time.sleep(2.5)
wins = cabinets()
titles = []
for h in wins:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(h, buf, 512)
    titles.append(buf.value)
print("   操作后窗口数:", len(wins), " 标题:", titles, flush=True)
print("   →", "✓ 真的打开了" if len(wins) > base_wins else "✗ 未打开", flush=True)

print("\n④ 点「打开」→", flush=True)
close_all()
time.sleep(1.2)
st, j = post(base + "/open", {"tid": tid, "mode": "file"}, token)
print("   返回:", st, j, flush=True)

close_all()
try:
    p.kill()
except Exception:
    pass
srv.shutdown()
print("\n测试结束", flush=True)