# -*- coding: utf-8 -*-
"""v2.3.0 vs v3.1.11 安装包 —— 真实解包逐项对比（最终版）。"""
import io
import os
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
MB = 1048576
HERE = os.path.dirname(os.path.abspath(__file__))
VERCMP = os.path.join(os.path.dirname(HERE), "_vercmp")
# 兼容：按实际存在位置查找
for _cand in (VERCMP,
              os.path.join(os.path.dirname(os.path.dirname(HERE)), "_vercmp"),
              r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\_vercmp"):
    if os.path.isfile(os.path.join(_cand, "v3111_payload.zip")):
        VERCMP = _cand
        break

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402


def load_230():
    r = CArchiveReader(r"D:\software\小助下载器_v2.3.0_安装包.exe")
    for n in r.toc:
        if n.endswith(".zip") and "payload" in n:
            return zipfile.ZipFile(io.BytesIO(r.extract(n)))
    return None


def load_3111():
    raw = open(os.path.join(VERCMP, "v3111_payload.zip"), "rb").read()
    return zipfile.ZipFile(io.BytesIO(raw))


def items(z):
    d = {}
    for i in z.infolist():
        d[i.filename.replace("\\", "/")] = (i.file_size, i.compress_size)
    return d


z230 = load_230()
z3111 = load_3111()
a, b = items(z230), items(z3111)

print("=" * 80)
print("一、载荷总量对比")
print("=" * 80)
for tag, z in (("v2.3.0", z230), ("v3.1.11", z3111)):
    its = z.infolist()
    u = sum(i.file_size for i in its)
    c = sum(i.compress_size for i in its)
    print("%-10s 条目 %4d 个 | 解压 %7.1f MB | 压缩(payload) %6.1f MB" % (tag, len(its), u / MB, c / MB))
print()

print("=" * 80)
print("二、顶层分组对比（按压缩后体积，决定安装包大小）")
print("=" * 80)


def agg(d):
    g = {}
    for k, (u, c) in d.items():
        top = k.split("/")[0] if "/" in k else "(根目录文件)"
        x = g.setdefault(top, [0, 0, 0])
        x[0] += 1
        x[1] += u
        x[2] += c
    return g


g1, g2 = agg(a), agg(b)
keys = sorted(set(g1) | set(g2), key=lambda k: -(g2.get(k, [0, 0, 0])[2] + g1.get(k, [0, 0, 0])[2]))
print("%-34s %9s %9s %10s" % ("分组", "2.3.0", "3.1.11", "差(MB)"))
print("-" * 68)
for k in keys[:16]:
    c1 = g1.get(k, [0, 0, 0])[2] / MB
    c2 = g2.get(k, [0, 0, 0])[2] / MB
    n2 = g2.get(k, [0, 0, 0])[0]
    print("%-34s %9.2f %9.2f %+10.2f   (%d 文件)" % (k[:34], c1, c2, c2 - c1, n2))
print("-" * 68)
t1 = sum(v[2] for v in g1.values()) / MB
t2 = sum(v[2] for v in g2.values()) / MB
print("%-34s %9.2f %9.2f %+10.2f" % ("合计", t1, t2, t2 - t1))
print()

print("=" * 80)
print("三、单文件差异（只看压缩后相差 > 0.3MB 的）")
print("=" * 80)
print("%-52s %9s %9s %10s" % ("文件", "2.3.0", "3.1.11", "差(MB)"))
print("-" * 82)
rows = []
for k in set(a) | set(b):
    c1 = a.get(k, (0, 0))[1] / MB
    c2 = b.get(k, (0, 0))[1] / MB
    if abs(c2 - c1) > 0.3:
        rows.append((k, c1, c2, c2 - c1))
for k, c1, c2, d in sorted(rows, key=lambda x: -abs(x[3])):
    print("%-52s %9.2f %9.2f %+10.2f" % (k[:52], c1, c2, d))
print()

print("=" * 80)
print("四、结论")
print("=" * 80)
up = [(k, a.get(k, (0, 0))[1] / MB, b.get(k, (0, 0))[1] / MB) for k in set(a) | set(b)]
gain = sum(max(0, c2 - c1) for _, c1, c2 in up)
loss = sum(max(0, c1 - c2) for _, c1, c2 in up)
print("· v3.1.11 比 v2.3.0 多出的压缩体积       : %+.2f MB" % (t2 - t1))
print("· 其中新增/变大项合计                     : +%.2f MB" % gain)
print("· 其中减少/消失项合计                     : -%.2f MB" % loss)
print()
# 归因
print("· 主要增项：")
for k, c1, c2, d in sorted([r for r in rows if r[3] > 0], key=lambda x: -x[3])[:8]:
    print("    %-50s %+.2f MB" % (k[:50], d))
print("· 主要减项：")
for k, c1, c2, d in sorted([r for r in rows if r[3] < 0], key=lambda x: x[3])[:8]:
    print("    %-50s %+.2f MB" % (k[:50], d))