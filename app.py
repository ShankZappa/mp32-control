#!/usr/bin/env python3
"""Native-window launcher for the MP32 Control Panel (cross-platform).
Run: pip install pywebview  then  python3 app.py
Wraps the existing web server in a real OS window (not a browser tab)."""
import os
import sys
import threading
import socket
import time
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import mp32_gui as M


def _storage_dir():
    """Persistent per-user dir so the webview keeps localStorage (Local Notes,
    channel metadata, HLC clock) across app restarts. pywebview defaults to
    private mode, which wipes web storage on quit."""
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

_runtime = {}

def _serve():
    """Bind and serve before starting anything that reaches the network.

    The window is opened as soon as this server answers, so nothing that can stall on the
    network may run before the bind. The UI renders its own discovering/connecting states, so
    an early page showing "Finding device…" is always better than an empty window.
    """
    bc  = M.BeaconListener()
    dev = M.MP32Device(M.DEVICE_IP, M.DEVICE_PORT)
    dev.beacon = bc
    ps  = M.PeerService(dev)
    hs  = M.StableHostService(ps, M.SERVER_PORT)
    direct_url = f"http://{M.local_lan_ip()}:{M.SERVER_PORT}"
    M.Handler.device = dev; M.Handler.beacon = bc; M.Handler.peers_svc = ps
    M.Handler.host_service = hs
    M.Handler.direct_mobile_url = direct_url
    M.Handler.mobile_url = hs.url if hs.available else direct_url
    try:
        server = ThreadingHTTPServer((M.SERVER_BIND, M.SERVER_PORT), M.Handler)
    except OSError as e:
        # Previously this died silently inside a daemon thread and the window opened onto a
        # refused connection — the blank-page report.
        _runtime['bind_error'] = str(e)
        return
    _runtime.update(beacon=bc, device=dev, peers=ps, host=hs, server=server)
    threading.Thread(target=server.serve_forever, name="HTTPServer", daemon=True).start()

    bc.start()
    ps.start()
    hs.start()
    dev.start(session_enabled=M.DEVICE_SUPPORTS_MULTIPLE_CLIENTS)

def _cleanup():
    """Send peer/mDNS goodbyes and release the hardware before the process exits."""
    try: _runtime.get('host').stop()
    except Exception: pass
    try: _runtime.get('device').stop()
    except Exception: pass
    try: _runtime.get('peers').stop()
    except Exception: pass
    try: _runtime.get('beacon').stop()
    except Exception: pass
    try:
        server = _runtime.get('server')
        if server:
            server.shutdown()
            server.server_close()
    except Exception: pass

def _wait_for_server(host="127.0.0.1", port=M.SERVER_PORT, timeout=30.0):
    """Block until the UI page actually loads, so the window never opens to a blank page.

    A TCP connect is not enough: the socket accepts as soon as the server is bound, which can
    be before it is answering. This fetches the page and requires a real HTTP 200 with a body.
    """
    end = time.time() + timeout
    while time.time() < end:
        if _runtime.get('bind_error'):
            return False
        try:
            with urlopen(f"http://{host}:{port}/", timeout=1.0) as r:
                if r.status == 200 and len(r.read(512)) > 0:
                    return True
        except Exception:
            time.sleep(0.15)
    return False

if __name__ == "__main__":
    threading.Thread(target=_serve, daemon=True).start()
    if not _wait_for_server():
        reason = _runtime.get('bind_error') or f"the panel did not start within 30 seconds"
        print(f"MP32 Control could not start: {reason}", file=sys.stderr)
        if _runtime.get('bind_error'):
            print(f"Another copy is probably already running — open "
                  f"http://127.0.0.1:{M.SERVER_PORT} in a browser.", file=sys.stderr)
        _cleanup()
        sys.exit(1)
    import webview  # pip install pywebview

    class Api:
        """Native Save/Open dialogs for the packaged app (blob downloads don't work
        inside the webview). The browser version uses its own download instead."""
        def save_preset(self, content, suggested):
            try:
                w = webview.windows[0]
                path = w.create_file_dialog(webview.SAVE_DIALOG, save_filename=suggested or 'mp32-preset.json')
                if not path:
                    return ''
                if isinstance(path, (list, tuple)):
                    path = path[0]
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return path
            except Exception as e:
                return 'ERR:' + str(e)
        def load_preset(self):
            try:
                w = webview.windows[0]
                res = w.create_file_dialog(webview.OPEN_DIALOG, file_types=('JSON (*.json)', 'All files (*.*)'))
                if not res:
                    return ''
                p = res[0] if isinstance(res, (list, tuple)) else res
                with open(p, encoding='utf-8') as f:
                    return f.read()
            except Exception:
                return ''

    win = webview.create_window("MP32 Control Panel",
                                f"http://127.0.0.1:{M.SERVER_PORT}",
                                width=1480, height=840, min_size=(900, 600),
                                js_api=Api())
    def _kick():
        """Reload only if the webview really came up empty.

        The unconditional reload this replaces fired on every launch, including successful
        ones, and could throw away a page that had already rendered. The server is verified
        to be answering before the window is created, so an empty document here means the
        webview itself dropped the first paint — retry a few times, then leave it alone.
        """
        for _ in range(3):
            time.sleep(1.5)
            try:
                filled = win.evaluate_js(
                    "!!document.getElementById('stxt') && document.body.children.length > 0")
                if filled:
                    return
                win.load_url(f"http://127.0.0.1:{M.SERVER_PORT}")
            except Exception:
                return    # window closed or JS unavailable; nothing useful left to do
    threading.Thread(target=_kick, daemon=True).start()
    try:
        # private_mode=False + a real storage_path make localStorage survive a quit,
        # so Local Notes (and channel metadata) persist until the user deletes them.
        webview.start(private_mode=False, storage_path=_storage_dir())
    finally:
        _cleanup()
