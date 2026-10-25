#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo")
import localserver

exe = r"C:\Program Files\XZDM\小助下载器.exe"
data = os.path.expandvars(r"%APPDATA%\小助下载器")

# 清理旧 pid
pid_file = os.path.join(data, "gui.pid")
if os.path.exists(pid_file):
    try:
        with open(pid_file) as f:
            old = int(f.read().strip())
        os.system(f"taskkill /F /PID {old} >nul 2>&1")
    except Exception:
        pass

proc = subprocess.Popen([exe, "--silent"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
print(f"started pid={proc.pid}")

for i in range(20):
    time.sleep(1)
    try:
        with open(os.path.join(data, "gui.pid")) as f:
            gui_pid = f.read().strip()
    except Exception:
        gui_pid = "?"
    try:
        with open(os.path.join(data, "api.port")) as f:
            port = int(f.read().strip())
    except Exception:
        port = None
    print(f"[{i+1}s] gui_pid={gui_pid} port={port}", flush=True)
    if port:
        try:
            url = f"http://127.0.0.1:{port}/ping"
            resp = urllib.request.urlopen(url, timeout=2).read().decode("utf-8", errors="replace")
            print(f"  /ping -> {resp}")
        except Exception as e:
            print(f"  /ping error: {e}")

print("killing...")
proc.terminate()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()
