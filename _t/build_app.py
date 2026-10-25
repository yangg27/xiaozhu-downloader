#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_app.py · 冻结小助下载器主程序
=====================================
用 PyInstaller 把 gui.py 打成 one-dir 形态，输出到 dist_app/。

注意：构建主程序**必须**使用带 tkinter 的系统 Python，否则运行报 No module _tkinter。
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
SPEC = os.path.join(ROOT, "xzd_app.spec")
# PyInstaller 默认把 one-dir 产物放在 dist/<name>/，我们复制到 dist_app/ 保持文档一致
DIST_SRC = os.path.join(ROOT, "dist", "小助下载器")
DIST = os.path.join(ROOT, "dist_app", "小助下载器")


def run():
    if not os.path.exists(SPEC):
        print(f"[错误] 找不到 spec 文件：{SPEC}", file=sys.stderr)
        sys.exit(2)

    # 优先使用环境变量 PYTHON，然后找系统 Python（带 tkinter），最后回退
    py = os.environ.get("PYTHON")
    if not py:
        candidates = [
            r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python313\python.exe",
            r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python312\python.exe",
            r"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe",
            r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                py = c
                break
    if not py:
        py = shutil.which("python") or sys.executable

    print(f"[build_app] 使用 Python: {py}")

    cmd = [py, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC]
    print(f"[build_app] 执行: {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if rc != 0:
        print("[build_app] PyInstaller 构建失败", file=sys.stderr)
        sys.exit(rc)

    if not os.path.isdir(DIST_SRC):
        print(f"[build_app] 找不到构建产物：{DIST_SRC}", file=sys.stderr)
        sys.exit(3)

    # 把产物挪到 dist_app/ 下（与文档一致）
    if os.path.exists(DIST):
        shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(os.path.dirname(DIST), exist_ok=True)
    shutil.move(DIST_SRC, DIST)

    # 把用户可见资源复制到 exe 同级
    app_dir = DIST
    for name, src_name in (
        ("使用说明.md", "使用说明.md"),
    ):
        src = os.path.join(ROOT, src_name)
        dst = os.path.join(app_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"[build_app] 已复制: {name}")

    # tray.ico 由 PyInstaller datas 直接打包到 _internal/，不再复制到 exe 同级

    # 把浏览器扩展源码打包为 .crx，只保留 crx 产物
    ext_src = os.path.join(ROOT, "_assets", "browser-extension")
    if os.path.isdir(ext_src):
        ext_crx = os.path.join(app_dir, "browser-extension.crx")
        ext_tmp = os.path.join(tempfile.gettempdir(), "browser-extension.zip")
        with zipfile.ZipFile(ext_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(ext_src):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, ext_src)
                    zf.write(full, arc)
        shutil.copy2(ext_tmp, ext_crx)
        try:
            os.remove(ext_tmp)
        except Exception:
            pass
        print("[build_app] 已打包 browser-extension.crx")

    # 生成带箭头标注的扩展安装指引图
    guide_script = os.path.join(ROOT, "_t", "gen_extension_guide.py")
    guide_out = os.path.join(app_dir, "浏览器扩展安装指引.png")
    if os.path.exists(guide_script):
        rc = subprocess.run([py, guide_script, guide_out]).returncode
        if rc == 0:
            print("[build_app] 已生成扩展安装指引图")
        else:
            print("[build_app] 扩展安装指引图生成失败", file=sys.stderr)

    # 顺手清理 PyInstaller 默认 dist/ 空目录
    try:
        os.rmdir(os.path.dirname(DIST_SRC))
    except OSError:
        pass

    print(f"[build_app] 完成: {app_dir}")


if __name__ == "__main__":
    run()
