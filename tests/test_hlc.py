"""Suite 6 — the hybrid logical clock behind every metadata write.

Not one of the original five, but it earns its place: an HLC mistake is the single most
expensive failure in this project's testing history. Wall-clock timestamps break
convergence across machines with skewed clocks — a controller whose clock runs ahead always
wins, so the other side's later edits are silently rejected. The HLC packs
`(physical_ms, logical_counter)` into one sortable integer (`pt * 65536 + lc`) and bumps on
every timestamp it sees, so all controllers converge to the network maximum regardless of
skew.

The trap for anyone writing tests: a plain millisecond value is ~65000x too small next to
any HLC value. Use one in a test and every last-write-wins comparison fails while the code
is perfectly fine. That property is asserted here directly, so the trap is documented by a
check rather than by a comment somebody has to find.

**This suite found a defect.** The logical counter does not survive in the browser, because
`pt * 65536` overflows IEEE-754 exact-integer range. See "Logical counter is lost to
floating-point precision" in `docs/VERIFICATION.md`. The affected properties are recorded
with `known_bad` rather than deleted, so they turn back into ordinary checks the moment the
defect is fixed.

Browser code, extracted verbatim from the shipped UI and run under node.
"""
from __future__ import annotations

import sys

import jsbridge
from harness import Checks, report

PRELUDE = "let hlc = 0;\nfunction hlcSave(){}\n"
FNS = ["hlcNow", "hlcUpdate"]
REF = "docs/VERIFICATION.md — HLC counter precision"


def main() -> int:
    ck = Checks("hybrid logical clock")

    if not jsbridge.node_available():
        print("  SKIPPED: node is not on PATH")
        ck.ok(False, "HLC logic could not be tested (node missing)")
        return report(ck)

    print("  encoding shape")
    r = jsbridge.run(FNS, """
      const a = hlcNow(), b = hlcNow(), c = hlcNow();
      const result = { monotonic: a < b && b < c,
                       shape: Math.floor(a / 65536) > 1e12,
                       exceedsExactRange: a > Number.MAX_SAFE_INTEGER,
                       incrementLost: (a + 1) === a };
    """, PRELUDE)
    ck.ok(r["shape"], "the physical half is a real millisecond clock, not a counter")
    ck.ok(r["exceedsExactRange"],
          "an HLC value is above Number.MAX_SAFE_INTEGER — the root of the defect below")
    ck.ok(r["incrementLost"],
          "at that magnitude a +1 increment does not change the value at all")
    ck.known_bad(r["monotonic"],
                 "successive timestamps in the same millisecond strictly increase", REF)

    print("  ordering across milliseconds — this half works, and is what carries normal use")
    r = jsbridge.run(FNS, """
      const first = hlcNow();
      const start = Date.now();
      while (Date.now() - start < 3) {}          // cross a millisecond boundary
      const second = hlcNow();
      const result = { ordered: second > first };
    """, PRELUDE)
    ck.ok(r["ordered"], "writes in different milliseconds are correctly ordered")

    print("  clock skew between machines")
    r = jsbridge.run(FNS, """
      hlc = 0;
      const ahead = (Date.now() + 3600_000) * 65536;   // a peer an hour ahead
      hlcUpdate(ahead);
      const adopted = hlc >= ahead;
      const mine = hlcNow();
      const result = { adopted, winsNext: mine > ahead };
    """, PRELUDE)
    ck.ok(r["adopted"], "a controller adopts a peer's clock when the peer is ahead")
    ck.known_bad(r["winsNext"],
                 "our next write outranks a fast peer's, so skew cannot lock us out", REF)

    r = jsbridge.run(FNS, """
      const start = hlcNow();
      hlcUpdate((Date.now() - 3600_000) * 65536);      // a peer an hour behind
      const after = hlc;
      const result = { neverGoesBack: after >= start };
    """, PRELUDE)
    ck.ok(r["neverGoesBack"], "a lagging peer never drags this controller's clock backwards")

    print("  the test-authoring trap")
    r = jsbridge.run(FNS, """
      const real = hlcNow();
      const rawMs = Date.now();
      const result = { ratio: real / rawMs, rawLoses: rawMs < real };
    """, PRELUDE)
    ck.ok(r["rawLoses"],
          "a raw-millisecond timestamp loses to any HLC value — the classic test mistake")
    ck.ok(60000 < r["ratio"] < 70000,
          f"the gap is roughly the 65536 shift (measured {r['ratio']:.0f}x)")

    print("  migration from pre-HLC controllers")
    r = jsbridge.run(FNS, """
      hlc = 0;
      const legacy = Date.now();                        // a pre-HLC raw-ms timestamp
      hlcUpdate(legacy);
      const upgraded = hlcNow();
      const result = { migrationIsOneWay: upgraded > legacy };
    """, PRELUDE)
    ck.ok(r["migrationIsOneWay"],
          "an upgraded controller outranks a pre-HLC controller's raw timestamps")

    print("  the persisted clock must not restart below what was seen on the LAN")
    ck.ok(jsbridge.source_contains(r"let hlc = \+\(localStorage\.getItem\('mp32_hlc'\)\|\|0\)"),
          "the clock is restored from storage, so a reopened controller never restarts low")
    ck.ok(jsbridge.source_contains(r"function hlcSave\(\)\{ try\{ localStorage\.setItem\('mp32_hlc'"),
          "every clock change is persisted")

    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
