"""用构建中间产物 dist_setup/setup.py 校验 v3.1.9 安装包载荷。

setup.py 是安装包 exe 的构建源，其中的 PAYLOAD_ZIP_B64 就是打进 exe 的载荷。
再额外把 dist_app/小助下载器 目录做一致性比对，确认打包源=当前产物。
"""
import base64
import hashlib
import io
import os
import zipfile

REPO = r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo"
SETUP_PY = os.path.join(REPO, "dist_setup", "setup.py")
SRC_DIR = os.path.join(REPO, "dist_app", "小助下载器")
EXE = os.path.join(REPO, "dist_setup", "小助下载器_v3.1.9_安装包",
                   "小助下载器_v3.1.9_安装包.exe")

raw = open(EXE, "rb").read()
print("安装包 = %d 字节 (%.1f MB)" % (len(raw), len(raw) / 1048576))
print("MD5    = %s" % hashlib.md5(raw).hexdigest())
print("SHA256 = %s" % hashlib.sha256(raw).hexdigest())

txt = open(SETUP_PY, encoding="utf-8").read()
MARK = 'PAYLOAD_ZIP_B64 = """'
i = txt.find(MARK)
j = txt.find('"""', i + len(MARK))
blob = txt[i + len(MARK):j].strip()
print("\nsetup.py 内嵌 base64 长度 = %d (mod4=%d)" % (len(blob), len(blob) % 4))
payload = base64.b64decode(blob)
print("载荷 zip = %d 字节 (%.1f MB)" % (len(payload), len(payload) / 1048576))

zf = zipfile.ZipFile(io.BytesIO(payload))
names = zf.namelist()
print("载荷文件总数 = %d" % len(names))

ext = [n for n in names if "browser-extension/" in n]
crx = [n for n in names if n.lower().endswith(".crx")]
pngs = [n for n in names if n.lower().endswith(".png")]
exes = [n for n in names if n.lower().endswith(".exe")]

print("\n--- 扩展（文件夹形态）---")
for n in sorted(ext):
    print("    %-48s %7d B" % (n, zf.getinfo(n).file_size))

print("\ncrx = %s   ★ 期望为空" % (crx if crx else "无"))
print("图片 = %s" % sorted(os.path.basename(n) for n in pngs))
print("可执行 = %s" % sorted(os.path.basename(n) for n in exes))


def peek(suffix: str) -> str:
    for n in names:
        if n.endswith(suffix):
            return zf.read(n).decode("utf-8", "replace")
    return ""


manifest = peek("browser-extension/manifest.json")
bg = peek("browser-extension/background.js")
pj = peek("browser-extension/popup.js")
readme = peek("browser-extension/README.md")
sm = peek("使用说明.md")

checks = [
    ("manifest 版本 1.2.1", '"version": "1.2.1"' in manifest),
    ("background 含 xzdFetch", "xzdFetch" in bg),
    ("background 含 AbortController", "AbortController" in bg),
    ("background 含 API_TIMEOUT_MS", "API_TIMEOUT_MS" in bg),
    ("popup 含 exists 失效判断", "exists" in pj),
    ("popup 含 showHint", "showHint" in pj),
    ("扩展含 README.md", bool(readme)),
    ("无 crx 残留", len(crx) == 0),
    ("扩展文件数 = 10", len(ext) == 10),
    ("含 default-settings.png", any("default-settings.png" in n for n in pngs)),
    ("含 浏览器扩展安装指引.png", any("安装指引" in n for n in pngs)),
    ("含 使用说明.md", bool(sm)),
    ("使用说明.md 不含 crx 字样", "crx" not in sm.lower()),
]

print("\n--- 内容校验 ---")

# 载荷与 dist_app 目录一致性（逐文件比字节）
def rel(n: str) -> str:
    """zip 内路径本身就是相对 SRC_DIR 的（无顶层目录前缀）。"""
    return n.strip("/")

on_disk = {}
for root, _, files in os.walk(SRC_DIR):
    for fn in files:
        p = os.path.join(root, fn)
        r = os.path.relpath(p, SRC_DIR).replace("\\", "/")
        on_disk[r] = p

diff = []
missing = []
for n in names:
    r = rel(n)
    if not r:
        continue
    p = on_disk.get(r)
    if not p:
        missing.append(r)
        continue
    if zf.getinfo(n).file_size != os.path.getsize(p):
        diff.append(r)

extra = [r for r in on_disk if r not in {rel(n) for n in names}]
checks.append(("载荷覆盖 dist_app 全部 %d 个文件" % len(on_disk), not missing))
checks.append(("载荷文件大小与磁盘一致", not diff))

allok = True
for label, ok in checks:
    print("  %s %s" % ("[OK]  " if ok else "[FAIL]", label))
    allok = allok and ok

if missing:
    print("\n  载荷缺失:", missing[:10])
if diff:
    print("  大小不符:", diff[:10])
if extra:
    print("  磁盘多出（未进包）:", extra[:10])

print("\n" + ("★ 载荷校验全部通过" if allok else "★ 存在问题"))
raise SystemExit(0 if allok else 1)