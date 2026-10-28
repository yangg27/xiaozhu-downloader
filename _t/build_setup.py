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
VERSION = "3.1.14"
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
    datas=[("payload.zip", ".")],                          # ★ v3.1.12：ZIP 二进制直嵌，替代 base64
    hiddenimports=["ctypes", "ctypes.wintypes", "winreg", "subprocess", "shutil", "zipfile", "tempfile", "time", "sys", "tkinter"],
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
    strip=True,                                    # ★ v3.1.12 瘦身
    upx=True,                                      # ★ v3.1.12 瘦身：安装器自身也用 UPX 压缩
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "_assets", "tray.ico") if os.path.exists(os.path.join(ROOT, "_assets", "tray.ico")) else None,
)
'''

SETUP_PY_HEADER = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器安装器（v{version}）
由 build_setup.py 自动生成，单文件自解压安装。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  强制 PyInstaller 打包 ctypes.wintypes
from ctypes import wintypes
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

'''

SETUP_PY_FOOTER = r'''


def _shell_copy_self_to_temp():
    """把卸载器自身复制到 %TEMP% 再删除原目录（解决删不掉自己的问题）。

    ★ v3.1.11 修复（缺陷 I，真机实测 + 探针实证）：
      副本名加 pid 后缀 —— 旧实现固定名 `xzd_uninstall_helper.exe`，
      与自删脚本共用固定名，多实例/重试时会互相覆盖；
      且旧副本若仍被占用会导致 copy2 抛错 → 卸载链路整体中断。
      现在：唯一名 + 旧副本尽力清理 + 拷贝失败降级复用旧副本。
    """
    try:
        src = sys.executable
        tmp = os.path.join(tempfile.gettempdir(),
                           "xzd_uninstall_helper_%d.exe" % os.getpid())
        shutil.copy2(src, tmp)
        return tmp
    except Exception:
        # 降级：清理历史固定名副本后重试一次
        try:
            legacy = os.path.join(tempfile.gettempdir(), "xzd_uninstall_helper.exe")
            for _ in range(10):
                try:
                    if os.path.exists(legacy):
                        os.remove(legacy)
                        break
                except Exception:
                    time.sleep(0.5)
            src = sys.executable
            tmp = os.path.join(tempfile.gettempdir(),
                               "xzd_uninstall_helper_%d.exe" % os.getpid())
            shutil.copy2(src, tmp)
            return tmp
        except Exception:
            return sys.executable


def _wait_pid_dead(pid: int, timeout: float = 5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            from ctypes import wintypes
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
        except Exception:
            pass
        # ★ v3.1.11 修复（缺陷 J，真机实测 + 探针实证）：
        #   现场：安装目录**文件全被删净（0 个文件）**，只剩一个空壳目录，
        #         而日志显示 `delete_tree ok=False exists=True`；
        #         稍后手工 os.rmdir 立刻成功 ⇒ **瞬时占用**。
        #   机理：Windows 的 delete-pending 语义 —— 卸载器自己的
        #         `卸载.exe` 被标记删除后立即从目录列表消失，但它（以及
        #         one-file 模式下负责清理 _MEI 的 bootloader 父进程）持有的
        #         **目录句柄**要等进程真正退出才释放；在此期间该目录无法 rmdir。
        #         旧实现只给 5×0.5s = 2.5s，且最后只做一次 robocopy 就放弃。
        #   修复：在 robocopy 之后，**单独对「删空目录」做一轮长重试**
        #         （最多 40 次 × 500ms = 20s），并在每轮前重试清空内容。
        for i in range(40):
            if not os.path.exists(path):
                return True
            try:
                # 目录可能又冒出被延迟删除的项，清一遍
                if os.path.isdir(path):
                    for _r, _d, _f in os.walk(path, topdown=False):
                        for fn in _f:
                            try:
                                os.remove(os.path.join(_r, fn))
                            except Exception:
                                pass
                        for dn in _d:
                            try:
                                os.rmdir(os.path.join(_r, dn))
                            except Exception:
                                pass
                    os.rmdir(path)
                else:
                    os.remove(path)
                return True
            except Exception:
                time.sleep(0.5)
        # 最后再试一次 robocopy 兜底（占用方可能此时才退出）
        try:
            empty = tempfile.mkdtemp(prefix="xzd_del_")
            subprocess.run(["robocopy", empty, path, "/MIR", "/NJH", "/NJS", "/NDL", "/NC", "/NS", "/NP"], capture_output=True, timeout=30)
            shutil.rmtree(path, ignore_errors=True)
            shutil.rmtree(empty, ignore_errors=True)
        except Exception:
            pass
        return not os.path.exists(path)
    elif os.path.isfile(path):
        for i in range(5):
            try:
                os.chmod(path, 0o777)
                os.remove(path)
                return True
            except Exception:
                time.sleep(0.3)
        # ★ v3.1.11（缺陷 J 同族）：单文件也做一轮长重试
        for i in range(40):
            if not os.path.exists(path):
                return True
            try:
                os.remove(path)
                return True
            except Exception:
                time.sleep(0.5)
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
    # ★ v3.1.11 修复（缺陷 I 同族，真机实测发现）：
    #   旧实现在 %TEMP% 留下固定名 `xzd_mklnk.vbs` **从不清理**（现场实证：
    #   %TEMP%\xzd_mklnk.vbs 494 字节残留）。现改为 pid 唯一名 + 用完即删。
    vbs = os.path.join(tempfile.gettempdir(), "xzd_mklnk_%d.vbs" % os.getpid())
    try:
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
    finally:
        try:
            if os.path.exists(vbs):
                os.remove(vbs)
        except Exception:
            pass


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
    # ★ v3.1.12：从 PyInstaller _MEIPASS 读 ZIP 二进制，替代 base64 解码（省 +33% 膨胀）
    meipass = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    tmp_zip = os.path.join(meipass, "payload.zip")
    _log(f"extract start meipass={meipass} zip_exists={os.path.exists(tmp_zip)} install_dir={INSTALL_DIR}")
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
    _log(f"extract ok files={len(os.listdir(INSTALL_DIR))}")
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
            ico = os.path.join(INSTALL_DIR, "_internal", "tray.ico")
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
    ico = os.path.join(INSTALL_DIR, "_internal", "tray.ico")
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


def _cleanup_temp_leftovers():
    """清理 %TEMP% 下本软件的历史遗留垃圾（缺陷 I 配套）。

    清理对象（全部为**本软件自己创建**的固定名文件，不触碰用户数据）：
      · xzd_uninstall_helper.exe / xzd_uninstall_helper_<pid>.exe
      · xzd_delself.vbs / xzd_delself_<pid>.vbs
      · xzd_mklnk.vbs / xzd_mklnk_<pid>.vbs
      · xzd_payload.zip（解包中间产物）
      · _MEIxxxxxx 解包目录（one-file 模式遗留）

    安全约束：
      · 只处理 %TEMP% 根目录下的精确前缀，绝不递归扫描其他位置
      · 每个文件删除前先判断是否被占用，删不掉就跳过（不重试堵塞卸载）
      · 当前正在运行的自己（sys.executable）不删（由自删脚本收尾）
    """
    tmpdir = tempfile.gettempdir()
    me = os.path.normcase(os.path.abspath(sys.executable))
    prefixes = ("xzd_uninstall_helper", "xzd_delself", "xzd_mklnk")
    removed, skipped = [], []
    try:
        names = os.listdir(tmpdir)
    except Exception:
        return
    for name in names:
        low = name.lower()
        target = None
        if any(low.startswith(p) for p in prefixes) and low.endswith((".exe", ".vbs")):
            target = os.path.join(tmpdir, name)
        elif low == "xzd_payload.zip":
            target = os.path.join(tmpdir, name)
        elif low.startswith("_mei") and os.path.isdir(os.path.join(tmpdir, name)):
            target = os.path.join(tmpdir, name)
        if not target:
            continue
        if os.path.normcase(os.path.abspath(target)) == me:
            continue
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
                if os.path.exists(target):
                    skipped.append(name)
                else:
                    removed.append(name)
            else:
                os.remove(target)
                removed.append(name)
        except Exception:
            skipped.append(name)
    if removed:
        _log(f"temp cleanup removed: {removed}")
    if skipped:
        _log(f"temp cleanup skipped(busy): {skipped}")


def uninstall():
    _log(f"uninstall start argv={sys.argv} exe={sys.executable}")
    # ★ v3.1.11 新增（缺陷 I）：顺手清理历史版本留下的 %TEMP% 垃圾。
    #   旧版自删脚本失败后会永久残留 77MB helper 与两个 vbs；
    #   新版卸载时主动扫一遍，把历史包袱一并清掉。
    _cleanup_temp_leftovers()
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
    _log(f"helper start target={target_dir} parent={parent_pid} me={sys.executable}")
    if parent_pid:
        dead = _wait_pid_dead(parent_pid, timeout=10.0)
        _log(f"parent dead={dead}")
    # 再等一下确保文件锁释放
    time.sleep(2.0)
    ok = _delete_tree(target_dir)
    _log(f"delete_tree ok={ok} exists={os.path.exists(target_dir)}")

    # ★ v3.1.11 修复（缺陷 I，真机实测 + 探针实证，两条硬事实）：
    #
    #   事实 1：安装包是 PyInstaller **one-file** 模式（PE 内含 _MEIPASS/pyi- 标记）
    #     ⇒ helper 运行时是 **bootloader 父进程 + Python 子进程** 双进程，
    #       **两者都持有同一个 exe 的文件锁**。
    #       `os._exit(0)` 只结束 Python 子进程，父进程还要清理 ~77MB 的
    #       `_MEIxxxxxx` 解包目录，实测约需 2–3 秒才真正退出。
    #
    #   事实 2（探针实测）：`FileSystemObject.DeleteFile` 对**运行中的 exe**
    #     返回 `ERR 70 没有权限`：
    #         cscript rc=0
    #         output = ERR1=70 没有权限 | ERR2=70 没有权限
    #       旧脚本**没有 `On Error Resume Next`** ⇒ 第 3 行一抛错就中止，
    #       第 4 行永不执行 ⇒ vbs 不自删 + helper 永久残留 77MB。
    #     且旧脚本只 `WScript.Sleep 1000`，而实测锁释放与可删时刻之间
    #     裕度仅约 320ms（时间线：helper 进程 10484ms 消失 / 文件 10807ms 可删）
    #     ⇒ **必然的竞态**：第一次卸载失败、第二次成功。
    #
    #   现场证据（%TEMP%）：xzd_uninstall_helper.exe 77,259,562 字节 +
    #     xzd_delself.vbs 456 字节**内容完整**残留（⇒ 脚本未跑完）。
    #
    #   修复：
    #     · On Error Resume Next + 最多 30 次 × 500ms 重试（上限 15s）
    #     · vbs 名称带 pid，避免多实例冲突
    #     · vbs 自删放最后并同样带重试（删除目标不存在时 DeleteFile 也报错，
    #       故用 fso.FileExists 先行判断）
    me = sys.executable
    vbs = os.path.join(tempfile.gettempdir(), "xzd_delself_%d.vbs" % os.getpid())
    try:
        with open(vbs, "w", encoding="utf-16") as f:
            f.write('On Error Resume Next\n')
            f.write('Set fso=CreateObject("Scripting.FileSystemObject")\n')
            f.write('Dim i\n')
            # 第 1 段：反复尝试删除 helper 自身（等到 bootloader 父进程退完）
            f.write('For i = 1 To 30\n')
            f.write('  If fso.FileExists("%s") Then\n' % me)
            f.write('    fso.DeleteFile "%s", True\n' % me)
            f.write('  End If\n')
            f.write('  If Not fso.FileExists("%s") Then Exit For\n' % me)
            f.write('  WScript.Sleep 500\n')
            f.write('Next\n')
            # 第 2 段：删除本脚本（自身删除同样可能短暂失败，也重试）
            f.write('For i = 1 To 10\n')
            f.write('  If fso.FileExists("%s") Then\n' % vbs)
            f.write('    fso.DeleteFile "%s", True\n' % vbs)
            f.write('  End If\n')
            f.write('  If Not fso.FileExists("%s") Then Exit For\n' % vbs)
            f.write('  WScript.Sleep 300\n')
            f.write('Next\n')
        subprocess.Popen(["cscript", "//nologo", vbs],
                         creationflags=0x00000008,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _log(f"self-delete scheduled: {os.path.basename(vbs)}")
    except Exception as exc:
        _log(f"self-delete schedule failed: {exc}")
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


def _show_result_dialog(title: str, message: str, buttons: list[tuple[str, str]]):
    """用 tkinter 显示可自定义按钮文字的对话框。

    buttons: [(label, return_value), ...]
    返回被点击按钮对应的 return_value，关闭窗口返回最后一个 return_value。
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return buttons[-1][1] if buttons else None

    result = buttons[-1][1] if buttons else None

    def choose(val):
        nonlocal result
        result = val
        root.destroy()

    root = tk.Tk()
    root.title(title)
    # 按钮宽度按字数自适应：2按钮520，3按钮580；高度240
    n_btns = len(buttons)
    w0 = 520 if n_btns <= 2 else 600
    root.geometry(f"{w0}x240")
    root.resizable(False, False)
    root.configure(bg="#f8f9fa")
    root.attributes("-topmost", True)
    try:
        root.iconbitmap(os.path.join(INSTALL_DIR, "_internal", "tray.ico"))
    except Exception:
        pass

    FONT = ("Microsoft YaHei UI", 10)
    frm = tk.Frame(root, bg="#f8f9fa", padx=20, pady=20)
    frm.pack(fill=tk.BOTH, expand=True)

    lbl = tk.Label(frm, text=message, justify=tk.LEFT, wraplength=w0 - 60,
                   bg="#f8f9fa", fg="#212529", font=FONT)
    lbl.pack(fill=tk.BOTH, expand=True, anchor=tk.W)

    # tk.Button 宽度更可预测（字符数），配合字体和内部留白保证中文清晰可见
    btn_frm = tk.Frame(frm, bg="#f8f9fa")
    btn_frm.pack(fill=tk.X, pady=(12, 0))
    for label, val in buttons:
        btn = tk.Button(btn_frm, text=label, font=FONT, width=8, padx=12, pady=4,
                        bg="#e9ecef", fg="#212529", activebackground="#dee2e6",
                        activeforeground="#000000", relief="flat", borderwidth=1,
                        cursor="hand2", command=lambda v=val: choose(v))
        btn.pack(side=tk.RIGHT, padx=6)

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w0) // 2}+{(sh - 240) // 2}")
    root.mainloop()
    return result


def _data_dir() -> str:
    """与主程序一致的数据目录：%APPDATA%\\小助下载器"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _api_port() -> int:
    """读取主程序写下的端口提示，失败返回 0"""
    try:
        p = os.path.join(_data_dir(), "api.port")
        with open(p, "r", encoding="utf-8") as f:
            port = int(f.read().strip())
        return port if 1024 <= port <= 65535 else 0
    except Exception:
        return 0


def _open_help():
    """调用运行中主程序的 /help 接口打开帮助页面；失败则兜底打开使用说明。"""
    import urllib.request
    port = _api_port()
    if port:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/help", method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                resp.read()
            return
        except Exception:
            pass
    # 兜底：打开使用说明文件
    p = os.path.join(INSTALL_DIR, "使用说明.md")
    if os.path.exists(p):
        try:
            subprocess.Popen(["explorer", os.path.abspath(p)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _is_control_panel_uninstall():
    """判断是否由控制面板/系统卸载入口调用（参数里带 /S 或 /u）。"""
    if len(sys.argv) <= 1:
        return False
    a = sys.argv[1].lower().lstrip("-/")
    return a in ("u", "uninstall") or a.startswith("uninstall")


def _is_silent_install():
    """判断是否要求静默安装（参数带 /S /silent /quiet）。"""
    if len(sys.argv) <= 1:
        return False
    a = sys.argv[1].lower().lstrip("-/")
    return a in ("s", "silent", "quiet")


def _choose_uninstall_or_repair():
    """弹出选择：卸载 / 重装。返回 'uninstall' | 'repair' | 'cancel'。"""
    msg = (
        "检测到已安装过本软件。\n\n"
        "· 重装：保留之前的下载文件和设置，重新安装一遍（可修复异常）\n"
        "· 卸载：删除软件，但保留下载目录里的文件和配置\n"
        "· 取消：不执行任何操作"
    )
    return _show_result_dialog(
        f"卸载 / 重装 {APP_NAME}",
        msg,
        [("取消", "cancel"), ("卸载", "uninstall"), ("重装", "repair")]
    )


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
    _log(f"repair: helper={helper} installer_tmp={installer_tmp} install_dir={INSTALL_DIR} pid={pid}")
    try:
        subprocess.Popen([helper, "__DO_REPAIR__", INSTALL_DIR, str(pid), installer_tmp],
                         creationflags=0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _log(f"repair: Popen failed: {e}")
        # 降级：直接启动临时安装器（会弹出安装界面）
        try:
            subprocess.Popen([installer_tmp],
                             creationflags=0x00000008,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e2:
            _log(f"repair: fallback Popen also failed: {e2}")
    _log("repair helper launched, exiting")
    time.sleep(0.3)  # 确保子进程已创建
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


def _is_uninstaller_exe() -> bool:
    """当前可执行文件是否为卸载器副本。"""
    name = os.path.basename(sys.executable).lower()
    return name in ("卸载.exe", "uninstall.exe")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "__DO_UNINSTALL__":
        _do_uninstall_from_helper()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "__DO_REPAIR__":
        _do_repair_from_helper()
        return
    arg = sys.argv[1].lower().lstrip("-/") if len(sys.argv) > 1 else ""

    # 卸载器被 /S /silent /quiet 调用 → 静默卸载
    if _is_uninstaller_exe() and arg in ("s", "silent", "quiet"):
        uninstall()
        return
    # 卸载器被 /U 调用 → 卸载
    if _is_uninstaller_exe() and arg in ("u", "uninstall"):
        uninstall()
        return

    # 安装器被 /S /silent /quiet 调用 → 静默安装
    if arg in ("s", "silent", "quiet"):
        try:
            install()
        except Exception:
            pass
        return
    if arg in ("u", "uninstall"):
        uninstall()
        return
    if _is_already_running():
        _message_box("安装器已经在运行。")
        return
    # 控制面板调用直接卸载；用户主动双击卸载器则给选择；静默模式直接卸载
    is_silent = _is_silent_install()
    if os.path.basename(sys.executable).lower() in ("卸载.exe", "uninstall.exe") and not _is_control_panel_uninstall() and not is_silent:
        choice = _choose_uninstall_or_repair()
        if choice == "repair":
            _repair()
            return
        if choice == "uninstall":
            uninstall()
            return
        return
    silent = _is_silent_install()
    try:
        install()
        if not silent:
            choice = _show_result_dialog(
                "安装完成",
                f"{APP_NAME} v{VERSION} 已安装到：\n{INSTALL_DIR}\n\n"
                "· 软件已自动在后台启动，托盘区可找到图标\n"
                "· 浏览器扩展：在扩展页选「加载已解压的扩展程序」，"
                "指向安装目录下的 browser-extension 文件夹\n"
                "· 任务记录和设置会自动保留",
                [("查看帮助", "help"), ("开始使用", "done")]
            )
            if choice == "help":
                _open_help()
    except Exception as e:
        if not silent:
            _message_box(f"安装失败：{type(e).__name__}: {e}", "安装失败", 0x10)
        raise


if __name__ == "__main__":
    main()
'''


def _zip_payload(dst: str) -> str:
    """v3.1.12：保存 ZIP 到文件（供 PyInstaller datas 二进制直嵌），不再 base64 编码。"""
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(SRC_DIR):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, SRC_DIR)
                zf.write(full, arc)
    return dst


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

    # 1) 生成 setup.py（v3.1.12：ZIP 以二进制直嵌，不再 base64 编码）
    setup_name = f"小助下载器_v{VERSION}_安装包"
    payload_zip = os.path.join(OUT_DIR, "payload.zip")
    _zip_payload(payload_zip)
    setup_py = os.path.join(OUT_DIR, "setup.py")
    with open(setup_py, "w", encoding="utf-8") as f:
        f.write(SETUP_PY_HEADER.format(app_name=APP_NAME, app_id=APP_ID, version=VERSION))
        f.write(SETUP_PY_FOOTER)

    # 2) 复制图标
    ico_src = os.path.join(ROOT, "_assets", "tray.ico")
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
