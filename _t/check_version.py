#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建前版本一致性校验。
读取 dlcore.py / _t/build_setup.py / gui.py 三处版本号，
不一致时退出非零并打印错误。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def get_versions() -> dict[str, str]:
    versions = {}

    dlcore = _read(os.path.join(ROOT, "dlcore.py"))
    m = re.search(r'ENGINE_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', dlcore)
    if not m:
        raise SystemExit("无法在 dlcore.py 中找到 ENGINE_VERSION")
    versions["dlcore.py"] = m.group(1)

    setup = _read(os.path.join(ROOT, "_t", "build_setup.py"))
    m = re.search(r'VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', setup)
    if not m:
        raise SystemExit("无法在 _t/build_setup.py 中找到 VERSION")
    versions["_t/build_setup.py"] = m.group(1)

    gui = _read(os.path.join(ROOT, "gui.py"))
    # 匹配模块注释里的版本，例如 "图形界面  gui.py   v2.6.3"
    m = re.search(r'gui\.py\s+v([0-9]+\.[0-9]+\.[0-9]+)', gui)
    if not m:
        raise SystemExit("无法在 gui.py 模块注释中找到版本")
    versions["gui.py"] = m.group(1)

    return versions

def main() -> int:
    versions = get_versions()
    vals = set(versions.values())
    if len(vals) != 1:
        print("版本号不一致，构建前请统一：")
        for f, v in versions.items():
            print(f"  {f}: {v}")
        return 1
    ver = vals.pop()
    print(f"版本一致性校验通过：{ver}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
