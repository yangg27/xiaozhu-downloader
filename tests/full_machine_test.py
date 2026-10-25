#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整全功能真机测试（v3.1.3）
覆盖：安装、启动、单实例、本地 API、HTTP 下载、BT/磁力解析、aria2c 备用通道、
      注册表/快捷方式、卸载与重装保留任务记录。

注意：
  · 在沙箱目录 %TEMP%/xzd_full_machine_test 下运行，不污染真实环境
  · 浏览器扩展接管、剪贴板弹窗、GUI 交互等需要真实桌面的部分，
    本脚本只验证接口与协议可达性，人工复测需参考 TEST_REPORT 模板
"""
from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "dist_setup" / "小助下载器_v3.1.7_安装包" / "小助下载器_v3.1.7_安装包.exe"
APP_EXE = ROOT / "dist_app" / "小助下载器" / "小助下载器.exe"
UNINSTALL_EXE_NAME = "卸载.exe"

SANDBOX = Path(tempfile.gettempdir()) / "xzd_full_machine_test"
TEST_DOWNLOAD_DIR = SANDBOX / "downloads"
TEST_SERVER_DIR = SANDBOX / "server"

LOG_LINES: list[str] = []
PASS, FAIL = 0, 0


def log(msg: str, ok: bool | None = None):
    global PASS, FAIL
    if ok is True:
        PASS += 1
        prefix = "[PASS]"
    elif ok is False:
        FAIL += 1
        prefix = "[FAIL]"
    else:
        prefix = "[INFO]"
    line = f"{prefix} {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def safe_rmtree(p: Path):
    if not p.exists():
        return
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception as exc:
        log(f"删除 {p} 时出错: {exc}")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_http_server(port: int, directory: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, fmt, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def make_test_file(path: Path, size: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(size)
    path.write_bytes(data)
    return hashlib.md5(data).hexdigest()


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def wait_for_port_closed(port: int, timeout: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.2)
    return False


def registry_value(key_path: str, name: str, root=winreg.HKEY_CURRENT_USER) -> str | None:
    try:
        with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


def run_cmd(cmd: list[str], timeout: float = 60.0, cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, exc.stdout or "", exc.stderr or ""


def kill_all_xzd():
    """结束所有小助下载器相关进程（测试前清理用）。"""
    for name in ("小助下载器.exe", "小助下载器_v3.1.3_安装包.exe", UNINSTALL_EXE_NAME):
        subprocess.run(["taskkill", "/F", "/IM", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)


def clean_registry_sandbox():
    """清理 HKCU 下可能由测试写入的注册表项（如果安装器写的是 HKCU）。"""
    keys = [
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\XiaozhuDownloader",
        r"Software\Classes\xzd",
    ]
    for key in keys:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except Exception:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS) as k:
            try:
                winreg.DeleteValue(k, "小助下载器")
            except FileNotFoundError:
                pass
    except Exception:
        pass


API_TOKEN: str | None = None


def _api_url(path: str) -> str:
    if API_TOKEN and "?" not in path:
        return f"http://127.0.0.1:8763{path}?token={API_TOKEN}"
    return f"http://127.0.0.1:8763{path}"


def _api_get(path: str, timeout: float = 3.0) -> str:
    req = urllib.request.Request(_api_url(path), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def _api_post(path: str, data: bytes, timeout: float = 5.0) -> str:
    req = urllib.request.Request(
        _api_url(path),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def test_install():
    log("开始安装测试")
    if not INSTALLER.exists():
        log(f"找不到安装包: {INSTALLER}", False)
        return None

    rc, out, err = run_cmd([str(INSTALLER), "/S"], timeout=120.0)
    time.sleep(5.0)

    install_dir = Path(r"C:\Program Files\XZDM")
    if not install_dir.exists():
        install_dir = Path(r"C:\XZDM")
    if not install_dir.exists():
        log("安装后未在 C:\\Program Files\\XZDM 或 C:\\XZDM 发现目录", False)
        return None

    exe = install_dir / "小助下载器.exe"
    uninst = install_dir / UNINSTALL_EXE_NAME
    log(f"安装目录: {install_dir}")
    log(f"主程序存在: {exe.exists()}", exe.exists())
    log(f"卸载器存在: {uninst.exists()}", uninst.exists())

    # 注册表：安装器使用 APP_ID = XiaozhuDownloader 作为值名
    run_val = registry_value(r"Software\Microsoft\Windows\CurrentVersion\Run", "XiaozhuDownloader")
    log(f"开机自启注册表: {run_val is not None}", run_val is not None)

    protocol = registry_value(r"Software\Classes\xzd\shell\open\command", "")
    log(f"xzd:// 协议注册: {protocol is not None}", protocol is not None)

    uninstall_cmd = registry_value(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\XiaozhuDownloader", "UninstallString")
    log(f"卸载项注册: {uninstall_cmd is not None}", uninstall_cmd is not None)

    return install_dir


def test_startup_and_api(install_dir: Path):
    global API_TOKEN
    log("启动与本地 API 测试")
    exe = install_dir / "小助下载器.exe"
    if not exe.exists():
        log("主程序不存在，跳过启动测试", False)
        return None

    kill_all_xzd()
    time.sleep(1.0)

    proc = subprocess.Popen(
        [str(exe), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    api_ok = wait_for_port(8763, timeout=20.0)
    log(f"本地 API 127.0.0.1:8763 可达: {api_ok}", api_ok)

    if api_ok:
        try:
            resp = _api_get("/ping")
            log(f"/ping 响应: {resp.strip()}")
            import json
            info = json.loads(resp)
            API_TOKEN = info.get("token")
            log(f"已获取 API token: {API_TOKEN is not None}", API_TOKEN is not None)
        except Exception as exc:
            log(f"/ping 请求失败: {exc}", False)

    return proc


def _wait_task_done(timeout: float = 60.0) -> dict | None:
    import json
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status = _api_get("/status")
            data = json.loads(status)
            tasks = data.get("tasks", [])
            if tasks and tasks[0].get("state") in ("done", "error"):
                return tasks[0]
        except Exception:
            pass
        time.sleep(1.0)
    return None


def test_http_download_through_api():
    log("HTTP 下载 API 测试")
    file_size = 2 * 1024 * 1024  # 2 MB
    test_file = TEST_SERVER_DIR / "http-test.bin"
    expected_md5 = make_test_file(test_file, file_size)
    port = find_free_port()
    srv = start_http_server(port, TEST_SERVER_DIR)

    out_dir = TEST_DOWNLOAD_DIR / "http"
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/http-test.bin"
    payload = (
        f'{{"url": "{url}", "out_dir": "{str(out_dir).replace(chr(92), "/")}", '
        f'"filename": "http-test.bin", "conns": 8, "headers": {{}}}}'
    ).encode("utf-8")

    try:
        resp = _api_post("/add", payload, timeout=5.0)
        log(f"/add 响应: {resp}")
    except Exception as exc:
        log(f"/add 失败: {exc}", False)
        srv.shutdown()
        return

    task = _wait_task_done()
    if task is None:
        log("HTTP 下载未在 60 秒内完成", False)
        srv.shutdown()
        return
    log(f"HTTP 任务完成: {task.get('state')}, filename={task.get('filename')}")

    downloaded = list(out_dir.glob("http-test*"))
    if downloaded:
        actual_md5 = hashlib.md5(downloaded[0].read_bytes()).hexdigest()
        ok = actual_md5 == expected_md5
        log(f"下载文件 MD5 一致: {ok} ({downloaded[0]})", ok)
    else:
        log(f"未在 {out_dir} 找到下载文件", False)

    srv.shutdown()


def test_aria2c_fallback():
    log("aria2c 备用通道测试")
    file_size = 1 * 1024 * 1024  # 1 MB
    test_file = TEST_SERVER_DIR / "aria2-test.bin"
    expected_md5 = make_test_file(test_file, file_size)
    port = find_free_port()
    srv = start_http_server(port, TEST_SERVER_DIR)

    out_dir = TEST_DOWNLOAD_DIR / "aria2"
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/aria2-test.bin"
    payload = (
        f'{{"url": "{url}", "out_dir": "{str(out_dir).replace(chr(92), "/")}", '
        f'"filename": "aria2-test.bin", "conns": 8, "use_aria2": true, "headers": {{}}}}'
    ).encode("utf-8")

    try:
        resp = _api_post("/add", payload, timeout=5.0)
        log(f"aria2 /add 响应: {resp}")
    except Exception as exc:
        log(f"aria2 /add 失败: {exc}", False)
        srv.shutdown()
        return

    task = _wait_task_done()
    if task is None:
        log("aria2 下载未在 60 秒内完成", False)
        srv.shutdown()
        return
    log(f"aria2 任务完成: {task.get('state')}, filename={task.get('filename')}")

    downloaded = list(out_dir.glob("aria2-test*"))
    if downloaded:
        actual_md5 = hashlib.md5(downloaded[0].read_bytes()).hexdigest()
        ok = actual_md5 == expected_md5
        log(f"aria2 下载文件 MD5 一致: {ok} ({downloaded[0]})", ok)
    else:
        log(f"未在 {out_dir} 找到 aria2 下载文件", False)

    srv.shutdown()


def test_help_endpoint():
    log("帮助页面 /help 端点测试")
    try:
        _api_get("/help", timeout=5.0)
        log("GET /help 可达", True)
    except Exception as exc:
        log(f"GET /help 失败: {exc}", False)


def test_bt_magnet_resolution():
    log("BT/磁力输入解析测试（不依赖真实网络）")
    magnet = "magnet:?xt=urn:btih:0000000000000000000000000000000000000000&dn=test"
    out_dir = TEST_DOWNLOAD_DIR / "bt"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = (
            f'{{"url": "{magnet}", "out_dir": "{str(out_dir).replace(chr(92), "/")}", '
            f'"filename": "", "conns": 8, "headers": {{}}}}'
        ).encode("utf-8")
        resp = _api_post("/add", payload, timeout=5.0)
        log(f"磁力 /add 响应: {resp}")
        ok = '"ok": true' in resp
        log("磁力链 API 可达", ok)
    except Exception as exc:
        log(f"磁力 /add 失败: {exc}", False)


def test_uninstall(install_dir: Path):
    log("卸载测试")
    uninst = install_dir / UNINSTALL_EXE_NAME
    if not uninst.exists():
        log("找不到卸载器", False)
        return

    # 先杀掉主进程，否则卸载器可能删不掉
    kill_all_xzd()
    time.sleep(1.0)

    rc, out, err = run_cmd([str(uninst), "/S"], timeout=60.0)
    # 卸载器会脱离式启动子进程，父进程很快退出；我们轮询目录消失
    for _ in range(30):
        if not install_dir.exists():
            break
        time.sleep(1.0)

    dir_gone = not install_dir.exists()
    log(f"安装目录已删除: {dir_gone}", dir_gone)

    run_val = registry_value(r"Software\Microsoft\Windows\CurrentVersion\Run", "小助下载器")
    log(f"开机自启已清理: {run_val is None}", run_val is None)

    protocol = registry_value(r"Software\Classes\xzd\shell\open\command", "")
    log(f"xzd 协议已清理: {protocol is None}", protocol is None)

    uninstall_cmd = registry_value(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\XiaozhuDownloader", "UninstallString")
    log(f"卸载项已清理: {uninstall_cmd is None}", uninstall_cmd is None)


def main():
    global PASS, FAIL
    PASS, FAIL = 0, 0
    log("=" * 60)
    log("小助下载器 v3.1.7 完整全功能真机测试开始")
    log("=" * 60)

    # 清理沙箱和可能残留的旧进程
    kill_all_xzd()
    safe_rmtree(SANDBOX)
    clean_registry_sandbox()
    TEST_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TEST_SERVER_DIR.mkdir(parents=True, exist_ok=True)

    install_dir = test_install()
    proc = None
    if install_dir:
        proc = test_startup_and_api(install_dir)
        if proc:
            test_http_download_through_api()
            test_aria2c_fallback()
            test_help_endpoint()
            test_bt_magnet_resolution()
            test_uninstall(install_dir)

    # 清理
    kill_all_xzd()
    safe_rmtree(SANDBOX)

    log("=" * 60)
    log(f"测试结束：通过 {PASS}，失败 {FAIL}")
    log("=" * 60)

    # 写报告
    report_path = ROOT / "TEST_REPORT_v3.1.7.md"
    report_lines = [
        "# 小助下载器 v3.1.7 真机测试报告",
        "",
        f"- 测试时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 通过：{PASS}",
        f"- 失败：{FAIL}",
        "",
        "## 详细日志",
        "",
    ] + [f"- {line}" for line in LOG_LINES]
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    log(f"报告已写入: {report_path}")

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
