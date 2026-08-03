"""List cached Entra tenants by richness. Ad-hoc helper, not a test."""
from __future__ import annotations

from app.entra import cache


def main() -> None:
    idx = cache.read_index()
    rows = []
    for tid, meta in idx.items():
        if not isinstance(meta, dict):
            continue
        ca = (cache.read_domain(tid, "ca") or {}).get("data") or {}
        apps = (cache.read_domain(tid, "apps") or {}).get("data") or {}
        ppl = (cache.read_domain(tid, "people") or {}).get("data") or {}
        ca_status = ((meta.get("domains") or {}).get("ca") or {}).get("status", "?")
        rows.append((
            len(ca.get("policies") or []),
            tid,
            ca_status,
            len(apps.get("service_principals") or []),
            len(ppl.get("users") or []),
        ))
    rows.sort(reverse=True)
    print(f"{'policies':>8}  {'sps':>6}  {'users':>6}  {'ca':>12}  tenant")
    for pol, tid, status, sps, users in rows:
        print(f"{pol:>8}  {sps:>6}  {users:>6}  {status:>12}  {tid}")


if __name__ == "__main__":
    main()
