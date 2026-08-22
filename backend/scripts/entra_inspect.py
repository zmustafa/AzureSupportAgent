"""Print the live Entra findings and domain notes for manual spot-checking.

Usage: backend\\.venv\\Scripts\\python.exe backend\\scripts\\entra_inspect.py <connection-id> [pillar]
"""
from __future__ import annotations

import json
import sys

import httpx

API = "http://127.0.0.1:35001/api"
HEADERS = {"Origin": "http://127.0.0.1:35001", "Sec-Fetch-Site": "same-origin"}


def main() -> None:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    pillar = sys.argv[2] if len(sys.argv) > 2 else ""
    with httpx.Client(timeout=60, headers=HEADERS) as c:
        c.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})
        params = {"connection_id": conn, "limit": 500}
        if pillar:
            params["pillar"] = pillar
        body = c.get(f"{API}/entra/findings", params=params).json()

        print("DOMAIN STATE")
        for name, d in (body["meta"]["domains"] or {}).items():
            print(f"  {name:8} {d['status']:12} items={d['item_count']:<5} {d.get('error','')[:80]}")
            for note in d.get("notes") or []:
                print(f"           note: {note}")

        print(f"\nFINDINGS ({body['total']}) {body['by_severity']}")
        for f in body["findings"]:
            print(f"\n  [{f['severity'].upper():8}] {f['signal_id']}")
            print(f"    {f['title']}")
            print(f"    object: {f['object_name']} ({f['object_kind']})")
            ev = json.dumps(f["evidence"], default=str)
            print(f"    evidence: {ev[:300]}")


if __name__ == "__main__":
    main()
