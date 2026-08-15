"""Suite 2 — startup ordering and the "never fabricate device state" rule.

Two separate promises are checked here.

**The HTTP server accepts before anything reaches the network.** Connecting to an
unreachable device can take ~36 s (10 attempts x 3 s timeout plus back-off). Any startup
that opened the device before its HTTP server would serve nothing for that whole time,
which is exactly what once showed the packaged application as a blank window. The page is
built to render `discovering` / `connecting` states, so it must be reachable early enough
to render them.

**An unvalidated session reports nothing rather than something plausible.** `/api/status`
returns `config: null` when there is no validated live session — never a fabricated
all-zero configuration, which would show a real desk as if every preamp were at zero gain.
Device writes are refused with 503 for the same reason.
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from harness import Checks, Cluster, report, wait_for

DEVICE_WRITES = [
    ("/api/set_gain", {"idx": 0, "gain": 10}),
    ("/api/set_phantom", {"idx": 0, "phantom": 1}),
    ("/api/set_pretype", {"idx": 0, "pretype": 1}),
    ("/api/set_all_gain", {"gain": 0}),
    ("/api/set_all_phantom", {"phantom": 0}),
    ("/api/recall_preset", {"idx": 1}),
    ("/api/save_preset", {"idx": 1}),
    ("/api/set_power", {"on": 1}),
]


def status_code(controller, path, payload):
    req = urllib.request.Request(controller.base + path, data=b"{}"
                                 if payload is None else __import__("json").dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    ck = Checks("startup ordering")
    with Cluster() as cl:
        a = cl.start("node-A")

        # The controller answered wait_ready() before this line, which already proves the
        # server was accepting; these checks are about what it serves at that moment.
        ck.ok(wait_for(lambda: a.get("/api/status") is not None, 3),
              "/api/status answers immediately after start, with no device present")

        st = a.get("/api/status")
        ck.ok("connection_state" in st,
              "status carries a connection_state instead of a bare offline flag")
        ck.ok(st.get("connection_state") in
              ("waiting_for_web_host", "discovering", "tcp_error", "config_empty",
               "loading_config", "online"),
              f"connection_state is a known state (got {st.get('connection_state')!r})")
        ck.ok(st.get("config") is None,
              "config is null for an unvalidated session, never fabricated zeros")
        ck.ok(st.get("controller_role") in ("web_host", "desktop"),
              "controller_role is reported")
        ck.ok("unreachable_hosts" in st,
              "unreachable_hosts is exposed so a blocked host is diagnosable")
        ck.ok(isinstance(st.get("direct_server_url"), str)
              and st["direct_server_url"].startswith("http://"),
              "a direct fallback URL is published for phones")

        # Resolved per request, not snapshotted at start: a controller that runs for weeks
        # outlives its DHCP lease and would otherwise advertise an address that has moved.
        ck.ok(a.get("/api/status").get("direct_server_url") == st["direct_server_url"],
              "the direct URL is stable while the address is stable")

        print("  device writes must be refused without a validated live session")
        refused = 0
        for path, payload in DEVICE_WRITES:
            if status_code(a, path, payload) == 503:
                refused += 1
        ck.equal(refused, len(DEVICE_WRITES),
                 "every device write returns 503 without a validated session")

        print("  non-device endpoints must still work while the device is absent")
        ck.ok(isinstance(a.get("/api/peers"), dict), "/api/peers serves during startup")
        ck.ok(isinstance(a.get("/api/devices").get("devices"), list),
              "/api/devices serves during startup")
        ck.ok(isinstance(a.get("/api/meta_state").get("fields"), dict),
              "/api/meta_state serves during startup")
        ck.ok(a.get("/api/peaks").get("peaks") is not None,
              "/api/peaks serves a defined value during startup")

        peaks = a.get("/api/peaks")["peaks"]
        ck.ok(all(p == 99 for p in peaks),
              "offline peaks default to 99 (silence), never 0 (full scale)")

        print("  the UI itself must be served, not just the API")
        with urllib.request.urlopen(a.base + "/", timeout=4) as r:
            body = r.read().decode("utf-8", "ignore")
        ck.ok(r.status == 200 and "<html" in body.lower(), "the panel page is served")
        ck.ok("MP32" in body, "the served page is the MP32 Control panel")

        print("  metadata sync must work with no device attached at all")
        k = cl.key("name:7")
        a.push(k, "Overhead L")
        ck.equal(a.value(k), "Overhead L",
                 "metadata is accepted while the device is absent")

    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
