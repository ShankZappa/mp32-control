"""Suite 1 — chronological web-host election.

The rule under test (`StableHostService._should_lead`): the longest-running eligible
controller holds the role. One rule has to cover the whole lifecycle, because the previous
newest-wins rule plus a separate "do not steal from the incumbent" branch pulled in
opposite directions, and that second branch is what let a dead holder deadlock the network.

Real controller processes, real multicast, real HTTP. mDNS publication is disabled in the
launcher so a test never claims `mp32-control.local` from a live controller; the election
itself is untouched.
"""
from __future__ import annotations

import sys
import time

from harness import Checks, Cluster, report, wait_for

GRACE = 3.0     # HOST_ELECTION_GRACE is 2.5 s; allow a little slack
SETTLE = 6.0    # peer expiry is 3.5 s, so an inheritance needs longer than that


def hosts(controllers):
    return [c.node for c in controllers if c.is_web_host()]


def main() -> int:
    ck = Checks("host election")
    with Cluster() as cl:
        print("  starting A, then B a second later, then C")
        a = cl.start("node-A")
        time.sleep(1.0)
        b = cl.start("node-B")
        time.sleep(1.0)
        c = cl.start("node-C")

        ck.ok(wait_for(lambda: a.is_web_host(), GRACE + 4),
              "the first-started controller takes the role")
        ck.ok(not b.is_web_host(), "a controller started second does not take the role")
        ck.ok(not c.is_web_host(), "a controller started third does not take the role")
        ck.ok(wait_for(lambda: len(hosts([a, b, c])) == 1, 4),
              "exactly one controller claims the role")

        ck.ok(wait_for(lambda: len(a.peers()) == 2, 6), "A sees both other controllers")
        ck.ok(a.get("/api/status").get("controller_role") == "web_host",
              "the holder reports controller_role=web_host")
        ck.ok(b.get("/api/status").get("controller_role") == "desktop",
              "a non-holder reports controller_role=desktop")

        print("  A quits — B should inherit without a handover step")
        a.stop()
        ck.ok(wait_for(lambda: b.is_web_host(), SETTLE + 4),
              "the next-oldest inherits when the holder disappears")
        ck.ok(not c.is_web_host(), "the youngest does not inherit ahead of the middle one")
        ck.ok(wait_for(lambda: len(hosts([b, c])) == 1, 4),
              "still exactly one holder after inheritance")

        print("  A restarts — it must come back as the youngest and not take the role back")
        a2 = cl.start("node-A")
        time.sleep(GRACE + 2.0)
        ck.ok(not a2.is_web_host(), "a restarted controller does not take the role back")
        ck.ok(b.is_web_host(), "the inheriting holder keeps the role when the old one returns")
        ck.ok(wait_for(lambda: len(hosts([a2, b, c])) == 1, 4),
              "exactly one holder after the restart")

        print("  B quits — C should inherit, and A must still rank behind C")
        b.stop()
        ck.ok(wait_for(lambda: c.is_web_host(), SETTLE + 4),
              "the role passes on again to the next-oldest")
        ck.ok(not a2.is_web_host(),
              "the restarted controller still ranks behind an older peer")

        print("  every remaining controller must name the same holder")
        ck.ok(wait_for(lambda: len(hosts([a2, c])) == 1, 4),
              "no split-brain: the surviving controllers agree on one holder")
        ck.ok(a2.get("/api/status").get("stable_host_available") is not None,
              "stable_host_available is reported for diagnostics")

    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
