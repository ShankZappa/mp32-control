"""Suite 8 — nothing is written to a device that has not been confirmed as an MP32.

On 2026-10-29 a ZEN Q was pointed at by hand through the Device panel's "Connect to device"
field and reached `CRITICAL ERROR / HardFault_Handler`. Report-format registration is
host-wide, sticky and first-one-wins, and a format that does not match the attached unit
describes hardware that unit does not have. `docs/PROTOCOL.md` had warned about exactly
this; the warning was in prose, and prose does not stop a socket.

`_auto_target()` had always confirmed the attached unit from the passive admin read before
choosing a target. `retarget()` — the manual field, and the remembered address — went
straight to the socket, so the check had to move to the last point every path passes
through: immediately before the first write.

These checks drive `MP32Device._confirm_unit_type()` directly with a scripted admin reply.
No hardware is involved and none could be: that is the point of the gate.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mp32_gui as G  # noqa: E402
from harness import Checks, report  # noqa: E402


def device_with_admin(reply, ip="10.0.0.5"):
    """An MP32Device whose admin-port read returns `reply` — a parsed admin dict, or None
    for a host that does not answer at all."""
    d = G.MP32Device(ip, 2021)
    G.query_admin = lambda _ip, timeout=1.2: reply       # patched per case, restored below
    return d


def main() -> int:
    ck = Checks("unit guard")
    real_query_admin = G.query_admin
    try:
        print("  a host with an MP32 attached is allowed")
        d = device_with_admin({"server_version": "1.8.9",
                               "devices": ["MP32 with SN:1302917060309"], "clients": []})
        ck.ok(d._confirm_unit_type(), "an MP32 passes the gate")

        print("  the unit that actually faulted must be refused")
        d = device_with_admin({"server_version": "1.47",
                               "devices": ["ZenQ with SN:04704121000194"], "clients": []})
        ck.ok(not d._confirm_unit_type(), "a ZEN Q is refused")
        ck.ok("ZenQ" in (d._load_error or ""), "the refusal names what was found")
        ck.equal(d.connection_state, "wrong_unit", "the state says the unit is wrong")
        ck.ok("firmware" in (d._load_error or "").lower(),
              "the refusal says why, not just that")

        print("  any other model is refused the same way")
        for name in ("Galaxy32 with SN:4200322000105", "Orion Studio", "Discrete 8"):
            d = device_with_admin({"server_version": "1.8", "devices": [name], "clients": []})
            ck.ok(not d._confirm_unit_type(), f"{name.split()[0]} is refused")

        print("  a host that will not say what it has is refused, not assumed")
        d = device_with_admin(None)
        ck.ok(not d._confirm_unit_type(), "an unreachable admin port is refused")
        ck.equal(d.connection_state, "unit_unconfirmed", "the state says it is unconfirmed")
        ck.ok("blind" in (d._load_error or "").lower(),
              "the refusal says it will not register blind")

        print("  a host with a device server but nothing plugged in")
        d = device_with_admin({"server_version": "1.8.18", "devices": [], "clients": []})
        ck.ok(not d._confirm_unit_type(), "a host with no unit attached is refused")
        ck.ok("nothing" in (d._load_error or "").lower(),
              "the refusal says nothing was attached")

        print("  several units on one host: ours being present is enough")
        d = device_with_admin({"server_version": "1.8.9",
                               "devices": ["ZenQ with SN:0470", "MP32 with SN:1302"],
                               "clients": []})
        ck.ok(d._confirm_unit_type(),
              "a host serving our unit alongside another is allowed")

        print("  the match is case-insensitive, as the admin reply's wording is not ours")
        d = device_with_admin({"server_version": "1.8.9",
                               "devices": ["mp32 with sn:1302"], "clients": []})
        ck.ok(d._confirm_unit_type(), "lower-case naming still matches")

        print("  the gate sits in the connect path, not only in discovery")
        import inspect
        src = inspect.getsource(G.MP32Device._try_connect)
        gate = src.index("_confirm_unit_type")
        write = src.index("_register_report_format")
        ck.ok(gate < write,
              "_confirm_unit_type runs before _register_report_format in _try_connect")
        ck.ok("s.close()" in src[gate:write],
              "the socket is closed on refusal rather than left open")

        print("  retarget must not reach the device on its own")
        src = inspect.getsource(G.MP32Device.retarget)
        ck.ok("_register_report_format" not in src and "send_command" not in src,
              "retarget only moves the target; every write still passes the gate")
    finally:
        G.query_admin = real_query_admin

    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
