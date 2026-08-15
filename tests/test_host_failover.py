"""Suite 5 — web-host liveness, and the contract the phone's recovery depends on.

**A heartbeat is not proof of service.** A multicast heartbeat proves only that a
controller's process is alive. A host blocked by a local firewall, bound to a different
interface, or left with a dead server thread keeps heartbeating `web_leader: true` and keeps
publishing `mp32-control.local`. That happened here: one controller heartbeat as leader and
answered mDNS while its TCP 8765 was closed, and every other controller sat in
`waiting_for_web_host` for 25 days while the MP32 was reachable the whole time. There was no
recovery path and no diagnostic. A peer whose HTTP port stays unreachable must therefore
lose web-host candidacy entirely, and must be *named* in the status so it is visible.

**Coverage boundary, stated plainly.** The liveness rules are exercised in-process against
the real `StableHostService`, because the shipped probe connects to a peer's LAN address on
*this* controller's own port — several controllers on one machine cannot produce a genuine
unreachable peer. What runs over real HTTP here is the server-side contract the phone's
recovery is built on: `/api/peers` so the page can remember addresses, CORS and preflight so
it can poll a peer cross-origin, and the `/api/status` fields behind the handover indicator.

The end-to-end PWA case — kill the host, watch an installed iPad PWA recover through a
remembered peer rather than Safari's DNS cache — is not covered here and stays on the
physical checklist in `docs/VERIFICATION.md`.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mp32_gui as G  # noqa: E402
from harness import Checks, Cluster, report, wait_for  # noqa: E402


class FakePeers:
    """Stands in for PeerService so peer liveness can be scripted exactly."""

    def __init__(self, host="this-mac", started_at=1000.0):
        self.host = host
        self.id = "self0000"
        self.started_at = started_at
        self.web_leader = False
        self.device = None
        self._peers = []

    def set(self, *peers):
        self._peers = list(peers)

    def active_peers(self, max_age=None):
        return list(self._peers)


def peer(pid, host, started_at, ip="10.0.0.9", web_leader=False, online=True):
    return {"id": pid, "host": host, "started_at": started_at, "ip": ip,
            "web_leader": web_leader, "online": online, "last_seen": time.time()}


def liveness_checks(ck: Checks):
    fp = FakePeers(host="this-mac", started_at=1000.0)
    hs = G.StableHostService(fp, 8765)
    hs._started = 0.0            # past the election grace, so results are deterministic
    older = peer("older001", "other-mac", 500.0)

    fp.set(older)
    ck.ok(not hs._should_lead(), "an older reachable peer wins the election")

    print("  a peer unreachable past the grace period must lose candidacy")
    hs._unreachable_since["older001"] = time.time() - (G.HOST_PROBE_GRACE + 1)
    ck.ok(hs._peer_unreachable(older), "the peer is classified unreachable after the grace")
    ck.ok(older not in hs._electable_peers(), "an unreachable peer is not electable")
    ck.ok(hs._should_lead(), "we take the role from a peer that heartbeats but does not serve")

    print("  the grace period must not fire early — a brief blip is not a dead host")
    hs._unreachable_since["older001"] = time.time() - 1.0
    ck.ok(not hs._peer_unreachable(older), "a peer unreachable for 1 s is not yet excluded")
    ck.ok(not hs._should_lead(), "a brief blip does not hand the role over")

    print("  recovery: once the port answers again, candidacy must come back")
    hs._unreachable_since.pop("older001", None)
    ck.ok(not hs._peer_unreachable(older), "a recovered peer is no longer unreachable")
    ck.ok(not hs._should_lead(), "the recovered peer gets the role back, with no flapping")

    print("  a blocked host must be named, not silently ignored")
    hs._unreachable_since["older001"] = time.time() - (G.HOST_PROBE_GRACE + 1)
    named = hs.unreachable_hosts()
    ck.ok(len(named) == 1, "the blocked host is reported in the diagnostics")
    ck.equal(named[0].get("host"), "other-mac", "the diagnostic names the blocked host")

    print("  a previous incarnation of ourselves must never evict us")
    ghost = peer("ghost001", "this-mac", 100.0)
    fp.set(ghost)
    hs2 = G.StableHostService(FakePeers(host="this-mac", started_at=1000.0), 8765)
    hs2._started = 0.0
    hs2.peers._peers = [ghost]
    ck.ok(hs2._peer_is_stale_self(ghost),
          "an older peer on our own host is recognised as a dead previous instance")
    ck.ok(ghost not in hs2._electable_peers(), "a stale self is not electable")
    ck.ok(hs2._should_lead(), "we do not defer to our own dead process")

    print("  a genuinely newer instance on our host is NOT a stale self")
    newer_self = peer("newer001", "this-mac", 5000.0)
    ck.ok(not hs2._peer_is_stale_self(newer_self),
          "a newer instance on the same host is not treated as a ghost")

    print("  proxy targets must be ordered oldest-first and exclude dead hosts")
    fp3 = FakePeers(host="this-mac", started_at=9000.0)
    hs3 = G.StableHostService(fp3, 8765)
    hs3._started = 0.0
    leader = peer("lead0001", "mac-1", 100.0, ip="10.0.0.1", web_leader=True)
    plain = peer("plain001", "mac-2", 200.0, ip="10.0.0.2")
    dead = peer("dead0001", "mac-3", 50.0, ip="10.0.0.3", web_leader=True)
    fp3.set(leader, plain, dead)
    hs3._unreachable_since["dead0001"] = time.time() - (G.HOST_PROBE_GRACE + 1)
    urls = hs3.leader_base_urls()
    ck.ok("http://10.0.0.3:8765" not in urls,
          "an unreachable host is never a proxy target, so /api cannot stall on it")
    ck.equal(urls[0], "http://10.0.0.1:8765",
             "the elected leader is the first proxy candidate")
    ck.ok("http://10.0.0.2:8765" in urls,
          "an online non-leader is kept as a fallback, since presence can beat the leader flag")

    hs3.active = True
    ck.equal(hs3.leader_base_urls(), [],
             "the holder proxies to nobody, so it cannot recurse into itself")


def http_contract_checks(ck: Checks):
    with Cluster() as cl:
        a, b = cl.start_all("node-A", "node-B")
        ck.ok(wait_for(lambda: len(a.peers()) >= 1, 8),
              "a controller learns its peers, which is what the page remembers addresses from")

        p = a.peers()[0]
        ck.ok(p.get("ip"), "each remembered peer carries an address the page can probe")
        ck.ok(p.get("host"), "each peer carries a host name, so the UI can name it")
        ck.ok("web_leader" in p, "each peer reports whether it claims the web-host role")
        ck.ok(isinstance(p.get("started_at"), float),
              "each peer broadcasts started_at, which is what the election ranks on")

        me = a.get("/api/peers").get("self") or {}
        ck.ok(me.get("id") and me.get("host"), "a controller identifies itself to the page")

        print("  cross-origin polling must work: the page runs on a fallback address")
        status, headers = a.raw("/api/status")
        ck.equal(headers.get("Access-Control-Allow-Origin"), "*",
                 "/api/status is readable cross-origin from a fallback host")
        _, opt_headers = a.raw("/api/status", method="OPTIONS")
        ck.equal(opt_headers.get("Access-Control-Allow-Origin"), "*",
                 "preflight answers, so a cross-origin POST is not blocked")
        ck.ok("POST" in (opt_headers.get("Access-Control-Allow-Methods") or ""),
              "preflight permits POST, which the control calls need")
        ck.ok("Content-Type" in (opt_headers.get("Access-Control-Allow-Headers") or ""),
              "preflight permits the JSON content type")

        print("  the handover indicator's inputs must be present on every controller")
        for c in (a, b):
            st = c.get("/api/status")
            ck.ok("stable_host_active" in st and "stable_host_available" in st,
                  f"{c.node} reports the stable-host state the header renders")
            ck.ok(isinstance(st.get("unreachable_hosts"), list),
                  f"{c.node} reports unreachable_hosts so a blocked host is nameable")
            ck.ok(isinstance(st.get("server_url"), str) and st["server_url"],
                  f"{c.node} publishes the mobile URL the PWA returns to")

        print("  a peer disappearing must expire, or the page would probe a dead address")
        before = len(a.peers())
        b.stop()
        ck.ok(wait_for(lambda: len(a.peers()) < before, 8),
              "a quit controller expires from the peer list within the timeout")


def main() -> int:
    ck = Checks("host failover")
    liveness_checks(ck)
    http_contract_checks(ck)
    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
