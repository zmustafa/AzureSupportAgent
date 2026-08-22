"""Report findings whose object_name is a raw GUID — a name nobody can act on."""
from __future__ import annotations

import collections
import re
import sys

import httpx

API = "http://127.0.0.1:35001/api"
CONN = sys.argv[1]
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

C = httpx.Client(timeout=180, headers={"Origin": "http://127.0.0.1:35001",
                                       "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

body = C.get(f"{API}/entra/inbox", params={"connection_id": CONN, "limit": 2000}).json()
findings = body["findings"]
bad = [f for f in findings if GUID.fullmatch(str(f.get("object_name") or "").strip())]

print(f"{len(bad)} of {len(findings)} findings name their object by raw GUID")
for signal, n in collections.Counter(f["signal_id"] for f in bad).most_common():
    print(f"  {signal:42} {n}")
for f in bad[:5]:
    print(f"\n  {f['signal_id']}  {f['object_kind']}  {f['object_name']}")
    print(f"    {f['title'][:120]}")
