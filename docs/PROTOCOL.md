# MP32 interoperability protocol notes

Wire behaviour required by the independent MP32 Control client, recorded for interoperability,
testing and maintenance.

## Scope of this document

This describes **what this client sends and what it must receive back** in order to work. It
is a maintenance reference for the code in this repository, not a description of anyone
else's software.

Everything recorded here was confirmed the same way:

1. run this client against MP32 hardware the maintainer owns;
2. exercise one control at a time and record the resulting device state;
3. repeat the request and confirm the same result, so the entry is reproducible;
4. write the serializers, state handling and UI in this project independently.

## What is, and is not, originated here

The client, its serialization, its state handling and its interface are written for this
project. `REPORT_FORMAT` in `mp32_protocol.py` is not: it is a description of the device's
own report format — which commands exist and how their fields are laid out.

It is carried here because the client cannot start without it. The host refuses every
device request until a report format is registered (see below), and no working subset or
substitute is known. The format governs how the *host* packs reports for the device over
USB; a client on the network never sees those bytes, so it is not something a client can
derive by testing its own connection.

This is stated openly rather than left for a reader to work out. The repository contains no
vendor source code, no vendor application binaries, no vendor assets, no decompiled
material, and no captured network data.

Do not commit network logs, diagnostic dumps or device inventories. They routinely contain
serial numbers, hostnames, private IP addresses and other identifying data. Contribute
sanitized test vectors instead, carrying only the fields needed to reproduce a result.

## Topology

A unit is reached through the computer it is plugged into, not directly. That host runs a
device server exposing two kinds of TCP port:

- an **admin port**, one per host, reporting what that host has attached;
- a **device port** per attached unit, on the ports immediately above the admin port.

A unit is therefore addressed as *(host address, device port)*. Device ports **move when the
host's server restarts** and must be rediscovered rather than remembered.

## Finding a unit

Connecting to the admin port and reading a single frame returns a plaintext status listing the
server version, the units attached to that host, and the clients currently connected. It is a
passive read: nothing is registered and no device state is touched.

It is also the only dependable way to tell which host owns a unit. A discovery multicast group
exists — `DISCOVERY_GROUP` / `DISCOVERY_PORT` in `mp32_protocol.py` — but a host with nothing
attached announces itself the same way as one with a device, and on this network the machines
that actually own units produce no announcement that reaches other computers. Probing the admin
port across the local subnet is reliable where the announcement is not.

Control addresses are discovered dynamically. IP addresses and ports must never be treated as
permanent identifiers.

## Control framing

Control messages use a TCP frame consisting of:

1. a four-byte big-endian integer holding the total frame length, **including the length field
   itself**;
2. a UTF-8 JSON payload.

Request payloads use a JSON array:

```text
[command_name, [positional_arguments], {keyword_arguments}]
```

For this device's commands the third element is ignored and arguments must be positional. That
is a property of these commands, not of the transport — related Antelope units pass selectors
in the third element, so do not assume it is dead weight when reading other models.

Encoding helpers: `mp32_protocol.encode_command()` and `mp32_protocol.frame_payload()`.

## The report format must be registered first

**This is the single most important thing in this document.**

A host refuses **every** device request until some client has registered a report format,
answering:

```json
{"type": "single", "contents": "", "COMMAND_STATUS": "FAIL"}
```

`initialize_format([REPORT_FORMAT])` is the only command that registers it. Three properties
matter, and all three caused confusion here before they were understood:

1. registration is **host-wide**, not per-connection — one client's registration serves all;
2. it is **sticky until the host's server process restarts**;
3. **only the first registration is kept** — a later client's is silently ignored.

Consequences worth designing around:

- A client that never registers appears to work only after some other panel has been run on
  that host, and appears to break again after the host reboots. This is the most confusing
  failure mode in the system, and it looks exactly like a session or permissions problem.
- Registering a format that does not match the attached unit **breaks that host for every
  client, including the vendor's own panel**, until its server is restarted. Confirm the
  attached unit from the admin port before opening any device port, and never register a format
  on a host serving a different kind of unit.
- A client must send the **complete** format. A partial registration becomes the format in
  force for everyone else too.

Verified against host server 1.8.9 with no vendor panel running: `get_config` returned
`COMMAND_STATUS FAIL` before registration and a full 32-channel configuration immediately
after, on the same socket and on a fresh one.

## Multiple clients

The server serves many clients simultaneously and broadcasts each change to the others.
Verified with three concurrent clients, each receiving a complete configuration.

An apparent single-client limit is almost always the unregistered-format failure above. This
project drew that wrong conclusion once and built a single-session architecture around it
before the real cause was found.

## Commands used by this client

Name constants live in `mp32_protocol.py`:

- retrieve channel configuration and input types;
- set per-channel gain, phantom power and input type;
- recall and save presets;
- request power/standby changes.

Exact positional arguments are exercised by the physical-device tests. Unknown commands and
fields must not be guessed at or sent to hardware — a report format describes a whole product
family, so it names commands for hardware a given unit may not have, and asking about absent
hardware has put a related unit into a firmware fault. Reads are not automatically safe.

`preset_recall` is a write in every sense: it overwrote a working channel configuration during
testing on this project's own hardware.

## Asynchronous state

The client handles direct replies, cyclic telemetry, and notifications produced by changes made
from other connected clients. UI state is refreshed from observed device state, while names,
colours, groups, stereo links, shared cards and controller presence use MP32 Control's own peer
protocol.

## Calibration

Raw gain limits and display calibration are centralized in `mp32_protocol.py` and mirrored in
the browser UI. **Stored values are raw offsets, not engineering units** — a stored `0` displays
as 5 dB, and the zero point lives in the panel rather than the device. Values may vary with
firmware or input mode, and a revision that moves the offset keeps every number while changing
what it means. Any change must include a sanitized observation table and physical verification.

VU telemetry is dB below full scale: `0` is loudest and `60+` effectively silence. The UI
inverts that magnitude. Treat out-of-range values defensively.

## Legal and contribution note

This is technical documentation for an independently created interoperability client, not legal
advice or a statement about rights in every jurisdiction. Contributors are responsible for
ensuring they are permitted to test the equipment they use. Do not copy implementation code,
documentation, or assets from third-party software into this repository.
