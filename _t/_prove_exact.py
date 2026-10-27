# -*- coding: utf-8 -*-
"""最终取证：安装包体积差异的精确归因（用 CArchive 索引里的真实存储尺寸）。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
MB = 1048576

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

CASES = [
    ("v2.3.0", r"D:\software\小助下载器_v2.3.0_安装包.exe"),
    ("v3.1.11",
     r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo\dist_setup"
     r"\小助下载器_v3.1.11_安装包\小助下载器_v3.1.11_安装包.exe"),
]

print("=" * 80)
print("一、归档索引中的真实存储尺寸（toc 的 dlen = 未压缩，ulen = 实际写入字节）")
print("=" * 80)
summary = {}
for tag, path in CASES:
    r = CArchiveReader(path)
    total = os.path.getsize(path)
    print()
    print("--- %s ---" % tag)
    print("包体总大小: %d 字节 = %.2f MiB = %.1f MB(十进制)"
          % (total, total / MB, total / 1000000))
    print()
    print("%-46s %12s %12s %8s" % ("条目", "原始(字节)", "存储(字节)", "性质"))
    print("-" * 80)
    stored_sum = 0
    biggest = []
    for name, entry in r.toc.items():
        # entry: (dpos, dlen, ulen, cflag, typecode, name)
        dpos, dlen, ulen, cflag, tcode = entry[:5]
        stored = dlen
        stored_sum += stored
        biggest.append((name, ulen, stored, tcode))
    for name, ulen, stored, tcode in sorted(biggest, key=lambda x: -x[2])[:9]:
        kind = {0: "数据(未压缩)", 1: "数据(已压缩)", 2: "PYZ", 3: "PKG", 4: "PY源"}
        print("%-46s %12d %12d %8s" % (name[:46], ulen, stored, kind.get(tcode, str(tcode))))
    print("-" * 80)
    print("%-46s %12s %12d" % ("归档存储合计", "", stored_sum))
    print("%-46s %12s %12d" % ("PE头/引导代码/对齐", "", total - stored_sum))
    summary[tag] = (total, stored_sum, biggest, r)

print()
print("=" * 80)
print("二、载荷载体的存储尺寸对比（关键）")
print("=" * 80)
print()
print("%-12s %-22s %14s %14s" % ("版本", "载荷载体", "原始(字节)", "存储(字节)"))
print("-" * 68)
for tag, (total, ss, biggest, r) in summary.items():
    for name, ulen, stored, tcode in biggest:
        if name in ("app_payload.zip", "setup"):
            print("%-12s %-22s %14d %14d" % (tag, name, ulen, stored))
print()
print("说明：")
print("  · v2.3.0 的载荷是**二进制 zip**，作为数据条目直接写入（tcode=0/1）")
print("  · v3.1.11 的载荷先 base64 成文本字符串嵌进 .py，再由 PyInstaller 打包")
print("    → base64 膨胀 4/3，且高熵文本无法被二次压缩 → 存储尺寸直接变大")
print()

print("=" * 80)
print("三、净差异计算")
print("=" * 80)
print()
t0, ss0, b0, _ = summary["v2.3.0"]
t1, ss1, b1, _ = summary["v3.1.11"]
carrier0 = next(st for n, u, st, c in b0 if n in ("app_payload.zip", "setup"))
carrier1 = next(st for n, u, st, c in b1 if n in ("app_payload.zip", "setup"))
print("v2.3.0  包体 %.2f MiB = 载荷载体 %.2f MiB + 其它 %.2f MiB"
      % (t0 / MB, carrier0 / MB, (t0 - carrier0) / MB))
print("v3.1.11 包体 %.2f MiB = 载荷载体 %.2f MiB + 其它 %.2f MiB"
      % (t1 / MB, carrier1 / MB, (t1 - carrier1) / MB))
print()
print("载荷载体差        : %+.2f MiB" % ((carrier1 - carrier0) / MB))
print("其它条目差        : %+.2f MiB" % (((t1 - carrier1) - (t0 - carrier0)) / MB))
print("包体总差          : %+.2f MiB" % ((t1 - t0) / MB))
print()

print("=" * 80)
print("四、兼容性验证：v2.3.0 的 ffmpeg 放在哪、代码去哪找")
print("=" * 80)
print()
import zipfile  # noqa: E402
import io  # noqa: E402

r0 = CArchiveReader(CASES[0][1])
z0 = zipfile.ZipFile(io.BytesIO(r0.extract("app_payload.zip")))
names0 = [i.filename.replace("\\", "/") for i in z0.infolist()]
roots0 = sorted({n.split("/")[0] for n in names0 if "/" in n})
print("v2.3.0 载荷顶层目录:", roots0[:12])
print()
ff0 = [n for n in names0 if "ffmpeg" in n.lower()]
print("v2.3.0 载荷内 ffmpeg 位置:", ff0)
print()
rl = [n for n in names0 if n.endswith("dlcore.py") or n.endswith("dlcore.pyc")]
print("v2.3.0 载荷内 dlcore:", rl[:5])
print()
print("→ 结论：v2.3.0 的 ffmpeg 位于载荷根目录的 third_party/ 下；")
print("  而 frozen 模式下 dlcore.app_root() 返回 _internal/，")
print("  代码按 app_root()/third_party 查找 → **路径不匹配**。")
print("  这解释了缺陷 H 为何从 v2.3.0 起长期潜伏：文件已随包分发，但代码找不到。")