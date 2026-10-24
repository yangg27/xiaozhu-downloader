#!/usr/bin/env python3
"""测试 dlcore ERROR -> aria2c 重试路径。"""
import os
import sys
import tempfile
import time

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

import dlcore

# 强制使用一个会因 UA 被拒绝的 URL，让主引擎失败，再切 aria2c
url = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
out_dir = tempfile.mkdtemp()

mgr = dlcore.Manager(data_dir=os.path.join(out_dir, "state"))
mgr.cfg["max_tasks"] = 2
task = mgr.add_task(url, out_dir=out_dir, filename="dummy.pdf", conns=4)
print(f"task added: {task.tid}, url={url}")

# add_task 默认 start=True，等待失败即可

# 等待失败
t0 = time.time()
while task.state != dlcore.STATE_ERROR and time.time() - t0 < 30:
    time.sleep(0.5)
print(f"state={task.state}, phase={task.phase}, err={task.err[:80]}")

# 把 UA 改成简单的，让 aria2c 能下载
print("triggering aria2 retry...")
task.http.ua = "Mozilla/5.0"
task.retry_with_aria2()

# 等待完成
t0 = time.time()
while task.state not in (dlcore.STATE_DONE, dlcore.STATE_ERROR) and time.time() - t0 < 60:
    time.sleep(0.5)
print(f"final state={task.state}, phase={task.phase}, err={task.err[:80]}")
print(f"output: {getattr(task, 'created_final', '')}")
print(f"size: {os.path.getsize(task.created_final) if task.created_final and os.path.exists(task.created_final) else 0}")
