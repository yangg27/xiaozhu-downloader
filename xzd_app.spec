# -*- mode: python ; coding: utf-8 -*-
"""
小助下载器 · PyInstaller 打包配置（v2.5.0）
==========================================
产物形态：**one-dir**（`小助下载器.exe` + `_internal/`），不是 one-file。

为什么选 one-dir（这是刻意的取舍）：
  · 本程序是**常驻托盘**的后台下载器，开机自启 + 长期运行。
    one-file 每次启动都要把几十 MB 解压到 %TEMP%，自启时既慢又反复占磁盘；
    one-dir 启动是直接的 exe 映射，启动耗时和常驻内存都更低。
  · 用户要的是「双击安装 → 之后一直安静跑」，不是「每次启动等解压」。

资源策略：
  · tray.ico / browser-extension/ / 使用说明.md 由构建脚本**拷到 exe 同级**，
    这样用户能直接看到「浏览器扩展」文件夹（需要手动在 Chrome/Edge 里加载），
    协议图标的 DefaultIcon 路径也才有效。
  · 同时把 tray.ico 塞一份进 _internal/（打包内），
    万一用户只拷了 exe+_internal 而漏了同级 ico，托盘也不会变成白板。
  · third_party/aria2/ 内置 aria2c.exe + N_m3u8DL-RE.exe（v2.5.0），
    安装包自包含，用户无需额外安装即可使用 aria2c 备用极速通道。

体积取舍（实测）：
  · Pillow  7.0 MB  → 带。托盘「下载中/出错/暂停」三色图标靠它生成；
                       没有它托盘只能永远是同一个图标，状态反馈差一档。
  · cryptography 10.3 MB → 带。HLS(m3u8) 的 AES-128 解密走它；
                       没有它会退化到纯 Python AES（慢 20~50 倍，拖垮 m3u8 下载）。
  · aria2c 5.4 MB + N_m3u8DL-RE 16.4 MB → 带。v2.5.0 备用极速通道必需，
                       安装后即可直接用，不再依赖外部安装。
  · numpy/pandas/matplotlib/pip/setuptools → **排除**（与本程序毫无关系，
                       不带它们能省 120MB+）。
"""

import os

ROOT = os.path.abspath(os.path.dirname(SPEC))
OUT = os.path.join(ROOT, "dist_app")

excludes = [
    # 与本程序无关的第三方大包（省 120MB+）
    "numpy", "pandas", "matplotlib", "scipy", "setuptools", "pip",
    "wheel", "pkg_resources", "IPython", "notebook", "pytest",
    "sqlite3", "xmlrpc", "pydoc_data", "lib2to3", "distutils",
    # 明确没用到、且容易把 PyInstaller 拉进一堆东西的模块
    "curses", "readline", "asyncio", "concurrent.futures.process",
    "multiprocessing", "test", "unittest", "doctest", "pickletools",
]

a = Analysis(
    ["gui.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # 内嵌一份图标（兜底）：one-dir 下会落到 _internal/
        (os.path.join(ROOT, "tray.ico"), "."),
        # v2.5.0：内置 aria2c + N_m3u8DL-RE，安装包自包含，无需用户安装
        (os.path.join(ROOT, "third_party", "aria2"), "third_party/aria2"),
    ],
    hiddenimports=[
        # gui.py 内部有惰性 import（import tray / import sysintegration），
        # 静态分析看不到，必须显式声明，否则运行到那一步会 ImportError
        "tray", "localserver", "sysintegration", "dlcore", "aria2_bridge",
        "tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk",
        "http.server", "http.client", "urllib.request", "urllib.parse",
        "urllib.error", "urllib.response", "ssl", "_ssl", "socket", "select",
        "ctypes", "ctypes.wintypes", "winreg", "subprocess", "secrets", "hmac",
        "hashlib", "base64", "queue", "threading", "concurrent.futures",
        "cryptography.hazmat.primitives.ciphers",
        "cryptography.hazmat.primitives.ciphers.algorithms",
        "cryptography.hazmat.primitives.ciphers.modes",
        "cryptography.hazmat.backends.openssl",
        "PIL.Image", "PIL.ImageFile",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,          # 去掉 assert 与 __debug__ 分支（本程序无 assert，纯省体积）
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="小助下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # 不用 UPX：UPX 压过的 exe 常被安全软件误报，且解压拖慢冷启动
    console=False,        # 无控制台窗口 ← 静默启动的关键
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "tray.ico"),
    version=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="小助下载器",
)