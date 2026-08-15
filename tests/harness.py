"""Shared harness for the MP32 Control test suites.

Starts real controller processes and talks to them over the real HTTP API and the real
multicast peer transport. Nothing about the metadata, election or API paths is mocked.

Two traps cost a lot of time the first time these suites were written. Both are handled
here so no individual test has to remember them:

1. **Timestamps must be HLC-shaped** (`physical_ms << 16 | counter`). The UI stamps every
   metadata write with a hybrid logical clock, and a plain millisecond value is ~65000x
   too small — it loses every last-write-wins comparison against a real event, so the test
   fails while the code is fine. Use `hlc()`.
2. **Assert on your own run's keys.** Controllers under test would otherwise share the LAN
   multicast group with any running copy of the app, whose browser re-announces its own
   stored metadata mid-test. `Cluster` gives every run a private multicast group and port,
   and `key()` namespaces keys per run, so this is structural rather than a discipline.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAUNCHER = os.path.join(HERE, "_controller.py")

READY_TIMEOUT = 20.0          # generous: a cold Python start on a busy machine is slow
CONVERGE_TIMEOUT = 8.0        # heartbeat is 1 s, snapshots every 4 s


# ── Hybrid logical clock ──────────────────────────────────────────────────────
# Mirrors hlcNow() in the browser: pack (physical_ms, logical_counter) into one sortable
# integer so writes stay comparable across machines with skewed clocks.
_hlc_lock = threading.Lock()
_hlc = 0


def hlc(bump_ms: int = 0) -> int:
    """Next HLC timestamp. `bump_ms` shifts the physical part forward to script an
    explicitly later write without waiting for wall-clock time to pass."""
    global _hlc
    with _hlc_lock:
        now = int(time.time() * 1000) + bump_ms
        pt_old, lc_old = _hlc >> 16, _hlc & 0xFFFF
        if now > pt_old:
            pt, lc = now, 0
        else:
            pt, lc = pt_old, lc_old + 1
        _hlc = (pt << 16) | lc
        return _hlc


def stale_hlc(behind_ms: int = 60_000) -> int:
    """An HLC value that is genuinely older than anything this run has written, for
    testing that a stale edit cannot win or resurrect a tombstone."""
    return ((int(time.time() * 1000) - behind_ms) << 16)


# ── Assertions ────────────────────────────────────────────────────────────────
class Checks:
    """Counts named checks so a suite reports 'n checks passed', not just 'ok'."""

    def __init__(self, suite: str):
        self.suite = suite
        self.passed = 0
        self.failures: List[str] = []
        self.known: List[str] = []
        self.unexpected_passes: List[str] = []

    def ok(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed += 1
            print(f"    ok   {description}")
            return True
        self.failures.append(description)
        print(f"    FAIL {description}")
        return False

    def equal(self, actual: Any, expected: Any, description: str) -> bool:
        return self.ok(actual == expected, f"{description} (got {actual!r}, want {expected!r})"
                       if actual != expected else description)

    def known_bad(self, condition: bool, description: str, ref: str) -> bool:
        """A property that SHOULD hold but does not, because of a defect that is recorded
        and not yet fixed. It does not fail the run — but if it starts passing, that is
        reported loudly, because a test that goes quiet when the bug is fixed is a test
        nobody promotes."""
        if condition:
            self.unexpected_passes.append(f"{description}  [{ref}]")
            print(f"    NOW PASSING — promote this check: {description}  [{ref}]")
            return True
        self.known.append(f"{description}  [{ref}]")
        print(f"    known defect: {description}  [{ref}]")
        return False

    @property
    def total(self) -> int:
        return self.passed + len(self.failures)


# ── Controllers ───────────────────────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Controller:
    """One real controller process."""

    def __init__(self, node: str, group: str, mcast_port: int,
                 started_at: Optional[float] = None):
        self.node = node
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.peer_id: Optional[str] = None
        self.started_at: Optional[float] = None
        self._log: List[str] = []
        cmd = [sys.executable, LAUNCHER, "--port", str(self.port), "--node", node,
               "--group", group, "--mcast-port", str(mcast_port)]
        if started_at is not None:
            cmd += ["--started-at", repr(started_at)]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._log.append(line)
            if self.peer_id is None and line.startswith('{"ready"'):
                try:
                    info = json.loads(line)
                    self.peer_id = info["peer_id"]
                    self.started_at = info["started_at"]
                except (ValueError, KeyError):
                    pass

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.node} exited early:\n" + "\n".join(self._log))
            try:
                self.get("/api/peers", timeout=1.0)
                if self.peer_id:
                    return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"{self.node} never became ready:\n" + "\n".join(self._log))

    # HTTP -------------------------------------------------------------------
    def get(self, path: str, timeout: float = 4.0) -> Dict[str, Any]:
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def post(self, path: str, payload: Dict[str, Any], timeout: float = 4.0) -> Dict[str, Any]:
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def raw(self, path: str, method: str = "GET", timeout: float = 4.0):
        """Return (status, headers) — for checking CORS and preflight."""
        req = urllib.request.Request(self.base + path, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers)

    # Metadata ---------------------------------------------------------------
    def push(self, key: str, value: Any, ts: Optional[int] = None) -> int:
        ts = hlc() if ts is None else ts
        self.post("/api/meta_event", {"key": key, "value": value, "ts": ts})
        return ts

    def fields(self) -> Dict[str, Any]:
        return (self.get("/api/meta_state") or {}).get("fields", {})

    def value(self, key: str) -> Any:
        item = self.fields().get(key)
        return None if item is None else item.get("value")

    def is_web_host(self) -> bool:
        return bool(self.get("/api/status").get("stable_host_active"))

    def peers(self) -> List[Dict[str, Any]]:
        return (self.get("/api/peers") or {}).get("peers", [])

    def stop(self, timeout: float = 6.0):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)

    def __repr__(self) -> str:
        return f"<Controller {self.node} :{self.port}>"


class Cluster:
    """A set of controllers isolated from every other MP32 Control on the network."""

    def __init__(self):
        # Admin-scoped multicast, randomised per run so two runs — or a run and a real
        # app — never share a group.
        rid = uuid.uuid4().int
        self.group = f"239.255.{(rid >> 8) % 200 + 20}.{rid % 200 + 20}"
        self.mcast_port = 20000 + (rid % 20000)
        self.run_id = uuid.uuid4().hex[:8]
        self.controllers: List[Controller] = []

    def key(self, name: str) -> str:
        """Namespace a metadata key to this run, so an unrelated controller's
        re-announced metadata can never satisfy or break an assertion."""
        return f"{name}-{self.run_id}"

    def start(self, node: str, started_at: Optional[float] = None) -> Controller:
        c = Controller(node, self.group, self.mcast_port, started_at)
        self.controllers.append(c)
        c.wait_ready()
        return c

    def start_all(self, *nodes: str, settle: float = 0.0) -> List[Controller]:
        out = [self.start(n) for n in nodes]
        if settle:
            time.sleep(settle)
        return out

    def stop_all(self):
        for c in self.controllers:
            c.stop()
        self.controllers.clear()

    def __enter__(self) -> "Cluster":
        return self

    def __exit__(self, *exc):
        self.stop_all()


# ── Waiting ───────────────────────────────────────────────────────────────────
def wait_for(predicate, timeout: float = CONVERGE_TIMEOUT, interval: float = 0.15) -> bool:
    """Poll until `predicate()` is true. Returns the final result rather than raising,
    so the caller reports it as a named check."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_value(controller: Controller, key: str, expected: Any,
               timeout: float = CONVERGE_TIMEOUT) -> bool:
    return wait_for(lambda: controller.value(key) == expected, timeout)


def wait_peers(controller: Controller, count: int, timeout: float = CONVERGE_TIMEOUT) -> bool:
    return wait_for(lambda: len(controller.peers()) >= count, timeout)


def report(checks: Checks) -> int:
    line = f"\n  {checks.suite}: {checks.passed}/{checks.total} checks passed"
    if checks.known:
        line += f", {len(checks.known)} known defect(s)"
    print(line)
    for f in checks.failures:
        print(f"    - FAILED: {f}")
    for k in checks.known:
        print(f"    - known:  {k}")
    for u in checks.unexpected_passes:
        print(f"    - PROMOTE: {u}")
    return 0 if not checks.failures else 1
