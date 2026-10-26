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
import time

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
    # ★ v3.1.9：不再用 shutil.rmtree 删除旧产物目录。
    #   一是它会触发工作区的"批量删除安全策略"（要删 900+ 个文件，远超阈值），
    #   导致整个构建在最后一步被拦下、必须人工接手；
    #   二是改名归档比直接删除更安全（构建出问题还能回退对比）。
    # ★ v3.1.10：归档名带时间戳，**绝不删除既有归档**。
    #   原实现先 `rmtree(archive)` 再 rename 到固定名 —— 既踩安全策略
    #   （实测 `SAFE_DELETE_BULK_CONFIRM_REQUIRED count=987`），
    #   又在"归档已存在"时把整个构建卡在最后一步。改为每次一个新名字，
    #   归档只增不删（定期由人工清理即可）。
    if os.path.exists(DIST):
        archive = os.path.join(os.path.dirname(DIST),
                               "_old_产物归档_%d" % int(time.time()))
        try:
            os.rename(DIST, archive)
            print(f"[build_app] 旧产物已归档: {os.path.basename(archive)}")
        except OSError as exc:
            # 改名失败（如目录被占用）时**不删除**，如实报错让人处理。
            print(f"[build_app] 旧产物归档失败（未删除，请手动处理）: {exc}",
                  file=sys.stderr)
            sys.exit(4)
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

    # v3.1.9: 浏览器扩展改为**文件夹**分发（不再打 crx）。
    # 原因：Chrome/Edge 会拦截本地 .crx 的拖拽安装（提示"无法从该网站添加应用"），
    # 用户还要手动改后缀解压，体验差且容易卡住。
    # 直接给 browser-extension 文件夹 → 「加载已解压的扩展程序」一步到位。
    ext_src = os.path.join(ROOT, "_assets", "browser-extension")
    ext_dst = os.path.join(app_dir, "browser-extension")
    if os.path.isdir(ext_src):
        if os.path.exists(ext_dst):
            shutil.rmtree(ext_dst, ignore_errors=True)
        shutil.copytree(ext_src, ext_dst)
        # 剔除可能残留的打包垃圾
        for junk in ("default-settings.png",):
            p = os.path.join(ext_dst, junk)
            if os.path.exists(p):
                os.remove(p)
        n = sum(len(fs) for _r, _d, fs in os.walk(ext_dst))
        print(f"[build_app] 已复制浏览器扩展文件夹: browser-extension/（{n} 个文件）")

    # 生成带箭头标注的扩展安装指引图
    guide_script = os.path.join(ROOT, "_t", "gen_extension_guide.py")
    guide_out = os.path.join(app_dir, "浏览器扩展安装指引.png")
    if os.path.exists(guide_script):
        rc = subprocess.run([py, guide_script, guide_out]).returncode
        if rc == 0:
            print("[build_app] 已生成扩展安装指引图")
        else:
            print("[build_app] 扩展安装指引图生成失败", file=sys.stderr)

    # v3.1.9: 帮助页要展示「默认设置截图」，放 exe 同级供运行时加载。
    #   优先用 _assets/ 里已生成好的；没有则现场渲染一张。
    shot_dst = os.path.join(app_dir, "default-settings.png")
    shot_src = os.path.join(ROOT, "_assets", "default-settings.png")
    shot_script = os.path.join(ROOT, "_t", "gen_settings_shot.py")
    made = False
    if os.path.exists(shot_script):
        rc = subprocess.run([py, shot_script, shot_dst]).returncode
        made = (rc == 0 and os.path.exists(shot_dst))
    if not made and os.path.exists(shot_src):
        shutil.copy2(shot_src, shot_dst)
        made = True
    if made:
        print("[build_app] 已生成默认设置截图: default-settings.png")
    else:
        print("[build_app] 默认设置截图生成失败（帮助页将显示提示文字）",
              file=sys.stderr)

    # 顺手清理 PyInstaller 默认 dist/ 空目录
    try:
        os.rmdir(os.path.dirname(DIST_SRC))
    except OSError:
        pass

    print(f"[build_app] 完成: {app_dir}")


if __name__ == "__main__":
    run()
