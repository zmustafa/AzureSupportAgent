"""Ad-hoc perf probe for the CA coverage matrix. Not a test; run manually."""
from __future__ import annotations

import copy
import pathlib
import tempfile
import time
import uuid

from app.entra import ca_engine, cache, demo
from app.entra import snapshot as sm


def _cells(analysis):
    return sum(len(r["cells"]) for r in analysis["coverage"]["matrix"])


def main() -> None:
    cache.set_root_for_tests(pathlib.Path(tempfile.mkdtemp()) / "entra")
    demo.seed()
    data = sm.load(demo.DEMO_TENANT)["data"]

    t = time.perf_counter()
    a = ca_engine.analyze(data)
    print(f"demo tenant                          -> {(time.perf_counter()-t)*1000:7.0f} ms, {_cells(a)} cells")

    big = copy.deepcopy(data)
    big["people"]["users"] = [
        {"id": f"u{i}", "upn": f"u{i}@c.com", "display_name": f"u{i}", "enabled": True,
         "user_type": "Guest" if i % 10 == 0 else "Member", "mfa_registered": True}
        for i in range(5000)
    ]
    big["apps"]["service_principals"] = [
        {"object_id": f"o{i}", "app_id": str(uuid.UUID(int=i)), "display_name": f"app{i}",
         "sp_type": "Application", "enabled": True, "is_first_party": False}
        for i in range(800)
    ]
    big["ca"]["policies"] = [
        {"id": f"p{i}", "display_name": f"Policy {i}", "state": "enabled",
         "conditions": {"include_users": ["All"], "exclude_users": [f"u{i}"],
                        "include_groups": [], "exclude_groups": [], "include_roles": [],
                        "exclude_roles": [], "include_apps": ["All"], "exclude_apps": [],
                        "client_app_types": ["all"], "user_actions": [], "auth_contexts": []},
         "grant": {"operator": "OR", "controls": ["mfa"], "auth_strength_id": ""},
         "session": {"sign_in_frequency": True}}
        for i in range(60)
    ]
    t = time.perf_counter()
    a2 = ca_engine.analyze(big)
    print(f"5,000 users / 800 apps / 60 policies -> {(time.perf_counter()-t)*1000:7.0f} ms, {_cells(a2)} cells")


if __name__ == "__main__":
    main()
