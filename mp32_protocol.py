"""MP32 interoperability protocol helpers.

Wire-level constants and serialization for the independent client. Everything here
describes what this client sends and what the host server requires back in order to
interoperate, and each value was confirmed by running the client against hardware owned by
the project maintainer.

One exception is marked where it appears: `REPORT_FORMAT` is a description of the device's
own report format and is **not originated by this project**. It is carried here because the
client cannot start without it. See the interoperability section of README.md and
docs/PROTOCOL.md for why, and for what this repository does not contain.

Protocol details can vary by firmware. Contributors should confirm any change against
their own physical hardware and record the result so that others can reproduce it.
"""

from __future__ import annotations

import json
import struct
from typing import Any

DEFAULT_CONTROL_PORT = 2021
DISCOVERY_GROUP = "239.192.5.8"
DISCOVERY_PORT = 5008

# Every host running the device server also answers on an admin port. Connecting to it and
# reading one frame returns a plaintext status listing the server version, the units plugged
# into that machine, and the clients currently connected. It is the only reliable way found
# so far to tell which machine on the network actually owns a unit, because a host with no
# device attached announces itself on the discovery group exactly like one that has a device.
ADMIN_PORT = 2020
# Device servers are published on their own ports above the admin port; a machine with one
# unit attached serves it on the first of these.
DEVICE_PORT_CANDIDATES = (2021, 2022, 2023, 2024)


def parse_admin_status(text: str) -> dict:
    """Pull the useful fields out of an admin-port status reply.

    Returns {"server_version": str, "devices": [str], "clients": [str]}. `devices` is empty
    when the host reports "None", which is how a controller machine with no unit attached
    differs from the machine the unit is actually plugged into.
    """
    out = {"server_version": "", "devices": [], "clients": []}
    section = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("server ver"):
            parts = line.split()
            if len(parts) > 2:
                out["server_version"] = parts[2].rstrip(".,")
            continue
        if low.startswith("plugged devices"):
            section = "devices"
            continue
        if low.startswith("connected clients"):
            section = "clients"
            continue
        if low.startswith("status:"):
            section = None
            continue
        if section and low != "none":
            out[section].append(line)
    return out

INITIALIZE_FORMAT = "initialize_format"
GET_CONFIG = "get_config"
GET_TYPES = "get_types"
SET_GAIN = "set_pre_gain"
SET_PHANTOM = "set_pre_phantom"
SET_INPUT_TYPE = "set_pre_type"
SET_POWER = "set_power"
PRESET_RECALL = "preset_recall"
PRESET_SAVE = "preset_save"

GAIN_MAX_RAW = (56, 29, 36)  # Mic, Line, Hi-Z; verified against the current test unit

# The host server refuses every device request until a client has registered the report
# format for the session, answering `{"type":"single","contents":"","COMMAND_STATUS":"FAIL"}`
# to anything else. Registration is server-wide and sticky until the host server restarts,
# which is why a controller that never sends it appears to work only after some other panel
# has connected first, and appears to "lose the session" after the host machine reboots.
#
# Registration is idempotent from the client's perspective: the server ignores the request
# when a format is already registered. It is therefore safe to send on every connect, and a
# client must send the complete format rather than only the requests it uses — a partial
# registration would be the one in force for every other client too.
#
# Confirmed 2026-07-29 against firmware 1.4 / host server 1.8.9: get_config returns
# COMMAND_STATUS FAIL before registration and a full 32-channel config immediately after,
# on the same socket and on a fresh one.
#
# NOT ORIGINATED BY THIS PROJECT. This is a description of the device's own report
# format. It is carried here because the client cannot start without it and no working
# subset or substitute is known -- the format governs how the host packs reports for the
# device over USB, which a client on the network never sees. See the interoperability
# section of README.md and docs/PROTOCOL.md.
#
# Field entries are [name, ctype] with an optional bit width and an optional constant value.
REPORT_FORMAT = {
    "authorative": True,
    "cyclic_reports": {
        # Telemetry pushed by the device; peaks are magnitude below full scale.
        "0x73": [
            ["power_on", "ubyte", 1],
            ["usb_mode", "ubyte", 2],
            ["current_preset", "ubyte", 3],
            ["reserved", "ubyte", 1],
            ["device_updated", "ubyte", 1],
            ["peaks", "ubyte * 32"],
        ],
    },
    "requests": {
        "set_config_feature": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 2, "fields": [["feature", "ubyte"], ["status", "ubyte"]]},
        },
        "set_pre_gain": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 3, "fields": [
                ["id", "ubyte"],
                ["gain", "byte"],
                ["reserved1", "ubyte", 8, 0],
                ["reserved2", "ubyte", 1, 0],
                ["zerocross", "ubyte", 1],
                ["pretype", "ubyte", 2],
                ["reserved3", "ubyte", 4, 0],
            ]},
            "auto_send_notification": True,
        },
        "set_pre_type": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 4, "fields": [
                ["id", "ubyte"],
                ["reserved1", "int16", 16, 0],
                ["reserved2", "ubyte", 2, 0],
                ["pretype", "ubyte", 2],
                ["reserved3", "ubyte", 4, 0],
            ]},
            "auto_send_notification": True,
        },
        "set_pre_phantom": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 5, "fields": [["id", "ubyte"], ["phantom", "ubyte"]]},
            "auto_send_notification": True,
        },
        "preset_save": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 6, "fields": [["preset_idx", "ubyte"]]},
        },
        "preset_recall": {
            "header": {"report_id": "0x70", "ext2": 0, "ext3": 0},
            "params": {"payload_id": 7, "fields": [["preset_idx", "ubyte"]]},
        },
        "get_types": {
            "header": {"report_id": "0x74", "ext2": 3, "ext3": 0},
            "returns": {"fields": [["pretype", "ubyte"]], "count": 32},
        },
        "get_config": {
            "header": {"report_id": "0x74", "ext2": 4, "ext3": 0},
            "returns": {"fields": [
                ["gain", "ubyte", 6],
                ["phantom", "ubyte", 1],
                ["zerocross", "ubyte", 1],
            ], "count": 32},
        },
    },
}


def encode_command(command: str, *args: Any) -> bytes:
    """Encode the request shape this client uses: [command, positional arguments, options]."""
    return json.dumps([command, list(args), {}], separators=(",", ":")).encode("utf-8")


def frame_payload(payload: bytes) -> bytes:
    """Prefix a payload with the 4-byte big-endian total frame length the host expects."""
    return struct.pack("!i", len(payload) + 4) + payload


def max_raw_gain(input_type: int) -> int:
    return GAIN_MAX_RAW[int(input_type) % len(GAIN_MAX_RAW)]


def raw_gain_to_display(raw: int, input_type: int) -> int:
    """Current UI calibration, confirmed on the maintainer's own hardware where noted."""
    raw, input_type = int(raw), int(input_type)
    if input_type == 0:
        return 5 if raw == 0 else raw + 12
    if input_type == 1:
        return raw - 9
    if input_type == 2:
        return raw + 4
    return raw
