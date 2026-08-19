"""Run every MP32 Control test suite and summarise.

    python3 tests/run_all.py

No hardware required: these suites cover peer metadata, the web-host election, the HTTP API
and the browser-side group, card and clock logic. Anything device-facing is on the physical
checklist in `docs/VERIFICATION.md` and is deliberately not simulated here.

Exit code is non-zero if any suite fails. Recorded known defects do not fail the run, but
they are listed at the end so they cannot quietly become normal.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ("test_host_election.py", "chronological web-host election"),
    ("test_startup_order.py", "startup ordering and unvalidated-session safety"),
    ("test_group_metadata.py", "per-group metadata sync"),
    ("test_notes_cards.py", "shared notes cards and leases"),
    ("test_host_failover.py", "web-host liveness and the phone's recovery contract"),
    ("test_hlc.py", "hybrid logical clock"),
    ("test_metadata_persistence.py", "metadata survives a controller restart"),
]


def main() -> int:
    print("MP32 Control — test suites")
    print("=" * 74)
    results, total_checks, total_known = [], 0, 0
    started = time.time()

    for filename, description in SUITES:
        print(f"\n{description}  ({filename})")
        print("-" * 74)
        proc = subprocess.run([sys.executable, os.path.join(HERE, filename)],
                              cwd=HERE, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)

        checks = known = 0
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if "checks passed" in stripped:
                try:
                    checks = int(stripped.split(":")[1].split("/")[1].split()[0])
                except (IndexError, ValueError):
                    pass
            if stripped.startswith("known defect:"):
                known += 1
        total_checks += checks
        total_known += known
        results.append((description, proc.returncode == 0, checks, known))

    print("\n" + "=" * 74)
    print(f"Summary — {total_checks} checks in {time.time() - started:.0f}s")
    print("=" * 74)
    for description, ok, checks, known in results:
        flag = "PASS" if ok else "FAIL"
        note = f"  ({known} known defect{'s' if known != 1 else ''})" if known else ""
        print(f"  {flag}  {checks:>3} checks  {description}{note}")

    failed = [d for d, ok, _, _ in results if not ok]
    if total_known:
        print(f"\n  {total_known} recorded known defect(s) — see docs/VERIFICATION.md.")
    if failed:
        print(f"\n  {len(failed)} suite(s) FAILED: " + ", ".join(failed))
        return 1
    print("\n  All suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
