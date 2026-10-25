"""v3.1.8 frozen app 冒烟测试：直接跑打包后的 exe，验证启动 + /ping + /help。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

APP = r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo\dist_app\小助下载器\小助下载器.exe"
PORT = "18790"

env = dict(os.environ)
env["XZD_API_PORT"] = PORT
env["XZD_DATA_DIR"] = r"C:\Users\Sunny_yang\AppData\Local\Temp\xzd_smoke_v318"

proc = subprocess.Popen([APP], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print("启动 exe, pid =", proc.pid)

base = "http://127.0.0.1:" + PORT
ok_ping = ok_help = False
token = ""
for i in range(40):
    time.sleep(0.5)
    if proc.poll() is not None:
        out = proc.stdout.read().decode("utf-8", "replace")
        print("!! 进程提前退出, rc =", proc.returncode)
        print(out[-3000:])
        sys.exit(1)
    try:
        with urllib.request.urlopen(base + "/ping", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
            token = data.get("token", "")
            print("ping OK:", data)
            ok_ping = True
            break
    except Exception:
        continue

if not ok_ping:
    print("!! /ping 始终不通")
    proc.terminate()
    sys.exit(1)

# 验证 /help（帮助页唤起）
try:
    url = base + "/help" + ("?token=" + token if token else "")
    with urllib.request.urlopen(url, timeout=3) as r:
        print("help OK:", r.status, r.read().decode("utf-8")[:200])
        ok_help = True
except Exception as e:
    print("help 失败:", e)

# 验证 /add 真下载
try:
    body = json.dumps({"url": "https://www.example.com/"}).encode()
    req = urllib.request.Request(
        base + "/add" + ("?token=" + token if token else ""),
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        print("add OK:", r.status, r.read().decode("utf-8")[:200])
except Exception as e:
    print("add 失败:", e)

time.sleep(2)
alive = proc.poll() is None
print("进程存活:", alive)

proc.terminate()
try:
    proc.wait(timeout=8)
except Exception:
    proc.kill()
print("结果: ping=%s help=%s alive=%s" % (ok_ping, ok_help, alive))
sys.exit(0 if (ok_ping and ok_help and alive) else 1)