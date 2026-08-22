"""Block until the current Entra job finishes, then print the per-domain outcome."""
from __future__ import annotations

import sys
import time

import httpx

API = "http://127.0.0.1:35001/api"
CONN = sys.argv[1]

C = httpx.Client(timeout=120, headers={"Origin": "http://127.0.0.1:35001",
                                       "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

last = ""
for _ in range(400):
    status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
    job = status.get("job") or {}
    msg = str(job.get("last_message") or "")
    if msg != last:
        print(f"   {msg[:130]}")
        last = msg
    if job.get("status") not in ("running", "queued"):
        print("job:", job.get("status"), str(job.get("error") or "")[:200])
        break
    time.sleep(10)

status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
print()
for name, d in (status["meta"].get("domains") or {}).items():
    err = str(d.get("error") or "")[:90]
    print(f"  {name:12} {str(d.get('status')):14} items={d.get('item_count', 0):>7}  {err}")
    for note in (d.get("notes") or [])[:4]:
        print(f"       note: {str(note)[:130]}")
