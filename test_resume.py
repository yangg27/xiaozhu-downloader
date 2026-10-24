#!/usr/bin/env python3
"""测试断点续传：先下载一部分，杀掉进程，再启动看能否继续。"""
import os
import signal
import subprocess
import sys
import tempfile
import time

root = os.path.dirname(os.path.abspath(__file__))
out_dir = "C:/Users/Sunny_yang/AppData/Local/Temp/xzd_resume_test"
os.makedirs(out_dir, exist_ok=True)

# 启动本地文件服务器提供一个大文件
server_dir = tempfile.mkdtemp()
file_path = os.path.join(server_dir, "big.bin")
with open(file_path, "wb") as f:
    f.write(os.urandom(10 * 1024 * 1024))  # 10 MB

server = subprocess.Popen(
    [sys.executable, "-m", "http.server", "18080", "--directory", server_dir],
    creationflags=subprocess.CREATE_NO_WINDOW,
)
time.sleep(1)

try:
    url = "http://127.0.0.1:18080/big.bin"
    # 第一次：下载几秒后杀掉
    p1 = subprocess.Popen(
        [sys.executable, "dlcore.py", "-u", url, "-o", out_dir, "-f", "big.bin", "-c", "4", "--json"],
        cwd=root,
    )
    time.sleep(2)
    p1.terminate()
    try:
        p1.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p1.kill()

    # 检查是否有部分文件和位图
    files = os.listdir(out_dir)
    print("after interrupt:", files)
    partial = [f for f in files if f.startswith("big.bin")]
    print("partial files:", partial)

    # 第二次：继续下载
    p2 = subprocess.run(
        [sys.executable, "dlcore.py", "-u", url, "-o", out_dir, "-f", "big.bin", "-c", "4", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    print("second run rc:", p2.returncode)
    print("second run stdout tail:", p2.stdout[-500:])
finally:
    server.terminate()
    try:
        server.wait(timeout=3)
    except subprocess.TimeoutExpired:
        server.kill()
