"""Quick read of the activations API — used while building the tab. Usage: <connection_id>"""
from __future__ import annotations

import collections
import json
import sys

import httpx

API = "http://127.0.0.1:35001/api"
CONN = sys.argv[1]
C = httpx.Client(timeout=300, headers={"Origin": "http://127.0.0.1:35001",
                                       "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

d = C.get(f"{API}/entra/privileged/activations",
          params={"connection_id": CONN, "days": 0}).json()
print(f"total {d['total']}   facets {json.dumps(d['facets'])}")
print(f"caps  {json.dumps(d['capabilities'])}")
print(f"ledger {json.dumps(d['ledger'])}")
print("planes", collections.Counter(s["plane"] for s in d["sessions"]))
print("status", collections.Counter(s.get("status") for s in d["sessions"]))
print("granted", collections.Counter(s.get("granted") for s in d["sessions"]))
print("hours known:", sum(1 for s in d["sessions"] if s.get("granted_hours") is not None),
      "of", len(d["sessions"]))
print("\nnewest:")
for s in d["sessions"][:6]:
    print(f"  {s['plane']:5} {s['label'][:30]:30} {s['role_name'][:24]:24} "
          f"{s['scope_type']:14} {(s['start'] or '')[:19]} "
          f"{s['granted_hours']}h  just={s['justification_quality']} "
          f"granted={s['granted']}")

if len(sys.argv) > 2 and sys.argv[2] == "--actions":
    target = next((s for s in d["sessions"] if s["granted"]), None)
    if target:
        print(f"\nactions for {target['id']}")
        a = C.get(f"{API}/entra/privileged/activations/{target['id']}/actions",
                  params={"connection_id": CONN}).json()
        print("  counts:", json.dumps(a.get("counts")))
        print("  standing entra:", a.get("standing_entra_roles"))
        print("  notes:", a.get("notes"))
        for act in (a.get("actions") or [])[:8]:
            print(f"    {act['at'][:19]}  {act['plane']:5} {act['operation'][:44]:44} "
                  f"{act['attribution']}")
