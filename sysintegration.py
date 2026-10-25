#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小助下载器 · 系统集成  sysintegration.py
=======================================
零依赖实现 Windows 系统集成（纯 winreg + subprocess）：

  1. 开机自启      —— 写 HKCU\\...\\Run，静默后台启动，不弹窗
  2. 协议注册      —— 注册 xzd:// 自定义协议，供浏览器扩展唤起下载器
  3. 单实例控制    —— 通过本地接口探测，避免重复启动
  4. 首次安装引导  —— 一键完成上述全部配置

所有写入都限定在 **当前用户** 范围内（HKCU），不需要管理员权限。
卸载时（uninstall_all）会把写入的注册表项清理干净。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import winreg

APP_NAME = "小助下载器"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "XiaozhuDownloader"
PROTOCOL_KEY = r"Software\Classes\xzd"
APP_KEY = r"Software\XiaozhuDownloader"

def _discover_python_candidates() -> list:
    """动态探测本机 Python 解释器候选（自启命令必须是绝对路径）。

    覆盖顺序：当前解释器同目录 → %LOCALAPPDATA%\\Programs\\Python\\* →
    C:\\Python* → 系统 PATH 扫到的安装目录；不写死任何用户名。
    """
    cands, seen = [], set()

    def add(p):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)

    # 1) 当前解释器同目录优先（最常见的自装场景）
    if sys.executable:
        d = os.path.dirname(os.path.abspath(sys.executable))
        for name in ("pythonw.exe", "python.exe"):
            add(os.path.join(d, name))

    # 2) 标准用户级安装位置（%LOCALAPPDATA%\\Programs\\Python\\Python3xx）
    roots = []
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    for base in roots:
        for sub in ("Programs/Python", "Python"):
            root = os.path.join(base, *sub.split("/"))
            if not os.path.isdir(root):
                continue
            try:
                for name in sorted(os.listdir(root), reverse=True):
                    d = os.path.join(root, name)
                    if os.path.isdir(d):
                        for exe in ("pythonw.exe", "python.exe"):
                            add(os.path.join(d, exe))
            except OSError:
                pass

    # 3) 盘根目录常见安装点（C:\\Python313 等）
    for drive in ("C:", "D:"):
        for name in ("Python313", "Python312", "Python311", "Python310"):
            add(os.path.join(drive + "\\", name, "pythonw.exe"))
    return cands


PY_CANDIDATES = _discover_python_candidates()


def base_dir() -> str:
    """程序资源目录（源码运行 = 本文件目录；冻结运行 = exe 目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def launch_target() -> tuple[str, str]:
    """返回 (可执行文件, 参数前缀)，把"怎么启动自己"这件事集中到一处。

    源码运行  → (pythonw.exe, '"<项目>/gui.py"')
    冻结运行  → (<安装目录>/小助下载器.exe, '')   ← 打包后自启/唤起都用它
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), ""
    py = find_python_gui()
    gui = os.path.join(base_dir(), "gui.py")
    return py, f'"{gui}"'


def find_python_gui() -> str:
    """优先 pythonw.exe（无控制台窗口，静默启动的关键）"""
    for p in PY_CANDIDATES:
        if os.path.exists(p):
            return p
    # 回退：当前解释器的同目录 pythonw
    exe = sys.executable
    d = os.path.dirname(exe)
    for name in ("pythonw.exe", "python.exe"):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return exe


def autostart_command(silent: bool = True) -> str:
    """生成自启命令行（带引号，路径含空格也安全）"""
    exe, prefix = launch_target()
    args = " --silent" if silent else ""
    if prefix:
        return f'"{exe}" {prefix}{args}'
    return f'"{exe}"{args}'


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------

def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(v)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable_autostart(silent: bool = True) -> tuple[bool, str]:
    cmd = autostart_command(silent)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
        return True, cmd
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def disable_autostart() -> tuple[bool, str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        return True, "已关闭开机自启"
    except FileNotFoundError:
        return True, "原本就未开启"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# 自定义协议 xzd://  —— 让浏览器能唤起下载器
# --------------------------------------------------------------------------

def is_protocol_registered() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY) as k:
            winreg.QueryValueEx(k, "URL Protocol")
            return True
    except OSError:
        return False


def register_protocol() -> tuple[bool, str]:
    """注册 xzd:// 协议，浏览器打开该链接即唤起下载器（静默模式）"""
    exe, prefix = launch_target()
    # 协议处理器：接收 URL 作为 %1 参数
    if prefix:
        cmd = f'"{exe}" {prefix} "%1"'
    else:
        cmd = f'"{exe}" "%1"'
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:小助下载器 Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                PROTOCOL_KEY + r"\DefaultIcon", 0,
                                winreg.KEY_SET_VALUE) as k:
            ico = os.path.join(base_dir(), "_internal", "tray.ico")
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, ico if os.path.exists(ico) else exe)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                PROTOCOL_KEY + r"\shell\open\command", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
        return True, f"xzd:// 协议已注册"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def unregister_protocol() -> tuple[bool, str]:
    try:
        _delete_tree(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY)
        return True, "xzd:// 协议已注销"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _delete_tree(root, path: str):
    """递归删除注册表键"""
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + sub)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        # 静默是有意为之：注册表键/子键本就不存在时视为「已删除」，属预期情况
        pass


# --------------------------------------------------------------------------
# 单实例 / 唤起
# --------------------------------------------------------------------------

def api_port(default: int = 8763) -> int:
    """本实例使用的本地接口端口。

    环境变量 XZD_API_PORT 可覆盖（供启动计时/并行测试使用：
    多个实例各用各的端口，就不会因单实例保护互相拦截）。
    """
    try:
        v = int(os.environ.get("XZD_API_PORT") or 0)
    except ValueError:
        v = 0
    return v if 1024 <= v <= 65535 else int(default)


def port_hint_path() -> str:
    """端口提示文件：运行中的实例把自己的实际端口写在这里。

    为什么需要它（v1.6.0 关键优化）：
      原先每次启动都要从 8763 起盲扫 13 个端口判断"是否已有实例在跑"。
      本机 127.0.0.1 对**无人监听**的端口不回 RST，connect_ex 只能一直等到
      超时（实测每个 0.35s × 13 = 4.62s）—— 启动时白白卡 4.6 秒。
      现在改为：先直接读端口提示文件（一次 open，微秒级），
      只有提示文件缺失/失效时才退回端口扫描。
    """
    return os.path.join(data_dir(), "api.port")


def read_port_hint() -> int:
    try:
        with open(port_hint_path(), "r", encoding="utf-8") as f:
            p = int(f.read().strip())
        return p if 1024 <= p <= 65535 else 0
    except Exception:
        return 0


def write_port_hint(port: int):
    try:
        p = port_hint_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(port)))
        os.replace(tmp, p)
    except Exception:
        # 静默是有意为之：端口提示文件写失败只影响下次启动的探测速度，不影响功能
        pass


def clear_port_hint():
    try:
        os.remove(port_hint_path())
    except Exception:
        # 静默是有意为之：提示文件可能本就不存在（首次启动）
        pass


def _probe_one(p: int, timeout: float = 0.30) -> bool:
    """单个端口探活：能 HTTP /ping 通且是我们的服务才算数"""
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        if s.connect_ex(("127.0.0.1", p)) != 0:
            return False
    finally:
        s.close()
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/ping",
                                    timeout=min(2.0, timeout * 4)) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
            return j.get("app") == APP_NAME
    except Exception:
        return False


def _listen_ports_fast() -> set:
    """一次性拿到本机所有 TCP 监听端口（纯 ctypes 调 GetExtendedTcpTable）。

    为什么需要（v1.6.0 第二记重锤）：
      本机/部分安全软件环境下，127.0.0.1 对**未监听**端口不回 RST，
      connect_ex 只能一直等到超时（实测 0.35s/端口）。
      与其"逐个试探再等超时"，不如**一次性问系统要监听表**——
      一次 API 调用（实测 < 2ms）就知道了全部真相，不用赌超时。

    返回监听端口集合；任何异常都返回空集合（调用方自动回退到慢路径）。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        AF_INET = 2
        TCP_TABLE_OWNER_PID_LISTENER = 3
        size = wintypes.DWORD(0)
        # 第一次调用只为拿所需缓冲区大小（返回 ERROR_INSUFFICIENT_BUFFER=122）
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                     AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if size.value <= 0:
            return set()
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                           AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != 0:
            return set()
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        # MIB_TCPROW_OWNER_PID：6 个 DWORD，第 3 个是 dwLocalPort（网络字节序，低 16 位）
        row_size = 24
        base = ctypes.addressof(buf) + 4
        ports = set()
        for i in range(min(int(n), 4096)):
            row = ctypes.cast(base + i * row_size, ctypes.POINTER(wintypes.DWORD))
            raw = int(row[2]) & 0xFFFF
            ports.add(((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF))
        return ports
    except Exception:
        return set()


def probe_running(port: int = 8763, span: int = 12,
                  budget: float = 0.9) -> tuple[bool, int]:
    """探测下载器是否在运行（返回 是否运行, 实际端口）。

    三级策略（从快到慢）：
      ① 监听表快查（< 2ms）：一次 GetExtendedTcpTable 拿到全部 LISTEN 端口，
         候选区间内有命中才去 HTTP /ping 确认 —— 绝大多数情况走这条
      ② 端口提示文件（< 5ms）：上次运行的实例写在 data/api.port
      ③ 区间扫描（≤ budget 秒）：仅在前两者都失效时兜底，
         绝不因为"本机不回 RST"把启动拖成 4.6 秒
    """
    port = api_port(port)
    cands = list(range(port, port + span))

    # ---- ① 监听表快查 ----
    listen = _listen_ports_fast()
    if listen:
        alive = [p for p in cands if p in listen]
        for p in alive:
            if _probe_one(p, timeout=0.25):
                return True, p
        hint = read_port_hint()
        if hint and hint in listen and hint not in cands:
            if _probe_one(hint, timeout=0.25):
                return True, hint
        # 监听表是权威的：候选区间内没有监听 → 直接判定"没在跑"，
        # 不必再靠超时一个个试（这一句是 4.6s → 2ms 的关键）
        if not alive and not (hint and hint in listen):
            return False, port

    # ---- ② 端口提示文件 ----
    hint = read_port_hint()
    if hint and _probe_one(hint, timeout=0.25):
        return True, hint
    if _probe_one(port, timeout=0.25):
        return True, port

    # ---- ③ 区间扫描兜底（带硬预算）----
    t0 = time.time()
    for p in cands:
        if p in listen or (hint and p == hint):
            continue
        if time.time() - t0 > budget:
            break
        if _probe_one(p, timeout=0.25):
            return True, p
    return False, port


def data_dir() -> str:
    """可写数据目录（与 dlcore.data_root 保持一致，避免循环导入故本地实现）"""
    env = os.environ.get("XZD_DATA_DIR")
    if env:
        d = os.path.abspath(env)
        os.makedirs(d, exist_ok=True)
        return d
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
    else:
        d = os.path.join(base_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def launch_silent(wait: bool = False, timeout: float = 20.0) -> tuple[bool, str]:
    """静默启动下载器（后台，无窗口）；已在运行则直接返回成功"""
    running, _ = probe_running()
    if running:
        return True, "已在运行"
    exe, prefix = launch_target()
    # DETACHED_PROCESS + CREATE_NO_WINDOW：彻底无控制台窗口
    DETACHED = 0x00000008
    flags = DETACHED | 0x00000200  # CREATE_NEW_PROCESS_GROUP
    if prefix:
        argv = [exe, os.path.join(base_dir(), "gui.py"), "--silent"]
    else:
        argv = [exe, "--silent"]
    try:
        subprocess.Popen(argv, cwd=base_dir(),
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"启动失败：{type(e).__name__}: {e}"
    if not wait:
        return True, "已静默启动"
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        ok, _ = probe_running()
        if ok:
            return True, "已启动并就绪"
        time.sleep(0.4)
    return False, "启动超时"


def wake_from_protocol(url: str) -> bool:
    """解析 xzd:// 链接并处理（供 gui.py 收到协议调用时使用）
    xzd://add?url=<下载地址>&referer=<来源>
    xzd://show            仅显示主窗口
    返回是否处理成功
    """
    import urllib.parse
    if not url or not url.lower().startswith("xzd:"):
        return False
    parsed = urllib.parse.urlsplit(url)
    action = (parsed.netloc or parsed.path or "").strip("/").lower()
    q = urllib.parse.parse_qs(parsed.query)
    if action in ("show", ""):
        return True
    if action == "add":
        target = (q.get("url") or [""])[0]
        ref = (q.get("referer") or [""])[0]
        name = (q.get("filename") or [""])[0]
        if target:
            return ("__ADD__", target, ref, name)  # 交由 gui 添加任务
    return False


# --------------------------------------------------------------------------
# 单实例保护 —— 把唤起请求转交给已在运行的实例
# --------------------------------------------------------------------------

def read_token() -> str:
    """读取运行中实例的接口令牌（同一用户的本机进程可读）"""
    try:
        with open(os.path.join(data_dir(), "token.txt"),
                  "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def send_wake(link: str, port: int = 0, timeout: float = 4.0) -> bool:
    """把 xzd:// 链接交给已在运行的实例处理；成功返回 True"""
    import urllib.parse
    import urllib.request
    tok = read_token()
    if not tok:
        return False
    if not port:
        running, port = probe_running()
        if not running:
            return False
    qs = urllib.parse.urlencode({"link": link, "token": tok})
    url = f"http://127.0.0.1:{port}/wake?{qs}"
    try:
        req = urllib.request.Request(url)
        req.add_header("X-XZD-Token", tok)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json
            d = json.loads(r.read().decode("utf-8", "replace"))
            return bool(d.get("ok"))
    except Exception:
        return False


def handoff(silent: bool, protocol: str) -> bool:
    """单实例保护：若已有实例在跑，把本次请求转交给它。
    返回 True 表示已转交/无需启动（调用方应直接结束）。

    规则：
      · 带 xzd:// 链接      → 转交链接（点下载链接时的主路径）
      · 无参数（显示窗口）  → 转交 xzd://show
      · 静默启动且已在运行  → 直接退出，不重复启动
    """
    running, port = probe_running()
    if not running:
        return False
    if protocol:
        link = protocol
    elif not silent:
        link = "xzd://show"
    else:
        return True       # 静默且已在运行：无需做任何事
    if send_wake(link, port):
        return True
    # 转交失败：宁可不显示窗口，也绝不启动第二个实例
    # （双实例会争抢同一份 jobs.json，可能损坏任务列表）
    return True


# --------------------------------------------------------------------------
# 一键安装 / 卸载
# --------------------------------------------------------------------------

def install_all(silent: bool = True) -> list[str]:
    """一键完成：开机自启 + 协议注册"""
    msgs = []
    ok, info = enable_autostart(silent)
    msgs.append(("开机自启已开启" if ok else "开机自启失败") + f"：{info}")
    ok2, info2 = register_protocol()
    msgs.append(info2)
    return msgs


def uninstall_all() -> list[str]:
    msgs = []
    ok, info = disable_autostart()
    msgs.append(info)
    ok2, info2 = unregister_protocol()
    msgs.append(info2)
    return msgs


def status() -> dict:
    running, port = probe_running()
    return {
        "autostart": is_autostart_enabled(),
        "protocol": is_protocol_registered(),
        "running": running,
        "port": port,
        "python": find_python_gui(),
        "command": autostart_command(),
    }


if __name__ == "__main__":
    print(f"{APP_NAME} · 系统集成状态")
    print("=" * 56)
    st = status()
    print(f"  开机自启    : {'已开启' if st['autostart'] else '未开启'}")
    print(f"  xzd:// 协议 : {'已注册' if st['protocol'] else '未注册'}")
    print(f"  运行状态    : {'运行中，端口 ' + str(st['port']) if st['running'] else '未运行'}")
    print(f"  Python      : {st['python']}")
    print(f"  自启命令    : {st['command']}")
    if len(sys.argv) > 1:
        if sys.argv[1] == "install":
            for m in install_all():
                print("  " + m)
        elif sys.argv[1] == "uninstall":
            for m in uninstall_all():
                print("  " + m)
        elif sys.argv[1] == "start":
            print("  " + launch_silent(wait=True)[1])
