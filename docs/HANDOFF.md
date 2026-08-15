# MP32 Control — current handoff

Current architecture release: **1.3.1**.

## Architecture

Device state and GUI coordination are separate concerns, because only one of them has an
authority to defer to.

- **Device state is read from and written to the device, by every controller directly.**
  Gain, phantom, input type, preset, power, config, and VU values are device-authoritative:
  the hardware knows its own state, and the host server broadcasts each change to every other
  connected client. These values never travel through peer metadata and are never restored
  during a handover. An earlier scheme that carried gain through metadata was unstable for
  exactly this reason — it created a second, lagging copy of data that already had an
  authoritative home, and then needed conflict resolution over it.
- **Metadata is replicated to every controller, because the device does not store it.**
  Channel names, colours, groups, and Public Notes have no home on the hardware, so
  `PeerService` replicates them peer-to-peer with per-field last-write-wins. A joining
  controller bootstraps them from the current host, which gives it one unambiguous place to
  ask. Local Notes remain browser-local.
- **Replication is continuous, so inheriting the role transfers nothing.** A host that
  crashes or loses its network cannot hand anything over, so nothing may depend on it doing
  so. Every controller already holds a full metadata replica; the host is only the bootstrap
  source for newcomers and the publisher of the PWA hostname.
- `StableHostService` elects that host and nothing else.

## Phone layout

The header holds four groups: logo, presets, secondary buttons, and status+power. Status and
power are their own group (`.hstatus`) rather than the tail of the secondary row, because the
row order has to differ by width.

- **Under 620 px** the header stacks into three rows — logo/status/power, then presets, then
  everything secondary. Previously all seven right-hand controls shared one line, so the power
  button, the one control that must always be reachable, was the first thing pushed off the
  screen edge. Word labels on About and Device become icons under 430 px; the status pill
  shows a short state (`Online`, `Standby`, `No host`) instead of an ellipsised sentence.
- **620–900 px** stays on one line: a landscape phone has height to spare least of all, and
  the whole strip fits at that width. The logo is the element allowed to shrink, otherwise the
  preset group is pushed over the top of it.
- **Above 900 px** is unchanged.

Every header and card control has a 40 px minimum touch target. Verified with no horizontal
overflow and no overlapping groups at 320, 390, 844, and 1440 px.

## Shared notes are cards, not one text field

Public Notes were a single shared textarea synced as one metadata field. Last-write-wins over
one field means the whole document: two people typing at once, and the later write erased
everything the other had written.

Each card is now its own metadata key (`card:<id>`), so it rides the existing per-field
last-write-wins with no protocol change. Two people on different cards never collide, and two
on the same card lose one card at worst.

**Locking is politeness, not correctness.** A lease (`lock:<id>`, 15 s, renewed every 5 s
while a field is focused) marks a card read-only for everyone else and names who holds it.
Leases carry their own expiry, so an editor whose browser or machine dies never leaves a card
stuck. Nothing depends on the lease being delivered: if it is lost, per-card last-write-wins
still bounds the damage to that card. A controller that loses a simultaneous grab stops
editing rather than fighting, and says so.

**Deletion is a tombstone.** A card is deleted by writing `deleted: true` with a timestamp,
never by removing the key. Absence must not mean deletion, or a controller joining with an
empty store would wipe everyone's cards. A stale edit cannot resurrect a deleted card.

**Edits sync while typing** (800 ms debounce), not on blur, so a crash mid-edit cannot take
everything typed since the field was focused.

**Migration** carries the old textarea into a card with the fixed id
`migrated-public-notes`. The "already migrated" flag is per-browser but cards are shared, so a
generated id would let every browser and phone still holding old notes add its own duplicate —
observed during testing. One deterministic key makes them converge instead.

## Host election — chronological, single rule

The longest-running eligible controller holds the role. `min(started_at, id)` over the
active peers plus ourselves; peer id breaks ties as a stable total order.

One rule covers the whole lifecycle, so no controller negotiates and none can race:

| Event | Outcome |
|---|---|
| B starts while older A holds the role | B ranks behind A and does not take it |
| A disappears | B is already the minimum and inherits, with no handover step |
| A restarts later | A returns as the youngest and cannot take the role back |

Every controller broadcasts its own `started_at`, so all of them rank the same candidate set
from identical values and reach the same answer without agreeing on anything first. Clock
skew between machines can change *which* controller wins; it cannot make them disagree.

This replaced a newest-wins rule plus a separate "do not steal from the incumbent" branch.
The two pulled in opposite directions, and the second branch is what allowed a dead holder to
deadlock the whole network. Chronology encodes stickiness on its own, so the branch is gone.

Excluded from candidacy: a previous incarnation of ourselves — it is older, so it would
otherwise win and we would defer to a dead process — and any peer whose HTTP port is proven
unreachable.

## Report-format registration — read this before anything else

The host server refuses every device request until a client has registered the report
format, replying:

```json
{"type": "single", "contents": "", "COMMAND_STATUS": "FAIL"}
```

`initialize_format([REPORT_FORMAT])` is the only command that registers it. Registration is
server-wide and sticky until the host server process restarts. A controller that never sends
it therefore appears to work only after the vendor panel has been run once on that host, and
appears to break again after the host machine reboots.

`MP32Device._register_report_format()` now sends it on every connect. The server ignores the
request when a format is already registered, so it is safe to repeat, and the client must
send the **complete** format — a partial registration would become the format in force for
the vendor panel too.

Verified 2026-07-29 against host server 1.8.9 with no vendor panel running: `get_config`
returned `COMMAND_STATUS FAIL` before registration and a full 32-channel config immediately
after, on the same socket and on a fresh one.

## Device-session capability decision — corrected

The earlier conclusion that the MP32 withholds config from additional TCP clients was a
misreading of this same failure. The probe clients were not being denied a session; they had
simply never registered a report format, so every request failed regardless of who else was
connected.

Re-tested 2026-07-29 with the controller online and holding a validated session: two further
independent clients each received a complete 32-channel config. The host server is
multi-client, which matches its own documentation of serving as many clients as the OS
permits.

The documented precondition for

```python
DEVICE_SUPPORTS_MULTIPLE_CLIENTS = True
```

is therefore met. Flipping it lets every desktop own a direct session and decouples device
access from web-host election, removing the failure mode where a controller is online but
device-blind because another machine holds the only session. It remains `False` pending the
cross-machine physical test in `docs/VERIFICATION.md`, because the change alters how every
controller reaches the device.

## Config validity — important

`MP32Device._try_connect()` uses a private `_initializing` transport state so
`send_command(GET_CONFIG)` can run before the UI is marked Online. A session that returns
empty config is closed immediately; retry uses a fresh TCP socket because session rights
appear to be assigned at accept-time. Only a response with at least 32 channels sets
`config_valid` and `connected`.

When config is empty, the socket is closed and reconnect is retried. `/api/status` returns
`config: null` for an unvalidated local session, never a fabricated all-zero config. Device
writes return HTTP 503 unless a validated live session exists.

`connection_state` distinguishes web-host wait, discovery, TCP connect, config load, empty
config, and transport errors. The header displays that state instead of a generic Offline.

## VU convention — do not invert

The device reports magnitude below full scale:

- `0` = maximum/loudest (`0 dBFS`);
- increasing values are quieter;
- `60+` = effectively silence.

The UI maps `(60 - value) / 60` to bar height and displays the value with a minus sign.
Offline/default peak arrays use `99`, not `0`, so an unopened feed never appears full-scale.

## Web-host liveness — heartbeat is not proof of service

A multicast heartbeat proves only that a controller's process is alive. It does not prove
that its HTTP server is reachable by the other controllers. A host blocked by a local
firewall, bound to a different interface, or left with a dead server thread continues to
heartbeat `web_leader: true` and continues to publish `mp32-control.local`.

Observed on 2026-07-29: a controller heartbeat `web_leader: true` and answered mDNS, but
its TCP 8765 was closed. Every other controller therefore sat in `waiting_for_web_host`
indefinitely — for 25 days on one machine — while the MP32 itself was reachable the whole
time. There was no recovery path and no diagnostic.

`StableHostService` now runs a probe thread that TCP-connects to each active peer's HTTP
port every second. A peer continuously unreachable for `HOST_PROBE_GRACE` loses web-host
candidacy entirely: it is excluded from the incumbent check, from the final election
tiebreak, and from `leader_base_urls()` proxy targets. Peers sharing our own address are
skipped — a second instance on this machine is handled by `_peer_is_stale_self`.

The grace period is deliberately longer than `PEER_TIMEOUT`: a controller that simply quits
disappears from `active_peers()` first, so this path only ever fires for alive-but-not-
serving hosts and never shortens normal failover.

`/api/status` exposes `unreachable_hosts`, and the header names the blocked host instead of
showing a bare `Waiting for Web Host…`.

## Phone/PWA recovery when its host dies

The page uses relative URLs, so it follows whichever controller currently owns
`mp32-control.local`. That is sufficient only while the name resolves promptly, and it does
not after a crash: a host that dies never withdraws its mDNS record, and iOS caches
resolutions past their TTL.

The page therefore also remembers every controller it has seen through `/api/peers`. After
three consecutive failed status polls it probes the remembered addresses directly and adopts
the first that answers, so recovery never waits on DNS. Cross-origin works because the API
already sends `Access-Control-Allow-Origin` and answers preflight.

It prefers the page's own origin throughout: while running on a fallback address it re-probes
the origin every 5 seconds and returns to it as soon as the stable name points somewhere
live, so all controllers and the PWA converge back on one address.

Verified against two real controller servers: with the page's own host killed, it recovered
through a remembered peer address in 0.8 s. The iOS PWA case is still on the physical list.

## Stable host and timing

- peer heartbeat: 1 second;
- peer expiry: 3.5 seconds;
- election check: 0.5 seconds;
- initial election grace: 2.5 seconds;
- host liveness probe: 1 second, 0.6 second connect timeout;
- host unreachable grace before losing candidacy: 6 seconds;
- mDNS TTL: 5 seconds;
- failed mDNS publication retry: 2 seconds.

`local_lan_ip()` is cached for 5 seconds rather than resolved once at startup. A controller
that runs for weeks outlives its DHCP lease; the previous start-up snapshot kept
advertising a stale `direct_server_url` (observed: `.169` advertised while the host had
moved to `.112`) and compared the wrong address when excluding itself from proxy targets.

Native shutdown explicitly releases mDNS, the live device socket, peer service, beacon,
and HTTP server.

## Files

- `mp32_gui.py` — device client, peer metadata, web-host election, HTTP API, embedded UI;
- `mp32_protocol.py` — independent protocol constants and serialization;
- `app.py` — native pywebview launcher and cleanup;
- `docs/VERIFICATION.md` — physical and cross-platform verification matrix;
- `docs/PROTOCOL.md` — publishable protocol observations.

## Required physical verification

1. Start the current source on two desktops and verify one is labelled `Web Host`.
2. Confirm non-zero hardware gains appear correctly on both; no controller may show default
   zeros while claiming Online.
3. Change gain/type/phantom from each desktop and confirm all views converge to hardware.
4. Quit the web host; confirm the next peer takes over and the PWA reconnects through
   `mp32-control.local`.
5. Reopen the former host; it must show current hardware values and must not evict the
   healthy web host.
6. Build Windows only on Windows; test the same sequence across Mac, Windows, and iPad.

`device_preflight.py` enforces the single-device read-only gate before Mac and Windows
release builds. `MP32_SKIP_DEVICE_TEST=1` exists only for packaging CI and produces an
unverified artifact.

Latest physical preflight status (2026-07-29): **PREFLIGHT PASSED** — 32 channels and live
cyclic telemetry in 2.56 s, with no vendor panel running. Both blockers are resolved: the
empty-`get_config` failure was report-format registration, and the discovery failure was the
beacon-only gate.

`device_preflight.py` now drives the same discovery the application uses instead of requiring
a beacon record whose name contains `MP32`. A beacon-only gate fails on this network even
when the unit is present and fully reachable, because the host that owns it announces nothing
that reaches other machines. The gate reports the unit and host server it found.

## Discovery does not find the device host

On the maintainer's network the only announcement on the discovery multicast group comes
from a host that has **no device plugged**. The machine that
actually has the MP32 connected does not announce anything that reaches other controllers,
so `BeaconListener` never learns the real target.

The controller currently only reaches the device through the remembered address in
`last_device.json`. That hides the problem on a machine that has connected before and makes
it total on a fresh install: a newly packaged app on a new computer has nothing remembered
and will never find the unit. `device_preflight.py` fails for the same reason — it requires
a beacon whose name contains `MP32`.

The admin/status port is a reliable way to confirm which host actually owns the unit:
connecting to it returns a plaintext status listing the server version, the attached units,
and the connected clients. It is the natural basis for a discovery fallback that does not
depend on the beacon. See `docs/PROTOCOL.md`.

Do not commit network logs, private device identifiers, private IP addresses, vendor
code/binaries, credentials, or signing certificates. Protocol notes must be sanitized and
reproducible.
