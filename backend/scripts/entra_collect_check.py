"""Re-collect a connection and report the per-domain outcome. Dev helper."""
import sys
import time

import httpx

CONN = sys.argv[1] if len(sys.argv) > 1 else ""
API = "http://127.0.0.1:35001/api"
C = httpx.Client(timeout=900, headers={"Origin": "http://127.0.0.1:35001",
                                       "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

r = C.post(f"{API}/entra/refresh", params={"connection_id": CONN},
           json={"domains": [], "force": True})
print("refresh:", r.status_code)

for _ in range(180):
    time.sleep(5)
    status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
    job = status.get("job") or {}
    if job.get("status") not in ("running", "queued"):
        break

status = C.get(f"{API}/entra/status", params={"connection_id": CONN}).json()
print(f"\ntenant {status['meta']['tenant_id']}")
for name, d in (status["meta"].get("domains") or {}).items():
    err = str(d.get("error") or "")[:110]
    print(f"  {name:12} {d.get('status'):14} items={d.get('item_count', 0):>6}  {err}")

posture = C.get(f"{API}/entra/posture", params={"connection_id": CONN}).json()
s = posture["score"]
print(f"\nscore={s['score']} grade={s['grade']!r} coverage={s['coverage']:.0%}")

pim = C.get(f"{API}/entra/privileged/pim-policies", params={"connection_id": CONN}).json()
print(f"PIM policies: {len(pim.get('policies') or [])}")
for p in (pim.get("policies") or [])[:5]:
    print(f"  {p['role_name']:34} score={p['score']:3} missing={','.join(p['failed_controls']) or '-'}")

esc = C.get(f"{API}/entra/graph/escalations", params={"connection_id": CONN}).json()
print(f"\nescalations: {esc['total']} {esc['by_primitive']}")
