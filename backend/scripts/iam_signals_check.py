"""Dev smoke: run the IAM signal registry + score over the demo dataset.

Not a test — a fast way to eyeball which signals fire, which report *not measured* and why, and
how the score and coverage fall out. Uses a throwaway cache dir so it never touches .data.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

from app.iam import cache, demo, findings, score


def _p(text: str) -> None:
    """Print without exploding on a cp1252 console (findings contain arrows and dashes)."""
    sys.stdout.write(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8") + "\n")


def main() -> None:
    d = pathlib.Path(tempfile.mkdtemp())
    cache._DATA, cache._INDEX, cache._BLOBS, cache._migrated = d, d / "i.json", d / "b", True
    demo.seed_demo("t1")

    results = findings.evaluate("t1")
    _p(f"signals: {len(results)} | measured: {sum(1 for r in results if r.measured)}")
    for r in results:
        mark = "x" if not r.measured else ("!" if r.findings else ".")
        _p(f"  {mark} {r.spec.id:42} {len(r.findings):>2}  {r.reason[:60]}")

    s = score.compute(results)
    _p(f"\nscore={s['score']} coverage={s['coverage']} grade={s['grade']} ({s['grade_withheld_reason']})")
    for p in s["pillars"]:
        _p(f"  {p['key']:5} w{p['weight']:>3} score={str(p['score']):>5} {p['state']:16} frac={p['measured_fraction']}")

    for r in results:
        for f in r.findings:
            _p(f"\n[{f.severity:8}] {f.title}\n   {f.subject_label}\n   {f.detail}")


if __name__ == "__main__":
    main()
