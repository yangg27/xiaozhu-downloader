#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端回归测试脚本。

不依赖 GUI，使用 dlcore CLI + localserver 完成以下流程：
  1. 启动本地 HTTP 服务器提供测试文件
  2. 通过 dlcore CLI 下载该文件并校验内容
  3. 启动 localserver，模拟浏览器扩展 /add 接口发起下载
  4. 校验任务状态、历史记录
  5. 清理所有临时文件

运行：
    python tests/e2e_regression.py
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import random
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dlcore
import localserver


TEST_PAYLOAD = b"XZDM-E2E-TEST-" + b"0" * 4096
TEST_FILENAME = "e2e-test.bin"


def free_port() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), lambda *a, **k: None) as s:
        return s.server_address[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(TEST_PAYLOAD)))
        self.send_header("Content-Disposition", f"attachment; filename={TEST_FILENAME}")
        self.end_headers()
        self.wfile.write(TEST_PAYLOAD)

    def log_message(self, *a):
        return


def start_http_server(port: int):
    srv = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_cli_download(http_port: int, out_dir: str) -> str:
    """用 dlcore CLI 直接下载测试文件，返回保存路径。"""
    url = f"http://127.0.0.1:{http_port}/{TEST_FILENAME}"
    cmd = [
        sys.executable, "dlcore.py",
        "-u", url,
        "-o", out_dir,
        "-c", "4",
        "--json",
    ]
    print(f"[e2e] CLI: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise RuntimeError(f"CLI 下载失败：{r.returncode}")
    path = os.path.join(out_dir, TEST_FILENAME)
    if not os.path.exists(path):
        # dlcore 可能会用 safe_filename 后的名字
        for fn in os.listdir(out_dir):
            if fn.startswith("e2e-test"):
                path = os.path.join(out_dir, fn)
                break
    return path


def run_api_download(http_port: int, out_dir: str) -> tuple[dlcore.Manager, str, localserver.LocalAPI]:
    """用 localserver /add 接口发起下载，返回 manager、任务 tid、api。"""
    data_dir = os.path.join(out_dir, ".data")
    mgr = dlcore.Manager(data_dir=data_dir)
    mgr.set_cfg(download_dir=out_dir, max_parallel_tasks=2, connections=4)
    api = localserver.LocalAPI(mgr, port=free_port())
    api.start()
    url = f"http://127.0.0.1:{http_port}/{TEST_FILENAME}"
    body = json.dumps({"url": url, "filename": TEST_FILENAME}).encode("utf-8")
    req = urllib.request.Request(
        f"{api.base_url}/add",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-XZD-Token": api.token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    print(f"[e2e] /add response: {result}")
    if not result.get("ok"):
        raise RuntimeError(f"/add 失败：{result}")
    return mgr, result["tid"], api


def wait_task_done(mgr: dlcore.Manager, tid: str, timeout: float = 60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        task = mgr.get(tid)
        if task and task.state == dlcore.STATE_DONE:
            return True
        if task and task.state == dlcore.STATE_ERROR:
            raise RuntimeError(f"任务出错：{task.err}")
        time.sleep(0.5)
    raise TimeoutError("任务未在限定时间内完成")


def main() -> int:
    print("[e2e] 开始端到端回归测试")
    http_port = free_port()
    srv = start_http_server(http_port)

    with tempfile.TemporaryDirectory(prefix="xzd_e2e_") as tmp:
        out_dir = os.path.join(tmp, "downloads")
        os.makedirs(out_dir, exist_ok=True)

        # ---- 1. CLI 下载 ----
        print("[e2e] 1/3 CLI 下载测试")
        path1 = run_cli_download(http_port, out_dir)
        with open(path1, "rb") as f:
            data1 = f.read()
        assert data1 == TEST_PAYLOAD, "CLI 下载内容不匹配"
        print(f"[e2e] CLI 下载成功：{path1}")

        # ---- 2. API 下载 ----
        print("[e2e] 2/3 localserver /add 下载测试")
        mgr, tid, api = run_api_download(http_port, out_dir)
        wait_task_done(mgr, tid)
        task = mgr.get(tid)
        final = task.created_final or task.final_path()
        print(f"[e2e] API 下载完成：{final}")
        with open(final, "rb") as f:
            data2 = f.read()
        assert data2 == TEST_PAYLOAD, "API 下载内容不匹配"

        # ---- 3. 历史记录 ----
        print("[e2e] 3/3 历史记录检查")
        items = mgr.history(limit=10)
        assert any(h.get("tid") == tid for h in items), "历史记录中找不到刚完成的任务"
        print("[e2e] 历史记录检查通过")

        api.stop()

    srv.shutdown()
    print("[e2e] 全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"[e2e] 断言失败：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"[e2e] 异常：{type(e).__name__}: {e}")
        sys.exit(1)
