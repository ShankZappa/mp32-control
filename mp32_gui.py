#!/usr/bin/env python3
"""
MP32 Control — independent network control panel
Connects directly to the MP32 device over TCP and provides a web-based GUI.
Works with Python 3.9+; zeroconf is optional from source and bundled in desktop builds.
"""
from __future__ import annotations

import socket
import json
import struct
import threading
import time
import math
import uuid
import os
import sys
import webbrowser
import mp32_protocol as protocol
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler
from typing import Optional, List, Dict, Any

try:
    from zeroconf import Zeroconf, ServiceInfo, IPVersion
    ZEROCONF_AVAILABLE = True
except ImportError:
    Zeroconf = ServiceInfo = IPVersion = None
    ZEROCONF_AVAILABLE = False

# ── Device Configuration ──────────────────────────────────────────────────────
# ┌──────────────────────────────────────────────────────────────────────────┐
# │  TEST WITHOUT A DEVICE: set DEMO_MODE = True                              │
# │  Pretends to be "online" so all controls are unlocked — you can turn      │
# │  knobs, toggle 48V, name/colour/group channels and watch two computers    │
# │  sync over the network, all WITHOUT a real MP32. Set back to False to     │
# │  control the actual device.                                               │
# └──────────────────────────────────────────────────────────────────────────┘
DEMO_MODE    = False
# Every controller owns a direct device session. The device is the single authority for gain,
# phantom, input type, preset, power, and VU: each controller reads it from the device and
# writes changes straight back, and the host server broadcasts each change to the others. No
# device value is ever replicated through peer metadata, which is what made an earlier
# metadata-carried gain scheme unstable — it created a second, lagging copy of data that
# already had an authoritative home.
#
# Verified 2026-07-29 with three simultaneous clients, each receiving a complete 32-channel
# config. The earlier "additional clients get an empty config" reading was the unregistered
# report format failing every request, not a session limit.
DEVICE_SUPPORTS_MULTIPLE_CLIENTS = True

DEVICE_IP    = "192.168.1.100"   # generic fallback; beacon auto-discovery overrides it
DEVICE_PORT  = protocol.DEFAULT_CONTROL_PORT  # fallback; discovery normally supplies the live port


def _state_dir() -> str:
    """Persistent per-user dir for small state files (last known device target)."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/MP32 Control")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MP32 Control")
    else:
        base = os.path.expanduser("~/.mp32-control")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base


def load_last_device() -> Optional[tuple]:
    """Return the last verified (ip, port) so we can connect even when no discovery
    beacon reaches us (IGMP-snooping switches may not forward the multicast until
    something else on this host joins the group)."""
    try:
        with open(os.path.join(_state_dir(), "last_device.json"), encoding="utf-8") as f:
            d = json.load(f)
        ip, port = d.get("ip"), int(d.get("port", 0))
        if ip and port:
            return ip, port
    except Exception:
        pass
    return None


def save_last_device(ip: str, port: int):
    try:
        with open(os.path.join(_state_dir(), "last_device.json"), "w", encoding="utf-8") as f:
            json.dump({"ip": ip, "port": int(port)}, f)
    except Exception:
        pass
def _meta_path() -> str:
    return os.path.join(_state_dir(), "metadata.json")


def load_meta_fields() -> Dict[str, Any]:
    """Restore replicated metadata from disk.

    Without this the controller holds metadata only in memory, so a restart empties it and
    the first browser to reconnect re-seeds whatever it still remembers — including values
    deleted while it was closed. Persisting means the network's current state outlives the
    process and an older stored copy simply loses the timestamp comparison.
    """
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        fields = data.get("fields") if isinstance(data, dict) else None
        if not isinstance(fields, dict):
            return {}
        out = {}
        for key, item in fields.items():
            if isinstance(item, dict) and "ts" in item:
                out[str(key)] = {"value": item.get("value"), "ts": item["ts"]}
        return out
    except (OSError, ValueError):
        return {}


def save_meta_fields(fields: Dict[str, Any]):
    """Write atomically: a controller killed mid-write must not leave a truncated file that
    reads as 'no metadata' and hands the network back to a stale browser."""
    tmp = _meta_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fields": fields}, f, separators=(",", ":"))
        os.replace(tmp, _meta_path())
    except (OSError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


DEVICE_MATCH = "MP32"            # auto-connect to the discovered device whose name contains this ("" = off)
SERVER_PORT  = 8765
SERVER_BIND  = "0.0.0.0"   # LAN test: allow phones/tablets on the same network
STABLE_HOST  = "mp32-control.local"
PEER_HEARTBEAT_INTERVAL = 1.0
META_PERSIST_INTERVAL = 2.0   # seconds between metadata flushes to disk
PEER_TIMEOUT = 3.5
HOST_ELECTION_GRACE = 2.5
HOST_MDNS_TTL = 5
# A multicast heartbeat only proves a controller's process is alive; it does not prove its
# HTTP server is reachable by other controllers. A host that heartbeats but cannot serve
# (blocked by a local firewall, bound to another interface, crashed server thread) would
# otherwise hold the role forever and strand every other controller in waiting_for_web_host.
HOST_PROBE_INTERVAL = 1.0   # seconds between liveness probes of each peer's HTTP port
HOST_PROBE_TIMEOUT  = 0.6   # per-probe TCP connect timeout
HOST_PROBE_GRACE    = 6.0   # continuous unreachable time before a peer loses candidacy
LAN_IP_CACHE_TTL    = 5.0   # re-read the local LAN address at most this often
DEVICE_SCAN_INTERVAL = 30.0 # minimum gap between full network searches for the unit
GAIN_MIN     = 0
GAIN_MAX     = 65
NUM_CHANNELS = 32
_BASE_DIR     = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))  # PyInstaller-aware
ASSET_DIR     = os.path.join(_BASE_DIR, 'assets')
ASSET_FILES   = {
    '/assets/save.png': 'save.png',
    '/assets/load.png': 'load.png',
    '/assets/group.png': 'group.png',
    '/assets/stereo-link.png': 'stereo-link.png',
    '/assets/bmc-mark.png': 'bmc-mark.png',
    '/app-icon.png': 'mp32-control.png',
}

# ── Device Communication Layer ────────────────────────────────────────────────
class MP32Device:
    def __init__(self, ip: str, port: int):
        self.ip   = ip
        self.port = port
        if ip == DEVICE_IP:                    # generic fallback → prefer last verified target
            saved = load_last_device()
            if saved:
                self.ip, self.port = saved
                print(f"[MP32] Starting from last known device {self.ip}:{self.port}")
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.demo = DEMO_MODE
        self.beacon = None              # BeaconListener, for auto-discovery
        self.auto_match = DEVICE_MATCH  # auto-connect to a discovered device by name
        self.config: List[Dict[str, Any]] = [
            {"gain": 0, "phantom": 0, "zerocross": 1, "pretype": 0} for _ in range(NUM_CHANNELS)
        ]
        self.peaks: List[int]  = [99] * NUM_CHANNELS
        self.power_on          = False
        self.current_preset    = 1
        self.device_info: Dict[str, Any] = {}
        self._lock             = threading.Lock()
        self._running          = False
        self._thread: Optional[threading.Thread] = None
        self.session_enabled   = False
        self._load_error       = ""
        self.peers             = None   # set by PeerService so discovery can learn from peers
        self._last_scan        = 0.0
        self._initializing     = False
        self.config_valid      = False
        self.connection_state  = "waiting_for_web_host"
        self.last_error        = ""

    # ── Low-level framing (verified: length field INCLUDES its own 4 bytes) ──
    def _send_raw(self, data: bytes):
        self.sock.sendall(protocol.frame_payload(data))

    def _recv_msg(self, timeout: float = 3.0) -> Optional[Dict]:
        self.sock.settimeout(timeout)
        hdr = b""
        while len(hdr) < 4:
            chunk = self.sock.recv(4 - len(hdr))
            if not chunk:
                return None
            hdr += chunk
        length = struct.unpack('!i', hdr)[0] - 4   # body = length - 4
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        try:
            return json.loads(data.decode('utf-8', errors='ignore'))
        except Exception:
            return None

    # ── Thread-safe command send ───────────────────────────────────────────
    # Wire format is a JSON ARRAY [command_name, args, kwargs] with POSITIONAL
    # args. Only get_* commands return a "single" reply; set_* are fire-and-forget.
    def send_command(self, command_name: str, *args, expect_reply: bool = False) -> Optional[Dict]:
        if self.demo:
            return None   # demo mode: no real I/O, local state is updated by the callers
        if not self.connected and not self._initializing:
            return None
        with self._lock:
            payload = protocol.encode_command(command_name, *args)
            try:
                self._send_raw(payload)
                if not expect_reply:
                    return None
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    msg = self._recv_msg(timeout=max(0.1, deadline - time.time()))
                    if msg is None:
                        break
                    mtype = msg.get("type")
                    if mtype in ("single", "rejection"):
                        return msg
                    self._apply_message(msg)
            except Exception as e:
                print(f"[CMD ERROR] {command_name}: {e}")
                self.connected = False
                self.config_valid = False
            return None

    # ── Apply an incoming async message (cyclic telemetry or notification) ──
    def _apply_message(self, msg: Dict):
        mtype = msg.get("type")
        if mtype == "cyclic":
            contents = msg.get("contents", {})
            if isinstance(contents, dict):
                self.power_on       = bool(contents.get("power_on", self.power_on))
                self.current_preset = contents.get("current_preset", self.current_preset)
                peaks = contents.get("peaks", [])
                if peaks:
                    self.peaks = list(peaks)[:NUM_CHANNELS]
        elif mtype == "notification":
            self._apply_notification(msg)

    def _apply_notification(self, msg: Dict):
        # Another client changed something; server broadcasts [command, args, kwargs].
        # We mirror it into local state so the GUI reflects external changes.
        contents = msg.get("contents")
        if not isinstance(contents, list) or len(contents) < 2:
            return
        cmd, args = contents[0], contents[1]
        try:
            if cmd == protocol.SET_GAIN and len(args) >= 2:
                i = int(args[0])
                self.config[i]["gain"] = int(args[1])
                if len(args) >= 3: self.config[i]["zerocross"] = int(args[2])
                if len(args) >= 4: self.config[i]["pretype"]   = int(args[3])
            elif cmd == protocol.SET_PHANTOM and len(args) >= 2:
                self.config[int(args[0])]["phantom"] = int(args[1])
            elif cmd == protocol.SET_INPUT_TYPE and len(args) >= 2:
                self.config[int(args[0])]["pretype"] = int(args[1])
            elif cmd == protocol.SET_POWER and args:
                self.power_on = bool(args[0])
            elif cmd == protocol.PRESET_RECALL and args:
                self.current_preset = int(args[0])
        except (IndexError, ValueError, KeyError, TypeError):
            pass

    # ── Background read loop ───────────────────────────────────────────────
    def _read_loop(self):
        while self._running:
            if not self.demo and not self.session_enabled:
                self.connected = False
                time.sleep(0.25)
                continue
            if not self.connected:
                if not self.session_enabled:
                    time.sleep(0.25)
                    continue
                # Connect attempt first, back-off second: this loop owns the initial connect
                # as well as reconnects, so the UI must not wait out a sleep before the very
                # first attempt.
                self._auto_target()
                self._try_connect()
                if not self.connected:
                    time.sleep(2.0)
                continue
            try:
                self.sock.settimeout(1.5)
                hdr = b""
                while len(hdr) < 4:
                    chunk = self.sock.recv(4 - len(hdr))
                    if not chunk:
                        raise ConnectionError("Socket closed")
                    hdr += chunk
                length = struct.unpack('!i', hdr)[0] - 4   # body = length - 4
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(length - len(data))
                    if not chunk:
                        raise ConnectionError("Socket closed mid-message")
                    data += chunk
                try:
                    msg = json.loads(data.decode('utf-8', errors='ignore'))
                    self._apply_message(msg)
                except Exception:
                    pass
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[READ LOOP] Disconnected: {e}")
                self.connected = False
                self.config_valid = False
                self.connection_state = "disconnected"
                self.last_error = str(e)
                try:
                    self.sock.close()
                except Exception:
                    pass

    # ── Connect ────────────────────────────────────────────────────────────
    def _retarget(self, ip: str, port: int, why: str):
        if (ip, int(port)) != (self.ip, self.port):
            self.ip, self.port = ip, int(port)
            print(f"[MP32] {why}: targeting {ip}:{port}")
            save_last_device(ip, int(port))

    def _auto_target(self):
        """Find the unit, in increasing order of cost.

        The discovery beacon alone is not enough: the only announcing host on the studio
        network is a controller machine with nothing plugged in, while the machine that owns
        the unit announces nothing that reaches us. Each step below is skipped once an earlier
        one has produced a target, and the scan only runs when everything cheaper has failed.
        """
        # 1 — the beacon, when it does carry a matching device record.
        if self.beacon and self.auto_match:
            for d in self.beacon.list():
                name = (d.get('device_name') or d.get('name') or '')
                if self.auto_match.lower() in name.lower() and d.get('ip') and d.get('port'):
                    self._retarget(d['ip'], int(d['port']), "Beacon")
                    return

        # 2 — another controller already on the network is broadcasting the address it uses.
        # A new machine joining a working studio learns the target without probing anything.
        peer_ips = []
        for peer in (self.peers.active_peers() if self.peers else []):
            ip = peer.get("device")
            if ip and ip not in peer_ips:
                peer_ips.append(ip)
        for ip in peer_ips:
            if ip == self.ip and self.port in protocol.DEVICE_PORT_CANDIDATES:
                return                      # already pointed where the others are
            # Confirm passively that this host really serves our kind of unit before opening
            # a device port on it — see scan_for_device on why that write must be earned.
            info = query_admin(ip)
            names = (info or {}).get("devices") or []
            if not any(self.auto_match.lower() in n.lower() for n in names):
                continue
            port = find_device_on_host(ip)
            if port:
                self._retarget(ip, port, "Peer controller")
                return

        # 3 — ask the hosts we know of directly, then the rest of the subnet. This is what
        # makes a freshly installed copy with nothing remembered able to find the hardware.
        if time.time() - self._last_scan < DEVICE_SCAN_INTERVAL:
            return
        self._last_scan = time.time()
        known = [d['ip'] for d in (self.beacon.list() if self.beacon else []) if d.get('ip')]
        known += [ip for ip in peer_ips if ip not in known]
        if self.ip and self.ip not in known:
            known.append(self.ip)
        local = local_lan_ip()
        candidates = known + [ip for ip in subnet_candidates(local) if ip not in known]
        print(f"[MP32] Searching the network for a host with a unit attached "
              f"({len(candidates)} addresses)…")
        hit = scan_for_device(candidates)
        if hit:
            print(f"[MP32] Found {', '.join(hit['devices'])} on {hit['ip']} "
                  f"(server {hit['server_version']})")
            self._retarget(hit['ip'], hit['port'], "Network scan")
        else:
            print("[MP32] No host on this network reports an attached unit")

    def _try_connect(self):
        if not self.demo and not self.session_enabled:
            return
        last_error = ""
        # Session eligibility is decided when MP32 accepts the socket. If get_config is
        # empty, repeating it on that same socket cannot promote the session; reconnect.
        for attempt in range(10):
            if not self.session_enabled:
                return
            s = None
            try:
                self.connection_state = "connecting"
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3.0)
                s.connect((self.ip, self.port))
                if not self.session_enabled:
                    s.close()
                    return
                self.sock = s
                self._initializing = True
                self.connection_state = "loading_config"
                self._register_report_format()
                loaded = self._initial_load()
                self._initializing = False
                if not loaded:
                    last_error = self._load_error or "device returned an empty config"
                    self.connection_state = "config_empty"
                    raise ConnectionError(last_error)
                self.config_valid = True
                self.connected = True
                self.connection_state = "online"
                self.last_error = ""
                save_last_device(self.ip, self.port)   # verified live target
                print(f"[MP32] Connected to {self.ip}:{self.port}")
                return
            except Exception as e:
                self._initializing = False
                self.connected = False
                self.config_valid = False
                last_error = str(e)
                if self.connection_state != "config_empty":
                    self.connection_state = "tcp_error"
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
                self.sock = None
                if attempt < 9 and self.session_enabled:
                    time.sleep(0.6)
        self.last_error = last_error
        print(f"[MP32] Connection failed: {last_error} — retrying with fresh sessions")

    def _register_report_format(self):
        """Register the report format before the first request on this connection.

        Without it the host server answers every device request with COMMAND_STATUS FAIL,
        which is what made this controller appear to need the vendor panel started first.
        The server sends no reply to this command and ignores it when a format is already
        registered, so it is fire-and-forget and safe to repeat on every connect.
        """
        try:
            self.send_command(protocol.INITIALIZE_FORMAT, protocol.REPORT_FORMAT,
                              expect_reply=False)
        except Exception as e:
            # Not fatal on its own: another client may already have registered a format,
            # in which case the following get_config still succeeds.
            print(f"[INIT] Could not send the report format: {e}")

    def _initial_load(self) -> bool:
        # Runs before the background read thread starts, so direct reads are safe.
        self._load_error = ""
        try:
            cfg = self.send_command(protocol.GET_CONFIG, expect_reply=True)
            if cfg and cfg.get("type") == "rejection":
                self._load_error = "host server rejected this remote connection"
                print("[INIT] Device rejected remote connection.")
                return False
            if cfg and cfg.get("COMMAND_STATUS") == "FAIL":
                # Distinct from "no config": the server processed the request and refused it.
                # Seen when the report format is not registered, or when the host cannot
                # reach the unit over USB.
                self._load_error = ("host server refused get_config (COMMAND_STATUS FAIL) — "
                                    "report format rejected, or the unit is not reachable "
                                    "from its host machine")
                print(f"[INIT] {self._load_error}")
                return False
            if cfg and isinstance(cfg.get("contents"), list) and len(cfg["contents"]) >= NUM_CHANNELS:
                for i, ch in enumerate(cfg["contents"][:NUM_CHANNELS]):
                    self.config[i].update(ch)
            else:
                self._load_error = "device returned an empty config"
                print("[INIT] get_config returned no channels")
                return False
            # Input types (pretype) come from a separate command
            types = self.send_command(protocol.GET_TYPES, expect_reply=True)
            if types and isinstance(types.get("contents"), list):
                for i, t in enumerate(types["contents"][:NUM_CHANNELS]):
                    if isinstance(t, dict) and "pretype" in t:
                        self.config[i]["pretype"] = t["pretype"]
            return True
        except Exception as e:
            print(f"[INIT] Error loading config: {e}")
            return False

    def start(self, session_enabled: bool = False):
        self._running = True
        if self.demo:
            self.connected = True
            self.power_on = True
            print("[MP32] DEMO MODE — no device; controls unlocked, metadata/peer sync still live")
            self._thread = threading.Thread(target=self._demo_loop, daemon=True)
            self._thread.start()
            return
        self.session_enabled = bool(session_enabled)
        if self.session_enabled:
            self.connection_state = "discovering"
        # The first connect runs inside the read loop rather than here. Connecting inline can
        # take ~36s against an unreachable device (10 attempts × 3s timeout + back-off), and
        # any caller that starts the device before its HTTP server would serve nothing for
        # that whole time — which is what showed the packaged app as a blank window.
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def set_session_enabled(self, enabled: bool):
        """Enable this desktop's live MP32 session; never transfers device state."""
        if self.demo:
            return
        enabled = bool(enabled)
        if enabled == self.session_enabled:
            return
        self.session_enabled = enabled
        if enabled:
            print("[MP32] Web host opening the single live device session")
            self.connection_state = "discovering"
            self.last_error = ""
            return   # the read loop performs discovery and connection
        print("[MP32] Web host changed; releasing this live device session")
        self.connected = False
        self.config_valid = False
        self.connection_state = "waiting_for_web_host"
        self.last_error = ""
        self.peaks = [99] * NUM_CHANNELS
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def _demo_loop(self):
        # Animate peak meters so the VU bars move (no real device).
        t = 0
        while self._running:
            t += 1
            self.peaks = [10 + int(50 * abs(math.sin(t * 0.25 + i * 0.7)))
                          for i in range(NUM_CHANNELS)]
            time.sleep(0.08)

    def stop(self):
        self._running = False
        self.session_enabled = False
        self.connected = False
        self.config_valid = False
        self.connection_state = "stopped"
        try:
            self.sock.close()
        except Exception:
            pass

    # ── Control API ────────────────────────────────────────────────────────
    # All set_* commands use POSITIONAL args and are fire-and-forget; we update
    # local state optimistically (device echoes via cyclic/notification).
    def set_gain(self, idx: int, gain: int):
        # gain is the RAW value; max depends on input mode (Mic 56, Line 29, Hi-Z 36)
        max_raw = protocol.max_raw_gain(self.config[idx].get("pretype", 0))
        gain = max(GAIN_MIN, min(max_raw, gain))
        zerocross = self.config[idx].get("zerocross", 1)
        pretype   = self.config[idx].get("pretype", 0)
        self.send_command(protocol.SET_GAIN, idx, gain, zerocross, pretype)
        self.config[idx]["gain"] = gain

    def set_phantom(self, idx: int, enabled: bool):
        val = 1 if enabled else 0
        self.send_command(protocol.SET_PHANTOM, idx, val)
        self.config[idx]["phantom"] = val

    def set_pretype(self, idx: int, pretype: int):
        self.send_command(protocol.SET_INPUT_TYPE, idx, int(pretype))
        self.config[idx]["pretype"] = int(pretype)

    def set_power(self, on: bool):
        # Not in the official command table — left available but unverified.
        self.send_command(protocol.SET_POWER, 1 if on else 0)
        self.power_on = on

    def recall_preset(self, idx: int):
        self.send_command(protocol.PRESET_RECALL, idx)
        self.current_preset = idx

    def save_preset(self, idx: int):
        self.send_command(protocol.PRESET_SAVE, idx)

    def retarget(self, ip: str, port: Optional[int] = None):
        """Point the connection at a different device IP/port (e.g. after the
        MP32 moves to another host). The read loop reconnects automatically."""
        self.ip = ip
        if port:
            self.port = int(port)
        try:
            self.sock.close()
        except Exception:
            pass
        self.connected = False   # read loop will reconnect to the new target
        print(f"[MP32] Retargeting to {self.ip}:{self.port}")

    def get_status(self) -> Dict:
        return {
            "connected":      self.connected,
            "power_on":       self.power_on,
            "current_preset": self.current_preset,
            "config":         self.config if self.config_valid or self.demo else None,
            "peaks":          self.peaks,
            "device_ip":      self.ip,
            "device_port":    self.port,
            "device_info":    self.device_info,
            "demo":           self.demo,
            "connection_state": self.connection_state,
            "connection_error": self.last_error,
        }


# ── Beacon discovery (find compatible devices on the LAN regardless of IP) ────
# Compatible devices multicast a JSON discovery heartbeat on the local network:
#   {"ip","port","uuid","name","type","protocol","properties":{serial_number,
#    device_name,firmware_version,...},"interval"}.  We listen and keep a live list.
def _join_multicast(s: socket.socket, group: str, rejoin: bool = False):
    """(Re)join a multicast group on the default AND the primary LAN interface.

    A single INADDR_ANY membership can land on the wrong interface (VPN, multiple
    NICs), and an IGMP-snooping switch then never forwards the group to this host —
    the classic 'discovery only works once some other application opens the stream'
    symptom. Joining per-interface is the standard remedy. rejoin=True drops first so
    the kernel emits a fresh IGMP report.
    """
    grp = socket.inet_aton(group)
    ifaces = [struct.pack("4sl", grp, socket.INADDR_ANY)]
    try:
        lan = local_lan_ip()
        if lan and not lan.startswith("127."):
            ifaces.append(grp + socket.inet_aton(lan))
    except Exception:
        pass
    for mreq in ifaces:
        if rejoin:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except OSError:
                pass
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass   # duplicate membership etc. — keep whatever we already have


def _read_one_frame(sock) -> Optional[bytes]:
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    total = struct.unpack("!i", head)[0]
    if total < 4 or total > 4_000_000:
        return None
    body = b""
    while len(body) < total - 4:
        chunk = sock.recv(total - 4 - len(body))
        if not chunk:
            break
        body += chunk
    return body


def query_admin(ip: str, timeout: float = 1.2) -> Optional[Dict[str, Any]]:
    """Ask one host what it has plugged in, or None if it runs no device server."""
    try:
        with socket.create_connection((ip, protocol.ADMIN_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            body = _read_one_frame(s)
    except OSError:
        return None
    if not body:
        return None
    try:
        msg = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    contents = msg.get("contents")
    if not isinstance(contents, str):
        return None
    info = protocol.parse_admin_status(contents)
    info["ip"] = ip
    return info


def probe_device_port(ip: str, port: int, timeout: float = 1.5) -> bool:
    """True if a full channel config can be read from ip:port.

    Registers the report format first, because an unregistered server refuses every request
    and would otherwise look identical to a wrong port.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(protocol.frame_payload(
                protocol.encode_command(protocol.INITIALIZE_FORMAT, protocol.REPORT_FORMAT)))
            s.sendall(protocol.frame_payload(protocol.encode_command(protocol.GET_CONFIG)))
            for _ in range(8):
                body = _read_one_frame(s)
                if not body:
                    return False
                msg = json.loads(body.decode("utf-8", "replace"))
                if msg.get("type") == "cyclic":
                    continue
                contents = msg.get("contents")
                return isinstance(contents, list) and len(contents) >= NUM_CHANNELS
    except (OSError, ValueError):
        return False
    return False


def find_device_on_host(ip: str) -> Optional[int]:
    """Return the port serving a unit on this host, or None."""
    for port in protocol.DEVICE_PORT_CANDIDATES:
        if probe_device_port(ip, port):
            return port
    return None


def scan_for_device(candidates: List[str], match: str = DEVICE_MATCH,
                    workers: int = 24) -> Optional[Dict[str, Any]]:
    """Find a host with a matching unit attached, checking candidates concurrently.

    The discovery beacon cannot be relied on alone: on the studio network the only announcing
    host is a controller machine with no unit plugged in, while the machine that owns the unit
    announces nothing that reaches other controllers. Without this, a freshly installed copy
    with no remembered address never finds the hardware at all.

    `match` is enforced against the admin port's device list, which is a passive read, BEFORE
    any device port is touched. This matters: probing a device port registers a report format,
    that registration is sticky and server-wide, and registering the MP32 format on a host
    serving a different Antelope unit would be the format in force for that unit's own panel.
    Other studio hardware must never be written to while looking for ours.
    """
    found: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def check(ip: str):
        if found:
            return
        info = query_admin(ip)
        if not info:
            return
        names = info.get("devices") or []
        if not any(match.lower() in n.lower() for n in names):
            return          # no unit, or somebody else's unit — leave it completely alone
        port = find_device_on_host(ip)
        if port:
            with lock:
                found.append({"ip": ip, "port": port,
                              "devices": names,
                              "server_version": info.get("server_version", "")})

    threads = []
    for ip in candidates:
        while len([t for t in threads if t.is_alive()]) >= workers:
            time.sleep(0.02)
        if found:
            break
        t = threading.Thread(target=check, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=6.0)
    return found[0] if found else None


def subnet_candidates(local_ip: str) -> List[str]:
    """Every address on this machine's /24, nearest-first from our own address."""
    parts = local_ip.split(".")
    if len(parts) != 4 or not parts[3].isdigit():
        return []
    base, own = ".".join(parts[:3]), int(parts[3])
    order = sorted(range(1, 255), key=lambda n: (abs(n - own), n))
    return [f"{base}.{n}" for n in order if n != own]


class BeaconListener:
    GROUP = protocol.DISCOVERY_GROUP
    PORT  = protocol.DISCOVERY_PORT

    def __init__(self):
        self.devices: Dict[str, Dict[str, Any]] = {}   # uuid -> info
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, name="BeaconListener", daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
            s.bind(("", self.PORT))
            _join_multicast(s, self.GROUP)
            s.settimeout(1.0)
        except Exception as e:
            print(f"[BEACON] Listener failed to start: {e}")
            return
        print(f"[BEACON] Listening on {self.GROUP}:{self.PORT}")
        last_join = time.time()
        while self._running:
            # Refresh the membership periodically: it forces fresh IGMP reports so a
            # snooping switch keeps (or starts) forwarding the device announces to us.
            if time.time() - last_join > 45:
                _join_multicast(s, self.GROUP, rejoin=True)
                last_join = time.time()
            try:
                data, addr = s.recvfrom(8192)
                msg = json.loads(data.decode("utf-8", "ignore"))
                uuid = msg.get("uuid") or msg.get("ip") or addr[0]
                props = msg.get("properties") or {}
                with self._lock:
                    self.devices[uuid] = {
                        "name":        msg.get("name"),
                        "ip":          msg.get("ip", addr[0]),
                        "port":        msg.get("port"),
                        "device_name": props.get("device_name"),
                        "serial":      props.get("serial_number"),
                        "firmware":    props.get("firmware_version"),
                        "last_seen":   time.time(),
                    }
            except socket.timeout:
                pass
            except Exception:
                pass

    def list(self, max_age: float = 5.0) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [d for d in self.devices.values() if now - d["last_seen"] < max_age]


# ── Peer presence (see other GUI controllers on the LAN) ──────────────────────
# Each instance multicasts a heartbeat for web-host election and newest-write-wins
# metadata. Device parameters are never included in peer/handoff state.
class PeerService:
    GROUP = "239.255.42.99"
    PORT  = 5009

    def __init__(self, device: MP32Device):
        self.device = device
        device.peers = self      # lets discovery learn the target from other controllers
        self.id = uuid.uuid4().hex[:8]
        self.host = socket.gethostname()
        self.started_at = time.time()
        self.peers: Dict[str, Dict[str, Any]] = {}
        self.fields: Dict[str, Dict[str, Any]] = load_meta_fields()   # key -> {value, ts}
        self._fields_dirty = False
        self._tx = None
        self._lock = threading.Lock()
        self._running = False
        self.web_leader = False

    def start(self):
        self._running = True
        try:
            self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except Exception:
            self._tx = None
        threading.Thread(target=self._listen,   name="PeerListen",   daemon=True).start()
        threading.Thread(target=self._announce, name="PeerAnnounce", daemon=True).start()
        threading.Thread(target=self._persist_loop, name="PeerPersist", daemon=True).start()

    def stop(self):
        self._running = False
        self._flush_fields()

    def _flush_fields(self):
        with self._lock:
            if not self._fields_dirty:
                return
            snapshot = dict(self.fields)
            self._fields_dirty = False
        save_meta_fields(snapshot)

    def _persist_loop(self):
        # Batched rather than written per event: metadata arrives in bursts (a browser
        # seeding on load pushes every key it holds) and each burst is one write.
        while self._running:
            time.sleep(META_PERSIST_INTERVAL)
            self._flush_fields()

    def _listen(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
            s.bind(("", self.PORT))
            _join_multicast(s, self.GROUP)
            s.settimeout(1.0)
        except Exception as e:
            print(f"[PEER] Listener failed: {e}")
            return
        last_join = time.time()
        while self._running:
            if time.time() - last_join > 45:
                _join_multicast(s, self.GROUP, rejoin=True)
                last_join = time.time()
            try:
                data, addr = s.recvfrom(65535)
                m = json.loads(data.decode("utf-8", "ignore"))
                if m.get("id") == self.id:
                    continue
                if m.get("type") == "meta":
                    self.apply_meta_event(m.get("key"), m.get("value"), m.get("ts"), broadcast=False)
                    continue
                if m.get("type") == "meta_snapshot":
                    fields = m.get("fields") or {}
                    if isinstance(fields, dict):
                        for key, item in fields.items():
                            if isinstance(item, dict):
                                self.apply_meta_event(key, item.get("value"), item.get("ts"), broadcast=False)
                    continue
                with self._lock:
                    # One machine runs one controller at a time, but a restarted app keeps
                    # the same host+ip with a NEW session id and a newer started_at. Treat
                    # started_at as an epoch: the newest incarnation per (host, ip) wins and
                    # any older one is a ghost. This stops a just-closed instance's lingering
                    # stale web-host heartbeat from evicting the freshly started process.
                    host = m.get("host")
                    m_started = float(m.get("started_at") or time.time())
                    superseded = False
                    for peer_id, peer in list(self.peers.items()):
                        if peer_id != m.get("id") and peer.get("host") == host and peer.get("ip") == addr[0]:
                            if float(peer.get("started_at") or 0.0) <= m_started:
                                del self.peers[peer_id]      # m is the newer incarnation
                            else:
                                superseded = True            # a newer incarnation is already known
                    if not superseded:
                        self.peers[m["id"]] = {
                            "host":      host,
                            "ip":        addr[0],
                            "device":    m.get("device"),
                            "online":    m.get("online"),
                            "web_leader": bool(m.get("web_leader", False)),
                            "started_at": m_started,
                            "last_seen": time.time(),
                        }
            except socket.timeout:
                pass
            except Exception:
                pass

    def _announce(self):
        tick = 0
        while self._running:
            msg = {"type": "presence", "id": self.id, "host": self.host,
                   "device": self.device.ip, "online": self.device.connected,
                   "web_leader": self.web_leader, "started_at": self.started_at}
            if self._tx:
                try:
                    self._tx.sendto(json.dumps(msg).encode(), (self.GROUP, self.PORT))
                except Exception:
                    pass
                # Metadata events are sent immediately, but UDP can drop a packet and a
                # controller may join later. Periodic snapshots make groups/colours/notes
                # converge without relying on that single event packet.
                if tick % 4 == 0:
                    with self._lock:
                        fields = dict(self.fields)
                    if fields:
                        snap = {"type": "meta_snapshot", "id": self.id, "fields": fields}
                        try:
                            payload = json.dumps(snap).encode()
                            if len(payload) <= 65507:
                                self._tx.sendto(payload, (self.GROUP, self.PORT))
                        except Exception:
                            pass
            tick += 1
            time.sleep(PEER_HEARTBEAT_INTERVAL)

    def info(self, max_age: float = PEER_TIMEOUT) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            peers = [dict(p, id=k) for k, p in self.peers.items() if now - p["last_seen"] < max_age]
        return {"self": {"id": self.id, "host": self.host, "device": self.device.ip,
                          "web_leader": self.web_leader, "started_at": self.started_at},
                "peers": peers}

    def active_peers(self, max_age: float = PEER_TIMEOUT) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [dict(p, id=k) for k, p in self.peers.items()
                    if now - p["last_seen"] < max_age]

    # ── Shared metadata (channel names/colours/groups) — per-field last-write-wins ──
    def apply_meta_event(self, key, value, ts, broadcast: bool = True):
        if not key or ts is None:
            return
        with self._lock:
            cur = self.fields.get(key)
            if cur and cur["ts"] >= ts:
                return   # we already have a newer/equal value
            self.fields[key] = {"value": value, "ts": ts}
            self._fields_dirty = True
        if broadcast and self._tx:
            msg = {"type": "meta", "id": self.id, "key": key, "value": value, "ts": ts}
            try:
                self._tx.sendto(json.dumps(msg).encode(), (self.GROUP, self.PORT))
            except Exception:
                pass

    def meta_state(self) -> Dict[str, Any]:
        with self._lock:
            return {"fields": dict(self.fields)}


# ── Stable web-host name with automatic Mac/Windows failover ────────────────
class StableHostService:
    """Elect one sticky controller for the stable PWA hostname and metadata presence.

    Because live probing currently shows the MP32 withholding config from additional TCP
    clients, the web host also owns the one live device session as a transport fallback.
    No device state is elected, cached in peers, or handed off. If multi-client support is
    verified, DEVICE_SUPPORTS_MULTIPLE_CLIENTS enables a direct session per desktop.
    """
    SERVICE_TYPE = "_http._tcp.local."
    SERVICE_NAME = "MP32 Control._http._tcp.local."

    def __init__(self, peers: PeerService, port: int):
        self.peers = peers
        self.port = int(port)
        self.available = ZEROCONF_AVAILABLE
        self.active = False
        self._running = False
        self._started = time.time()
        self._zc = None
        self._info = None
        self._last_publish_attempt = 0.0
        self._stable_ip = None
        self._last_resolve_attempt = 0.0
        self._unreachable_since: Dict[str, float] = {}
        self._probe_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://{STABLE_HOST}:{self.port}"

    def start(self):
        if not self.available:
            print("[HOST] zeroconf unavailable; web-host election still active")
        self._running = True
        threading.Thread(target=self._loop, name="StableWebHost", daemon=True).start()
        threading.Thread(target=self._probe_loop, name="StableHostProbe", daemon=True).start()
        if self.available:
            threading.Thread(target=self._resolve_loop, name="StableHostResolver", daemon=True).start()

    def stop(self):
        self._running = False
        self._deactivate()

    def _peer_is_stale_self(self, p: Dict[str, Any]) -> bool:
        """True if p is a previous incarnation of THIS controller — same machine,
        started no later than us. Its lingering heartbeat must never evict us."""
        return (p.get("host") == self.peers.host
                and p.get("id") != self.peers.id
                and float(p.get("started_at") or 0.0) <= self.peers.started_at)

    def _tcp_reachable(self, ip: str) -> bool:
        try:
            with socket.create_connection((ip, self.port), timeout=HOST_PROBE_TIMEOUT):
                return True
        except OSError:
            return False

    def _peer_unreachable(self, p: Dict[str, Any]) -> bool:
        """True once a peer's HTTP port has been continuously unreachable for
        HOST_PROBE_GRACE seconds. Such a peer cannot serve the PWA or proxy /api, so it is
        not a valid web-host candidate no matter what its heartbeat claims."""
        with self._probe_lock:
            since = self._unreachable_since.get(p.get("id"))
        return since is not None and (time.time() - since) >= HOST_PROBE_GRACE

    def _electable_peers(self) -> List[Dict[str, Any]]:
        """Active peers that may legitimately win or hold the web-host role."""
        return [p for p in self.peers.active_peers()
                if not self._peer_is_stale_self(p) and not self._peer_unreachable(p)]

    def unreachable_hosts(self) -> List[Dict[str, Any]]:
        """Diagnostic: peers that heartbeat but cannot be reached on the HTTP port.

        Surfaced in /api/status so a firewall-blocked or dead host is visible instead of
        appearing as an unexplained permanent 'waiting for web host'.
        """
        now, out = time.time(), []
        with self._probe_lock:
            failing = dict(self._unreachable_since)
        for p in self.peers.active_peers():
            since = failing.get(p.get("id"))
            if since is not None and (now - since) >= HOST_PROBE_GRACE:
                out.append({"host": p.get("host"), "ip": p.get("ip"),
                            "port": self.port, "web_leader": bool(p.get("web_leader")),
                            "unreachable_seconds": round(now - since, 1)})
        return out

    def _probe_loop(self):
        """Continuously verify that each peer's HTTP port actually accepts connections.

        Peers sharing our address are skipped: a second instance on this machine is handled
        by _peer_is_stale_self, and probing our own listener would always succeed anyway.
        """
        while self._running:
            local_ip = local_lan_ip()
            probed = set()
            for peer in self.peers.active_peers():
                ip, pid = peer.get("ip"), peer.get("id")
                if not ip or not pid or ip == local_ip:
                    continue
                probed.add(pid)
                ok = self._tcp_reachable(ip)
                with self._probe_lock:
                    if ok:
                        self._unreachable_since.pop(pid, None)
                    elif pid not in self._unreachable_since:
                        self._unreachable_since[pid] = time.time()
                        print(f"[HOST] Peer {peer.get('host')} ({ip}:{self.port}) not answering; "
                              f"it loses web-host candidacy in {HOST_PROBE_GRACE:.0f}s")
            with self._probe_lock:
                for pid in [k for k in self._unreachable_since if k not in probed]:
                    del self._unreachable_since[pid]
            time.sleep(HOST_PROBE_INTERVAL)

    def _should_lead(self) -> bool:
        """CHRONOLOGICAL election: the longest-running eligible controller holds the role.

        One rule covers every case, so no controller has to negotiate or defer specially:

        - a controller that starts while an older one is running never outranks it, so the
          role is sticky without a separate "do not steal from the incumbent" branch;
        - when the holder disappears, the next-oldest is already the minimum and takes over
          with no handover step;
        - a controller that restarts comes back as the youngest and cannot take the role back
          from whoever inherited it.

        Every peer broadcasts its own started_at, so all peers rank the same candidate set and
        converge on the same winner without agreeing on anything first. Clock skew between
        machines can change *which* controller wins, but not whether they agree — the order is
        computed from identical broadcast values. Peer id breaks ties as a stable total order.

        Excluded from candidacy: a previous incarnation of ourselves (it is older, so it would
        otherwise win and we would defer to a dead process), and peers whose HTTP port is
        proven unreachable (they cannot serve the PWA or bootstrap a newcomer).
        """
        peers = self._electable_peers()
        me = {"id": self.peers.id, "started_at": self.peers.started_at}
        key = lambda p: (float(p.get("started_at") or 0.0), p["id"])
        if not self.active and time.time() - self._started < HOST_ELECTION_GRACE:
            # Hear a full round of heartbeats before claiming anything, so a controller that
            # starts into an established network never briefly double-claims the role.
            return False
        return min(peers + [me], key=key)["id"] == self.peers.id

    def _publish_mdns(self, ip: str):
        if not self.available or self._zc:
            return
        self._last_publish_attempt = time.time()
        zc = None
        try:
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            info = ServiceInfo(
                self.SERVICE_TYPE,
                self.SERVICE_NAME,
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties={"path": "/", "app": "MP32 Control"},
                server=STABLE_HOST + ".",
            )
            zc.register_service(info, ttl=HOST_MDNS_TTL, allow_name_change=False)
            self._zc, self._info = zc, info
            print(f"[HOST] Published {self.url} ({ip})")
        except Exception as e:
            try:
                if zc:
                    zc.close()
            except Exception:
                pass
            self._zc = self._info = None
            print(f"[HOST] Could not publish {STABLE_HOST}; retrying: {e}")

    def _activate(self):
        ip = local_lan_ip()
        if not ip or ip.startswith("127."):
            return
        self._publish_mdns(ip)
        self.active = True
        self.peers.web_leader = True
        if not DEVICE_SUPPORTS_MULTIPLE_CLIENTS:
            self.peers.device.set_session_enabled(True)
        print(f"[HOST] Active web host → {self.url} ({ip})")

    def _deactivate(self):
        self.active = False
        self.peers.web_leader = False
        if not DEVICE_SUPPORTS_MULTIPLE_CLIENTS:
            self.peers.device.set_session_enabled(False)
        zc, info = self._zc, self._info
        self._zc = self._info = None
        if zc:
            try:
                if info:
                    zc.unregister_service(info)
            except Exception:
                pass
            try:
                zc.close()
            except Exception:
                pass

    def leader_base_urls(self) -> List[str]:
        """Return resilient remote web-host candidates in preferred order.

        Presence can arrive before its leader flag, so an online peer and the stable mDNS
        record are valid fallbacks. The local IP is excluded to prevent proxy recursion, and
        peers proven unreachable are excluded so /api calls are not proxied into a dead host
        and stalled on its connect timeout.
        """
        if self.active:
            return []
        peers = self._electable_peers()
        # Oldest-first, matching the election: the longest-running controller is the one that
        # holds the role, so it is also the first candidate during a handover.
        key = lambda p: (float(p.get("started_at") or 0.0), p["id"])
        leaders = sorted([p for p in peers if p.get("web_leader") and p.get("ip")], key=key)
        online = sorted([p for p in peers if p.get("online") and p.get("ip")], key=key)
        local_ip, urls = local_lan_ip(), []
        for peer in leaders + online:
            if peer["ip"] != local_ip:
                url = f"http://{peer['ip']}:{self.port}"
                if url not in urls:
                    urls.append(url)
        if not urls and self._stable_ip and self._stable_ip != local_ip:
            urls.append(f"http://{self._stable_ip}:{self.port}")
        return urls

    def leader_base_url(self) -> Optional[str]:
        urls = self.leader_base_urls()
        return urls[0] if urls else None

    def _loop(self):
        while self._running:
            should = self._should_lead()
            if should and not self.active:
                self._activate()
            elif should and self.active and self.available and not self._zc:
                if time.time() - self._last_publish_attempt >= 2.0:
                    self._publish_mdns(local_lan_ip())
            elif not should and self.active:
                print("[HOST] Yielding web-host role to another controller")
                self._deactivate()
            time.sleep(0.5)

    def _resolve_loop(self):
        while self._running:
            if not self.active:
                self._last_resolve_attempt = time.time()
                try:
                    self._stable_ip = socket.gethostbyname(STABLE_HOST)
                except OSError:
                    self._stable_ip = None
            time.sleep(1.0)


# ── Embedded HTML/CSS/JS ───────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<meta name="theme-color" content="#08090f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MP32 Control">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/app-icon.png">
<link rel="icon" type="image/png" sizes="512x512" href="/app-icon.png">
<link rel="shortcut icon" href="/app-icon.png">
<title>MP32 Control</title>
<style>
:root{
  --bg:#08090f; --card:rgba(18,20,38,.9); --strip:rgba(15,17,35,.95);
  --accent:#7c5cff; --accent2:#4fc3f7; --phantom:#ff9a00; --green:#00e676;
  --red:#ff5252; --yellow:#ffeb3b; --t1:#eeeeff; --t2:#7a7a9a; --t3:#3a3a5a;
  --border:rgba(124,92,255,.15); --glow:rgba(124,92,255,.45);
  --mic:#4fc3f7; --line:#00e676; --hiz:#ff9a00;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--t1);min-height:100vh;
  background-image:radial-gradient(ellipse at 15% 10%,rgba(124,92,255,.06),transparent 55%),
  radial-gradient(ellipse at 85% 90%,rgba(79,195,247,.04),transparent 55%);padding-bottom:24px}

.hdr{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:16px;
  padding:0 22px;height:62px;background:rgba(8,9,15,.96);border-bottom:1px solid var(--border);backdrop-filter:blur(20px)}
.logo{display:flex;align-items:center;gap:11px}
.logo-i{width:40px;height:40px;border-radius:10px;object-fit:contain;display:block;box-shadow:0 0 18px rgba(79,195,247,.28)}
.logo-t h1{font-size:16px;font-weight:700;letter-spacing:.5px}
.logo-t p{font-size:10px;color:var(--t2)}
.hmid{flex:1;display:flex;align-items:center;justify-content:center;gap:6px}
.plabel{font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:1.5px;margin-right:4px}
.pbtn{width:32px;height:30px;border-radius:7px;border:1px solid var(--border);background:var(--card);
  color:var(--t2);font-size:12px;font-family:monospace;cursor:pointer;transition:all .15s}
.pbtn:hover{border-color:var(--accent);color:var(--accent)}
.pbtn.active{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 12px var(--glow)}
.savebtn{padding:0 11px;height:30px;border-radius:7px;border:1px solid rgba(0,230,118,.3);
  background:rgba(0,230,118,.07);color:var(--green);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;cursor:pointer;margin-left:6px}
.hright{display:flex;align-items:center;gap:11px}
/* Status and power are their own group so the phone layout can put them on the first row and
   keep the secondary buttons on a row of their own. On desktop the two groups sit side by
   side and read exactly as one strip, as before. */
.hstatus{display:flex;align-items:center;gap:11px;flex:0 0 auto}
/* Armed = waiting for a confirming second tap. Amber rather than the accent colour, so it
   reads as "this is about to do something", not as a normal selected state. */
@keyframes arm-pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,154,0,.55)}70%{box-shadow:0 0 0 9px rgba(255,154,0,0)}}
.armed,.pwr.armed,.savebtn.armed,.pbtn.armed,.tbtn.armed{
  border-color:var(--phantom)!important;color:var(--phantom)!important;
  background:rgba(255,154,0,.14)!important;animation:arm-pulse 1.1s ease-out infinite}
.hicon{display:inline}
#stxtShort{display:none}
.pill{display:flex;align-items:center;gap:6px;padding:5px 11px;border-radius:20px;border:1px solid var(--border);background:var(--card);font-size:11px;color:var(--t2)}
.dot{width:7px;height:7px;border-radius:50%;background:#444;transition:all .3s}
.dot.on{background:var(--green);box-shadow:0 0 7px var(--green)}
.dot.off{background:var(--red);box-shadow:0 0 7px var(--red)}
.pwr{width:38px;height:38px;border-radius:50%;border:2px solid var(--border);color:var(--t2);
  background:var(--card);font-size:16px;cursor:pointer;transition:all .2s}
.pwr.on{border-color:var(--green);color:var(--green);background:rgba(0,230,118,.1);box-shadow:0 0 14px rgba(0,230,118,.35)}

.toolbar{display:flex;align-items:center;gap:8px;padding:14px 22px 8px;flex-wrap:wrap}
.seg{display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.seg button{padding:7px 11px;background:var(--card);border:none;border-right:1px solid var(--border);
  color:var(--t2);font-size:12px;cursor:pointer;transition:all .15s}
.seg button:last-child{border-right:none}
.seg button.on{background:var(--accent);color:#fff}
.tbtn{padding:7px 13px;border-radius:8px;border:1px solid var(--border);background:var(--card);
  color:var(--t2);font-size:12px;cursor:pointer;transition:all .15s;display:flex;align-items:center;gap:6px}
.tbtn:hover{border-color:var(--accent);color:var(--accent)}
.iconbtn{width:38px;height:38px;padding:3px;justify-content:center;overflow:hidden;flex:0 0 38px}
.iconbtn img{width:31px;height:31px;object-fit:contain;border-radius:5px;filter:grayscale(1) contrast(1.18);opacity:.9}
.iconbtn:hover img,.iconbtn.on img,.iconbtn.unlink img{filter:none;opacity:1}
.layout-icon{font-size:19px;line-height:1;color:var(--accent2)}
.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
.tbtn.warn{border-color:rgba(255,82,82,.3);color:#ff8a8a}
.tbtn.warn:hover{border-color:var(--red);color:var(--red);background:rgba(255,82,82,.08)}
.tbtn.ph{border-color:rgba(255,154,0,.3);color:var(--phantom)}
.tbtn.ph.on{background:rgba(255,154,0,.14)}
.spacer{flex:1}

.offline-banner{display:none;align-items:center;gap:8px;margin:0 22px 8px;padding:9px 14px;border-radius:9px;
  background:rgba(255,82,82,.1);border:1px solid var(--red);color:#ff8a8a;font-size:12px;font-weight:500}
body.offline .offline-banner{display:flex}
.handover-banner{display:none;align-items:center;gap:10px;margin:0 22px 8px;padding:10px 14px;border-radius:9px;
  background:rgba(255,193,7,.1);border:1px solid rgba(255,193,7,.65);color:#ffd66b;font-size:12px;font-weight:600}
body.handover .handover-banner{display:flex}
.handover-spin{width:15px;height:15px;flex:0 0 15px;border:2px solid rgba(255,214,107,.3);border-top-color:#ffd66b;border-radius:50%;animation:handover-spin .8s linear infinite}
@keyframes handover-spin{to{transform:rotate(360deg)}}
/* Standby is a device state, not a connection state: we are online and the values shown are
   real, the unit is simply powered down. It must be unmistakable without implying the panel
   has lost the device, so the strips grey out but stay legible and the power button stays
   live — it is the way back. */
.standby-banner{display:none;align-items:center;gap:8px;margin:0 22px 8px;padding:9px 14px;border-radius:9px;
  background:rgba(255,154,0,.1);border:1px solid var(--phantom);color:#ffbe5c;font-size:12px;font-weight:600}
body.standby .standby-banner{display:flex}
body.standby:not(.offline):not(.handover) .strip{opacity:.55;filter:grayscale(.7)}
body.standby:not(.offline):not(.handover) .vu{opacity:.3}
@keyframes standby-pulse{0%,100%{opacity:1}50%{opacity:.45}}
body.standby:not(.offline):not(.handover) .pwr{animation:standby-pulse 1.6s ease-in-out infinite}

body.offline .strip,body.offline .toolbar .tbtn:not(.always),body.offline .pbtn,body.offline .savebtn,body.offline .pwr{
  opacity:.4;pointer-events:none;filter:grayscale(.5)}
body.handover .strip,body.handover .toolbar .tbtn:not(.always),body.handover .pbtn,body.handover .savebtn,body.handover .pwr{
  opacity:.4;pointer-events:none;filter:grayscale(.5)}

.wrap{display:flex;gap:14px;padding:4px 22px 0;align-items:flex-start}
.gridcol{flex:1;min-width:0}
.rowlabel{font-size:9px;color:var(--t3);letter-spacing:2px;text-transform:uppercase;padding:8px 2px 4px}
.grid{display:grid;gap:6px}
.grid.l-2x16{grid-template-columns:repeat(16,minmax(0,1fr))}
.grid.l-wrap{grid-template-columns:repeat(auto-fill,minmax(86px,1fr))}

.strip{background:var(--strip);border:1px solid var(--border);border-radius:11px;
  padding:16px 5px 10px;display:flex;flex-direction:column;align-items:center;gap:6px;position:relative;overflow:hidden;transition:border-color .15s,box-shadow .15s,background .2s}
.strip:hover{border-color:rgba(124,92,255,.35)}
.strip.changed{border-color:rgba(124,92,255,.5);box-shadow:0 0 0 1px rgba(124,92,255,.2),0 4px 16px rgba(124,92,255,.1)}
.ctint{position:absolute;top:0;left:0;right:0;height:11px;cursor:pointer;opacity:.9;
  border-bottom:1px solid rgba(255,255,255,.08);transition:height .15s,filter .15s}
.ctint:hover{height:14px;filter:brightness(1.3)}
.cname{font-size:10px;font-weight:600;color:var(--t1);width:100%;text-align:center;
  border:none;background:transparent;outline:none;padding:1px 2px;border-radius:4px}
.cname:focus{background:rgba(124,92,255,.12)}
.cname.def{color:var(--t2);font-family:monospace;font-weight:500}
.cnum{font-size:8px;color:var(--t3);font-family:monospace;letter-spacing:.5px;margin-top:-3px}

.vu{width:30px;height:78px;background:rgba(0,0,0,.55);border-radius:3px;position:relative;overflow:hidden;border:1px solid rgba(255,255,255,.05)}
.vu .fill{position:absolute;bottom:0;left:0;right:0;height:0%;transition:height .03s linear;
  background:linear-gradient(to top,#00e676 0,#00e676 60%,#ffeb3b 60%,#ffeb3b 82%,#ff5252 82%,#ff5252 100%)}
.vu .hold{position:absolute;left:0;right:0;height:2px;background:rgba(255,255,255,.9);bottom:0;transition:bottom .4s ease-out;z-index:2}
.vu .tick{position:absolute;left:0;width:3px;height:1px;background:rgba(255,255,255,.18)}
.vusc{position:absolute;inset:0;pointer-events:none;z-index:3}
.vusc span{position:absolute;right:2px;font-size:7px;line-height:1;color:rgba(255,255,255,.6);
  text-shadow:0 0 2px #000,0 0 2px #000;font-family:'JetBrains Mono',monospace;transform:translateY(50%)}
.vupk{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;color:var(--t2);line-height:1;margin-top:3px;min-height:11px}
.vupk.warn{color:var(--yellow)}
.vupk.hot{color:var(--red)}

.knob{width:40px;height:40px;border-radius:50%;background:radial-gradient(circle at 50% 38%,#2a2d52,#13152b);
  border:1px solid rgba(124,92,255,.35);position:relative;box-shadow:inset 0 1px 3px rgba(0,0,0,.6);cursor:ns-resize;touch-action:none}
.knob .ind{position:absolute;top:3px;left:50%;width:2px;height:13px;background:var(--accent2);border-radius:2px;transform-origin:50% 17px;box-shadow:0 0 5px var(--accent2);transition:transform .07s linear}
.gval{font-size:14px;font-weight:600;color:var(--accent2);font-family:monospace;line-height:1.1;margin-top:2px}
.gunit{font-size:7px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px}

.ph{width:58px;height:28px;min-width:58px;min-height:28px;padding:0;margin:0;border-radius:5px;border:1px solid rgba(255,154,0,.2);background:rgba(255,154,0,.04);
  color:rgba(255,154,0,.35);font-size:9px;font-weight:700;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;user-select:none;
  position:relative;z-index:4;touch-action:manipulation;appearance:none;-webkit-appearance:none}
.ph.on{border-color:var(--phantom);background:rgba(255,154,0,.16);color:var(--phantom);box-shadow:0 0 8px rgba(255,154,0,.35)}
.ph.hide{visibility:hidden}
.itype{font-size:9px;font-weight:700;padding:0;margin:0;width:58px;height:28px;min-width:58px;min-height:28px;text-align:center;border-radius:5px;letter-spacing:.4px;line-height:1;cursor:pointer;border:1px solid;transition:all .15s;user-select:none;
  display:flex;align-items:center;justify-content:center;position:relative;z-index:4;touch-action:manipulation;appearance:none;-webkit-appearance:none}
.ph:focus-visible,.itype:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.ph>span,.itype>span{pointer-events:none;display:flex;align-items:center;justify-content:center;width:100%;height:100%}
.itype.mic{color:var(--mic);border-color:rgba(79,195,247,.4);background:rgba(79,195,247,.08)}
.itype.line{color:var(--line);border-color:rgba(0,230,118,.4);background:rgba(0,230,118,.08)}
.itype.hiz{color:var(--hiz);border-color:rgba(255,154,0,.4);background:rgba(255,154,0,.08)}
.itype.hizcap{position:relative}
.itype.hizcap::after{content:'';position:absolute;top:3px;right:4px;width:3px;height:3px;border-radius:50%;background:var(--hiz);box-shadow:0 0 4px var(--hiz)}

.notes{position:fixed;z-index:180;top:62px;right:0;bottom:0;width:310px;background:rgba(18,20,38,.98);
  border-left:1px solid var(--accent);padding:16px;transform:translateX(100%);transition:transform .22s ease;box-shadow:-12px 0 30px rgba(0,0,0,.35)}
body.notes-open .notes{transform:translateX(0)}
.notes h3{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--t3);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.notes .nlocal{font-size:8px;background:rgba(124,92,255,.15);color:var(--accent2);padding:2px 6px;border-radius:10px;margin-left:auto;letter-spacing:.5px}
.notes-tabs{display:flex;gap:5px;margin-bottom:8px}
.note-tab{flex:1;padding:7px;border-radius:7px;border:1px solid var(--border);background:rgba(0,0,0,.2);color:var(--t2);font-size:10px;text-transform:uppercase;letter-spacing:.8px;cursor:pointer}
.note-tab.on{border-color:var(--accent);background:rgba(124,92,255,.15);color:var(--t1)}
.notes textarea{width:100%;height:calc(100% - 92px);min-height:220px;background:rgba(0,0,0,.25);border:1px solid var(--border);border-radius:8px;
  color:var(--t1);font-size:12px;line-height:1.7;padding:9px 10px;resize:vertical;outline:none;font-family:inherit}
.notes textarea.hide{display:none}
/* Shared cards. One editor per card is enforced only as politeness — a lease that another
   controller can see. Correctness sits underneath in per-card last-write-wins, so a lost
   lease costs at most one card, never the whole page of notes. */
.cards{height:calc(100% - 92px);overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding-right:2px}
.cards.hide{display:none}
.card-new{flex:0 0 auto;background:rgba(124,92,255,.12);border:1px dashed var(--accent);color:var(--accent2);
  border-radius:8px;padding:8px;font-size:11px;font-weight:600;cursor:pointer;letter-spacing:.4px}
.card-new:hover{background:rgba(124,92,255,.2)}
#cardList{display:flex;flex-direction:column;gap:8px;flex:0 0 auto}
.card-empty{font-size:10px;color:var(--t3);line-height:1.5;padding:4px 2px}
/* Natural height, never stretched to fill: a flex column would otherwise blow two cards up to
   fill the panel and hide the rest behind a scroll. */
.card{flex:0 0 auto;background:rgba(0,0,0,.25);border:1px solid var(--border);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:6px}
.card.locked{border-color:rgba(255,154,0,.5);background:rgba(255,154,0,.05)}
.card.mine{border-color:var(--accent)}
.card-head{display:flex;align-items:center;gap:6px}
.card-title{flex:1;min-width:0;background:transparent;border:0;color:var(--t1);font-size:12px;font-weight:600;
  font-family:inherit;padding:2px 0;outline:none}
.card-title::placeholder{color:var(--t3);font-weight:400}
.card-del{background:transparent;border:0;color:var(--t3);cursor:pointer;font-size:14px;line-height:1;padding:0 2px}
.card-del:hover{color:var(--red)}
.card-body{width:100%;min-height:60px;background:transparent;border:0;color:var(--t2);font-size:11px;
  font-family:inherit;line-height:1.5;resize:vertical;outline:none;padding:0}
.card[data-readonly="1"] .card-title,.card[data-readonly="1"] .card-body{opacity:.65;cursor:default}
.card-who{font-size:9px;color:var(--phantom);letter-spacing:.3px;display:none}
.card.locked .card-who{display:block}
.notes textarea:focus{border-color:var(--accent)}
.notes .hint{font-size:9px;color:var(--t3);margin-top:6px}
.notes-close{margin-left:5px;border:0;background:transparent;color:var(--t2);font-size:20px;line-height:1;cursor:pointer}
.notes-tab{position:fixed;z-index:181;right:0;top:48%;padding:12px 7px;border:1px solid var(--accent);border-right:0;border-radius:9px 0 0 9px;
  background:rgba(18,20,38,.96);color:var(--accent2);font-size:10px;letter-spacing:1px;text-transform:uppercase;writing-mode:vertical-rl;cursor:pointer;transition:right .22s ease}
body.notes-open .notes-tab{right:310px;background:var(--accent);color:#fff}

.toast{position:fixed;bottom:22px;right:22px;padding:11px 18px;border-radius:9px;background:rgba(18,20,38,.96);
  border:1px solid var(--accent);color:var(--t1);font-size:13px;font-weight:500;transform:translateY(70px);opacity:0;
  transition:all .3s cubic-bezier(.34,1.56,.64,1);backdrop-filter:blur(20px);z-index:999;pointer-events:none}
.toast.show{transform:translateY(0);opacity:1}
.tbtn.on{background:rgba(124,92,255,.18);border-color:var(--accent);color:var(--accent)}
.tbtn.unlink{background:rgba(255,82,82,.1);border-color:var(--red);color:#ff8a8a}
body.groupmode .strip{cursor:pointer}
body.groupmode .strip .knob,body.groupmode .strip .ph,body.groupmode .strip .itype,
body.groupmode .strip .cname,body.groupmode .strip .ctint{pointer-events:none}
.strip.sel{border-color:var(--accent2)!important;box-shadow:0 0 0 2px var(--accent2),0 0 16px rgba(79,195,247,.3)!important}
.glink{position:absolute;bottom:3px;left:5px;right:5px;height:3px;border-radius:2px;display:none}
.glink.on{display:block}
.palette{position:fixed;z-index:200;background:rgba(18,20,38,.98);border:1px solid var(--accent);border-radius:10px;
  padding:8px;display:none;grid-template-columns:repeat(4,1fr);gap:6px;backdrop-filter:blur(20px)}
.palette.show{display:grid}
.sw{width:24px;height:24px;border-radius:6px;cursor:pointer;border:1px solid rgba(255,255,255,.15)}
.sw:hover{transform:scale(1.12)}
.hbtn{padding:6px 11px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--t2);font-size:12px;cursor:pointer}
.hbtn:hover{border-color:var(--accent);color:var(--accent)}
.hbtn.history{width:34px;padding:6px 0;font-size:18px;line-height:1;text-align:center}
/* About and the support link are ONE control with two halves, not two buttons sitting
   next to each other. The border and background belong to the pill; the halves carry
   neither, and are separated by a single inset hairline. Hovering lights only the half the
   pointer is over, so it is obvious which one will fire. */
.hpair{display:inline-flex;align-items:stretch;border:1px solid var(--border);
  border-radius:8px;background:var(--card);overflow:hidden}
.hpair>.hbtn{border:none;background:transparent;border-radius:0;
  display:inline-flex;align-items:center;justify-content:center}
.hpair>.hbtn+.hbtn{box-shadow:inset 1px 0 0 var(--border)}
.hpair>.hbtn:hover{background:rgba(255,255,255,.05);color:var(--accent)}
.hpair:focus-within{border-color:var(--accent)}
/* Buy Me a Coffee's own cup, taken from the QR asset already in this repository and used as
   a mask so it takes the header's own colour like the ⓘ and ⚙ glyphs beside it. A tinted
   image sat on the button as a tile; a mask is an icon. Full brand colour on hover, where
   it is being looked at anyway. */
.hbtn.hbmc{padding-left:10px;padding-right:10px}
/* Connection state and controller count are one control: both are "who am I talking to",
   and each half opens the panel that explains its own side. The state half shows only its
   dot — the sentence it used to carry is the tooltip and the panel heading now.
   A ring marks this controller as the one publishing mp32-control.local, which is the one
   piece of the old label that a colour alone could not say. */
.hbtn.hstate{padding-left:11px;padding-right:11px}
/* Role is a shape, not a word and not a decoration on the state dot: filled means this
   machine publishes mp32-control.local and serves the phones, hollow means it follows one
   that does. The dot beside it stays the connection state, so the two facts never blur. */
/* A phone or tablet is on the web interface right now — lit in the same green as the
   connection dot, and simply out when nobody is. One meaning, one colour, no animation: a
   blinking light in front of someone mixing is an irritation, not information.

   It appears only on a controller that is the web host, since only a host serves handhelds,
   and only in the desktop window — a phone looking at this panel does not need to be told
   that a phone is connected. The slot is reserved either way, so nothing ever resizes. */
.hrole{width:12px;height:12px;margin-left:5px;flex:0 0 auto;color:var(--green);opacity:0;
  transition:opacity .25s ease}
.hbtn.hstate.served .hrole{opacity:1}
.hbtn.hpeers{padding-left:10px;padding-right:11px;gap:5px;font-variant-numeric:tabular-nums}
.hmon{width:13px;height:13px;display:block;flex:0 0 auto}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
/* Header buttons are icons. Words in this row are the first thing to run out of space on a
   phone, and every one of them has an icon that says the same. */
.hlabel{display:none}
.hbtn.hbmc i{display:block;width:14px;height:14px;background:currentColor;
  /* Assets are served with a day of cache, so a file that changes shape between builds is
     still the old one in an already-running webview. The query makes the URL change with it. */
  -webkit-mask:url(/assets/bmc-mark.png?v=131) center/contain no-repeat;
          mask:url(/assets/bmc-mark.png?v=131) center/contain no-repeat;
  transition:background-color .15s ease}
.hbtn.hbmc:hover i{background:#FFDD00}
.hbtn:disabled{opacity:.28;cursor:default;border-color:var(--border);color:var(--t3)}
.hbtn:disabled:hover{border-color:var(--border);color:var(--t3)}
.modal-bg{position:fixed;z-index:400;inset:0;display:none;align-items:center;justify-content:center;padding:20px;background:rgba(0,0,0,.72);backdrop-filter:blur(8px)}
.modal-bg.show{display:flex}
.about{width:min(520px,100%);border:1px solid var(--accent);border-radius:16px;background:rgba(18,20,38,.99);box-shadow:0 24px 80px rgba(0,0,0,.55);padding:22px}
.about-head{display:flex;align-items:center;gap:15px;margin-bottom:15px}.about-head img{width:76px;height:76px;border-radius:17px;object-fit:contain}.about h2{font-size:20px}.about .ver{font-size:11px;color:var(--accent2);margin-top:4px}
.about p{font-size:12px;line-height:1.65;color:var(--t2);margin:9px 0}.about strong{color:var(--t1)}
.about .close{width:100%;margin-top:10px;padding:9px;border-radius:8px;border:1px solid var(--accent);background:rgba(124,92,255,.15);color:var(--t1);cursor:pointer}
/* Support block. The QR is here because the desktop window cannot hand a link to a phone,
   and the phone is where people actually pay for things. */
.about-link{color:var(--accent);text-decoration:underline;text-underline-offset:2px}
.devpanel{position:fixed;z-index:200;top:56px;right:74px;width:290px;background:rgba(18,20,38,.98);border:1px solid var(--accent);border-radius:11px;padding:12px;display:none;backdrop-filter:blur(20px)}
.devpanel.show{display:block}
.dp-h{font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:var(--t3);margin-bottom:6px}
.dp-row{display:flex;gap:6px}
.devpanel input{flex:1;min-width:0;background:rgba(0,0,0,.3);border:1px solid var(--border);border-radius:6px;color:var(--t1);font-size:12px;padding:6px 8px;outline:none;font-family:monospace}
.devpanel input:focus{border-color:var(--accent)}
.devpanel .dp-row button{padding:6px 11px;border-radius:6px;border:1px solid var(--accent);background:rgba(124,92,255,.15);color:var(--accent2);font-size:11px;cursor:pointer;white-space:nowrap}
.dp-list{display:flex;flex-direction:column;gap:5px;max-height:170px;overflow:auto;margin-top:4px}
.dp-dev{display:flex;align-items:center;gap:8px;padding:7px 9px;border:1px solid var(--border);border-radius:7px;cursor:pointer}
.dp-dev:hover{border-color:var(--accent);background:rgba(124,92,255,.08)}
.dp-dev .nm{font-weight:600;color:var(--t1);font-size:12px} .dp-dev .meta{font-size:10px;color:var(--t2);font-family:monospace}
.dp-dev.cur{border-color:var(--green)}
.dp-empty{font-size:11px;color:var(--t3);padding:6px;line-height:1.5}
.dp-phone{font-size:11px;color:var(--t2);line-height:1.5}
.dp-url{font-family:monospace;font-size:15px;font-weight:600;color:var(--green);background:rgba(0,230,118,.08);
  border:1px solid rgba(0,230,118,.3);border-radius:7px;padding:8px 10px;margin:5px 0;text-align:center;cursor:pointer;user-select:all}
.dp-phone-hint{font-size:10px;color:var(--t3)}
#peerpanel{right:160px;width:300px}
.pcdot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);vertical-align:middle;margin-right:5px}
@media (max-width:900px){
  body{padding-bottom:calc(20px + env(safe-area-inset-bottom));-webkit-tap-highlight-color:transparent}
  /* Three unambiguous rows instead of one overflowing strip: identity + state + power on top,
     presets next, everything secondary last. Previously all seven right-hand controls sat on
     one line, so the power button — the one control that must always be reachable — was the
     first thing pushed off the screen edge. */
  .hdr{height:auto;min-height:62px;flex-wrap:wrap;gap:8px 6px;padding:calc(8px + env(safe-area-inset-top)) 10px 8px}
  .logo{order:1;flex:1 1 auto;min-width:0}.logo-t p{display:none}
  .hstatus{order:2;gap:8px}
  .hmid{order:3;flex-basis:100%;justify-content:flex-start;gap:8px;overflow-x:auto;padding-bottom:2px}
  .hright{order:4;flex-basis:100%;gap:6px;overflow-x:auto;padding-bottom:2px;justify-content:flex-start}
  /* One height for every button in the row, so the shapes stop looking arbitrary, and a
     40px minimum so each is a comfortable touch target. */
  .hright .hbtn{min-height:40px;min-width:44px;padding:0 10px;display:inline-flex;align-items:center;
    justify-content:center;gap:5px;flex:0 0 auto;font-size:13px}
  .hbtn.history{width:44px;min-width:44px;padding:0;font-size:19px}
  .hright .hpair{min-height:40px}
  .hright .hpair>.hbtn{min-height:0}
  .pill{padding:8px 11px;font-size:12px;white-space:nowrap;flex:0 0 auto}
  #stxt{display:none}#stxtShort{display:inline}
  .pbtn{width:44px;height:40px;font-size:14px}
  /* Save sits at the far edge, away from the numbered recall buttons: they do opposite
     things, and side by side a thumb aiming for one can catch the other. */
  .hmid{justify-content:flex-start}
  .savebtn{height:40px;padding:0 16px;font-size:13px;margin-left:auto;flex:0 0 auto}
  .pwr{width:44px;height:44px;flex:0 0 auto}
  .plabel{margin-right:2px}
  /* The device panel offers the phone's own address, a manual host-level connect, and a
     discovered-device list. On a phone the first is where you already are and the other two
     retarget the host controller for everybody — nothing there is useful from a phone, and
     the connect button is actively harmful if brushed. Connection state stays visible in the
     status pill and the controllers panel. */
  #devBtn{display:none}
  .toolbar{padding:10px;gap:7px;overflow-x:auto;flex-wrap:nowrap}.toolbar .spacer{display:none}
  .tbtn,.seg button{min-height:40px;white-space:nowrap}.offline-banner,.handover-banner,.standby-banner{margin:0 10px 8px}
  .wrap{padding:4px 10px 0;gap:10px;flex-direction:column}.gridcol{width:100%}
  .grid.l-2x16,.grid.l-wrap{grid-template-columns:repeat(auto-fill,minmax(94px,1fr));gap:8px}
  .strip{min-height:264px;padding:14px 6px 11px;gap:7px}.ctint{height:12px}.cname{font-size:12px;min-height:24px}
  .cnum{font-size:9px}.vu{width:20px;height:86px}.knob{width:48px;height:48px}.knob .ind{height:15px;transform-origin:50% 20px}
  .gval{font-size:17px}.ph,.itype{width:58px;height:32px;min-width:58px;min-height:32px;font-size:10px}
  .notes{top:calc(112px + env(safe-area-inset-top));width:min(88vw,320px);padding:12px}.notes textarea{min-height:140px}.toast{left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));text-align:center}
  /* Card controls get the same 40px touch floor as the header buttons. */
  .card-new{min-height:40px}
  .card-title{min-height:32px;font-size:13px}
  .card-body{min-height:80px;font-size:12px}
  .card-del{min-width:36px;min-height:36px;font-size:18px}
  body.notes-open .notes-tab{right:min(88vw,320px)}
  .devpanel,#peerpanel{top:calc(112px + env(safe-area-inset-top));left:8px;right:8px;width:auto;max-height:65vh;overflow:auto}
  .palette{grid-template-columns:repeat(4,32px);padding:10px}.sw{width:32px;height:32px}
}
/* Touch sizing applies to every small screen, but stacking into three rows should not: at
   this width the whole strip fits on one line, and a landscape phone has height to spare
   least of all. */
@media (max-width:900px) and (min-width:620px){
  .hdr{flex-wrap:nowrap;gap:10px}
  /* The logo must be the part that yields. Left growable it stays at full width and the
     preset group is pushed over the top of it. */
  .logo{order:1;flex:0 1 auto;min-width:0;overflow:hidden}
  .logo-t h1{white-space:nowrap}
  .plabel{display:none}                     /* the numbered buttons are self-explanatory */
  .hmid{order:2;flex:1 1 auto;flex-basis:auto;justify-content:center;overflow:visible;padding-bottom:0}
  .hright{order:3;flex:0 0 auto;flex-basis:auto;overflow:visible;padding-bottom:0}
  .hstatus{order:4}
}
@media (max-width:430px){
  .logo-i{width:32px;height:32px}.logo-t h1{font-size:14px}
  /* Word labels become icons: "About" and "⚙ Device" were the two widest items and the reason
     the row could not fit. The title attributes still carry the full names. */
  .hicon{display:inline}.hlabel{display:none}
  #aboutBtn,#devBtn{min-width:44px;padding:0}
  .hright .hbtn{font-size:12px}
  .grid.l-2x16,.grid.l-wrap{grid-template-columns:repeat(3,minmax(0,1fr))}
}
</style>
</head>
<body>

<header class="hdr">
  <div class="logo"><img class="logo-i" src="/app-icon.png" alt="MP32 Control"><div class="logo-t"><h1>MP32</h1><p>Independent Control Panel</p></div></div>
  <div class="hmid">
    <span class="plabel">Preset</span>
    <button class="pbtn active" id="pb1" onclick="recallPreset(1)">1</button>
    <button class="pbtn" id="pb2" onclick="recallPreset(2)">2</button>
    <button class="pbtn" id="pb3" onclick="recallPreset(3)">3</button>
    <button class="savebtn" onclick="savePreset()">Save</button>
  </div>
  <div class="hright">
    <button class="hbtn history" id="undoBtn" onclick="undoChange()" title="Undo (Ctrl/Cmd+Z)" aria-label="Undo" disabled>↶</button>
    <button class="hbtn history" id="redoBtn" onclick="redoChange()" title="Redo (Ctrl/Cmd+Shift+Z)" aria-label="Redo" disabled>↷</button>
    <button class="hbtn" id="gainModeBtn" onclick="toggleGainMode()" title="Gain display: classic dB ↔ raw device value">dB</button>
    <div class="hpair">
      <button class="hbtn" id="aboutBtn" onclick="toggleAbout(true)" title="About MP32 Control"><span class="hicon">ⓘ</span><span class="hlabel">About</span></button>
      <a class="hbtn hbmc" id="bmcBtn" href="https://buymeacoffee.com/franckreisner" target="_blank" rel="noopener noreferrer" title="Buy me a coffee" aria-label="Buy me a coffee"><i aria-hidden="true"></i></a>
    </div>
    <div class="hpair">
      <button class="hbtn hstate" id="devBtn" onclick="toggleDevPanel(event)" title="Device / connection"><span class="dot" id="dot"></span><svg class="hrole" id="hrole" viewBox="0 0 16 16" aria-hidden="true"><rect x="4.5" y="1.6" width="7" height="12.8" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.9 12.4h2.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><span id="stxt" class="sr">Connecting…</span><span id="stxtShort" class="sr">Connecting</span></button>
      <button class="hbtn hpeers" id="peersBtn" onclick="togglePeersPanel(event)" title="Controllers online"><svg class="hmon" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.6" y="2.4" width="12.8" height="8.6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 11v2.2M5.6 13.6h4.8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg><span id="peerCount">1</span></button>
    </div>
  </div>
  <div class="hstatus">
    <button class="pwr" id="pwr" onclick="togglePower()" title="Standby / Power">⏻</button>
  </div>
</header>

<div class="devpanel" id="peerpanel" onclick="event.stopPropagation()">
  <div class="dp-h">Controllers on the network</div>
  <div id="peerList" class="dp-list"></div>
  <div class="dp-empty" style="margin-top:8px">Gain, 48V, input type, names, colours, groups and Public Notes sync automatically. Local Notes stay private on this browser.</div>
</div>

<div class="devpanel" id="devpanel" onclick="event.stopPropagation()">
  <div class="dp-h">Phone / tablet access</div>
  <div class="dp-phone">
    <div>Permanent address on the same Wi-Fi:</div>
    <div class="dp-url" id="dpUrl" onclick="copyServerUrl()" title="Click to copy">—</div>
    <div class="dp-phone-hint">Automatically follows the active Mac or Windows host · Safari → Share → Add to Home Screen</div>
    <div style="margin-top:8px">Direct fallback address:</div>
    <div class="dp-phone-hint" id="dpDirect">—</div>
  </div>
  <div class="dp-h" style="margin-top:12px">Connect to device</div>
  <div class="dp-row">
    <input id="dpIp" placeholder="192.168.1.100" spellcheck="false">
    <input id="dpPort" placeholder="2028" style="flex:0 0 56px">
    <button onclick="connectManual()">Connect</button>
  </div>
  <div class="dp-h" style="margin-top:10px">Discovered on network</div>
  <div id="dpList" class="dp-list"><div class="dp-empty">Searching…</div></div>
</div>

<div class="toolbar">
  <button class="tbtn always iconbtn" id="layoutToggle" onclick="toggleLayout()" title="Switch to responsive wrap" aria-label="Toggle channel layout">
    <span class="layout-icon" id="layoutIcon">▦</span><span class="sr-only" id="layoutLabel">2 × 16 layout</span>
  </button>
  <button class="tbtn always iconbtn" id="groupBtn" onclick="groupAction('offset')" title="Group channels" aria-label="Group channels">
    <img src="/assets/group.png" alt=""><span class="sr-only" id="groupLabel">Group</span>
  </button>
  <button class="tbtn always iconbtn" id="linkBtn" onclick="groupAction('link')" title="Stereo Link" aria-label="Stereo Link">
    <img src="/assets/stereo-link.png" alt=""><span class="sr-only" id="linkLabel">Stereo Link</span>
  </button>
  <button class="tbtn always iconbtn" onclick="loadFile()" title="Load file" aria-label="Load file"><img src="/assets/load.png" alt=""></button>
  <button class="tbtn always iconbtn" onclick="saveFile()" title="Save file" aria-label="Save file"><img src="/assets/save.png" alt=""></button>
  <button class="tbtn ph" id="phAll" onclick="toggleAllPhantom()">48V All</button>
  <div class="spacer"></div>
  <button class="tbtn warn" onclick="cleanSlate()"><span>↺</span> Reset all</button>
  <input type="file" id="fileInput" accept="application/json" style="display:none">
</div>

<div class="offline-banner">🔒 Offline — device not connected. Controls are locked; no change will reach the device.</div>
<div class="handover-banner"><span class="handover-spin"></span><span id="handoverText">Re-establishing connection · handover in progress</span></div>
<div class="standby-banner">⏻ Device is in STANDBY — the preamp is powered down. Audio is not passing. Press the power button to switch it on.</div>

<div class="wrap">
  <div class="gridcol">
    <div class="grid l-2x16" id="grid"></div>
  </div>
  <div class="notes">
    <h3>Notes <button class="notes-close" onclick="toggleNotes(false)" title="Close notes">×</button></h3>
    <div class="notes-tabs">
      <button class="note-tab on" id="notesLocalTab" onclick="setNotesMode('local')">Local</button>
      <button class="note-tab" id="notesPublicTab" onclick="setNotesMode('public')">Public</button>
    </div>
    <textarea id="notes" aria-label="Local notes"></textarea>
    <div id="publicCards" class="cards hide">
      <button class="card-new" onclick="newCard()">+ New card</button>
      <div id="cardList"></div>
      <div class="card-empty" id="cardEmpty">No shared cards yet. Create one — everyone on the
        network sees it, and one person edits a card at a time.</div>
    </div>
    <div class="hint" id="notesHint">Auto-saved to this browser · private</div>
  </div>
</div>
<button class="notes-tab" id="notesTab" onclick="toggleNotes()" title="Open notes">Notes</button>

<div class="toast" id="toast"></div>
<div class="palette" id="palette"></div>
<div class="modal-bg" id="aboutModal" onclick="if(event.target===this)toggleAbout(false)">
  <section class="about" role="dialog" aria-modal="true" aria-labelledby="aboutTitle">
    <div class="about-head"><img src="/app-icon.png" alt="MP32 Control icon"><div><h2 id="aboutTitle">MP32 Control</h2><div class="ver">Version 1.3.2 · Custom control panel</div></div></div>
    <p><strong>MP32 Control</strong> is an independent remote control application for the Antelope Audio MP32 32-channel microphone preamplifier.</p>
    <p>It discovers the MP32 on the local network and controls gain, 48 V phantom power, input type, presets and live VU metering. Channel names, colours, groups, stereo links and Public Notes can be shared between desktop and mobile controllers on the same LAN.</p>
    <p>The app runs natively on macOS and Windows and also serves a phone/tablet web interface. It communicates directly with compatible hardware over the local network.</p>
    <p><strong>Built by Franck Reisner.</strong> If it saved you some time, the cup beside
    this button will <a class="about-link" href="https://buymeacoffee.com/franckreisner" target="_blank" rel="noopener noreferrer">buy me a coffee</a>.</p>
    <p>This is an independent project and is not affiliated with or endorsed by Antelope Audio.</p>
    <button class="close" onclick="toggleAbout(false)">Close</button>
  </section>
</div>

<script>
const N = 32;
const TYPES = ['mic','line','hiz'], TLABEL = {mic:'MIC',line:'LINE',hiz:'HI-Z'};
const COLORS = ['', '#ff5252','#ff9a00','#ffeb3b','#00e676','#4fc3f7','#7c5cff','#ff7ac8',
  '#00c7b7','#9be15d','#536dfe','#b0bec5'];

// ── Host failover ────────────────────────────────────────────────────────────
// The page is normally loaded from the stable hostname, so relative URLs follow whichever
// controller currently owns it. That alone is enough *if* the phone re-resolves the name
// promptly — but a host that crashes never withdraws its mDNS record, and iOS caches
// resolutions well past their TTL. So we also remember the controllers we have seen and, when
// the current base stops answering, try them by address. Cross-origin is fine: the API sends
// Access-Control-Allow-Origin and answers preflight.
let apiBase='';                    // '' = this page's own origin
let knownHosts=[];                 // absolute bases learned from /api/peers
let failoverIdx=0, lastOriginTry=0;
const HOST_PORT=location.port||'8765';

function rememberHosts(peers){
  (peers||[]).forEach(p=>{
    if(!p.ip) return;
    const base='http://'+p.ip+':'+HOST_PORT;
    if(base!==location.origin && !knownHosts.includes(base)) knownHosts.push(base);
  });
}
function apiUrl(path){ return apiBase+path; }

async function probeBase(base){
  try{
    const c=new AbortController(); const t=setTimeout(()=>c.abort(),1200);
    const r=await fetch(base+'/api/status?_='+Date.now(),{cache:'no-store',signal:c.signal});
    clearTimeout(t);
    return r.ok;
  }catch(e){ return false; }
}

async function tryFailover(){
  // Always prefer the page's own origin: it is the stable hostname, so returning to it keeps
  // every controller and the PWA agreeing on one address once the name points somewhere live.
  if(apiBase!=='' && Date.now()-lastOriginTry>5000){
    lastOriginTry=Date.now();
    if(await probeBase('')){ apiBase=''; return true; }
  }
  if(!knownHosts.length) return false;
  for(let i=0;i<knownHosts.length;i++){
    const base=knownHosts[(failoverIdx+i)%knownHosts.length];
    if(base===apiBase) continue;
    if(await probeBase(base)){
      failoverIdx=(failoverIdx+i)%knownHosts.length;
      apiBase=base;
      const txt=document.getElementById('handoverText');
      if(txt) txt.textContent='Reconnected via '+base.replace('http://','');
      return true;
    }
  }
  return false;
}

let st = { connected:false, power_on:false, current_preset:1, device_ip:'—',
  config: Array.from({length:N}, ()=>({gain:0,phantom:0,zerocross:1,pretype:0})),
  peaks: Array(N).fill(99), device_info:{}, demo:false, controller_role:'desktop',
  connection_state:'waiting_for_web_host', connection_error:'', unreachable_hosts:[] };

// ── Local metadata (names, colors, notes, layout) — persisted in this browser ──
let meta = loadMeta();
function loadMeta(){
  try{ return Object.assign({names:{},colors:{},layout:'2x16',_ts:{}}, JSON.parse(localStorage.getItem('mp32_meta')||'{}')); }
  catch(e){ return {names:{},colors:{},layout:'2x16',_ts:{}}; }
}
meta.cards = meta.cards || {};   // shared cards, keyed by id
meta.locks = meta.locks || {};   // advisory edit leases, keyed by card id
function saveMeta(){ localStorage.setItem('mp32_meta', JSON.stringify(meta)); }

let hold = Array(N).fill(0), holdTmr = Array(N).fill(null), holdDb = Array(N).fill(-Infinity);
let pending = {}, wasConnected = null;

// ── Bounded history for device settings (gain / 48V / input type) ──
const HISTORY_LIMIT = 8;
let undoStack = [], redoStack = [], historyBusy = false;
const gainBeforeZero = Array(N).fill(null);
function configSnapshot(){
  return st.config.map(c=>({gain:+(c.gain||0),phantom:c.phantom?1:0,pretype:+(c.pretype||0)}));
}
function snapshotEqual(a,b){
  return !!a && !!b && a.length===b.length && a.every((c,i)=>
    c.gain===b[i].gain && c.phantom===b[i].phantom && c.pretype===b[i].pretype);
}
function updateHistoryButtons(){
  const u=document.getElementById('undoBtn'), r=document.getElementById('redoBtn');
  if(u){ u.disabled=historyBusy || !st.connected || !undoStack.length; u.title=`Undo (${undoStack.length}/${HISTORY_LIMIT}) · Ctrl/Cmd+Z`; }
  if(r){ r.disabled=historyBusy || !st.connected || !redoStack.length; r.title=`Redo (${redoStack.length}/${HISTORY_LIMIT}) · Ctrl/Cmd+Shift+Z`; }
}
function commitHistory(before){
  if(historyBusy || snapshotEqual(before,configSnapshot())) return;
  undoStack.push(before);
  if(undoStack.length>HISTORY_LIMIT) undoStack.shift();
  redoStack=[];
  updateHistoryButtons();
}
async function applyHistorySnapshot(target,label){
  if(historyBusy || !st.connected) return;
  historyBusy=true; updateHistoryButtons();
  const current=configSnapshot(), changed=[];
  for(let i=0;i<N;i++) if(!snapshotEqual([current[i]],[target[i]])) changed.push(i);
  changed.forEach(i=>{ pending[i]=true; st.config[i]={...st.config[i],...target[i]}; });
  applyState();
  // Keep commands ordered for each channel: type defines the valid gain range.
  for(const i of changed){
    if(current[i].pretype!==target[i].pretype) await sendType(i,target[i].pretype);
    if(current[i].gain!==target[i].gain) await sendGain(i,target[i].gain);
    if(current[i].phantom!==target[i].phantom) await sendPhantom(i,target[i].phantom);
  }
  changed.forEach(i=>delete pending[i]);
  historyBusy=false; applyState(); updateHistoryButtons();
  toast(`${label} · ${changed.length} channel${changed.length===1?'':'s'}`);
}
async function undoChange(){
  if(historyBusy || !undoStack.length || !st.connected) return;
  const target=undoStack.pop(); redoStack.push(configSnapshot());
  if(redoStack.length>HISTORY_LIMIT) redoStack.shift();
  await applyHistorySnapshot(target,'Undo');
}
async function redoChange(){
  if(historyBusy || !redoStack.length || !st.connected) return;
  const target=redoStack.pop(); undoStack.push(configSnapshot());
  if(undoStack.length>HISTORY_LIMIT) undoStack.shift();
  await applyHistorySnapshot(target,'Redo');
}

// ── Groups: meta.groups = { gid: {mode:'link'|'offset', color, members:[idx...]} } ──
if(!meta.groups) meta.groups = {};
(function migrateGroups(){
  if(localStorage.getItem('mp32_groups_migrated')) return;
  // Re-announce each existing group under its own key. The gid is already unique and stable,
  // so two browsers migrating the same data converge rather than creating duplicates.
  Object.keys(meta.groups).forEach(gid=>{
    if(!meta._ts['group:'+gid]) meta._ts['group:'+gid] = meta._ts.groups || 0;
  });
  localStorage.setItem('mp32_groups_migrated','1');
})();
if(!meta._ts)    meta._ts = {};
let groupMode = null, selected = new Set(); // null | 'offset' | 'link'
let paramTs = {}; // transient LWW clocks for device params; never persisted across app restarts

// ── Hybrid Logical Clock for metadata LWW ──────────────────────────────────────
// Wall-clock Date.now() timestamps break convergence across machines with skewed
// clocks: a controller whose clock runs ahead always wins, so the other side's
// later edits are silently rejected. The HLC packs (physical_ms, logical_counter)
// into one sortable integer (pt*65536 + lc) and bumps on every timestamp it sees,
// so all connected controllers converge to the network maximum regardless of skew.
// It is persisted so a reopened controller never restarts below the value it last
// observed on the LAN. Pre-HLC raw-ms timestamps are tiny next to any HLC value,
// so post-upgrade writes simply win — a safe one-way migration.
let hlc = +(localStorage.getItem('mp32_hlc')||0);
function hlcSave(){ try{ localStorage.setItem('mp32_hlc', String(hlc)); }catch(e){} }
function hlcNow(){
  const now=Date.now(), ptOld=Math.floor(hlc/65536), lcOld=hlc%65536;
  let pt, lc;
  if(now>ptOld){ pt=now; lc=0; } else { pt=ptOld; lc=lcOld+1; }
  hlc=pt*65536+lc; hlcSave();
  return hlc;
}
function hlcUpdate(tsIn){
  if(!(tsIn>0)) return;
  const now=Date.now(), ptOld=Math.floor(hlc/65536), lcOld=hlc%65536;
  const ptIn=Math.floor(tsIn/65536), lcIn=tsIn%65536;
  const pt=Math.max(ptOld, ptIn, now);
  let lc;
  if(pt===ptOld && pt===ptIn) lc=Math.max(lcOld,lcIn)+1;
  else if(pt===ptOld) lc=lcOld+1;
  else if(pt===ptIn) lc=lcIn+1;
  else lc=0;
  hlc=pt*65536+lc; hlcSave();
}

// ── Metadata sync across computers (per-field last-write-wins; notes stay local) ──
function pushMeta(key, value){
  const ts=hlcNow();
  meta._ts[key]=ts; saveMeta();
  api('/api/meta_event', {key, value, ts});
}
function pushTypeMeta(i,value){
  const key='type:'+i, ts=hlcNow();
  paramTs[key]=ts;
  api('/api/meta_event',{key,value,ts});
}
function seedSharedMeta(){
  const send=(key,value)=>{
    let ts=meta._ts[key]||0;
    if(!ts){ ts=hlcNow(); meta._ts[key]=ts; }
    api('/api/meta_event',{key,value,ts});
  };
  Object.keys(meta.names||{}).forEach(i=>send('name:'+i,meta.names[i]||''));
  Object.keys(meta.colors||{}).forEach(i=>send('color:'+i,meta.colors[i]||''));
  Object.keys(meta.groups||{}).forEach(gid=>send('group:'+gid, meta.groups[gid]));
  Object.keys(meta.cards||{}).forEach(id=>send('card:'+id, meta.cards[id]));
  saveMeta();
}
async function syncMeta(){
  try{
    const r=await fetch(apiUrl('/api/meta_state')); const j=await r.json();
    const f=(j&&j.fields)||{}; let changed=false, cardsDirty=false;
    for(const key in f){
      const v=f[key], isType=key.indexOf('type:')===0;
      hlcUpdate(v.ts);   // track the network clock even for keys we end up rejecting
      const localTs=isType?(paramTs[key]||0):(meta._ts[key]||0);
      if(localTs >= v.ts) continue;
      const isParam = key.indexOf('gain:')===0 || key.indexOf('ph:')===0 || key.indexOf('type:')===0;
      // In real mode every device parameter is authoritative on the live device session.
      // Peer parameter events are demo-only and must never replay stale hardware state.
      if(isParam && !st.demo) continue;
      if(isType) paramTs[key]=v.ts; else meta._ts[key]=v.ts;
      changed=true;
      if(key.indexOf('name:')===0){ const i=+key.slice(5); if(v.value) meta.names[i]=v.value; else delete meta.names[i]; }
      else if(key.indexOf('color:')===0){ const i=+key.slice(6); if(v.value) meta.colors[i]=v.value; else delete meta.colors[i]; }
      else if(key.indexOf('group:')===0){ meta.groups[key.slice(6)]=v.value||{}; }
      else if(key==='groups'){
        // An older controller still sends the whole object. Merge it instead of replacing, so
        // its view cannot wipe groups it has never heard of.
        const incoming=v.value||{};
        Object.keys(incoming).forEach(gid=>{ if(!meta.groups[gid]) meta.groups[gid]=incoming[gid]; });
      }
      else if(key.indexOf('card:')===0){ meta.cards[key.slice(5)]=v.value||{}; cardsDirty=true; }
      else if(key.indexOf('lock:')===0){ meta.locks[key.slice(5)]=v.value||{}; cardsDirty=true; }
      else if(key==='public_notes'){
        // Retained so an older controller's notes still arrive; migrated into a card.
        localStorage.setItem('mp32_public_notes',v.value||'');
      }
      else if(key.indexOf('gain:')===0){ const i=+key.slice(5); if(st.config[i]){ st.config[i].gain=v.value; api('/api/set_gain',{idx:i,gain:v.value}); } }
      else if(key.indexOf('ph:')===0){ const i=+key.slice(3); if(st.config[i]){ st.config[i].phantom=v.value; api('/api/set_phantom',{idx:i,phantom:v.value}); } }
      else if(key.indexOf('type:')===0){
        const i=+key.slice(5), type=+v.value;
        if(st.config[i]){
          st.config[i].pretype=type;
          st.config[i].gain=Math.min(st.config[i].gain??0,maxRaw(type));
          sendType(i,type,false);
        }
      }
    }
    if(changed){ saveMeta(); applyState(); }
    // Only redraw when idle: a rebuild mid-edit would replace the focused field.
    if(cardsDirty && !editingCard && document.body.classList.contains('notes-open')) renderCards();
  }catch(e){}
}
setInterval(syncMeta, 80); // local backend poll; gives other controllers smooth demo updates
// Each group is its own synced key, `group:<gid>`. A single shared `groups` object could not
// merge: last-write-wins on the whole object meant whichever controller wrote last silently
// discarded every group the other had. Per-group keys let two people group different channels
// at the same time, and cost a lost group at worst instead of all of them.
function liveGroupIds(){ return Object.keys(meta.groups).filter(g=>meta.groups[g] && !meta.groups[g].deleted); }
function pushGroup(gid){ if(meta.groups[gid]) pushMeta('group:'+gid, meta.groups[gid]); }
function tombstoneGroup(gid){
  // Deleted by writing a tombstone, never by removing the key: absence must not mean deleted,
  // or a controller that has never seen a group would erase it for everyone.
  const g=meta.groups[gid]; if(!g || g.deleted) return;
  meta.groups[gid]={...g, members:[], deleted:true};
  pushMeta('group:'+gid, meta.groups[gid]);
}
function groupOf(i){ for(const gid of liveGroupIds()){ if(meta.groups[gid].members.includes(i)) return {gid, g:meta.groups[gid]}; } return null; }
function removeFromGroup(i){
  const r=groupOf(i); if(!r) return;
  r.g.members=r.g.members.filter(x=>x!==i);
  if(r.g.members.length<2) tombstoneGroup(r.gid); else pushGroup(r.gid);
}
function selectedGroup(){
  if(!selected.size) return null;
  const r=groupOf([...selected][0]);
  if(!r || r.g.members.length!==selected.size || !r.g.members.every(i=>selected.has(i))) return null;
  return r;
}
function clearGroupSelection(){
  selected.clear();
  document.querySelectorAll('.strip.sel').forEach(s=>s.classList.remove('sel'));
}
function selectMembers(members){
  clearGroupSelection();
  members.forEach(i=>{ selected.add(i); document.getElementById(`s${i}`)?.classList.add('sel'); });
}
function updateGroupControls(){
  const existing=selectedGroup();
  const gb=document.getElementById('groupBtn'), lb=document.getElementById('linkBtn');
  const groupOn=groupMode==='offset' || (!groupMode && existing?.g.mode==='offset');
  const linkOn=groupMode==='link' || (!groupMode && existing?.g.mode==='link');
  gb.className='tbtn always iconbtn'+(groupOn?(existing&&!groupMode?' unlink':' on'):'');
  lb.className='tbtn always iconbtn'+(linkOn?(existing&&!groupMode?' unlink':' on'):'');
  const groupText=existing?.g.mode==='offset'&&!groupMode?'Ungroup':'Group';
  const linkText=existing?.g.mode==='link'&&!groupMode?'Unlink':'Stereo Link';
  document.getElementById('groupLabel').textContent=groupText;
  document.getElementById('linkLabel').textContent=linkText;
  gb.title=groupText; gb.setAttribute('aria-label',groupText);
  lb.title=linkText; lb.setAttribute('aria-label',linkText);
  document.body.classList.toggle('groupmode', !!groupMode);
}
function groupAction(mode){
  const existing=selectedGroup();
  if(!groupMode && existing && existing.g.mode===mode){
    const members=[...existing.g.members];
    tombstoneGroup(existing.gid);
    members.forEach(i=>delete meta.colors[i]);
    saveMeta(); members.forEach(i=>pushMeta('color:'+i,''));
    clearGroupSelection(); updateGroupControls(); applyState();
    toast(mode==='link'?'Stereo link removed':'Group removed'); return;
  }
  if(groupMode===mode){
    if(selected.size<2){ groupMode=null; clearGroupSelection(); updateGroupControls(); toast('Selection cancelled'); return; }
    if(mode==='link' && selected.size!==2){ toast('Stereo Link needs exactly 2 channels'); return; }
    createGroup(mode); return;
  }
  groupMode=mode; clearGroupSelection(); updateGroupControls();
  toast(mode==='link'?'Select 2 channels, then press Stereo Link again':'Select channels, then press Group again');
}
function stripClick(i,e){
  if(!groupMode){
    if(e && e.target.closest('.knob,.ph,.itype,.cname,.ctint')) return;
    const r=groupOf(i);
    if(r) selectMembers(r.g.members); else clearGroupSelection();
    updateGroupControls(); return;
  }
  if(selected.has(i)) selected.delete(i);
  else {
    if(groupMode==='link' && selected.size>=2){ toast('Stereo Link allows exactly 2 channels'); return; }
    selected.add(i);
  }
  document.getElementById(`s${i}`).classList.toggle('sel', selected.has(i));
  updateGroupControls();
}
function createGroup(mode){
  if(selected.size<2){ toast('Select at least 2 channels'); return; }
  if(mode==='link' && selected.size!==2){ toast('Stereo link = exactly 2 channels'); return; }
  const members=[...selected].sort((a,b)=>a-b);
  members.forEach(removeFromGroup);
  const color=COLORS[1 + (liveGroupIds().length % (COLORS.length-1))];
  const gid='g'+Date.now()+Math.floor(Math.random()*1000);
  meta.groups[gid]={mode, color, members};
  members.forEach(i=>{ meta.colors[i]=color; });
  saveMeta(); groupMode=null; selectMembers(members);
  pushGroup(gid); members.forEach(i=>pushMeta('color:'+i, color));
  updateGroupControls(); applyState();
  toast(mode==='link'?'Stereo link created ⛓':'Group created ⛓');
}
function setGainLocal(j, v){ st.config[j].gain=v; drawGain(j, v, st.config[j].pretype??0); }

// ── Gain raw <-> display calibration, verified through device testing ──
const GAIN_MAX_RAW = [56,29,36];
function maxRaw(m){ return GAIN_MAX_RAW[m] ?? 56; }
function rawToDb(raw,m){ if(m===0) return raw===0?5:raw+12; if(m===1) return raw-9; if(m===2) return raw+4; return raw; }
function rgba(hex,a){
  if(!hex || hex[0]!=='#') return `rgba(124,92,255,${a})`;
  const n=parseInt(hex.slice(1),16); return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}

// ── Build strips ──
function build(){
  const g = document.getElementById('grid'); g.innerHTML='';
  for(let i=0;i<N;i++){
    const d=document.createElement('div');
    d.className='strip'; d.id=`s${i}`;
    d.innerHTML=`
      <div class="ctint" id="ct${i}" onclick="openPalette(event,${i})" title="Pick colour"></div>
      <input class="cname" id="cn${i}" maxlength="10" onchange="onName(${i},this.value)" spellcheck="false">
      <div class="cnum">CH ${String(i+1).padStart(2,'0')}</div>
      <div class="vu">
        <div class="fill" id="vf${i}"></div><div class="hold" id="vh${i}"></div>
        <div class="tick" style="bottom:80%"></div><div class="tick" style="bottom:60%"></div><div class="tick" style="bottom:40%"></div><div class="tick" style="bottom:20%"></div>
        <div class="vusc"><span style="bottom:95%">0</span><span style="bottom:80%">-12</span><span style="bottom:60%">-24</span><span style="bottom:40%">-36</span><span style="bottom:20%">-48</span></div>
      </div>
      <div class="vupk" id="vp${i}">&minus;&infin;</div>
      <div class="knob" id="kn${i}" data-i="${i}"><div class="ind" id="ki${i}"></div></div>
      <div class="gval" id="gv${i}">0</div><div class="gunit">dB</div>
      <button type="button" class="ph" id="ph${i}" onclick="event.stopPropagation();togglePh(${i})" aria-label="Channel ${i+1} 48 volt phantom power"><span>48V</span></button>
      <button type="button" class="itype mic${i<4?' hizcap':''}" id="it${i}" onclick="event.stopPropagation();cycleType(${i})" title="${i<4?'Mic / Line / Hi-Z':'Mic / Line'}" aria-label="Channel ${i+1} input type"><span>MIC</span></button>
      <div class="glink" id="gl${i}"></div>
    `;
    d.onclick = e=>stripClick(i,e);
    g.appendChild(d);
  }
  for(let i=0;i<N;i++) attachKnob(i);
  setLayout(meta.layout, true);
  updateGroupControls();
}

// The desktop application opens its own window on the loopback address; anything else is
// a phone, a tablet or another computer looking at the panel over the network.
const isLocalView = location.hostname === '127.0.0.1' || location.hostname === 'localhost';

// ── Apply state to DOM ──
function applyState(){
  document.getElementById('dot').className = st.connected?'dot on':'dot off';
  let offlineLabel={waiting_for_web_host:'Waiting for Web Host…',discovering:'Finding device…',
    connecting:'Connecting to device…',loading_config:'Loading device config…',
    config_empty:'Device busy · retrying…',tcp_error:'Device unreachable · retrying…',
    disconnected:'Connection lost · retrying…'}[st.connection_state]||'Offline';
  // Name the blocked host instead of waiting silently: a controller that heartbeats but
  // cannot be reached on its HTTP port is almost always a local firewall on that machine.
  const blocked=(st.unreachable_hosts||[]).filter(h=>h.web_leader);
  if(st.connection_state==='waiting_for_web_host' && blocked.length){
    const h=blocked[0];
    offlineLabel='Web Host '+(h.host||h.ip)+' unreachable on '+h.port+' · taking over…';
  }
  document.getElementById('stxt').textContent = st.connected
      ? (st.power_on
          ? ((st.demo?'Demo':'Online · '+(st.controller_role==='web_host'?'Web Host':'Desktop'))+(st.device_info && st.device_info.firmware?' · fw '+st.device_info.firmware:''))
          : 'Standby · device powered down')
      : offlineLabel;
  // A phone has no room for the full sentence, and an ellipsis in the middle of "Web Host"
  // reads as a fault. Show the state itself and drop the qualifiers.
  document.getElementById('stxtShort').textContent = st.connected
      ? (st.power_on ? (st.demo?'Demo':'Online') : 'Standby')
      : (st.connection_state==='waiting_for_web_host' ? 'No host'
        : st.connection_state==='config_empty' ? 'Device busy'
        : st.connected===false && st.connection_state==='online' ? 'Offline'
        : {discovering:'Finding…',connecting:'Connecting…',loading_config:'Loading…',
           tcp_error:'No device',disconnected:'Lost'}[st.connection_state] || 'Offline');
  // The header shows a dot, not a sentence. The words still exist — they are the button's
  // tooltip and the Device panel's own heading — so nothing is lost, it just stops taking
  // 180 px of a row that has to survive a phone.
  document.getElementById('devBtn').title =
      document.getElementById('stxt').textContent + ' — click for device and connection';
  const isHost = st.controller_role === 'web_host';
  const dev = document.getElementById('devBtn');
  dev.classList.toggle('webhost', isHost);
  // Only in the desktop window: /api/status reports the host's role to every client, so a
  // phone would otherwise light this up and be told about itself.
  dev.classList.toggle('served', isHost && isLocalView && (st.web_clients || 0) > 0);

  document.getElementById('pwr').classList.toggle('on', !!st.power_on);   // preserves .armed
  // Only meaningful while genuinely connected: a disconnected panel has no idea what the
  // unit's power state is, and must not claim it is in standby.
  document.body.classList.toggle('standby', !!st.connected && !st.power_on);
  // Toggle rather than reassign className: this runs on every status poll, and rewriting the
  // whole attribute wiped the "armed" class within a few hundred ms, leaving the confirm step
  // working but invisible.
  for(let p=1;p<=3;p++){ const b=document.getElementById(`pb${p}`);
    if(b) b.classList.toggle('active', p===st.current_preset); }
  const anyPhantom=st.config.some(c=>!!c.phantom), phAll=document.getElementById('phAll');
  phAll.classList.toggle('on',anyPhantom);
  phAll.title=anyPhantom?'Turn all 48V off':'Turn all 48V on';

  document.body.classList.toggle('offline', !st.connected);
  if(st.connected!==wasConnected){
    if(wasConnected!==null) toast(st.connected?'Reconnected ✅':'Offline — controls locked 🔒');
    wasConnected = st.connected;
  }

  for(let i=0;i<N;i++){
    const cfg = st.config[i]||{};
    const mode = cfg.pretype??0, raw = cfg.gain??0;
    const name = meta.names[i]||'', color = meta.colors[i]||'';

    // name + color
    const cn=document.getElementById(`cn${i}`);
    if(cn && document.activeElement!==cn){
      cn.value = name || `CH ${String(i+1).padStart(2,'0')}`;
      cn.className = 'cname'+(name?'':' def');
    }
    const ct=document.getElementById(`ct${i}`); if(ct) ct.style.background = color||'rgba(124,92,255,.22)';
    const strip=document.getElementById(`s${i}`);
    if(strip) strip.style.background = color
      ? `linear-gradient(180deg,${rgba(color,.11)},${rgba(color,.025)} 62%),var(--strip)`
      : 'var(--strip)';

    // changed highlight
    const changed = raw!==0 || cfg.phantom || mode!==0 || name;
    document.getElementById(`s${i}`).classList.toggle('changed', !!changed);

    // gain knob + dB (skip while dragging)
    if(!(i in pending)) drawGain(i, raw, mode);

    // phantom + type
    const ph=document.getElementById(`ph${i}`);
    if(ph) ph.className='ph'+(cfg.phantom?' on':'')+(mode!==0?' hide':'');
    const it=document.getElementById(`it${i}`);
    if(it){ it.className='itype '+TYPES[mode]+(i<4?' hizcap':''); const label=it.querySelector('span'); if(label) label.textContent=TLABEL[TYPES[mode]]; }

    // group link bar
    const gr=groupOf(i), gl=document.getElementById(`gl${i}`);
    if(gl){ if(gr){ gl.className='glink on'; gl.style.background=gr.g.color; } else gl.className='glink'; }
  }
  if(!groupMode && selected.size && !selectedGroup()) clearGroupSelection();
  updateGroupControls();
  updateHistoryButtons();
}
let gainMode = (meta.gainMode==='raw') ? 'raw' : 'db';   // 'db' = classic formula, 'raw' = device value
function drawGain(i, raw, mode){
  const ang = -135 + (raw/maxRaw(mode))*270;
  const ki=document.getElementById(`ki${i}`), gv=document.getElementById(`gv${i}`);
  if(ki) ki.style.transform=`translateX(-50%) rotate(${ang}deg)`;
  if(gv) gv.textContent = (gainMode==='raw') ? raw : rawToDb(raw, mode);
}
function toggleGainMode(){
  gainMode = (gainMode==='db') ? 'raw' : 'db';
  meta.gainMode = gainMode; saveMeta();
  const b=document.getElementById('gainModeBtn'); if(b) b.textContent = (gainMode==='raw')?'raw':'dB';
  document.querySelectorAll('.gunit').forEach(e=>e.textContent = (gainMode==='raw')?'raw':'dB');
  for(let i=0;i<N;i++) drawGain(i, (st.config[i]&&st.config[i].gain)||0, (st.config[i]&&st.config[i].pretype)||0);
  toast(gainMode==='raw'?'Showing raw device values':'Showing classic dB');
}

// ── VU meters — updated at high rate for snappy, followable peaks ──
// Device value is dB below full scale: 0 = loudest, 60+ = silence.
const VU_FLOOR = 60;
function updateMeters(){
  for(let i=0;i<N;i++){
    const below = Math.max(0, Math.abs(Number(st.peaks[i])||0));
    const pct = Math.max(0, Math.min(100, (VU_FLOOR-below)/VU_FLOOR*100));
    const db = below>=VU_FLOOR ? -Infinity : -Math.round(below);
    if(pct > hold[i]){ hold[i]=pct; holdDb[i]=db; clearTimeout(holdTmr[i]); holdTmr[i]=setTimeout(()=>{hold[i]=0; holdDb[i]=-Infinity;},1600); }
    const vf=document.getElementById(`vf${i}`), vh=document.getElementById(`vh${i}`), vp=document.getElementById(`vp${i}`);
    if(vf) vf.style.height=pct+'%';
    if(vh) vh.style.bottom=hold[i]+'%';
    if(vp){
      const d=holdDb[i];
      vp.textContent = Number.isFinite(d) ? (d===0 ? '0' : String(d)) : '−∞';
      vp.className = 'vupk'+(d>=-6?' hot':(d>=-18?' warn':''));
    }
  }
}
async function pollPeaks(){
  if(!st.connected) return;
  try{ const r=await fetch(apiUrl('/api/peaks')); const j=await r.json(); if(j.peaks){ st.peaks=j.peaks; updateMeters(); } }catch(e){}
}
setInterval(pollPeaks, 40);   // ~25 fps — cheap on loopback; device source is ~10 Hz so this mainly cuts latency

// ── Knob drag (group-aware) ──
function attachKnob(i){
  const k=document.getElementById(`kn${i}`);
  k.addEventListener('pointerdown', e=>{
    if(!st.connected || groupMode) return;
    e.preventDefault(); k.setPointerCapture(e.pointerId);
    const historyBefore=configSnapshot();
    const mode=st.config[i].pretype??0, mx=maxRaw(mode);
    let startY=e.clientY, startRaw=st.config[i].gain??0, cur=startRaw;
    pending[i]=true;
    const r=groupOf(i), startVals={};
    if(r) r.g.members.forEach(j=>{ startVals[j]=st.config[j].gain??0; pending[j]=true; });
    let lastTx=0, lastTxRaw=null, txTimer=null, finished=false;
    const applyTo=nr=>{
      setGainLocal(i,nr);
      if(r){ const delta=nr-startRaw; r.g.members.forEach(j=>{ if(j===i) return;
        const mxj=maxRaw(st.config[j].pretype??0);
        const v = r.g.mode==='link' ? Math.min(nr,mxj) : Math.max(0,Math.min(mxj, startVals[j]+delta));
        setGainLocal(j,v); }); }
    };
    const transmit=force=>{
      const now=Date.now();
      const wait=80-(now-lastTx);
      if(!force && wait>0){
        clearTimeout(txTimer);
        txTimer=setTimeout(()=>transmit(true),wait);
        return;
      }
      clearTimeout(txTimer); txTimer=null;
      if(lastTxRaw===cur) return;
      lastTx=now; lastTxRaw=cur; sendGain(i,cur);
      if(r) r.g.members.forEach(j=>{ if(j!==i) sendGain(j,st.config[j].gain); });
    };
    const move=ev=>{
      let nr=Math.round(startRaw+(startY-ev.clientY)/3); nr=Math.max(0,Math.min(mx,nr));
      cur=nr; applyTo(nr); transmit(false);
    };
    const finish=ev=>{
      if(finished) return; finished=true;
      try{ if(k.hasPointerCapture(e.pointerId)) k.releasePointerCapture(e.pointerId); }catch(_){}
      k.removeEventListener('pointermove',move); k.removeEventListener('pointerup',finish);
      k.removeEventListener('pointercancel',finish); k.removeEventListener('lostpointercapture',finish);
      transmit(true); delete pending[i];
      if(r) r.g.members.forEach(j=>{ if(j!==i) delete pending[j]; });
      commitHistory(historyBefore);
    };
    k.addEventListener('pointermove',move); k.addEventListener('pointerup',finish);
    k.addEventListener('pointercancel',finish); k.addEventListener('lostpointercapture',finish);
  });
  k.addEventListener('wheel', e=>{
    if(!st.connected || groupMode) return;
    e.preventDefault();
    const historyBefore=configSnapshot();
    const mx=maxRaw(st.config[i].pretype??0), startRaw=st.config[i].gain??0;
    let nr=startRaw + (e.deltaY<0?1:-1); nr=Math.max(0,Math.min(mx,nr));
    setGainLocal(i,nr); sendGain(i,nr);
    const r=groupOf(i);
    if(r){ const delta=nr-startRaw; r.g.members.forEach(j=>{ if(j===i) return;
      const mxj=maxRaw(st.config[j].pretype??0);
      const v=r.g.mode==='link'?Math.min(nr,mxj):Math.max(0,Math.min(mxj,(st.config[j].gain??0)+delta));
      setGainLocal(j,v); sendGain(j,v); }); }
    commitHistory(historyBefore);
  }, {passive:false});
  k.addEventListener('dblclick', ()=>{
    if(!st.connected||groupMode) return;
    const before=configSnapshot(), r=groupOf(i), members=r?r.g.members:[i];
    const restoring=(st.config[i].gain??0)===0 && gainBeforeZero[i]!==null;
    members.forEach(j=>{
      const current=st.config[j].gain??0;
      if(!restoring){ gainBeforeZero[j]=current; setGainLocal(j,0); sendGain(j,0); }
      else {
        const restored=Math.max(0,Math.min(maxRaw(st.config[j].pretype??0),gainBeforeZero[j]??0));
        setGainLocal(j,restored); sendGain(j,restored);
      }
    });
    commitHistory(before);
    toast(restoring?'Gain restored':'Gain zeroed');
  });
}

// ── Controls ──
function togglePh(i){
  if(!st.connected) return;
  const before=configSnapshot();
  const nv = st.config[i].phantom?0:1; st.config[i].phantom=nv; applyState();
  sendPhantom(i,nv);
  commitHistory(before);
  toast(`CH ${i+1}: 48V ${nv?'ON ⚡':'OFF'}`);
}
async function cycleType(i){
  if(!st.connected) return;
  const before=configSnapshot();
  const maxType = i < 4 ? 3 : 2;   // Hi-Z (input type 2) available only on channels 1-4
  const nv=((st.config[i].pretype??0)+1)%maxType; st.config[i].pretype=nv;
  // gain may exceed the new mode's max — clamp visually; device clamps on its side
  st.config[i].gain=Math.min(st.config[i].gain??0, maxRaw(nv));
  applyState();
  await sendType(i,nv,true);
  await sendGain(i, st.config[i].gain);
  commitHistory(before);
  toast(`CH ${i+1}: ${TLABEL[TYPES[nv]]}`);
}
function onName(i,v){
  v=v.trim();
  if(v) meta.names[i]=v; else delete meta.names[i];
  saveMeta(); pushMeta('name:'+i, v||''); applyState();
}
// ── Accidental-touch guard ────────────────────────────────────────────────────
// A phone is held, pocketed and passed around mid-session, and these controls change live
// hardware the moment they are hit: recalling a preset rewrites all 32 channels, saving
// overwrites a slot, 48 V can damage a microphone that is not built for it, and power cuts
// the audio. On a phone each one arms first and acts on a second deliberate tap. The desktop
// window keeps its single-click behaviour — a mouse does not brush controls by accident.
const ARM_MS=3000;
let armedKey=null, armedEl=null, armTimer=null;
function touchGuardActive(){ return window.matchMedia('(max-width:619px)').matches; }
function disarmTouch(){
  clearTimeout(armTimer); armTimer=null;
  if(armedEl) armedEl.classList.remove('armed');
  armedKey=null; armedEl=null;
}
function confirmTouch(key, el, message){
  if(!touchGuardActive()) return true;
  if(armedKey===key){ disarmTouch(); return true; }
  disarmTouch();
  armedKey=key; armedEl=el||null;
  if(armedEl) armedEl.classList.add('armed');
  armTimer=setTimeout(disarmTouch, ARM_MS);
  toast(message);
  return false;
}
// Anything else the user touches cancels the pending action, so an armed control never waits
// around to be triggered by an unrelated tap later.
document.addEventListener('pointerdown', e=>{
  if(armedKey && (!armedEl || !e.target.closest('.armed'))) disarmTouch();
}, true);

function recallPreset(idx){
  if(!st.connected) return;
  if(!confirmTouch('recall'+idx, document.getElementById('pb'+idx), `Tap ${idx} again to recall preset ${idx}`)) return;
  api('/api/recall_preset',{idx}); st.current_preset=idx; applyState(); toast(`Preset ${idx} recalled`);
}
function savePreset(){
  if(!st.connected) return;
  const idx=st.current_preset;
  if(!confirmTouch('save', document.querySelector('.savebtn'), `Tap Save again to overwrite preset ${idx}`)) return;
  api('/api/save_preset',{idx}); toast(`Preset ${idx} saved 💾`);
}
function togglePower(){
  if(!st.connected) return;
  if(!confirmTouch('power', document.getElementById('pwr'),
      st.power_on?'Tap power again to switch the device to standby':'Tap power again to switch the device on')) return;
  st.power_on=!st.power_on; applyState(); api('/api/set_power',{on:st.power_on?1:0}); toast(st.power_on?'Powered ON ⚡':'Standby');
}
function toggleAllPhantom(){
  if(!st.connected) return;
  const anyNow=st.config.some(c=>!!c.phantom);
  if(!confirmTouch('phall', document.getElementById('phAll'),
      anyNow?'Tap 48V All again to switch all phantom power off'
            :'Tap 48V All again to switch 48 V on for every channel')) return;
  const before=configSnapshot();
  const anyOn=st.config.some(c=>!!c.phantom); const nv=anyOn?0:1;
  for(let i=0;i<N;i++) st.config[i].phantom=nv;
  document.getElementById('phAll').classList.toggle('on', nv===1);
  applyState(); api('/api/set_all_phantom',{phantom:nv});
  if(st.demo) for(let i=0;i<N;i++) pushMeta('ph:'+i, nv);
  commitHistory(before);
  toast(nv?'48V ON — all channels ⚡':'48V OFF — all channels');
}
function cleanSlate(){
  if(!st.connected) return;
  if(!confirm('Reset all: zeroes EVERY channel (gain 0, 48V off, Mic) and clears names/colours. Are you sure?')) return;
  const before=configSnapshot();
  for(let i=0;i<N;i++){ st.config[i].gain=0; st.config[i].phantom=0; st.config[i].pretype=0; }
  meta.names={}; meta.colors={};
  liveGroupIds().forEach(tombstoneGroup);          // tombstone, so peers actually clear too
  saveMeta();
  for(let i=0;i<N;i++){ pushMeta('name:'+i,''); pushMeta('color:'+i,''); }
  api('/api/set_all_gain',{gain:0}); api('/api/set_all_phantom',{phantom:0});
  for(let i=0;i<N;i++) sendType(i,0,true);
  if(st.demo) for(let i=0;i<N;i++){ pushMeta('gain:'+i,0); pushMeta('ph:'+i,0); pushMeta('type:'+i,0); }
  commitHistory(before);
  applyState(); toast('Reset all — everything zeroed ✨');
}

// ── Layout ──
function setLayout(mode, silent){
  meta.layout=mode; saveMeta();
  const g=document.getElementById('grid'); g.className='grid l-'+mode;
  const btn=document.getElementById('layoutToggle'), icon=document.getElementById('layoutIcon'), label=document.getElementById('layoutLabel');
  const next=mode==='2x16'?'wrap':'2x16';
  icon.textContent=mode==='2x16'?'▦':'▥';
  label.textContent=mode==='2x16'?'2 × 16 layout':'Responsive wrap layout';
  btn.title=next==='wrap'?'Switch to responsive wrap':'Switch to 2 × 16';
  btn.setAttribute('aria-label',btn.title);
  if(!silent) toast(mode==='2x16'?'2 rows × 16':'Responsive wrap');
}
function toggleLayout(){ setLayout(meta.layout==='2x16'?'wrap':'2x16'); }

// ── Colors ──
let palTarget=-1;
function openPalette(e,i){
  e.stopPropagation();
  palTarget=i;
  const p=document.getElementById('palette');
  p.innerHTML='';
  COLORS.forEach(c=>{
    const s=document.createElement('div'); s.className='sw';
    s.style.background=c||'repeating-linear-gradient(45deg,#333,#333 4px,#222 4px,#222 8px)';
    s.title=c?'':'Clear';
    s.onclick=ev=>{
      ev.stopPropagation();
      const gr=groupOf(i), targets=gr?gr.g.members:[i];
      targets.forEach(j=>{ if(c) meta.colors[j]=c; else delete meta.colors[j]; pushMeta('color:'+j,c||''); });
      if(gr){ gr.g.color=c||'#7c5cff'; pushGroup(gr.gid); }
      saveMeta(); applyState(); p.classList.remove('show');
    };
    p.appendChild(s);
  });
  p.style.left=Math.max(8,Math.min(e.clientX-20, window.innerWidth-180))+'px';
  p.style.top=Math.min(e.clientY+8,window.innerHeight-150)+'px';
  p.classList.add('show');
}
document.addEventListener('click', ()=>{ document.getElementById('palette').classList.remove('show'); document.getElementById('devpanel').classList.remove('show'); document.getElementById('peerpanel').classList.remove('show'); });

// ── Device connection / discovery ──
function toggleDevPanel(e){
  if(e) e.stopPropagation();
  const p=document.getElementById('devpanel');
  const show=!p.classList.contains('show');
  document.getElementById('palette').classList.remove('show');
  p.classList.toggle('show', show);
  if(show){ const ip=document.getElementById('dpIp'); if(st.device_ip && st.device_ip!=='—') ip.value=st.device_ip; fetchDevices(); }
}
async function fetchDevices(){
  try{
    const r=await fetch(apiUrl('/api/devices')); const j=await r.json();
    const list=document.getElementById('dpList');
    if(!j.devices || !j.devices.length){
      list.innerHTML='<div class="dp-empty">No devices found yet. They appear automatically when on the same network as the MP32.</div>'; return;
    }
    list.innerHTML='';
    j.devices.forEach(d=>{
      const el=document.createElement('div');
      el.className='dp-dev'+(d.ip===st.device_ip?' cur':'');
      el.innerHTML=`<div style="flex:1"><div class="nm">${d.device_name||d.name||'Compatible device'}</div>`+
        `<div class="meta">${d.ip}:${d.port||2028}${d.serial?' · #'+d.serial:''}${d.firmware?' · fw '+d.firmware:''}</div></div>`;
      el.onclick=()=>connectTo(d.ip, d.port||2028);
      list.appendChild(el);
    });
  }catch(e){}
}
function connectManual(){
  const ip=document.getElementById('dpIp').value.trim();
  const port=parseInt(document.getElementById('dpPort').value)||2028;
  if(ip) connectTo(ip, port);
}
function copyServerUrl(){
  const u=document.getElementById('dpUrl').textContent;
  if(!u || u==='—') return;
  const done=()=>toast('Address copied 📋 — paste it on your phone');
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(u).then(done).catch(()=>fallbackCopy(u,done));
  } else { fallbackCopy(u,done); }
}
function fallbackCopy(text, done){
  const t=document.createElement('textarea');
  t.value=text; t.style.position='fixed'; t.style.opacity='0';
  document.body.appendChild(t); t.focus(); t.select();
  try{ document.execCommand('copy'); done(); }catch(e){ toast('Copy failed — long-press to copy'); }
  document.body.removeChild(t);
}
function connectTo(ip, port){
  api('/api/connect',{ip, port});
  document.getElementById('devpanel').classList.remove('show');
  toast('Connecting to '+ip+'…');
}
setInterval(()=>{ if(document.getElementById('devpanel').classList.contains('show')) fetchDevices(); }, 2000);

// ── Peers (other controllers on the LAN) ──
function togglePeersPanel(e){
  if(e) e.stopPropagation();
  const p=document.getElementById('peerpanel');
  const show=!p.classList.contains('show');
  document.getElementById('devpanel').classList.remove('show');
  document.getElementById('palette').classList.remove('show');
  p.classList.toggle('show', show);
  if(show) fetchPeers();
}
async function fetchPeers(){
  try{
    const r=await fetch(apiUrl('/api/peers')); const j=await r.json();
    const me=j.self||{}, peers=j.peers||[];
    rememberHosts(peers);
    document.getElementById('peerCount').textContent = peers.length+1;
    const list=document.getElementById('peerList');
    let html=`<div class="dp-dev cur"><div style="flex:1"><div class="nm">${me.host||'This computer'} <span style="color:var(--green)">(you)</span></div><div class="meta">→ ${me.device||'—'} · ${me.web_leader?'web host':'desktop'}</div></div></div>`;
    peers.forEach(p=>{ html+=`<div class="dp-dev"><div style="flex:1"><div class="nm">${p.host||'Controller'}</div><div class="meta">${p.ip} → ${p.device||'—'} · ${p.web_leader?'web host':'desktop'}</div></div></div>`; });
    list.innerHTML=html;
  }catch(e){}
}
async function updatePeerCount(){
  try{ const r=await fetch(apiUrl('/api/peers')); const j=await r.json();
    rememberHosts(j.peers);
    // Name our lease claims after the controller we are talking to, so "X is editing"
    // identifies a machine somebody recognises rather than an opaque client id.
    if(j.self && j.self.host && j.self.host!==clientName){
      clientName=j.self.host; localStorage.setItem('mp32_client_name',clientName);
    }
    document.getElementById('peerCount').textContent=(j.peers||[]).length+1;
    if(document.getElementById('peerpanel').classList.contains('show')) fetchPeers();
  }catch(e){}
}
setInterval(updatePeerCount, 3000); updatePeerCount();

// ── Notes ─────────────────────────────────────────────────────────────────────
const notesEl=document.getElementById('notes');
notesEl.value = localStorage.getItem('mp32_notes')||'';
notesEl.addEventListener('input', ()=>localStorage.setItem('mp32_notes', notesEl.value));

// ── Shared cards ──────────────────────────────────────────────────────────────
// Each card is its own metadata key, so it rides the existing per-field last-write-wins sync
// with no new protocol. Two people on different cards never collide; two on the same card
// lose one card at worst, instead of the single shared textarea this replaces, where the
// later writer erased everything the other had typed.
const LEASE_MS=15000, LEASE_RENEW_MS=5000, CARD_SAVE_MS=800;
const cardsEl=document.getElementById('publicCards');
const cardListEl=document.getElementById('cardList');
let clientId=localStorage.getItem('mp32_client');
if(!clientId){ clientId=Math.random().toString(36).slice(2,10); localStorage.setItem('mp32_client',clientId); }
let clientName=localStorage.getItem('mp32_client_name')||'This computer';
let editingCard=null, cardSaveTimers={}, leaseTimer=null;

function cardKey(id){ return 'card:'+id; }
function lockKey(id){ return 'lock:'+id; }

function lockHolder(id){
  const l=meta.locks[id];
  if(!l || !l.until || l.until < Date.now()) return null;   // leases expire on their own,
  return l;                                                  // so a crashed editor never sticks
}
function heldByOther(id){ const l=lockHolder(id); return l && l.client!==clientId ? l : null; }

function pushCard(id, card){
  const ts=hlcNow();
  meta.cards[id]=card; meta._ts[cardKey(id)]=ts; saveMeta();
  api('/api/meta_event',{key:cardKey(id),value:card,ts});
}
function pushLock(id, value){
  const ts=hlcNow();
  meta.locks[id]=value; meta._ts[lockKey(id)]=ts; saveMeta();
  api('/api/meta_event',{key:lockKey(id),value,ts});
}

function acquireLease(id){
  if(heldByOther(id)) return false;
  pushLock(id,{client:clientId,name:clientName,until:Date.now()+LEASE_MS});
  return true;
}
function releaseLease(id){
  const l=meta.locks[id];
  if(l && l.client===clientId) pushLock(id,{client:clientId,name:clientName,until:0});
}
function startEditing(id){
  if(!acquireLease(id)) return false;
  editingCard=id;
  clearInterval(leaseTimer);
  leaseTimer=setInterval(()=>{
    // Renewal doubles as the check that we still hold it: another controller can win a
    // simultaneous grab, and the loser must stop editing rather than fight over the card.
    if(editingCard!==id){ clearInterval(leaseTimer); return; }
    if(heldByOther(id)){ stopEditing(id,false); renderCards(); toast('Card taken over by '+heldByOther(id).name); return; }
    pushLock(id,{client:clientId,name:clientName,until:Date.now()+LEASE_MS});
  },LEASE_RENEW_MS);
  return true;
}
function stopEditing(id, release=true){
  if(editingCard===id) editingCard=null;
  clearInterval(leaseTimer); leaseTimer=null;
  if(release) releaseLease(id);
}

function newCard(){
  const id=Math.random().toString(36).slice(2,10)+Date.now().toString(36);
  pushCard(id,{title:'',body:'',created:Date.now(),author:clientName,deleted:false});
  renderCards();
  setTimeout(()=>{ const el=document.querySelector(`.card[data-id="${id}"] .card-title`); if(el) el.focus(); },40);
}
function deleteCard(id){
  const c=meta.cards[id];
  if(!c || c.deleted) return;
  if(heldByOther(id)){ toast(heldByOther(id).name+' is editing this card'); return; }
  if(!confirm('Delete this shared card for everyone?')) return;
  stopEditing(id);
  // A tombstone, not a removal: absence must never mean "deleted", or a controller joining
  // with an empty store would wipe everyone's cards. Only an explicit later delete wins.
  pushCard(id,{...c,deleted:true,body:'',title:c.title});
  renderCards();
}
function scheduleCardSave(id){
  clearTimeout(cardSaveTimers[id]);
  // Sync while typing, not on blur: a browser or machine that dies mid-edit would otherwise
  // take everything typed since the field was focused with it.
  cardSaveTimers[id]=setTimeout(()=>{
    const el=document.querySelector(`.card[data-id="${id}"]`);
    if(!el) return;
    const cur=meta.cards[id]||{created:Date.now(),author:clientName};
    pushCard(id,{...cur,
      title:el.querySelector('.card-title').value,
      body:el.querySelector('.card-body').value,
      deleted:false});
  },CARD_SAVE_MS);
}

function visibleCards(){
  return Object.keys(meta.cards)
    .filter(id=>meta.cards[id] && !meta.cards[id].deleted)
    .sort((a,b)=>(meta.cards[a].created||0)-(meta.cards[b].created||0) || (a<b?-1:1));
}
function renderCards(){
  const ids=visibleCards();
  document.getElementById('cardEmpty').style.display=ids.length?'none':'block';
  cardListEl.innerHTML=ids.map(id=>{
    const c=meta.cards[id], other=heldByOther(id), mine=editingCard===id;
    const esc=s=>String(s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
    return `<div class="card${other?' locked':''}${mine?' mine':''}" data-id="${id}" data-readonly="${other?1:0}">
      <div class="card-head">
        <input class="card-title" placeholder="Untitled card" value="${esc(c.title)}" ${other?'readonly':''}>
        <button class="card-del" title="Delete for everyone" onclick="deleteCard('${id}')">×</button>
      </div>
      <textarea class="card-body" placeholder="Shared with every controller" ${other?'readonly':''}>${esc(c.body)}</textarea>
      <div class="card-who">✎ ${esc(other?other.name:'')} is editing</div>
    </div>`;
  }).join('');
  cardListEl.querySelectorAll('.card').forEach(el=>{
    const id=el.dataset.id;
    el.querySelectorAll('.card-title,.card-body').forEach(f=>{
      f.addEventListener('focus',()=>{
        if(!startEditing(id)){ f.blur(); toast(heldByOther(id).name+' is editing this card'); renderCards(); }
      });
      f.addEventListener('blur',()=>{ clearTimeout(cardSaveTimers[id]); saveCardNow(id); stopEditing(id); renderCards(); });
      f.addEventListener('input',()=>scheduleCardSave(id));
    });
  });
}
function saveCardNow(id){
  const el=document.querySelector(`.card[data-id="${id}"]`);
  if(!el) return;
  const cur=meta.cards[id]||{created:Date.now(),author:clientName};
  const title=el.querySelector('.card-title').value, body=el.querySelector('.card-body').value;
  if(cur.title===title && cur.body===body) return;
  pushCard(id,{...cur,title,body,deleted:false});
}

// Carry the old shared textarea over so its contents are never silently dropped.
// The id is fixed rather than generated: the local "already migrated" flag lives in this
// browser, but cards are shared network-wide, so every browser and phone still holding old
// notes would otherwise add its own duplicate. With one deterministic key they all converge
// on a single card under the usual last-write-wins.
(function migrateNotes(){
  const old=localStorage.getItem('mp32_public_notes')||'';
  if(!old.trim() || localStorage.getItem('mp32_cards_migrated')) return;
  const id='migrated-public-notes';
  const existing=meta.cards[id];
  if(!existing || (!existing.deleted && (existing.body||'').length < old.length)){
    pushCard(id,{title:'Shared notes',body:old,created:(existing&&existing.created)||Date.now()-1,
                 author:clientName,deleted:false});
  }
  localStorage.setItem('mp32_cards_migrated','1');
})();
renderCards();
// Refresh other people's cards, but never while we are typing: rebuilding the list
// replaces the field under the cursor.
setInterval(()=>{ if(document.body.classList.contains('notes-open') && !editingCard) renderCards(); },2000);

function setNotesMode(mode){
  const isPublic=mode==='public';
  notesEl.classList.toggle('hide',isPublic);
  cardsEl.classList.toggle('hide',!isPublic);
  document.getElementById('notesLocalTab').classList.toggle('on',!isPublic);
  document.getElementById('notesPublicTab').classList.toggle('on',isPublic);
  document.getElementById('notesHint').textContent=isPublic
    ? 'Shared with every controller · one editor per card'
    : 'Auto-saved to this browser · private';
  if(isPublic){ renderCards(); } else { notesEl.focus(); }
}
function toggleNotes(force){
  const open=typeof force==='boolean'?force:!document.body.classList.contains('notes-open');
  document.body.classList.toggle('notes-open',open);
  document.getElementById('notesTab').textContent=open?'Close':'Notes';
  if(open) setTimeout(()=>document.querySelector('.notes textarea:not(.hide)')?.focus(),230);
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') toggleNotes(false); });
function toggleAbout(open){
  document.getElementById('aboutModal').classList.toggle('show',!!open);
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape') toggleAbout(false);
  if(!(e.ctrlKey||e.metaKey) || e.altKey || e.key.toLowerCase()!=='z') return;
  if(e.target && /INPUT|TEXTAREA/.test(e.target.tagName)) return;
  e.preventDefault();
  if(e.shiftKey) redoChange(); else undoChange();
});

// ── Save / load to file ──
function saveFile(){
  const obj={ app:'MP32-CP', version:1, ts:new Date().toISOString(),
    channels: st.config.map((c,i)=>({idx:i, name:meta.names[i]||'', color:meta.colors[i]||'',
      gain:c.gain, phantom:c.phantom, pretype:c.pretype})),
    groups: Object.fromEntries(liveGroupIds().map(g=>[g, meta.groups[g]])), notes: notesEl.value,
    cards: visibleCards().map(id=>({id, ...meta.cards[id]})) };
  const json=JSON.stringify(obj,null,2);
  const fn=`mp32-preset-${new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')}.json`;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.save_preset){
    window.pywebview.api.save_preset(json, fn).then(p=>{
      if(p && p.indexOf('ERR')!==0) toast('Saved 💾');
      else if(p && p.indexOf('ERR')===0) toast('Save failed');
    });
    return;
  }
  const blob=new Blob([json],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=fn;
  a.click(); URL.revokeObjectURL(a.href);
  toast('Saved to file 💾');
}
function loadFile(){ document.getElementById('fileInput').click(); }
document.getElementById('fileInput').addEventListener('change', e=>{
  const f=e.target.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=()=>{
    try{
      const obj=JSON.parse(r.result);
      if(!obj.channels) throw 0;
      const historyBefore=configSnapshot();
      obj.channels.forEach(ch=>{
        const i=ch.idx;
        if(ch.name) meta.names[i]=ch.name; else delete meta.names[i];
        if(ch.color) meta.colors[i]=ch.color; else delete meta.colors[i];
        st.config[i].pretype=ch.pretype||0; st.config[i].gain=ch.gain||0; st.config[i].phantom=ch.phantom||0;
        if(st.connected){
          sendType(i, ch.pretype||0);
          sendGain(i, ch.gain||0);
          sendPhantom(i, ch.phantom||0);
        }
      });
      if(obj.groups && typeof obj.groups==='object'){
        // Merged and announced per group: a preset saved yesterday must not delete a group
        // somebody added since.
        Object.keys(obj.groups).forEach(gid=>{ meta.groups[gid]=obj.groups[gid]; pushGroup(gid); });
      }
      if(typeof obj.notes==='string'){ notesEl.value=obj.notes; localStorage.setItem('mp32_notes',obj.notes); }
      // Cards from a file are merged, never used to replace what is already shared: a preset
      // saved yesterday must not delete a card somebody added since.
      if(Array.isArray(obj.cards)) obj.cards.forEach(c=>{
        if(!c || !c.id) return;
        pushCard(c.id,{title:c.title||'',body:c.body||'',created:c.created||Date.now(),
                       author:c.author||clientName,deleted:!!c.deleted});
      });
      else if(typeof obj.public_notes==='string' && obj.public_notes.trim()){
        // Older preset files carried one shared textarea; keep it as a card.
        const id='import'+Date.now().toString(36);
        pushCard(id,{title:'Imported notes',body:obj.public_notes,created:Date.now(),
                     author:clientName,deleted:false});
      }
      renderCards();
      saveMeta(); applyState();
      commitHistory(historyBefore);
      toast(st.connected?'Loaded + applied to device ✅':'Loaded (offline — will apply on reconnect)');
    }catch(err){ toast('Invalid file ❌'); }
    e.target.value='';
  };
  r.readAsText(f);
});

// ── API + polling ──
async function api(path,data){ try{ await fetch(apiUrl(path),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); }catch(e){} }
// Device parameters never enter peer metadata or handoff state. When the MP32 permits only
// one TCP session, non-host desktops use the web host's live device API as transport.
const gainTx=Array.from({length:N},()=>Promise.resolve());
const typeTx=Array.from({length:N},()=>Promise.resolve());
function sendGain(i,v){
  gainTx[i]=gainTx[i].then(()=>api('/api/set_gain',{idx:i,gain:v}));
  if(st.demo) pushMeta('gain:'+i, v);
  return gainTx[i];
}
function sendPhantom(i,v){ const p=api('/api/set_phantom',{idx:i,phantom:v}); if(st.demo) pushMeta('ph:'+i, v); return p; }
function sendType(i,v,share=true){
  pending[i]=true;
  typeTx[i]=typeTx[i].then(async()=>{
    await api('/api/set_pretype',{idx:i,pretype:v});
    // Let the local backend/device mirror settle before status polling owns the row again.
    await new Promise(resolve=>setTimeout(resolve,180));
    if((st.config[i]?.pretype??0)===v) delete pending[i];
  });
  if(share && st.demo) pushTypeMeta(i,v);
  return typeTx[i];
}
let pollBusy=false, statusFailures=0;
function setHandover(active){
  const was=document.body.classList.contains('handover');
  document.body.classList.toggle('handover',active);
  if(active){
    document.getElementById('dot').className='dot';
    document.getElementById('dot').style.background='#ffc107';
    document.getElementById('dot').style.boxShadow='0 0 7px #ffc107';
    document.getElementById('stxt').textContent='Re-establishing…';
    const txt=document.getElementById('handoverText');
    if(txt) txt.textContent='Re-establishing connection · handover in progress';
  }else{
    const dot=document.getElementById('dot');
    dot.style.background=''; dot.style.boxShadow='';
    if(was){ syncMeta(); pollPeaks(); toast('Connection restored ✅'); }
  }
}
async function poll(){
  if(pollBusy) return;
  pollBusy=true;
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),700);
  try{
    const r=await fetch(apiUrl('/api/status?_='+Date.now()),{cache:'no-store',signal:controller.signal});
    if(!r.ok) throw new Error('status '+r.status);
    const ns=await r.json();
    statusFailures=0; setHandover(false);
    st.connected=ns.connected; st.power_on=ns.power_on; st.device_ip=ns.device_ip;
    st.controller_role=ns.controller_role||'desktop';
    st.connection_state=ns.connection_state||'waiting_for_web_host'; st.connection_error=ns.connection_error||'';
    st.unreachable_hosts=ns.unreachable_hosts||[];
    st.web_clients=ns.web_clients||0;
    st.current_preset=ns.current_preset; st.peaks=ns.peaks||st.peaks; st.device_info=ns.device_info||{}; st.demo=ns.demo;
    if(ns.server_url){ const u=document.getElementById('dpUrl'); if(u) u.textContent=ns.server_url; }
    if(ns.direct_server_url){ const u=document.getElementById('dpDirect'); if(u) u.textContent=ns.direct_server_url; }
    for(let i=0;i<N;i++){ if(!(i in pending) && ns.config && ns.config[i]) st.config[i]=ns.config[i]; }
    applyState();
  }catch(e){
    statusFailures++;
    if(statusFailures>=2) setHandover(true);
    // Give the elected successor a moment to publish the name before going around it, then
    // fall back to a remembered address so recovery never waits on DNS caching.
    if(statusFailures>=3) await tryFailover();
  }finally{
    clearTimeout(timeout); pollBusy=false;
  }
}

// ── Toast ──
let toastTmr;
function toast(msg){ const el=document.getElementById('toast'); el.textContent=msg; el.classList.add('show'); clearTimeout(toastTmr); toastTmr=setTimeout(()=>el.classList.remove('show'),2400); }

// ── Init ──
build();
seedSharedMeta();
if(gainMode==='raw'){ const b=document.getElementById('gainModeBtn'); if(b) b.textContent='raw'; document.querySelectorAll('.gunit').forEach(e=>e.textContent='raw'); }
poll(); setInterval(poll,120);
</script>
</body>
</html>"""


PWA_MANIFEST = {
    "name": "MP32 Control Panel",
    "short_name": "MP32",
    "description": "Independent network control panel for the Antelope Audio MP32 32-channel microphone preamplifier.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#08090f",
    "theme_color": "#08090f",
    "orientation": "any",
    "icons": [{"src": "/app-icon.png", "sizes": "1254x1254", "type": "image/png", "purpose": "any maskable"}],
}

APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#08090f"/>
<rect x="58" y="58" width="396" height="396" rx="92" fill="#171932" stroke="#7c5cff" stroke-width="12"/>
<text x="256" y="294" text-anchor="middle" font-family="Arial,sans-serif" font-size="174" font-weight="700" fill="#eeeeff">MP</text>
<circle cx="389" cy="389" r="30" fill="#00e676"/>
</svg>"""


# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    device: MP32Device = None      # injected before server starts
    beacon: "BeaconListener" = None
    peers_svc: "PeerService" = None
    mobile_url: str = None         # LAN URL shown to the user for phone/tablet access
    direct_mobile_url: str = None  # current host IP fallback
    host_service: "StableHostService" = None
    DEVICE_POST_PATHS = {
        '/api/set_gain', '/api/set_all_gain', '/api/set_phantom',
        '/api/set_all_phantom', '/api/set_pretype', '/api/recall_preset',
        '/api/save_preset', '/api/set_power', '/api/connect',
    }

    # Which machines are actually using the web interface right now. Only the host serves
    # phones, and only non-loopback callers count: the desktop app's own window talks to
    # 127.0.0.1 and is not a second device.
    web_clients: Dict[str, float] = {}
    WEB_CLIENT_TTL = 12.0

    def note_web_client(self):
        ip = self.client_address[0] if self.client_address else ""
        if not ip or ip.startswith("127.") or ip == "::1":
            return
        Handler.web_clients[ip] = time.time()

    @classmethod
    def active_web_clients(cls) -> int:
        now = time.time()
        for ip, seen in list(cls.web_clients.items()):
            if now - seen > cls.WEB_CLIENT_TTL:
                del cls.web_clients[ip]
        return len(cls.web_clients)

    def log_message(self, fmt, *args):
        pass  # suppress logs

    def _send_json(self, data: Dict, code: int = 200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Dict:
        try:
            n = int(self.headers.get('Content-Length', 0))
            b = self.rfile.read(n)
            return json.loads(b) if b else {}
        except Exception:
            return {}

    def _proxy_json(self, path: str, method: str = 'GET', data: Optional[Dict] = None) -> Optional[Dict]:
        """Forward to the web host's live device session; never reads handoff state."""
        bases = self.host_service.leader_base_urls() if self.host_service else []
        if not bases:
            return None
        body = json.dumps(data).encode('utf-8') if data is not None else None
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        # Ignore HTTP(S)_PROXY environment variables for trusted direct LAN traffic.
        opener = build_opener(ProxyHandler({}))
        for base in bases:
            request = Request(base + path, data=body, headers=headers, method=method)
            try:
                with opener.open(request, timeout=0.8) as response:
                    return json.loads(response.read().decode('utf-8'))
            except Exception:
                continue
        return None

    def do_GET(self):
        self.note_web_client()
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            body = HTML_PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/manifest.webmanifest':
            body = json.dumps(PWA_MANIFEST).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/manifest+json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/app-icon.svg':
            body = APP_ICON_SVG.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ASSET_FILES:
            asset_path = os.path.join(ASSET_DIR, ASSET_FILES[path])
            try:
                with open(asset_path, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.send_response(404)
                self.end_headers()
        elif path == '/api/status':
            local_session = DEVICE_SUPPORTS_MULTIPLE_CLIENTS or bool(self.host_service and self.host_service.active)
            s = self.device.get_status() if local_session else self._proxy_json('/api/status')
            if s is None:
                s = self.device.get_status()
            s['server_url'] = self.mobile_url
            # Resolved per request: a long-running controller outlives its DHCP lease, and a
            # start-up snapshot would keep advertising an address the phone can no longer reach.
            s['direct_server_url'] = f"http://{local_lan_ip()}:{SERVER_PORT}"
            s['stable_host_active'] = bool(self.host_service and self.host_service.active)
            s['stable_host_available'] = bool(self.host_service and self.host_service.available)
            s['unreachable_hosts'] = self.host_service.unreachable_hosts() if self.host_service else []
            s['controller_role'] = 'web_host' if (self.host_service and self.host_service.active) else 'desktop'
            s['web_clients'] = self.active_web_clients()
            self._send_json(s)
        elif path == '/api/devices':
            self._send_json({'devices': self.beacon.list() if self.beacon else []})
        elif path == '/api/peers':
            self._send_json(self.peers_svc.info() if self.peers_svc else {'self': {}, 'peers': []})
        elif path == '/api/peaks':
            local_session = DEVICE_SUPPORTS_MULTIPLE_CLIENTS or bool(self.host_service and self.host_service.active)
            p = {'peaks': self.device.peaks} if local_session else self._proxy_json('/api/peaks')
            self._send_json(p if p is not None else {'peaks': self.device.peaks})
        elif path == '/api/meta_state':
            self._send_json(self.peers_svc.meta_state() if self.peers_svc else {})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        data = self._read_body()
        path = urlparse(self.path).path
        d    = self.device

        if path in self.DEVICE_POST_PATHS and not DEVICE_SUPPORTS_MULTIPLE_CLIENTS and not (self.host_service and self.host_service.active):
            result = self._proxy_json(path, 'POST', data)
            if result is None:
                self._send_json({'error': 'Live device host unavailable; retrying web-host handover'}, 503)
            else:
                self._send_json(result)
            return

        if path in self.DEVICE_POST_PATHS and path != '/api/connect':
            if not d.demo and not (d.connected and d.config_valid):
                self._send_json({'error': 'No validated live device session'}, 503)
                return

        if path == '/api/set_gain':
            d.set_gain(int(data.get('idx', 0)), int(data.get('gain', 0)))
            self._send_json({'ok': True})

        elif path == '/api/set_all_gain':
            g = int(data.get('gain', 0))
            for i in range(NUM_CHANNELS):
                d.set_gain(i, g)
            self._send_json({'ok': True})

        elif path == '/api/set_phantom':
            d.set_phantom(int(data.get('idx', 0)), bool(data.get('phantom', 0)))
            self._send_json({'ok': True})

        elif path == '/api/set_all_phantom':
            ph = bool(data.get('phantom', 0))
            for i in range(NUM_CHANNELS):
                d.set_phantom(i, ph)
            self._send_json({'ok': True})

        elif path == '/api/set_pretype':
            d.set_pretype(int(data.get('idx', 0)), int(data.get('pretype', 0)))
            self._send_json({'ok': True})

        elif path == '/api/recall_preset':
            d.recall_preset(int(data.get('idx', 1)))
            self._send_json({'ok': True})

        elif path == '/api/save_preset':
            d.save_preset(int(data.get('idx', 1)))
            self._send_json({'ok': True})

        elif path == '/api/set_power':
            d.set_power(bool(data.get('on', 0)))
            self._send_json({'ok': True})

        elif path == '/api/connect':
            ip = str(data.get('ip', '')).strip()
            port = data.get('port') or DEVICE_PORT
            if ip:
                d.retarget(ip, int(port))
                self._send_json({'ok': True})
            else:
                self._send_json({'error': 'no ip'}, 400)

        elif path == '/api/meta_event':
            if self.peers_svc:
                self.peers_svc.apply_meta_event(data.get('key'), data.get('value'), data.get('ts'))
            self._send_json({'ok': True})

        else:
            self._send_json({'error': 'Not found'}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


# ── Entry point ────────────────────────────────────────────────────────────────
_lan_ip_cache: Dict[str, Any] = {"ip": "", "at": 0.0}
_lan_ip_lock = threading.Lock()


def local_lan_ip(max_age: float = LAN_IP_CACHE_TTL) -> str:
    """Best-effort current LAN address, re-read at most every max_age seconds.

    Deliberately not resolved once at startup: a controller that runs for days will change
    address after a DHCP lease change, and a stale address produces both a wrong mobile URL
    and a wrong self-exclusion in the host election.
    """
    with _lan_ip_lock:
        if _lan_ip_cache["ip"] and time.time() - _lan_ip_cache["at"] < max_age:
            return _lan_ip_cache["ip"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))
        ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = '127.0.0.1'
    finally:
        s.close()
    with _lan_ip_lock:
        _lan_ip_cache.update(ip=ip, at=time.time())
    return ip


def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║   MP32 Control — Independent Control Panel       ║")
    print("║   Python 3.9+  ·  Mac / Windows / mobile web    ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n📡  Connecting to MP32 @ {DEVICE_IP}:{DEVICE_PORT} …")

    beacon = BeaconListener()
    device = MP32Device(DEVICE_IP, DEVICE_PORT)
    device.beacon = beacon          # auto-discover + connect to the MP32
    peers_svc = PeerService(device)
    host_service = StableHostService(peers_svc, SERVER_PORT)

    url    = f"http://127.0.0.1:{SERVER_PORT}"
    direct_mobile_url = f"http://{local_lan_ip()}:{SERVER_PORT}"
    Handler.device = device
    Handler.beacon = beacon
    Handler.peers_svc = peers_svc
    Handler.host_service = host_service
    Handler.direct_mobile_url = direct_mobile_url
    Handler.mobile_url = host_service.url if host_service.available else direct_mobile_url

    # Serve before starting anything that reaches the network. Every service below is
    # non-blocking now, but the page must not depend on that staying true: the UI is built to
    # display "discovering"/"connecting" states, so it is far better to show them than to
    # leave a window with nothing in it.
    try:
        server = ThreadingHTTPServer((SERVER_BIND, SERVER_PORT), Handler)
    except OSError as e:
        print(f"\n❌  Cannot open port {SERVER_PORT}: {e}")
        print("    MP32 Control is probably already running. Open " + url + " instead,")
        print("    or quit the other copy and start this one again.\n")
        return 1
    threading.Thread(target=server.serve_forever, name="HTTPServer", daemon=True).start()

    beacon.start()
    peers_svc.start()
    host_service.start()
    device.start(session_enabled=DEVICE_SUPPORTS_MULTIPLE_CLIENTS)

    print(f"🌐  This computer →  {url}")
    print(f"📱  Permanent URL →  {Handler.mobile_url}")
    print(f"    Direct fallback → {direct_mobile_url}")
    print("    Same Wi-Fi required. Safari → Share → Add to Home Screen.")
    print("    LAN TEST MODE: anyone on this local network can open this panel.")
    print("    Press Ctrl+C to stop.\n")

    # The server is already accepting, so the browser never races the bind.
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n🛑  Shutting down …")
        host_service.stop()
        device.stop()
        peers_svc.stop()
        beacon.stop()
        server.shutdown()
        server.server_close()
        print("   Done. Goodbye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
