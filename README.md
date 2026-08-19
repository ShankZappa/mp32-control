# MP32 Control

MP32 Control is an independent, cross-platform control panel for compatible Antelope
Audio MP32 microphone preamplifiers. It runs as a native macOS or Windows application
and serves the same interface to phones and tablets on the local network.

> This project is not affiliated with, sponsored by, or endorsed by Antelope Audio.

## Why this exists

The MP32 is capable hardware with a long service life. This is an independent client for
it, written for a working studio that needed the preamps reachable from more than one
machine at once, and from a phone or tablet standing at the mic rather than at the desk.

It replaces nothing on the device and requires no modification to it. It speaks to the same
host server over the local network that any other control client does, and more than one
client can be connected at the same time.

## Features

- 32-channel gain, 48 V phantom power, and Mic/Line/Hi-Z control
- live VU metering and preset controls
- channel names, colours, groups, stereo links, Local Notes, and shared Public Notes
- bounded Undo/Redo history and preset-file import/export
- Mac, Windows, iPhone, and iPad controller synchronization
- every controller holds its own device session; the elected web host only serves the mobile URL
- stable `http://mp32-control.local:8765` mobile URL with automatic host failover
- automatic local-network device discovery; no hardcoded device serial number

## Quick start from source

Python 3.9 or newer is required.

```bash
python3 -m pip install -r requirements.txt
python3 mp32_gui.py
```

The native desktop window can be started with:

```bash
python3 app.py
```

See [docs/BUILD.md](docs/BUILD.md) for standalone application builds.

## Tests

```bash
python3 tests/run_all.py
```

162 checks in about 50 seconds. No hardware and no network access required — the suites
start real controller processes and exercise the peer metadata, web-host election and HTTP
API over the real transports. [tests/README.md](tests/README.md) states exactly what is and
is not covered.

## Network and safety

> **The HTTP control API has no authentication.** Anyone who can reach port 8765 can change
> gain, phantom power, input type, and presets on your hardware. This is a deliberate design
> for a trusted private studio LAN. Never port-forward 8765, never expose it to the public
> internet, and do not run the controller on untrusted or public Wi-Fi. See
> [SECURITY.md](SECURITY.md).

Windows users may need to allow Private Network access for HTTP and mDNS.

48 V phantom power can damage equipment that is not designed for it. Verify connected
hardware before enabling phantom power or bulk controls.

## Interoperability protocol

The client, its serialization, state handling and interface are written for this project.
What it sends and what it needs back is documented in
[docs/PROTOCOL.md](docs/PROTOCOL.md), and every entry was confirmed by running this client
against hardware owned by the project maintainer.

One item needs saying plainly. `REPORT_FORMAT` in [mp32_protocol.py](mp32_protocol.py)
describes the device's report format — which commands exist and how their fields are laid
out. It is an interface description, and it is **not originated by this project**.

It is here because the client cannot work without it. The host server refuses *every*
device request, answering `COMMAND_STATUS: FAIL` to all of them, until some client has
registered a report format. No working subset or substitute is known: the format tells the
host how to pack reports for the device over USB, which is not observable from a client's
position on the network.

What this repository does **not** contain: vendor source code, vendor binaries, vendor
application assets, decompiled material, or captured network data.

## Untested: more than one unit on a network

Everything here has been verified against **one MP32**. It has never been run with two MP32s
on the same network, nor with any other preamp, and the interface draws a fixed 32 channels
regardless of what is actually attached.

If you have that hardware and try it, findings are genuinely welcome — open an issue. Useful
things to report: what the admin port reported was attached, how many channels came back,
whether each unit stayed independent, and anything that behaved differently from this
description. Please leave out serial numbers and private addresses.

One safety note if you do experiment. Report-format registration is **host-wide, sticky and
first-one-wins**, so registering a format that does not match the attached unit breaks that
host for every client on it — including the manufacturer's own panel — until its server
restarts. This client confirms the attached unit from a passive admin-port read before
opening any device port, and anything built on top of it must keep doing that.

Controlling several units from one panel is intended, and
[docs/MULTI_DEVICE_DESIGN.md](docs/MULTI_DEVICE_DESIGN.md) is the design for it. It will be
built when there is hardware to test it against, not before — a blind refactor is exactly how
commands start crossing between devices.

## Project status

Architecture and design decisions are in [docs/HANDOFF.md](docs/HANDOFF.md); what has been
proven on hardware and what has not is tracked in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

Known limitations at this release:

- the Windows executable has not yet been built or tested on a Windows machine;
- macOS builds are ad-hoc signed, so other Macs show a first-launch warning;
- the HTTP API has no authentication — see the warning above.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Hardware-affecting changes require documented
physical-device tests. The proposed multi-device architecture is described in
[docs/MULTI_DEVICE_DESIGN.md](docs/MULTI_DEVICE_DESIGN.md).

## License

Original project code and project-owned artwork are available under the [MIT License](LICENSE).
Product and company names remain the property of their respective owners; see [NOTICE](NOTICE).
Bundled dependencies retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
