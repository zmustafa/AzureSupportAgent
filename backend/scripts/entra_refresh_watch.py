"""Refresh selected Entra domains and stream the job log. Dev helper.

Usage: entra_refresh_watch.py <connection_id> [domain ...]
"""
from __future__ import annotations

import sys
import time

import httpx

API = "http://127.0.0.1:35001/api"
CONN = sys.argv[1]
DOMAINS = sys.argv[2:]

C = httpx.Client(timeout=1800, headers={"Origin": "http://127.0.0.1:35001",
                                        "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

r = C.post(f"{API}/entra/refresh", params={"connection_id": CONN},
           json={"domains": DOMAINS, "force": True})
print("refresh:", r.status_code, DOMAINS or "(all)")

seen = 0
started = time.time()
while time.time() - started < 3600:
    time.sleep(6)
    status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
    job = status.get("job") or {}
    events = job.get("events") or []
    for e in events[seen:]:
        print(f"  {e.get('level', ''):5} {e.get('message', '')[:150]}")
    seen = len(events)
    if job.get("status") not in ("running", "queued"):
        print("job:", job.get("status"), job.get("error", ""))
        break

status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
print()
for name, d in (status["meta"].get("domains") or {}).items():
    err = str(d.get("error") or "")[:100]
    print(f"  {name:12} {d.get('status'):14} items={d.get('item_count', 0):>7}  {err}")
    for note in (d.get("notes") or [])[:3]:
        print(f"       note: {str(note)[:120]}")
