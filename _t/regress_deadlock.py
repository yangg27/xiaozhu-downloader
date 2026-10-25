# -*- coding: utf-8 -*-
"""死锁回归：多任务并行下载时，/status 等 snapshots() 调用是否仍会挂起。

修复前的实测行为（源码与 exe 都复现）：
  · /ping  3-21ms   正常
  · /status 100% 超时（>25s 无响应）
  根因：set_target_conns 持 _target_lock 调 _spawn_workers → 同线程自锁，
  连带永不释放 Manager._lock → 所有 snapshots() 挂起。
"""
import faulthandler
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18803
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sandbox = tempfile.mkdtemp(prefix="xzd_dl_")
data_dir = os.path.join(sandbox, "data")
dl_dir = os.path.join(sandbox, "DL")
os.makedirs(dl_dir, exist_ok=True)

import http.server
import socketserver

PAYLOAD = bytes(range(256)) * 8192
SRV_PORT = 19011


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


srv = socketserver.ThreadingTCPServer(("127.0.0.1", SRV_PORT), H)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

PY = r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python313\python.exe"
env2 = dict(os.environ)
env2["XZD_API_PORT"] = str(PORT)
env2["XZD_DATA_DIR"] = data_dir
logpath = os.path.join(sandbox, "out.txt")
logf = open(logpath, "wb")
p = subprocess.Popen([PY, "-u", os.path.join(ROOT, "gui.py"), "--silent"],
                     env=env2, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)

base = "http://127.0.0.1:%d" % PORT
token = ""
for _ in range(60):
    time.sleep(0.5)
    try:
        with urllib.request.urlopen(base + "/ping", timeout=1.5) as r:
            token = json.loads(r.read().decode()).get("token", "")
        break
    except Exception:
        continue
print("就绪 pid=%d" % p.pid, flush=True)

HD = {"Content-Type": "application/json", "X-XZD-Token": token}
print("加 6 个任务（各 24 线程 → 必然触发速度均衡调线程）…", flush=True)
for i in range(6):
    body = json.dumps({"url": "http://127.0.0.1:%d/f%d.bin" % (SRV_PORT, i),
                       "out_dir": dl_dir, "filename": "f%d.bin" % i,
                       "conns": 24}).encode()
    req = urllib.request.Request(base + "/add", data=body, headers=HD)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print("  add%d 失败: %s" % (i, e), flush=True)

time.sleep(1.0)


def timed(path, timeout=8):
    t0 = time.time()
    try:
        req = urllib.request.Request(base + path, headers={"X-XZD-Token": token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return True, (time.time() - t0) * 1000, body
    except Exception as e:
        return False, (time.time() - t0) * 1000, str(e)


print("\n【关键验证】下载期间接口响应（修复前 /status 必挂）", flush=True)
ok_n = fail_n = 0
worst = 0
for i in range(15):
    ok, ms, body = timed("/status")
    if ok:
        ok_n += 1
        worst = max(worst, ms)
    else:
        fail_n += 1
        print("  #%02d ★ 失败 %.0fms %s" % (i, ms, body), flush=True)
    time.sleep(0.6)
print("  /status: 成功 %d / 失败 %d   最慢 %.0fms" % (ok_n, fail_n, worst), flush=True)

print("\n对照 /ping：", flush=True)
ok, ms, _ = timed("/ping", timeout=5)
print("  ping ok=%s %.0fms" % (ok, ms), flush=True)

print("\n/history：", flush=True)
ok, ms, _ = timed("/history?limit=10", timeout=8)
print("  history ok=%s %.0fms" % (ok, ms), flush=True)

# 等任务跑完
print("\n等任务完成…", flush=True)
for _ in range(80):
    time.sleep(0.5)
    ok, ms, body = timed("/status", timeout=5)
    if ok:
        try:
            js = json.loads(body)
            if not js.get("tasks"):
                break
        except Exception:
            pass

ok, ms, body = timed("/status", timeout=5)
print("  最终 /status ok=%s %.0fms" % (ok, ms), flush=True)
if ok:
    js = json.loads(body)
    print("  活动任务 =", len(js.get("tasks", [])), flush=True)

# history 完整性
ok, ms, body = timed("/history?limit=20", timeout=8)
if ok:
    items = json.loads(body).get("items", [])
    print("\n/history 条数 = %d  全部 exists = %s" % (
        len(items), all(x.get("exists") for x in items)), flush=True)

print("\n进程存活 =", p.poll() is None, flush=True)
logf.flush()
content = open(logpath, "rb").read().decode("utf-8", "replace")
print("日志含 TclError =", "TclError" in content, flush=True)
if "TclError" in content or "Traceback" in content:
    print("---- 日志尾部 ----", flush=True)
    print(content[-1500:], flush=True)

jobs = os.path.join(data_dir, "jobs.json")
if os.path.exists(jobs):
    with open(jobs, encoding="utf-8") as f:
        d = json.load(f)
    ts = [x["tid"] for x in d]
    print("jobs.json 条数 = %d 有重复 = %s" % (len(d), len(ts) != len(set(ts))), flush=True)

result = (fail_n == 0 and p.poll() is None and "TclError" not in content)
print("\n" + ("★ 死锁已修复：接口全程可用" if result else "★ 仍有问题"), flush=True)

try:
    p.kill()
except Exception:
    pass
srv.shutdown()
sys.exit(0 if result else 1)