# -*- coding: utf-8 -*-
"""v2.3.0 与 v3.1.11 安装包 —— 真实解包 + 逐项组成对比。

方法：解析 PyInstaller CArchive，把两版包内的文件按体积排序列出，
      用实测数据回答"多出来的 20MB 到底在哪"。
"""
import os
import struct
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8")

MAGIC = b"MEI\014\013\012\013\016"
MB = 1048576


def parse_carchive(path):
    """解析 PyInstaller one-file 的 CArchive 目录表，返回 [(name, size, csize), ...]"""
    data = open(path, "rb").read()
    pos = data.rfind(MAGIC)
    if pos < 0:
        return None, None, len(data)
    # 目录表起点：魔数之前是 cookie（24 字节）
    # cookie 结构（PyInstaller >= 4）：magic(8) + len(4) + toc(4) + pyvers(4) + pylibname(64)
    # 魔数位于 cookie 开头，因此 toc 偏移在 pos+8 处
    toc_off = struct.unpack("!i", data[pos + 8:pos + 12])[0]
    toc_len = struct.unpack("!i", data[pos + 12:pos + 16])[0]
    toc_pos = pos - toc_off
    toc = data[toc_pos:toc_pos + toc_len]

    entries = []
    p = 0
    while p < len(toc):
        (elen,) = struct.unpack("!i", toc[p:p + 4])
        p += 4
        if elen <= 0:
            break
        entry = toc[p:p + elen]
        p += elen
        # ENTRY STRUCT: (magic?, pos, cmprsdlen, uncmprsdlen, cmprsflag, typecd, name)
        if len(entry) < 18:
            continue
        dpos, clen, ulen, cflag, tcode = struct.unpack("!iiibB", entry[:14])
        name = entry[18:].split(b"\0")[0].decode("utf-8", "replace")
        entries.append((name, ulen, clen, tcode))
    return entries, toc_pos, len(data)


def report(path, label):
    print("=" * 78)
    print("%s  %s" % (label, os.path.basename(path)))
    print("=" * 78)
    entries, toc_pos, total = parse_carchive(path)
    print("包体大小: %d 字节 = %.2f MiB = %.1f MB(十进制)" % (total, total / MB, total / 1000000))
    if entries is None:
        print("!! 无法解析 CArchive（可能非 PyInstaller 产物）")
        return None
    print("归档内文件数: %d" % len(entries))
    print()
    print("%-52s %11s %11s" % ("文件", "原始(MB)", "压缩(MB)"))
    print("-" * 78)
    tot_u = tot_c = 0
    for name, ulen, clen, tcode in sorted(entries, key=lambda x: -x[1]):
        tot_u += ulen
        tot_c += clen
        if ulen > 200 * 1024:  # 只看 >200KB 的
            print("%-52s %11.2f %11.2f" % (name[:52], ulen / MB, clen / MB))
    print("-" * 78)
    print("%-52s %11.2f %11.2f" % ("归档合计", tot_u / MB, tot_c / MB))
    print()
    # 按顶层目录聚合
    agg = {}
    for name, ulen, clen, tcode in entries:
        key = name.split("/")[0] if "/" in name else name
        a = agg.setdefault(key, [0, 0, 0])
        a[0] += 1
        a[1] += ulen
        a[2] += clen
    print("%-44s %6s %11s %11s" % ("分组", "文件数", "原始(MB)", "压缩(MB)"))
    print("-" * 78)
    for k, (n, u, c) in sorted(agg.items(), key=lambda x: -x[1][2])[:20]:
        print("%-44s %6d %11.2f %11.2f" % (k[:44], n, u / MB, c / MB))
    print()
    return entries


e230 = report(r"D:\software\小助下载器_v2.3.0_安装包.exe", "【v2.3.0】")
print()
e3111 = report(
    r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo\dist_setup\小助下载器_v3.1.11_安装包\小助下载器_v3.1.11_安装包.exe",
    "【v3.1.11】")

if e230 and e3111:
    print()
    print("=" * 78)
    print("三、关键差异（只看 >1MB 的项）")
    print("=" * 78)
    print()
    d230 = {n: (u, c) for n, u, c, _ in e230}
    d3111 = {n: (u, c) for n, u, c, _ in e3111}
    keys = set(d230) | set(d3111)
    diffs = []
    for k in keys:
        u1, c1 = d230.get(k, (0, 0))
        u2, c2 = d3111.get(k, (0, 0))
        du = u2 - u1
        dc = c2 - c1
        if abs(du) > 1024 * 1024 or abs(dc) > 1024 * 1024:
            diffs.append((k, u1, u2, du, c1, c2, dc))
    print("%-46s %10s %10s %11s" % ("文件", "2.3.0(MB)", "3.1.11(MB)", "压缩差(MB)"))
    print("-" * 78)
    for k, u1, u2, du, c1, c2, dc in sorted(diffs, key=lambda x: -abs(x[6])):
        print("%-46s %10.2f %10.2f %+11.2f" % (k[:46], u1 / MB, u2 / MB, dc / MB))
    print()
    print("★ 正向差 = v3.1.11 多出来的；负向差 = v3.1.11 少了。")