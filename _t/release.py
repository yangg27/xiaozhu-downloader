#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地一键发布脚本。
用法：
    python _t/release.py [tag]

未提供 tag 时，自动读取 dlcore.py 中的 ENGINE_VERSION 并生成 vX.Y.Z。
流程：
    1. 校验三处版本号一致
    2. 确保 git 工作区干净
    3. 自动打 tag（若不存在）
    4. 构建主程序 + 安装包 + 便携版 zip
    5. 调用 gh CLI 创建/更新 Release 并上传 asset
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, cwd=None, check=True):
    print(f"[release] {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd or ROOT)
    if check and r.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(cmd)}")
    return r


def version_from_source() -> str:
    import re
    with open(os.path.join(ROOT, "dlcore.py"), "r", encoding="utf-8") as f:
        m = re.search(r'ENGINE_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', f.read())
    if not m:
        raise SystemExit("无法从 dlcore.py 读取 ENGINE_VERSION")
    return m.group(1)


def ensure_clean():
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    if r.stdout.strip():
        print("git 工作区有未提交改动：")
        print(r.stdout)
        raise SystemExit("请先提交或清理改动")


def build_all(tag: str):
    # 主程序
    run([sys.executable, "_t/build_app.py"])
    # 安装包
    run([sys.executable, "_t/build_setup.py"])
    # 便携版 zip
    portable = os.path.join(ROOT, "dist_setup", f"xiaozhu-downloader-{tag}-portable.zip")
    src = os.path.join(ROOT, "dist_app", "小助下载器")
    if os.path.exists(portable):
        os.remove(portable)
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, src)
                zf.write(full, arc)
    print(f"[release] 便携版: {portable}")


def find_assets(tag: str) -> tuple[str, str]:
    setup_name = f"xiaozhu-downloader-{tag}-setup.exe"
    portable_name = f"xiaozhu-downloader-{tag}-portable.zip"
    setup_path = os.path.join(ROOT, "dist_setup", setup_name)
    portable_path = os.path.join(ROOT, "dist_setup", portable_name)
    if not os.path.exists(setup_path):
        raise SystemExit(f"找不到安装包：{setup_path}")
    if not os.path.exists(portable_path):
        raise SystemExit(f"找不到便携版：{portable_path}")
    return setup_path, portable_path


def sha256(path: str) -> str:
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def release_notes(tag: str, setup_sha: str, zip_sha: str, zip_size_mb: float) -> str:
    return f"""## 下载

| 版本 | 文件 | 说明 |
|---|---|---|
| **安装包** | `xiaozhu-downloader-{tag}-setup.exe` | 双击安装，自包含，无需 Python |
| **便携版** | `xiaozhu-downloader-{tag}-portable.zip` | 解压即用，启动更快，适合常驻/自启 |

## 校验
- 安装包 SHA256：`{setup_sha}`
- 便携版 SHA256：`{zip_sha}`

## 说明
- 安装包：one-file 单文件，内含全部依赖（Python 运行时 + aria2c + ffmpeg）
- 便携版：one-dir 结构，解压后运行 `小助下载器.exe`，启动速度更快
- 浏览器扩展位于安装目录下的 browser-extension 文件夹，请手动加载一次
- 任务记录、配置和历史在 `%APPDATA%\\小助下载器`，卸载/重装默认保留

详见 [README.md](./README.md) 与 [使用说明.md](./使用说明.md)。
"""


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else f"v{version_from_source()}"
    if not tag.startswith("v"):
        tag = "v" + tag

    print(f"[release] 目标版本：{tag}")

    # 1. 版本校验
    run([sys.executable, "_t/check_version.py"])

    # 2. 工作区干净
    ensure_clean()

    # 3. tag
    r = subprocess.run(["git", "rev-parse", tag], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        run(["git", "tag", "-a", tag, "-m", f"release({tag})"])
        print(f"[release] 已创建 tag {tag}")
    else:
        print(f"[release] tag {tag} 已存在")

    # 4. 构建
    build_all(tag)

    # 5. 发布
    setup_path, portable_path = find_assets(tag)
    setup_sha = sha256(setup_path)
    zip_sha = sha256(portable_path)
    zip_size_mb = round(os.path.getsize(portable_path) / (1024 * 1024), 1)

    notes_path = os.path.join(ROOT, "dist_setup", "release_notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(release_notes(tag, setup_sha, zip_sha, zip_size_mb))

    # 使用 gh CLI 创建/编辑 release
    r = subprocess.run(["gh", "release", "view", tag], cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[release] Release {tag} 已存在，更新 assets 和 notes")
        run(["gh", "release", "upload", tag, setup_path, portable_path, "--clobber"])
        run(["gh", "release", "edit", tag, "--notes-file", notes_path, "--title", f"小助下载器 {tag}"])
    else:
        run(["gh", "release", "create", tag, setup_path, portable_path,
             "--title", f"小助下载器 {tag}",
             "--notes-file", notes_path])

    print(f"[release] 完成：{tag}")


if __name__ == "__main__":
    main()
