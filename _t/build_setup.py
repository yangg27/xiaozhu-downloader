#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
build_setup.py · 小助下载管理器 XZDM 安装包构建脚本
============================================
把 dist_app/小助下载器 打包成单个可执行安装包：
  dist_setup/小助下载器_v3.0.5_安装包.exe

安装器行为：
  1. 自解压到 C:\Program Files\XZDM
  2. 创建开始菜单快捷方式 + 桌面快捷方式
  3. 注册开机自启、xzd:// 协议、卸载项
  4. 立即后台静默启动
  5. 安装过程零第三方依赖、无需管理员权限（HKCU）

卸载器行为：
  · 清理注册表（自启、协议、卸载项）
  · 删除快捷方式
  · 删除安装目录（用脱离式子进程解决“删不掉自己”问题）
  · 支持卸载/重装选择
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(ROOT) == "_t":
    ROOT = os.path.dirname(ROOT)

APP_NAME = "小助下载器"
APP_ID = "XiaozhuDownloader"
VERSION = "3.1.3"
SRC_DIR = os.path.join(ROOT, "dist_app", "小助下载器")
OUT_DIR = os.path.join(ROOT, "dist_setup")
SPEC_FILE = os.path.join(OUT_DIR, "setup.spec")

INSTALLER_TEMPLATE = r'''# -*- mode: python ; coding: utf-8 -*-
import os
ROOT = os.path.abspath(os.path.dirname(SPEC))
a = Analysis(
    ["setup.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["ctypes", "ctypes.wintypes", "winreg", "subprocess", "shutil", "zipfile", "base64", "tempfile", "time", "sys"],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="{setup_name}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "tray.ico") if os.path.exists(os.path.join(ROOT, "tray.ico")) else None,
)
'''

SETUP_PY_HEADER = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器安装器（v{version}）
由 build_setup.py 自动生成，单文件自解压安装。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile

APP_NAME = "{app_name}"
APP_ID = "{app_id}"
VERSION = "{version}"


def _get_install_dir() -> str:
    """v3.0.8: 探测安装目录可写性，优先 C:\\Program Files\\XZDM，不可写则降级到 C:\\XZDM。"""
    candidates = [
        r"C:\\Program Files\\XZDM",
        r"C:\\XZDM",
    ]
    for cand in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
            test = os.path.join(cand, ".xzd_writable_test")
            with open(test, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(test)
            return cand
        except Exception:
            continue
    # 最后兜底：用户目录
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP_NAME)


INSTALL_DIR = _get_install_dir()
START_MENU = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\\Windows\\Start Menu\\Programs", APP_NAME)
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\\\" + APP_ID

PAYLOAD_ZIP_B64 = """
'''

SETUP_PY_FOOTER = r'''


def _shell_copy_self_to_temp():
    """把卸载器自身复制到 %TEMP% 再删除原目录（解决删不掉自己的问题）。"""
    try:
        src = sys.executable
        tmp = os.path.join(tempfile.gettempdir(), "xzd_uninstall_helper.exe")
        shutil.copy2(src, tmp)
        return tmp
    except Exception:
        return sys.executable


def _wait_pid_dead(pid: int, timeout: float = 5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            import ctypes.wintypes
            h = ctypes.windll.kernel32.OpenProcess(0x00000001, False, pid)
            if not h:
                return True
            ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True
        time.sleep(0.2)
    return False


def _delete_tree(path: str):
    if os.path.isdir(path):
        for i in range(5):
            try:
                # 先改文件属性，再尝试 shutil.rmtree
                for root, dirs, files in os.walk(path):
                    for d in dirs:
                        dp = os.path.join(root, d)
                        try:
                            os.chmod(dp, 0o777)
                        except Exception:
                            pass
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            os.chmod(fp, 0o777)
                        except Exception:
                            pass
                shutil.rmtree(path)
                return True
            except Exception:
                time.sleep(0.5)
        # 兜底：用 robocopy 空目录覆盖法删除顽固目录
        try:
            empty = tempfile.mkdtemp(prefix="xzd_del_")
            subprocess.run(["robocopy", empty, path, "/MIR", "/NJH", "/NJS", "/NDL", "/NC", "/NS", "/NP"], capture_output=True, timeout=30)
            shutil.rmtree(path, ignore_errors=True)
            shutil.rmtree(empty, ignore_errors=True)
            return not os.path.exists(path)
        except Exception:
            return False
    elif os.path.isfile(path):
        for i in range(5):
            try:
                os.chmod(path, 0o777)
                os.remove(path)
                return True
            except Exception:
                time.sleep(0.3)
    return False


def _reg_delete_tree(root, path: str):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                    _reg_delete_tree(root, path + "\\" + sub)
                except OSError:
                    break
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _create_shortcut(lnk: str, target: str, args: str = "", icon: str = "", desc: str = ""):
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    vbs = os.path.join(tempfile.gettempdir(), "xzd_mklnk.vbs")
    with open(vbs, "w", encoding="utf-16") as f:
        f.write(f"Set ws=CreateObject(\"WScript.Shell\")\n")
        f.write(f"Set l=ws.CreateShortcut(\"{lnk}\")\n")
        f.write(f"l.TargetPath=\"{target}\"\n")
        if args:
            f.write(f"l.Arguments=\"{args}\"\n")
        if icon:
            f.write(f"l.IconLocation=\"{icon}\"\n")
        if desc:
            f.write(f"l.Description=\"{desc}\"\n")
        f.write("l.Save\n")
    subprocess.run(["cscript", "//nologo", vbs], capture_output=True, timeout=30)


def _shortcut_for(name: str, desktop: bool = False):
    if desktop:
        return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", name + ".lnk")
    return os.path.join(START_MENU, name + ".lnk")


def _get_existing_install_dir() -> str:
    """从注册表读取旧安装位置（若存在）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            v, _ = winreg.QueryValueEx(k, "InstallLocation")
            if v and os.path.isdir(v):
                return v
    except Exception:
        pass
    return ""


def _extract():
    zip_bytes = base64.b64decode(PAYLOAD_ZIP_B64)
    tmp_zip = os.path.join(tempfile.gettempdir(), "xzd_payload.zip")
    with open(tmp_zip, "wb") as f:
        f.write(zip_bytes)
    # v3.0.8: 若本机已有旧版安装在其他目录，先迁移/清理，确保最终安装在 INSTALL_DIR
    prev = _get_existing_install_dir()
    if prev and prev != INSTALL_DIR and os.path.isdir(prev):
        backup = INSTALL_DIR + ".old"
        _delete_tree(backup)
        try:
            os.rename(prev, backup)
        except Exception:
            pass
        _delete_tree(backup)
    if os.path.exists(INSTALL_DIR):
        old = INSTALL_DIR + ".old"
        if os.path.exists(old):
            shutil.rmtree(old, ignore_errors=True)
        os.rename(INSTALL_DIR, old)
        shutil.rmtree(old, ignore_errors=True)
    os.makedirs(INSTALL_DIR, exist_ok=True)
    with zipfile.ZipFile(tmp_zip, "r") as z:
        z.extractall(INSTALL_DIR)
    try:
        os.remove(tmp_zip)
    except Exception:
        pass
    return INSTALL_DIR


def _register(exe: str):
    # 开机自启
    run_key = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, f'"{exe}" --silent')
    except Exception:
        pass
    # xzd:// 协议
    proto = r"Software\\Classes\\xzd"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, proto, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:" + APP_NAME + " Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, proto + r"\\DefaultIcon", 0, winreg.KEY_SET_VALUE) as k:
            ico = os.path.join(INSTALL_DIR, "tray.ico")
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, ico if os.path.exists(ico) else exe)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, proto + r"\\shell\\open\\command", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'"{exe}" "%1"')
    except Exception:
        pass
    # 卸载项
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "yangg27")
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, INSTALL_DIR)
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{os.path.join(INSTALL_DIR, "卸载.exe")}"')
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, exe)
            try:
                size_mb = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(INSTALL_DIR) for f in fn) // (1024 * 1024)
                winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_mb)
            except Exception:
                pass
    except Exception:
        pass


def _create_shortcuts(exe: str):
    ico = os.path.join(INSTALL_DIR, "tray.ico")
    if not os.path.exists(ico):
        ico = ""
    _create_shortcut(_shortcut_for(APP_NAME), exe, icon=ico, desc=APP_NAME)
    _create_shortcut(_shortcut_for("卸载 " + APP_NAME), os.path.join(INSTALL_DIR, "卸载.exe"), desc="卸载 " + APP_NAME)
    _create_shortcut(_shortcut_for(APP_NAME, desktop=True), exe, icon=ico, desc=APP_NAME)


def _launch(exe: str):
    DETACHED = 0x00000008
    try:
        subprocess.Popen([exe, "--silent"], cwd=INSTALL_DIR,
                         creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def install():
    _extract()
    exe = os.path.join(INSTALL_DIR, "小助下载器.exe")
    # 把安装包自身复制为卸载器，确保控制面板/快捷方式都能正常卸载
    uninstaller_src = sys.executable
    uninstaller_dst = os.path.join(INSTALL_DIR, "卸载.exe")
    try:
        shutil.copy2(uninstaller_src, uninstaller_dst)
    except Exception:
        pass
    _register(exe)
    _create_shortcuts(exe)
    _launch(exe)


def uninstall():
    _log(f"uninstall start argv={sys.argv} exe={sys.executable}")
    # 清理注册表
    _reg_delete_tree(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\xzd")
    _reg_delete_tree(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_ID)
    except FileNotFoundError:
        pass
    # 清理快捷方式及空目录
    for p in (_shortcut_for(APP_NAME), _shortcut_for("卸载 " + APP_NAME), _shortcut_for(APP_NAME, desktop=True)):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    # 删除开始菜单程序组空目录
    try:
        if os.path.isdir(START_MENU) and not os.listdir(START_MENU):
            os.rmdir(START_MENU)
    except Exception:
        pass
    # 杀掉运行中的主程序
    try:
        subprocess.run(["taskkill", "/f", "/im", "小助下载器.exe"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    # 删除安装目录：复制自身到 TEMP，父进程退出后由副本完成
    helper = _shell_copy_self_to_temp()
    pid = os.getpid()
    cmd = f'"{helper}" __DO_UNINSTALL__ "{INSTALL_DIR}" "{pid}"'
    _log(f"launching helper: {cmd}")
    subprocess.Popen(cmd, shell=True,
                     creationflags=0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _log("helper launched, exiting")
    os._exit(0)


def _log(msg: str):
    try:
        p = os.path.join(tempfile.gettempdir(), "xzd_uninstall.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _do_uninstall_from_helper():
    target_dir = sys.argv[2] if len(sys.argv) > 2 else INSTALL_DIR
    parent_pid = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0
    _log(f"helper start target={target_dir} parent={parent_pid}")
    if parent_pid:
        dead = _wait_pid_dead(parent_pid, timeout=10.0)
        _log(f"parent dead={dead}")
    # 再等一下确保文件锁释放
    time.sleep(2.0)
    ok = _delete_tree(target_dir)
    _log(f"delete_tree ok={ok} exists={os.path.exists(target_dir)}")
    # 删除自己（副本）
    me = sys.executable
    vbs = os.path.join(tempfile.gettempdir(), "xzd_delself.vbs")
    with open(vbs, "w", encoding="utf-16") as f:
        f.write(f"Set fso=CreateObject(\"Scripting.FileSystemObject\")\n")
        f.write(f"WScript.Sleep 1000\n")
        f.write(f"fso.DeleteFile \"{me}\"\n")
        f.write(f"fso.DeleteFile \"{vbs}\"\n")
    subprocess.Popen(["cscript", "//nologo", vbs],
                     creationflags=0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os._exit(0)


def _is_already_running():
    """简单单实例：找同名窗口/互斥（安装器无需严格单实例）。"""
    return False


def _message_box(msg: str, title: str = APP_NAME, style: int = 0x40):
    try:
        return ctypes.windll.user32.MessageBoxW(None, msg, title, style)
    except Exception:
        print(msg)
        return 1


def _is_control_panel_uninstall():
    """判断是否由控制面板/系统卸载入口调用（参数里带 /S 或 /u）。"""
    if len(sys.argv) <= 1:
        return False
    a = sys.argv[1].lower().lstrip("-/")
    return a in ("u", "uninstall") or a.startswith("uninstall")


def _choose_uninstall_or_repair():
    """弹出选择：卸载 / 重装。返回 'uninstall' | 'repair' | 'cancel'。"""
    # MB_YESNOCANCEL + 问号图标；Yes=重装，No=卸载，Cancel=取消
    res = _message_box(
        "您正在运行小助下载器卸载程序。\n\n"
        "· 点击「是」→ 保留用户数据并重新安装（修复/重装）\n"
        "· 点击「否」→ 卸载软件（保留下载的文件和设置）\n"
        "· 点击「取消」→ 什么都不做",
        f"卸载 / 重装 {APP_NAME}",
        0x23)  # MB_YESNOCANCEL | MB_ICONQUESTION
    # IDYES=6, IDNO=7, IDCANCEL=2, IDOK=1
    if res == 6:
        return "repair"
    if res == 7:
        return "uninstall"
    return "cancel"


def _repair():
    """重装：先清理注册表/快捷方式，再用当前卸载器里的 payload 重新安装。"""
    # 1. 把自身复制到临时目录作为安装源（当前 exe 在安装目录里，一会要删）
    installer_src = sys.executable
    installer_tmp = os.path.join(tempfile.gettempdir(), "xzd_repair_installer.exe")
    try:
        shutil.copy2(installer_src, installer_tmp)
    except Exception as e:
        _message_box(f"复制重装文件失败：{e}", "重装失败", 0x10)
        return
    # 2. 清理旧注册表与快捷方式（保留程序目录由 helper 删）
    try:
        _reg_delete_tree(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\xzd")
        _reg_delete_tree(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP_ID)
        except FileNotFoundError:
            pass
        for p in (_shortcut_for(APP_NAME), _shortcut_for("卸载 " + APP_NAME), _shortcut_for(APP_NAME, desktop=True)):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass
    # 3. 杀掉运行中的主程序，删除旧目录
    try:
        subprocess.run(["taskkill", "/f", "/im", "小助下载器.exe"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    helper = _shell_copy_self_to_temp()
    pid = os.getpid()
    cmd = f'"{helper}" __DO_REPAIR__ "{INSTALL_DIR}" "{pid}" "{installer_tmp}"'
    _log(f"launching repair helper: {cmd}")
    subprocess.Popen(cmd, shell=True,
                     creationflags=0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _log("repair helper launched, exiting")
    os._exit(0)


def _do_repair_from_helper():
    """helper：等父进程死后删除旧目录，再运行临时安装包重新安装。"""
    target_dir = sys.argv[2] if len(sys.argv) > 2 else INSTALL_DIR
    parent_pid = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0
    installer_tmp = sys.argv[4] if len(sys.argv) > 4 else ""
    _log(f"repair helper start target={target_dir} parent={parent_pid} installer={installer_tmp}")
    if parent_pid:
        _wait_pid_dead(parent_pid, timeout=10.0)
    time.sleep(2.0)
    _delete_tree(target_dir)
    _log(f"old dir removed exists={os.path.exists(target_dir)}")
    if installer_tmp and os.path.exists(installer_tmp):
        try:
            subprocess.Popen([installer_tmp],
                             creationflags=0x00000008,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log("installer launched from temp")
        except Exception as e:
            _log(f"launch installer failed: {e}")
    # 删除自己（副本）
    me = sys.executable
    vbs = os.path.join(tempfile.gettempdir(), "xzd_delself.vbs")
    try:
        with open(vbs, "w", encoding="utf-16") as f:
            f.write(f"Set fso=CreateObject(\"Scripting.FileSystemObject\")\n")
            f.write(f"WScript.Sleep 2000\n")
            f.write(f"fso.DeleteFile \"{me}\"\n")
            f.write(f"fso.DeleteFile \"{installer_tmp}\"\n")
            f.write(f"fso.DeleteFile \"{vbs}\"\n")
        subprocess.Popen(["cscript", "//nologo", vbs],
                         creationflags=0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    os._exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "__DO_UNINSTALL__":
        _do_uninstall_from_helper()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "__DO_REPAIR__":
        _do_repair_from_helper()
        return
    if len(sys.argv) > 1 and sys.argv[1].lower().lstrip("-/") in ("u", "uninstall"):
        uninstall()
        return
    if _is_already_running():
        _message_box("安装器已经在运行。")
        return
    # 控制面板调用直接卸载；用户主动双击卸载器则给选择
    if os.path.basename(sys.executable).lower() in ("卸载.exe", "uninstall.exe") and not _is_control_panel_uninstall():
        choice = _choose_uninstall_or_repair()
        if choice == "repair":
            _repair()
            return
        if choice == "uninstall":
            uninstall()
            return
        return
    try:
        install()
        _message_box(
            f"{APP_NAME} v{VERSION} 安装完成。\n"
            f"安装位置：{INSTALL_DIR}\n\n"
            f"· 浏览器扩展位于安装目录下的 browser-extension 文件夹，请手动加载一次。\n"
            f"· 任务记录、配置和历史已保留（位于 %APPDATA%\\{APP_NAME}）。",
            "安装完成")
    except Exception as e:
        _message_box(f"安装失败：{type(e).__name__}: {e}", "安装失败", 0x10)
        raise


if __name__ == "__main__":
    main()
'''


def _zip_payload() -> bytes:
    tmp = os.path.join(tempfile.gettempdir(), "xzd_payload.zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(SRC_DIR):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, SRC_DIR)
                zf.write(full, arc)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return data


def run():
    if not os.path.isdir(SRC_DIR):
        print(f"[错误] 找不到主程序产物：{SRC_DIR}\n请先运行 python _t/build_app.py", file=sys.stderr)
        sys.exit(2)

    py = None
    candidates = [
        r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python313\python.exe",
        r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python312\python.exe",
        r"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            py = c
            break
    if not py:
        py = shutil.which("python") or sys.executable

    print(f"[build_setup] 使用 Python: {py}")
    print(f"[build_setup] 打包主程序: {SRC_DIR}")

    os.makedirs(OUT_DIR, exist_ok=True)

    # 1) 生成 setup.py
    zip_data = _zip_payload()
    b64 = base64.b64encode(zip_data).decode("ascii")
    setup_name = f"小助下载器_v{VERSION}_安装包"
    setup_py = os.path.join(OUT_DIR, "setup.py")
    with open(setup_py, "w", encoding="utf-8") as f:
        f.write(SETUP_PY_HEADER.format(app_name=APP_NAME, app_id=APP_ID, version=VERSION))
        # 每 76 字符一行 base64
        for i in range(0, len(b64), 76):
            f.write(b64[i:i + 76] + "\n")
        f.write('"""\n')
        f.write(SETUP_PY_FOOTER)

    # 2) 复制图标
    ico_src = os.path.join(ROOT, "tray.ico")
    ico_dst = os.path.join(OUT_DIR, "tray.ico")
    if os.path.exists(ico_src):
        shutil.copy2(ico_src, ico_dst)

    # 3) 写 spec
    with open(SPEC_FILE, "w", encoding="utf-8") as f:
        f.write(INSTALLER_TEMPLATE.format(setup_name=setup_name))

    # 4) PyInstaller 打安装包（one-file 单文件，用户双击即用，无需 _internal 目录）
    print("[build_setup] 开始 PyInstaller 构建（one-file 模式）...")
    cmd = [py, "-m", "PyInstaller", "--clean", "--noconfirm", "-y", SPEC_FILE]
    rc = subprocess.run(cmd, cwd=OUT_DIR).returncode
    if rc != 0:
        print("[build_setup] PyInstaller 构建失败", file=sys.stderr)
        sys.exit(rc)

    # one-file 模式产物在 dist/<setup_name>.exe（单文件）
    dist_exe = os.path.join(OUT_DIR, "dist", setup_name + ".exe")
    final_dir = os.path.join(OUT_DIR, setup_name)
    if os.path.isdir(final_dir):
        # 改名让路
        bak = final_dir + ".old"
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
        os.rename(final_dir, bak)
    os.makedirs(final_dir, exist_ok=True)
    if os.path.isfile(dist_exe):
        shutil.copy2(dist_exe, os.path.join(final_dir, setup_name + ".exe"))
    else:
        print(f"[build_setup] 找不到 PyInstaller 输出：{dist_exe}", file=sys.stderr)
        sys.exit(3)

    # 5) 清理中间产物（保留 setup.py 供校验）
    for old in (os.path.join(OUT_DIR, "dist"), os.path.join(OUT_DIR, "build"), SPEC_FILE):
        if os.path.exists(old):
            if os.path.isdir(old):
                shutil.rmtree(old, ignore_errors=True)
            else:
                os.remove(old)
    old_bak = final_dir + ".old"
    if os.path.exists(old_bak):
        shutil.rmtree(old_bak, ignore_errors=True)

    exe_path = os.path.join(final_dir, setup_name + ".exe")
    print(f"[build_setup] 完成: {exe_path}")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"[build_setup] 大小: {size_mb:.1f} MB")


if __name__ == "__main__":
    run()
