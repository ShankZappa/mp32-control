# Tests

```bash
python3 tests/run_all.py
```

Roughly 35 seconds, 144 checks, no hardware and no network access required. Individual
suites run on their own the same way (`python3 tests/test_group_metadata.py`).

Requires Python 3.9+ and, for the browser-side suites, `node` on PATH. Without node those
suites report a failure rather than a silent pass, because a check that skips itself is
indistinguishable from a check that works.

## What these cover

Peer metadata replication, the web-host election, the HTTP API contract, and the browser
logic for groups, cards and the clock. Every controller in these suites is a **real process
running the shipped application**, talking over the real HTTP API and real multicast — not a
mock. That is deliberate: the defects this project actually shipped were in the interaction
between controllers, and unit tests over a mocked transport would not have caught either of
them.

| Suite | Checks | Covers |
|---|---:|---|
| `test_host_election.py` | 17 | Chronological election: stickiness, inheritance, restart, no split-brain |
| `test_startup_order.py` | 17 | HTTP serving before the network; never fabricating device state; 503 on writes without a validated session |
| `test_group_metadata.py` | 30 | Per-group keys, tombstones, newcomers wiping nothing, legacy whole-object merge |
| `test_notes_cards.py` | 34 | Per-card keys, lease acquire/expire/contend, tombstones, migration |
| `test_host_failover.py` | 35 | Liveness probe and candidacy loss, stale-self exclusion, proxy ordering, CORS and the fields the phone's recovery reads |
| `test_hlc.py` | 11 | Hybrid logical clock: skew adoption, migration, encoding limits |

## What these do NOT cover

Anything requiring the hardware, a second machine, or a real browser. Specifically: device
I/O, report-format registration against a live host, gain/phantom/preset behaviour,
cross-machine failover, and the installed iOS PWA recovering after its host dies. Those stay
on the physical checklist in [`../docs/VERIFICATION.md`](../docs/VERIFICATION.md), which is
the authority on what has actually been proven on hardware.

Two places drop below full-process fidelity, both stated in the suite that does it:

- `test_host_failover.py` drives the liveness rules in-process, because the shipped probe
  connects to a peer's LAN address on *this* controller's own port — several controllers on
  one machine cannot produce a genuinely unreachable peer.
- The `syncMeta()` legacy-merge branch and a few DOM-bound rules are guarded by asserting
  their shape in the shipped source, because they cannot be lifted out as standalone
  functions. Those checks say so in their own description.

## Two traps, handled by the harness

Both cost real time the first time these suites were written. Neither is left to discipline.

1. **Timestamps must be HLC-shaped** (`physical_ms << 16 | counter`). A plain millisecond
   value is ~65000x too small and loses every last-write-wins comparison, so the test fails
   while the code is fine. Use `harness.hlc()` and `harness.stale_hlc()`.
2. **Assert on your own run's keys.** Controllers under test would otherwise share the LAN
   multicast group with any running copy of the app, whose browser re-announces its own
   stored metadata mid-test. `Cluster` gives every run a private multicast group and port
   and namespaces keys through `cluster.key()`, so this is structural rather than something
   each test has to remember.

## Browser code

`jsbridge.py` extracts functions **verbatim from `mp32_gui.py` at test time** and runs them
under node. Nothing is copied into the tests. If a function is renamed or removed the
extraction raises, so a test can never quietly go on checking a stale copy of code that no
longer ships.

## Known defects

`Checks.known_bad()` records a property that *should* hold but does not, against an entry in
`docs/VERIFICATION.md`. It does not fail the run — but if it ever starts passing, the runner
says `NOW PASSING — promote this check`, so a fix cannot land unnoticed and leave a
permanently disabled test behind.

There is currently one: the HLC logical counter is destroyed by floating-point precision,
because `pt * 65536` lands thirteen times above `Number.MAX_SAFE_INTEGER`. See
`docs/VERIFICATION.md` for the analysis and the fix options.

## Isolation and safety

- Controllers bind to `127.0.0.1`, so an unauthenticated control API is never put on the
  network by a test run.
- mDNS publication is disabled, so a test never claims `mp32-control.local` from a real
  controller.
- The device is constructed but never started, so no test can reach or write to hardware.
- Each run uses its own multicast group and port, so two runs — or a run and the real app —
  never interfere.
