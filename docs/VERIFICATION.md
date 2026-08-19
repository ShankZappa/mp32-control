# MP32 Control — verification matrix and roadmap

What has been proven, what has not, and what is left to build. Renamed from `TOMORROW_TODO.md`,
which had stopped describing its own contents.

## Known defects

- [ ] **The HLC logical counter is lost to floating-point precision.** Found 2026-08-15 by
  `tests/test_hlc.py`. Not fixed — the fix changes the timestamp encoding, which every
  controller compares against, so it needs a decision and a cross-machine test rather than
  a quiet edit.

  `hlcNow()` packs the clock as `pt * 65536 + lc`. With `pt` a real millisecond clock
  (~1.79e12) the result is ~1.17e17 — **thirteen times above `Number.MAX_SAFE_INTEGER`**
  (9.007e15). At that magnitude the gap between representable doubles is 16, so `+1` does
  not change the value. The logical counter is therefore always zero: measured directly,
  three consecutive `hlcNow()` calls return the identical value.

  The physical half still works, so ordering across milliseconds is correct and normal
  editing on roughly-synced machines behaves. Two consequences remain:

  - **Two writes to the same key inside one millisecond collide.** The second carries an
    equal timestamp, and `PeerService.apply_meta_event` rejects on `cur["ts"] >= ts`, so it
    is silently discarded. Rare at human pace — card edits debounce at 800 ms — but real
    for anything programmatic or for a fast repeated action on one key.
  - **A peer whose clock runs ahead locks this controller out.** After adopting that peer's
    timestamp our next write is *equal* to it, not greater, so every controller rejects it.
    Edits then vanish silently until wall-clock time catches up to the skew — which is
    precisely the failure the HLC was introduced to prevent. An hour of skew is an hour of
    dropped edits.

  Fix options, none of them free, all of them protocol-visible:

  1. **Shrink the shift** (`pt * 1024 + lc`): 1.83e15, inside exact range, 1024 counter
     values per millisecond. Simple, but old and new timestamps are no longer comparable.
  2. **Offset the epoch** (`(now - 2025-01-01) * 65536 + lc`): ~3.3e15, inside range, keeps
     the full counter. Same comparability problem, and existing stored values — which are
     far larger — would win permanently until they age out.
  3. **Send the pair** (`{pt, lc}` or a decimal string): exact and future-proof, but it
     changes the shape of every metadata event on the wire.

  **Option 3 is the only migration-safe one, and that decides it.** Options 1 and 2 both
  make new timestamps *smaller* than existing ones — a controller that has not been updated
  keeps emitting ~1.17e17 values, which beat every new one forever. That trades one silent
  data loss for another. Option 3 keeps the physical millisecond in the same space, so a
  legacy numeric timestamp decodes cleanly as `(pt = ts / 65536, lc = 0)` and compares
  correctly against a new pair. Mixed-version networks stay ordered.

  Verify across at least two machines with a deliberately skewed clock before shipping, and
  keep one un-updated controller in that test — the mixed-version case is the whole risk.

- [x] **Groups and stereo links did not reach the other controller.** Fixed 2026-07-29.

  Cause: every group and stereo link lived in **one shared metadata key**, `groups`, holding
  the whole object — while names and colours use one key per channel. Last-write-wins on a
  whole-object key does not merge, it **replaces**: whichever controller wrote last silently
  discarded every group the other had. Two people grouping channels on two machines was
  precisely the case that could not work. Same defect shape as the old shared-notes textarea.

  Fix: each group is now its own key, `group:<gid>`, so two controllers grouping different
  channels never collide and a controller with no groups can never wipe one that has them.
  Deletion writes a tombstone rather than removing the key, because absence must not mean
  deleted. A whole-object `groups` event from an un-updated controller is still accepted, but
  **merged** rather than applied, so a mixed-version network cannot lose data. Existing groups
  migrate under their own ids, which are already unique and stable, so two browsers migrating
  the same data converge instead of duplicating.

  Verified: 19 checks across three live controllers — both controllers' groups survive
  simultaneous creation, editing one leaves the other untouched, a newly joined controller
  wipes nothing, tombstones propagate and a stale edit cannot resurrect a deleted group, and
  an empty legacy `groups` event wipes nothing. Plus 12 checks in the browser confirming the
  UI pushes per-group keys, never the whole object, and that `groupOf` skips tombstones.

  Note for future tests: controllers under test share the LAN multicast group with any running
  app, whose browser re-announces its own stored metadata. Assert on your own run's keys, not
  on the complete set. Timestamps must be HLC-shaped (`ms << 16 | counter`) or they lose every
  comparison against a real event.

## The automated tests

Rewritten 2026-08-15 and living in the repository at [`../tests/`](../tests/), after the
original five suites (69 checks, 2026-07-29/30) were lost with a cleared `/private/tmp/`
scratchpad.

```bash
python3 tests/run_all.py
```

Six suites, 144 checks, about 35 seconds, no hardware required. Every controller is a real
process running the shipped application over the real HTTP API and real multicast — the
property that made the originals worth having, since both defects this project shipped were
in the interaction between controllers rather than inside any one function. The browser half
(groups, cards, clock) runs under node against functions extracted verbatim from
`mp32_gui.py`, so a test can never drift from what ships.

| Suite | Checks |
|---|---:|
| chronological web-host election | 17 |
| startup ordering and unvalidated-session safety | 17 |
| per-group metadata sync | 30 |
| shared notes cards and leases | 34 |
| web-host liveness and the phone's recovery contract | 35 |
| hybrid logical clock | 11 |

The two hard-won details that cost the most time are now handled by the harness rather than
left to discipline — HLC-shaped timestamps via `harness.hlc()`, and per-run multicast
isolation plus namespaced keys via `Cluster`. See [`../tests/README.md`](../tests/README.md)
for the coverage boundary, which is stated explicitly: nothing device-facing, cross-machine,
or browser-rendered is simulated here. Those remain on the physical checklists below.

## Next up

- [ ] **Show only the channels the connected unit actually has.** The channel strip is
  currently fixed at 32. Related preamp hardware speaks the same way but exposes fewer
  preamps, so on such a unit every channel beyond its real count should be hidden rather
  than drawn as a dead control.

  Two constraints, both of which already exist in this project's notes and neither of
  which is optional:

  - **Derive the count from the configuration the device returns, never from probing.**
    `get_config` already answers with one entry per real channel; that length is the
    answer. Asking a unit about hardware it does not have is the failure mode
    `docs/PROTOCOL.md` warns about — it has put a related unit into a firmware fault.
  - **Do not register this unit's report format on a host serving a different unit.**
    Registration is host-wide, sticky, and first-one-wins, so a wrong format breaks that
    host for every client until its server restarts. Confirm the attached unit from the
    passive admin read first, exactly as `find_device_hosts()` already does.

  Guard the validation gate too. `MP32Device._initial_load()` accepts a configuration only
  when `len(cfg["contents"]) >= NUM_CHANNELS`, and `NUM_CHANNELS` is a fixed 32 — correct
  for the MP32 and wrong for anything smaller, which would fail validation, close the
  socket, and never come Online. The gate has to become "non-empty, and however many this
  unit reports", with the resulting count carried into `self.config`, `/api/status`, and
  the number of strips the UI draws.

  Build this only on top of a release that is already verified on hardware, and verify it
  against both an MP32 and the smaller unit before enabling it by default.

- [ ] iOS PWA test on the real iPad/iPhone: install from `mp32-control.local`, confirm the
  panel loads, then quit the web host and confirm the PWA recovers through a remembered peer
  address rather than waiting on Safari's DNS cache. Then reopen the former host and confirm
  the PWA returns to the stable hostname.
- [ ] Cards on a phone alongside a desktop: create cards from both, confirm each sees the
  other's, confirm the lease marks a card read-only on the second device, and confirm a card
  left mid-edit unlocks by itself after the lease expires.

- [x] Discovery fallback so a fresh install finds the unit without a remembered address:
  beacon, then peer controllers, then an admin-port scan. Device type is confirmed from the
  passive admin read before any device port is opened, so other Antelope hardware on the
  network is never written to while looking for ours.
- [x] `device_preflight.py` passes: 32 channels and cyclic telemetry in 2.56 s, no vendor
  panel running.
- [x] macOS Apple Silicon build produced and smoke-tested: the packaged app reaches `online`
  with 32 channels, becomes web host, and publishes `mp32-control.local`.

## Physical-device verification

- [ ] Verify standby/power behavior and recovery without risking active sessions.
- [ ] Verify preset recall and save independently for all three preset slots.
- [ ] Verify Mic, Line, and Hi-Z raw limits/display calibration against current firmware.
- [ ] Confirm which physical channels support Hi-Z on every supported hardware revision.
- [ ] Run an extended stability test with MP32 Control as the only connected client.
- [ ] Verify changes made by another legitimate controller are mirrored correctly.
- [ ] Reconfirm grouped and stereo-linked gain movement on hardware under rapid input.

## Cross-platform release verification

- [ ] Run M2 Mac, M3 Mac, Windows, and iPad together: verify exactly one desktop is `Web Host`,
  all controllers show the same real device values, and no reopened app displays defaults.
- [ ] Verify the chronological election on hardware: start three controllers in a known order,
  confirm the first-started holds the role; quit it and confirm the second inherits; reopen
  the first and confirm it does not take the role back.
- [ ] Verify `DEVICE_SUPPORTS_MULTIPLE_CLIENTS = True` across machines: every controller must
  reach `online` with its own session, a change made on one must appear on the others, and no
  controller may go device-blind when another quits.
- [ ] Quit the web host and verify the next peer takes over `mp32-control.local`, opens a
  validated live device session, and restores all feeds after the handover indicator.
- [ ] Reopen the former host: the current web host must remain stable and the reopened
  desktop must read the current hardware config rather than cached/default values.
- [ ] Build and smoke-test an Intel (`x86_64`) macOS application.
- [ ] Sign a distributable build with a Developer ID (`MP32_CODESIGN_IDENTITY`). The current
  1.3.1 bundle is ad-hoc signed, so macOS shows a first-launch warning on other machines.
- [ ] Build and smoke-test the Windows executable on a clean Windows machine.
- [ ] Test Windows firewall onboarding for TCP 8765 and mDNS UDP 5353.
- [ ] Verify stable-host failover Mac→Windows and Windows→Mac from an installed iPad PWA.
- [ ] Verify the handover indicator and automatic feed recovery after host loss.
- [ ] Test behavior on guest Wi-Fi/client-isolated networks and document the expected failure.
- [ ] Verify web-host liveness takeover on hardware: block TCP 8765 on the current host with
  its firewall while leaving the app running, and confirm the second controller takes over
  after the grace period, opens a validated device session, and names the blocked host in
  the header. Then unblock and confirm no flapping.
- [ ] Confirm why one Mac's TCP 8765 is closed while its app runs and heartbeats (the macOS
  firewall on an ad-hoc-signed build is the leading suspect) and document the onboarding
  step needed on every Mac.

## Completed behavior

- [x] Automatic device discovery and changing address/port handling.
- [x] Gain, 48 V, input-type controls, VU display, and preset UI.
- [x] Mac/Windows/iPhone controller presence and shared metadata.
- [x] Event plus periodic-snapshot recovery for names, colours, groups, and Public Notes.
- [x] Input-type command ordering and status-poll race protection.
- [x] Web-host election, and the single-session fallback it once needed. Superseded:
      every controller now holds its own device session (`DEVICE_SUPPORTS_MULTIPLE_CLIENTS`).
- [x] Two-channel-only Stereo Link selection.
- [x] Group/link colour propagation and reset on removal.
- [x] Local/Public Notes separation.
- [x] Eight-step Undo/Redo and gain double-click zero/restore.
- [x] Stable `mp32-control.local` host election and automatic web-feed recovery.
- [x] Native project icon, About metadata, Mac/Windows build scripts, and signing hooks.

## Multi-device roadmap

- [ ] Implement multiple physical MP32 containers only with access to at least two units.
  Follow `docs/MULTI_DEVICE_DESIGN.md` and keep demo-only work behind a disabled feature
  flag. A GitHub pull request must include the full physical acceptance matrix.

## Repository policy

- Do not commit vendor code/binaries, raw network logs, serial numbers, private IPs,
  certificates, or credentials.
- Protocol changes require sanitized reproducible results and physical-device tests.
- MIT licensing applies only to original project material; retain `NOTICE` and third-party
  license information.
