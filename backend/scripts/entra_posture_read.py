"""Read-only posture summary for a connection. Never triggers collection."""
from __future__ import annotations

import sys

import httpx

API = "http://127.0.0.1:35001/api"
CONN = sys.argv[1]

C = httpx.Client(timeout=180, headers={"Origin": "http://127.0.0.1:35001",
                                       "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})


def get(path: str, **params):
    return C.get(f"{API}{path}", params={"connection_id": CONN, **params}).json()


status = get("/entra/status")
print(f"tenant {status['meta']['tenant_id']}  loaded={status['meta']['loaded']}")
for name, d in (status["meta"].get("domains") or {}).items():
    print(f"  {name:12} {str(d.get('status')):14} items={d.get('item_count', 0):>7}")

posture = get("/entra/posture")
s = posture["score"]
print(f"\nscore={s['score']} grade={s['grade']!r} coverage={s['coverage']:.0%} "
      f"measured={s['measured_signals']}/{s['total_signals']}")
for p in s["pillars"]:
    print(f"  {p['key']:5} {str(p['score']):>5} {p['state']:15} "
          f"findings={p['findings']:4} measured={p['measured_signals']}/{p['total_signals']}")

for label, path in (
    ("signals/overview", "/entra/signals/overview"),
    ("signals/auth-methods", "/entra/signals/auth-methods"),
    ("signals/risky-users", "/entra/signals/risky-users"),
    ("signals/patterns", "/entra/signals/patterns"),
    ("governance/coverage", "/entra/governance/coverage"),
    ("governance/entitlement", "/entra/governance/entitlement"),
    ("privileged/overview", "/entra/privileged/overview"),
    ("apps", "/entra/apps"),
    ("graph/escalations", "/entra/graph/escalations"),
    ("inbox", "/entra/inbox"),
):
    body = get(path)
    keys = {k: (len(v) if isinstance(v, (list, dict)) else v)
            for k, v in body.items() if k != "meta"}
    print(f"\n{label}: {str(keys)[:300]}")
