"""Run one isolated MP32 Control controller for the test suites.

Spawned as a subprocess by `harness.py`. This is the real application: the same
`PeerService`, `StableHostService`, HTTP handler and routing table that ship. Only the
things that would reach outside the test are replaced, and each replacement is justified
below, because a harness that quietly stubs the thing under test proves nothing.

What is different from `mp32_gui.main()`, and why:

* **The device is constructed but never started.** These suites cover peer metadata,
  the web-host election and the HTTP API — none of which touch the hardware. Starting
  the device would only add a connect-retry loop against an MP32 that is not there.
  Anything device-facing belongs on the physical checklist in `docs/VERIFICATION.md`.
* **Own multicast group and port per run.** Controllers under test would otherwise share
  the LAN group with any copy of the app that happens to be running, whose browser
  re-announces its own stored metadata into the middle of the test. Isolating the group
  makes that structurally impossible instead of something each test has to remember.
* **Own `host` name per node.** One machine normally runs one controller, so the peer
  table treats a same-host/same-ip peer as a previous incarnation of itself and drops it
  (`PeerService._listen`, `StableHostService._peer_is_stale_self`). Several controllers
  on one machine must therefore present distinct host names or they delete each other.
* **mDNS publication disabled.** `mp32-control.local` is a single network-wide name. A
  test must never claim it from a real controller. The election itself is untouched —
  only the publish call is a no-op.
* **Loopback bind, and the peer HTTP probe forced reachable.** Binding to localhost keeps
  an unauthenticated control API off the network. The liveness probe connects to a peer's
  LAN address on *our own* port, which cannot work when several controllers share one
  machine, so it would wrongly mark every peer dead after the grace period and corrupt the
  election result. The probe logic itself is covered directly in `test_host_failover.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mp32_gui as G  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--node", required=True, help="peer host name for this controller")
    ap.add_argument("--group", required=True, help="isolated multicast group")
    ap.add_argument("--mcast-port", type=int, required=True)
    ap.add_argument("--started-at", type=float, default=None,
                    help="override started_at to script the chronological election")
    args = ap.parse_args()

    G.SERVER_PORT = args.port
    G.PeerService.GROUP = args.group
    G.PeerService.PORT = args.mcast_port
    G.StableHostService._publish_mdns = lambda self, ip: None
    G.StableHostService._tcp_reachable = lambda self, ip: True

    # An ip that is not the module default skips the "last known device" file, so a test
    # never inherits or rewrites the real user's remembered target.
    device = G.MP32Device("127.0.0.1", 2021)
    beacon = G.BeaconListener()          # constructed for /api/devices; never started
    device.beacon = beacon

    peers = G.PeerService(device)
    peers.host = args.node
    if args.started_at is not None:
        peers.started_at = args.started_at

    host_service = G.StableHostService(peers, args.port)

    G.Handler.device = device
    G.Handler.beacon = beacon
    G.Handler.peers_svc = peers
    G.Handler.host_service = host_service
    G.Handler.direct_mobile_url = f"http://127.0.0.1:{args.port}"
    G.Handler.mobile_url = host_service.url if host_service.available else G.Handler.direct_mobile_url

    # Ordering is asserted by test_startup_order.py: the HTTP server must be accepting
    # before any network service starts, so the page can render a "connecting" state
    # rather than nothing at all.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), G.Handler)
    threading.Thread(target=server.serve_forever, name="HTTPServer", daemon=True).start()

    peers.start()
    host_service.start()

    print(json.dumps({"ready": True, "node": args.node, "port": args.port,
                      "peer_id": peers.id, "started_at": peers.started_at}), flush=True)

    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        host_service.stop()
        peers.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
