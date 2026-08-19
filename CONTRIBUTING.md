# Contributing

Contributions are welcome for independently written code, documentation, and tests.

## Repository hygiene

- Do not submit vendor binaries, vendor source code, proprietary assets, or confidential
  material. One exception already exists and is bounded: the report-format description the
  client cannot start without, explained in [README.md](README.md#interoperability-protocol)
  and [docs/PROTOCOL.md](docs/PROTOCOL.md). Nothing further of that kind belongs here, and
  that one is not a precedent for adding more.
- Do not submit network logs or diagnostic dumps containing device serials, hostnames,
  credentials, or private network addresses. Provide sanitized field descriptions or test
  fixtures instead.
- Describe protocol behaviour as reproducible input/output: what the client sent, what came
  back, and how to repeat it.
- Keep third-party code under its original compatible license and document it clearly.

## Testing

- Run `python3 tests/run_all.py` — 144 checks, about 35 seconds, no hardware required.
  See [tests/README.md](tests/README.md) for what it does and does not cover.
- Run `python3 -m py_compile mp32_gui.py mp32_protocol.py app.py`.
- Test UI changes in both desktop and phone-sized layouts.
- Mark device behavior as verified only after testing on physical hardware.
- Include firmware and OS versions in pull requests, but redact device serials and private IPs.

Changes affecting multiple physical MP32 units must satisfy every acceptance test in
[docs/MULTI_DEVICE_DESIGN.md](docs/MULTI_DEVICE_DESIGN.md) before being enabled by default.
