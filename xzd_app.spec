# -*- mode: python ; coding: utf-8 -*-
"""
小助下载管理器 XZDM · PyInstaller 打包配置（v3.0.3）
==========================================
产物形态：**one-dir**（`小助下载器.exe` + `_internal/`），不是 one-file。

为什么选 one-dir（这是刻意的取舍）：
  · 本程序是**常驻托盘**的后台下载器，开机自启 + 长期运行。
    one-file 每次启动都要把几十 MB 解压到 %TEMP%，自启时既慢又反复占磁盘；
    one-dir 启动是直接的 exe 映射，启动耗时和常驻内存都更低。
  · 用户要的是「双击安装 → 之后一直安静跑」，不是「每次启动等解压」。

资源策略：
  · 浏览器扩展以**文件夹** browser-extension/ 形式拷到 exe 同级（v3.1.9 起，
    不再打 crx —— 浏览器会拦截本地 crx 安装，用户还得手动改后缀解压）。
  · 使用说明.md / 浏览器扩展安装指引.png / default-settings.png 同样拷到 exe 同级，
    这样用户能直接看到扩展文件、说明图和默认设置截图。
  · tray.ico 放在 _assets/ 下，由 PyInstaller datas 打包到 _internal/，
    安装目录根目录不再保留图标文件。
  · third_party/aria2/ 内置 aria2c.exe + N_m3u8DL-RE.exe（v2.6.0），
    安装包自包含，用户无需额外安装即可使用 aria2c 备用极速通道。
  · third_party/ffmpeg.exe（v3.1.10 起）—— HLS 的 ts→mp4 无损转封装、
    「仅音频」抽轨、自动/手动音视频合并都依赖它。此前漏打，导致
    **打包版这四个功能静默失效**（源码运行却正常）。

体积取舍（实测）：
  · Pillow  7.0 MB  → 带。托盘「下载中/出错/暂停」三色图标靠它生成；
                       没有它托盘只能永远是同一个图标，状态反馈差一档。
  · cryptography 10.3 MB → 带。HLS(m3u8) 的 AES-128 解密走它；
                       没有它会退化到纯 Python AES（慢 20~50 倍，拖垮 m3u8 下载）。
  · aria2c 5.4 MB + N_m3u8DL-RE 16.4 MB → 带。v2.6.0 备用极速通道必需，
                       安装后即可直接用，不再依赖外部安装。
  · ffmpeg.exe 58.2 MB → 带（v3.1.10 起）。HLS 转 mp4 / 音频提取 /
                       音视频合并的唯一依赖，缺它则功能静默失效。
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
        # 图标放在 _assets/，打包时会落到 _internal/；运行时也会释放到 exe 同级
        (os.path.join(ROOT, "_assets", "tray.ico"), "."),
        # v2.6.0：内置 aria2c + N_m3u8DL-RE，安装包自包含，无需用户安装
        (os.path.join(ROOT, "third_party", "aria2"), "third_party/aria2"),
        # ★ v3.1.10 修复（探针 probe-frozen-ffmpeg.py 实证，真实产品缺陷）：
        #   ffmpeg.exe 此前**从未被打包** —— `datas` 只有 aria2 一项，
        #   而 `app_root()` 在 frozen 下返回 exe 同级的 `_internal/`，
        #   `third_party/ffmpeg.exe` 在那里不存在 → **四处功能静默失效**：
        #     · `_remux_to_mp4()`   找不到就 `return`（完全静默）
        #     → HLS 的 ts 无损转 mp4 不执行，交付物停在 .ts
        #     · `_extract_audio()`  → 抛"缺少 FFmpeg，无法提取音频"
        #     · `_merge_with_audio()` → 抛"缺少 FFmpeg，无法合并音视频"
        #     · `merge_av()`（手动合并窗口）→ 报缺少 FFmpeg
        #   用户可见后果（frozen 实测）：HLS 任务拿到的是
        #     `smoke stream.ts` 而不是 `.mp4`；「仅音频」「音视频合并」
        #     在装好的正式版里直接不可用 —— 而源码运行时一切正常，
        #     只有打包版才坏（最典型的一类"打包遗漏"）。
        #   代价：安装包 +60MB（ffmpeg.exe 60,993,536 字节）。
        #   取舍：这三个功能是本产品的核心卖点之一（HLS/B站/YouTube
        #     视频站点下载），不能为了体积把它砍掉。
        (os.path.join(ROOT, "third_party", "ffmpeg.exe"), "third_party"),
    ],
    hiddenimports=[
        # gui.py 内部有惰性 import（import tray / import sysintegration），
        # 静态分析看不到，必须显式声明，否则运行到那一步会 ImportError
        "tray", "localserver", "sysintegration", "dlcore", "aria2_bridge",
        # v3.0.0: BT 支持
        "bt_trackers",
        # v3.0.1: 种子文件解析 + BT 自动规范化
        "torrent_parser",
        "bt_helper",
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
    icon=os.path.join(ROOT, "_assets", "tray.ico"),
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