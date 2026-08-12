"""Live architecture-pricing coverage audit.

This is intentionally an operator script, not a deterministic unit test.  It enumerates
the ARM provider catalog available to the current Azure CLI subscription, proves every
type reaches a terminal classifier state, then samples every distinct mapped service from
the public Retail Prices API through the same bounded parser used by the application.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.architectures.pricing import RESOURCE_RULES, classify_resource_type  # noqa: E402
from app.core.retail_prices import fetch_retail_prices  # noqa: E402


def arm_resource_types() -> list[str]:
    az = shutil.which("az")
    if not az:
        raise OSError("Azure CLI is not installed.")
    completed = subprocess.run(  # noqa: S603 - fixed executable/arguments; no shell
        [az, "provider", "list", "--expand", "resourceTypes", "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    providers = json.loads(completed.stdout)
    result: list[str] = []
    for provider in providers if isinstance(providers, list) else []:
        namespace = str(provider.get("namespace") or "")
        for resource in provider.get("resourceTypes") or []:
            resource_type = str(resource.get("resourceType") or "")
            if namespace and resource_type:
                result.append(f"{namespace}/{resource_type}")
    return sorted(set(result), key=str.lower)


async def retail_shapes(currency: str) -> list[dict[str, Any]]:
    services = sorted({rule.service_name for rule in RESOURCE_RULES.values()})
    semaphore = asyncio.Semaphore(4)

    async def inspect(service: str) -> dict[str, Any]:
        async with semaphore:
            result = await fetch_retail_prices(
                service,
                currency=currency,
                regions=("eastus", "Global", "Zone 1", ""),
                max_pages=1,
                max_items=1_000,
            )
        return {
            "service": service,
            "rows": len(result.items),
            "pages": result.pages,
            "truncated": result.truncated,
            "invalid_rows": result.invalid_rows,
            "error": result.error,
            "units": sorted({str(item.get("unitOfMeasure") or "") for item in result.items}),
            "item_fields": sorted(result.items[0]) if result.items else [],
        }

    return await asyncio.gather(*(inspect(service) for service in services))


async def run(currency: str, skip_retail: bool) -> dict[str, Any]:
    resource_types = arm_resource_types()
    states = Counter()
    mapped_services = Counter()
    errors: list[str] = []
    for arm_type in resource_types:
        try:
            classification = classify_resource_type(arm_type)
            state = classification["state"]
            states[state] += 1
            if classification.get("service_name"):
                mapped_services[classification["service_name"]] += 1
        except Exception as exc:  # noqa: BLE001 - audit reports classifier defects
            errors.append(f"{arm_type}: {type(exc).__name__}")
    shapes = [] if skip_retail else await retail_shapes(currency)
    return {
        "arm": {
            "resource_types": len(resource_types),
            "states": dict(sorted(states.items())),
            "mapped_services": dict(sorted(mapped_services.items())),
            "classifier_errors": errors,
        },
        "retail": {
            "currency": currency,
            "services": len(shapes),
            "successful": sum(1 for row in shapes if not row["error"]),
            "empty": sum(1 for row in shapes if not row["error"] and not row["rows"]),
            "failed": sum(1 for row in shapes if row["error"]),
            "shapes": shapes,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--skip-retail", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.currency.upper(), args.skip_retail))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 2
    print(json.dumps(report, indent=2))
    return 1 if report["arm"]["classifier_errors"] or report["retail"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())