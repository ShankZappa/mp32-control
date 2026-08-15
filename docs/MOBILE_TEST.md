# Mobile and PWA testing

MP32 Control serves a responsive controller to browsers on the same trusted LAN.

## Stable address

Start the desktop application on at least one Mac or Windows host, then open:

```text
http://mp32-control.local:8765
```

On iPhone or iPad, use Safari → Share → Add to Home Screen. The stable hostname follows
the elected desktop host. The Device panel also shows the current host's direct IP as a
fallback.

## Handover test

1. Run MP32 Control on two computers on the same LAN.
2. Open the stable address on the phone/tablet.
3. Confirm the first host remains the sticky leader.
4. Close the leader application.
5. Confirm the web UI displays “Re-establishing connection · handover in progress”.
6. Confirm another host takes over and status, metadata, and meter feeds recover.
7. With two desktop controllers open, confirm the Controllers panel labels exactly one as
   `web host`, and changes from either computer reflect the current hardware state everywhere.
7. Repeat in both Mac→Windows and Windows→Mac directions.

Expected failover is roughly 4–6 seconds on a healthy LAN. Browser and mDNS caching may
extend this slightly. If iOS has suspended the PWA in the background, recovery resumes
when the app returns to the foreground.

## Firewall and network requirements

- TCP 8765 must be reachable on the private LAN.
- mDNS UDP 5353 and the MP32 Control peer multicast must be allowed.
- Windows users should allow Private Network access when prompted.
- Guest Wi-Fi and client isolation commonly block discovery and peer synchronization.

## Security

The control API has no authentication. Do not expose it to the internet or use it on an
untrusted network.
