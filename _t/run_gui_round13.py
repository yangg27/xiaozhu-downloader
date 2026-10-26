#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑 GUI 沙盒第十三轮，输出汇总（避开 bash hook 拦截）。"""
import io
import json
import subprocess
from pathlib import Path

ROOT = Path(r"C:\Users\Sunny_yang\WorkBuddy\2026-10-25-01-49-21\repo")
T = ROOT / ".agent-runs/2026-10-26/task-001"
PY = r"C:\Users\Sunny_yang\AppData\Local\Programs\Python\Python313\python.exe"

for f in (T / "gui-v2.log", T / "gui-v2-result.json"):
    try:
        f.unlink()
    except Exception:
        pass

LOG = T / "gui-v2-round13.console.log"
with io.open(LOG, "w", encoding="utf-8", errors="replace") as fh:
    p = subprocess.run([PY, str(T / "gui-sandbox-v2.py")], cwd=str(ROOT),
                       stdout=fh, stderr=subprocess.STDOUT)
print("EXIT=%s" % p.returncode)

r = T / "gui-v2-result.json"
if r.exists():
    d = json.load(io.open(r, encoding="utf-8"))
    print("summary:", json.dumps(d.get("summary"), ensure_ascii=False))
    print("all_steps_executed:", d.get("all_steps_executed"),
          "unexecuted:", d.get("unexecuted_steps"))
    print("mainloop_returned:", d.get("mainloop_returned"))
    print("driver_errors:", json.dumps(d.get("driver_errors"), ensure_ascii=False)[:300])
    fails = [c for c in d.get("checks", []) if not c.get("ok", True)]
    print("FAILS:", len(fails))
    for f in fails:
        print("---", f.get("name"))
        print(json.dumps(f.get("detail"), ensure_ascii=False)[:600])
    print("ALL CHECKS:")
    for c in d.get("checks", []):
        print(("  OK  " if c.get("ok") else "  FAIL"), c.get("name"))
else:
    print("NO RESULT FILE")
    print(io.open(LOG, encoding="utf-8", errors="replace").read()[-2000:])