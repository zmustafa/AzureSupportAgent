"""Block operator-specific Azure identifiers from publishable repository files.

The private denylist lives in the ignored ``.security/live-identifiers.txt`` file. This
script is safe to publish because it contains no operator identifiers itself. Clones that
do not have a local denylist exit successfully.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / ".security" / "live-identifiers.txt"


def load_identifiers(path: Path = DENYLIST) -> list[str]:
    if not path.exists():
        return []
    identifiers: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.split("#", 1)[0].strip()
        if value:
            identifiers.append(value)
    return identifiers


def is_repository_file(path: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(ROOT)
    except ValueError:
        return False
    return resolved != DENYLIST.resolve()


def repository_candidates() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def find_hits(paths: list[Path], identifiers: list[str]) -> list[tuple[Path, int, str]]:
    lowered = [(value.casefold(), value) for value in identifiers]
    hits: list[tuple[Path, int, str]] = []
    for path in paths:
        if not path.is_file() or not is_repository_file(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            folded = line.casefold()
            if any(needle in folded for needle, _value in lowered):
                hits.append((path, line_number, line.strip()))
    return hits


def main(arguments: list[str]) -> int:
    identifiers = load_identifiers()
    if not identifiers:
        return 0
    supplied = [Path(argument) if Path(argument).is_absolute() else ROOT / argument for argument in arguments]
    candidates = supplied or repository_candidates()
    hits = find_hits(candidates, identifiers)
    if not hits:
        print(f"No live Azure identifiers found ({len(identifiers)} local values checked).")
        return 0
    print("Live Azure identifier(s) found in publishable files:", file=sys.stderr)
    for path, line_number, line in hits:
        relative = path.resolve().relative_to(ROOT).as_posix()
        print(f"  {relative}:{line_number}: {line}", file=sys.stderr)
    print("Replace with synthetic placeholders or move the fixture to an ignored path.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
