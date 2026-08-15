#!/usr/bin/env python3
"""Read-only physical MP32 preflight required before release builds."""

import json
import sys
import threading
import time

import mp32_gui as app


def fail(message: str) -> int:
    print(f"PREFLIGHT FAILED: {message}")
    print("No release build should be created. Close other device controllers and retry.")
    return 1


def main() -> int:
    # Use the same discovery the application uses — beacon, then peer controllers, then the
    # admin-port scan — rather than requiring a beacon record whose name contains "MP32".
    # The device host on this network announces nothing that reaches other machines, so a
    # beacon-only gate fails even when the unit is present and fully reachable.
    beacon = app.BeaconListener()
    beacon.start()
    time.sleep(2.0)                      # give announcements a chance before falling back

    device = app.MP32Device(app.DEVICE_IP, app.DEVICE_PORT)
    device.beacon = beacon
    device.session_enabled = True
    device._auto_target()
    if (device.ip, device.port) == (app.DEVICE_IP, app.DEVICE_PORT):
        beacon.stop()
        return fail("no host on this network reports an attached MP32")

    admin = app.query_admin(device.ip) or {}
    units = ", ".join(admin.get("devices") or []) or "unnamed"
    print(f"PREFLIGHT discovery: {units} on {device.ip}:{device.port} "
          f"(host server {admin.get('server_version') or 'unknown'})")
    beacon.stop()

    started = time.time()
    device._try_connect()
    if not device.connected or not device.config_valid or len(device.config) != app.NUM_CHANNELS:
        state = device.connection_state
        error = device.last_error or "no validated 32-channel config"
        device.stop()
        return fail(f"state={state}; {error}")

    device._running = True
    reader = threading.Thread(target=device._read_loop, daemon=True)
    reader.start()
    initial_peaks = list(device.peaks)
    time.sleep(2.5)
    cyclic_ok = device.connected and (device.peaks != initial_peaks or any(v != 99 for v in device.peaks))
    summary = {
        "channels": len(device.config),
        "elapsed_seconds": round(time.time() - started, 2),
        "cyclic_received": cyclic_ok,
    }
    device.stop()
    reader.join(timeout=2.0)
    if not cyclic_ok:
        return fail("validated config arrived, but no cyclic/VU feed was observed")
    print("PREFLIGHT PASSED: " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
