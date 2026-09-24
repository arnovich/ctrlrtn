#!/usr/bin/env python3
"""Assert the built wheel carries everything the source tree implies.

A wheel that builds is not a wheel that works. Non-Python files are included
only because hatchling sweeps everything under ``packages``, so moving one
between directories can drop it from the distribution while every test still
passes -- the editable install used for testing resolves it from the source
tree, where it always exists.

Run after ``uv build``. Exits non-zero with the offending path named.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# Files that must be inside the wheel, and why they matter.
REQUIRED = {
    "ctrlrtn/telemetry/prices.toml": (
        "default price table; the gateway raises on startup without it"
    ),
    "ctrlrtn/py.typed": (
        "PEP 561 marker; without it downstream type checkers ignore us"
    ),
}

ENTRY_POINT = "ctrlrtn = ctrlrtn.cli.commands:main"


def main() -> int:
    wheels = sorted(Path("dist").glob("*.whl"))
    if not wheels:
        print("no wheel in dist/ -- run `uv build` first", file=sys.stderr)
        return 1
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points = next(
            (
                archive.read(n).decode()
                for n in names
                if n.endswith("entry_points.txt")
            ),
            "",
        )

    failures = [
        f"  MISSING {path}\n          ({why})"
        for path, why in sorted(REQUIRED.items())
        if path not in names
    ]
    if ENTRY_POINT not in entry_points:
        failures.append(
            f"  MISSING entry point: {ENTRY_POINT}\n"
            f"          (console script would not resolve)"
        )

    if failures:
        print(f"{wheel.name} is incomplete:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(f"{wheel.name}: {len(REQUIRED)} required files + entry point present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
