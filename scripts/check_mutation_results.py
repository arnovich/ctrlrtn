#!/usr/bin/env python3
"""Fail CI when targeted mutation testing leaves unsafe outcomes."""

from __future__ import annotations

import subprocess
import sys

# These mutations are byte-for-byte behavioral equivalents: both supported
# header containers are case-insensitive, ASCII's codec name is
# case-insensitive, and httpx normalizes field names to lowercase. Keep the
# allow-list exact so any *new* survivor still fails the nightly gate.
ALLOWED_SURVIVORS = {
    "ctrlrtn.gateway.proxy.headers.x__hop_by_hop__mutmut_8",
    "ctrlrtn.gateway.proxy.headers.x__filtered__mutmut_3",
    "ctrlrtn.gateway.proxy.headers.x__filtered__mutmut_8",
}
UNSAFE = {"no tests", "timeout", "suspicious"}


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout, end="")
    rows = [line.strip() for line in result.stdout.splitlines() if ": " in line]
    unsafe = [line for line in rows if line.rsplit(": ", 1)[-1] in UNSAFE]
    unsafe.extend(
        line
        for line in rows
        if line.endswith(": survived")
        and line.removesuffix(": survived") not in ALLOWED_SURVIVORS
    )
    if unsafe:
        print("unsafe mutation outcomes remain:", file=sys.stderr)
        print("\n".join(unsafe), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
