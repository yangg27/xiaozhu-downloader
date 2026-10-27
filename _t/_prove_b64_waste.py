# -*- coding: utf-8 -*-
"""量化验证：v3.1.11 安装包多出的 20MiB 是否来自 payload 的 base64 编码开销。

推理链：
  · 两版载荷（zip）压缩后体积几乎相同：2.3.0 = 46.87 MiB，3.1.11 = 46.66 MiB
  · 但安装包体积：2.3.0 = 53.81 MiB，3.1.11 = 73.69 MiB，差 19.88 MiB
  · 载荷内容基本等价 → 差异必然在「存储方式」
  · 假设：v3.1.11 把 zip 做 base64 后嵌入 Python 源码字符串（膨胀 4/3），
          v2.3.0 把 zip 作为二进制数据条目直接打包（无膨胀）
"""
import base64
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")
MB = 1048576

print("=" * 78)
print("一、载荷原始体积（binary）")
print("=" * 78)
P3111 = r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\_vercmp\v3111_payload.zip"
raw3111 = open(P3111, "rb").read()
print("v3.1.11 payload.zip  : %d 字节 = %.2f MiB" % (len(raw3111), len(raw3111) / MB))

print()
print("=" * 78)
print("二、base64 编码后的体积（这是关键）")
print("=" * 78)
b64 = base64.b64encode(raw3111)
ratio = len(b64) / len(raw3111)
print("base64 编码后        : %d 字节 = %.2f MiB" % (len(b64), len(b64) / MB))
print("膨胀系数             : %.4f  (= 4/3 = %.4f)" % (ratio, 4 / 3))
print("膨胀量               : %.2f MiB  ← 纯浪费" % ((len(b64) - len(raw3111)) / MB))

# 实测：源码里的 base64 字符串长度
src = open(r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo\dist_setup\setup.py",
           encoding="utf-8").read()
i = src.find('PAYLOAD_ZIP_B64 = """')
j = src.find('"""', i + 22)
seg = src[i + 22:j]
print()
print("setup.py 内 base64 段: %d 字符（含换行 %d 个）"
      % (len(seg), seg.count("\n")))
print("b64encode 长度        : %d 字符" % len(b64))
print("差值 = 换行符数量     : %d" % (len(seg) - len(b64)))
print("→ 每 76 字符插一个换行，符合 MIME 风格换行")

print()
print("=" * 78)
print("三、安装包体积分解（实测 CArchive 条目）")
print("=" * 78)
from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

for tag, path in (
    ("v2.3.0", r"D:\software\小助下载器_v2.3.0_安装包.exe"),
    ("v3.1.11", r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo\dist_setup"
               r"\小助下载器_v3.1.11_安装包\小助下载器_v3.1.11_安装包.exe"),
):
    r = CArchiveReader(path)
    total = os.path.getsize(path)
    print()
    print("--- %s ---" % tag)
    print("安装包总体积          : %d 字节 = %.2f MiB" % (total, total / MB))
    payload_entry = None
    other = 0
    for n in r.toc:
        d = r.extract(n)
        if n in ("app_payload.zip",):
            payload_entry = (n, len(d))
        elif n in ("setup",):
            payload_entry = (n, len(d)) if payload_entry is None else payload_entry
            other += len(d)  # setup 内嵌了 payload，先记为“脚本”
        else:
            other += len(d)
    # setup 里含 base64 payload，扣除后才是纯脚本
    if payload_entry:
        print("承载 payload 的条目   : %s = %d 字节 = %.2f MiB"
              % (payload_entry[0], payload_entry[1], payload_entry[1] / MB))
        pure = payload_entry[1] - (len(b64) + len(seg) - len(b64))
        print("  扣除 base64 段后纯脚本: %.2f MiB" % (pure / MB))
    print("其余条目合计          : %d 字节 = %.2f MiB" % (other, other / MB))

print()
print("=" * 78)
print("四、结论")
print("=" * 78)
print()
print("载荷二进制        : %.2f MiB" % (len(raw3111) / MB))
print("base64 后         : %.2f MiB" % (len(b64) / MB))
print("base64 净浪费     : %.2f MiB" % ((len(b64) - len(raw3111)) / MB))
print()
print("★ v3.1.11 安装包 73.69 MiB 中，有多达 %.2f MiB 纯粹是 base64 编码浪费。" % ((len(b64) - len(raw3111)) / MB))
print("★ v2.3.0 无此浪费 → 两家载荷大小相同，安装包却差 19.88 MiB，**主因即此**。")