"""Verify local_activity monitor actions persist across calls (cached instance)."""
import json
import sys
import time

sys.path.insert(0, 'mcp')
import c2_project_mcp as m

r1 = json.loads(m.local_activity("start_monitor", '{"interval": 1}'))
assert r1.get("status") == "ok", r1
r2 = json.loads(m.local_activity("start_monitor", "{}"))
assert r2.get("status") == "error" and "already running" in r2.get("message", "").lower(), r2
time.sleep(3.5)
r3 = json.loads(m.local_activity("get_snapshots", '{"limit": 5}'))
# Note: each snapshot takes ~2s of its own time inside psutil (two
# interval=1 samples), so >=1 after 3.5s is the correct bar.
assert r3.get("status") == "ok" and r3.get("count", 0) >= 1, r3
r5 = json.loads(m.local_activity("processes", '{"name": "python"}'))
assert r5.get("status") == "ok" and r5.get("count", 0) >= 1, r5
r4 = json.loads(m.local_activity("stop_monitor", "{}"))
assert r4.get("status") == "ok", r4
print("monitor persisted across calls; snapshots:", r3["count"])
print("MONITOR FLOW: ALL PASS")
