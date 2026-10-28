# MP32 Control 1.3.2 — first Windows build: report

Written for the Claude Code session on the Mac that owns the repository, so the Windows
changes can be reviewed, merged and pushed to GitHub. Date: 2026-10-29.

## Result

- **The Windows build works.** `BUILD MP32 Control.bat` runs end to end and produces:
  - `dist\MP32 Control\MP32 Control.exe` — folder build, 34.7 MB
  - `dist\MP32-Control-1.3.2-Setup.exe` — Inno Setup installer, 20.5 MB, unsigned
- The packaged `.exe` starts, serves the panel on 8765, and the WebView2 window renders the
  full 32-channel UI (checked with a window capture). It shows "Offline" because no unit is on
  this network.
- A second copy now exits with code 1 and the "already running" message instead of opening a
  window (see finding 3).
- **The build is UNVERIFIED.** No MP32 is reachable on this network, so the physical preflight
  failed (`no host on this network reports an attached MP32`) and the build was made with
  `MP32_SKIP_DEVICE_TEST=1`. Do not publish this Setup.exe as a release. Rebuild on a Windows
  machine on the studio network so the preflight passes.

## Environment

Windows 11 Home 10.0.26300 (x64) · Python 3.14.5 from the Python install manager, reached as
`py -3` · pywebview 6.2.1 · pythonnet 3.2.1 · PyInstaller 6.22.3 · zeroconf 0.151.5 ·
Edge WebView2 156 · Inno Setup 6.7.3 (per-user install through winget).

## Findings

1. **The first build attempt failed at step 1, and the log didn't say why.** On this machine
   `python` resolves to the Microsoft Store alias in `WindowsApps`. It prints "Python was not
   found" and exits 49, even though a real Python 3.14 is installed and works as `py -3`. The
   `:no_python` branch didn't write to the log, so `build-log.txt` contained only its header.
2. **pythonnet works on Python 3.14.** pythonnet 3.2.1 installs and `import clr` works, so no
   Python version pin is needed. Older pythonnet 3.0.x does not support 3.14.
3. **Windows port-binding bug (affects Windows behaviour only):** `ThreadingHTTPServer` sets
   `SO_REUSEADDR`. On Windows that lets a second process bind a TCP port another process is
   already listening on, so the "another copy is running" check never fired.
4. **Startup race in `app.py` (affects all platforms):** `_wait_for_server()` polled
   `http://127.0.0.1:8765/` before its own server had bound. When another copy was already
   running, the poll got a 200 from *that* copy, so the second copy opened a window onto the
   first one before its own bind failed.
5. **Console encoding:** `main()` and the services print box-drawing and emoji characters.
   Redirected to a file on a legacy Windows code page, `print()` raises `UnicodeEncodeError`.
   That matters for `device_preflight.py >> build-log.txt`.
6. **Missing license texts:** on Windows the binary also bundles pythonnet, clr_loader, cffi,
   pycparser and bottle (MIT / BSD-3), which `collect_licenses.py` didn't collect.
7. **Inno Setup lookup:** a per-user install (winget `--scope user`, or "Install for me
   only") goes to `%LOCALAPPDATA%\Programs\Inno Setup 6`, which neither script searched.
8. **Inno Setup warnings:**
   - `ArchitecturesInstallIn64BitMode=x64` is deprecated in 6.7.
   - The `.iss` had no BOM. Inno Setup reads a script without a BOM as ANSI, so the "—" dashes
     in the WebView2 message box would come out as mojibake.
9. **Line endings:** the edited `.bat` files ended up with LF line endings partway through the
   work, and `cmd` labels and `goto` are unreliable in LF batch files. I can't say whether the
   originals arrived with LF or whether my editing tool caused it. All four `.bat` files are
   CRLF now. Recommended: add `.gitattributes` with `*.bat text eol=crlf` (and
   `*.iss text eol=crlf`) so git on the Mac can't convert them.
10. Harmless PyInstaller warnings remain: `webview.platforms.android` was not collected, and
    the hidden imports `pycparser.lextab` / `pycparser.yacctab` were not found.

## Changes, by file

| File | Change |
|---|---|
| `BUILD MP32 Control.bat` | Python detection tries `py -3`, `python`, `python3` and accepts the first one that actually runs code. Sets `PYTHONUTF8=1`. Builds in a private `.venv` next to the script, which is recreated if it is broken. Installs from `requirements.txt` and checks `import webview, clr, zeroconf, PyInstaller`. Preflight, license collection and PyInstaller run on the venv Python. New `:venv_failed` branch. `:no_python` now logs the failure and mentions the Store alias. `:die` logs `Result: FAILED`. Also searches the per-user Inno Setup path. CRLF. |
| `build_windows.bat` | Same Python detection, kept in `!PY!` and used for every call. Installs from `requirements.txt`. Sets `PYTHONUTF8=1`. CRLF. |
| `build_installer.bat` | Searches the per-user Inno Setup path too. |
| `installer/MP32 Control.iss` | UTF-8 BOM added. `ArchitecturesAllowed=x64compatible` and `ArchitecturesInstallIn64BitMode=x64compatible` replace `=x64`. |
| `requirements.txt` | Adds `pythonnet; sys_platform == "win32"`. Unchanged on the Mac. |
| `mp32_gui.py` | Near the top: `sys.stdout`/`sys.stderr` are reconfigured with `errors="replace"`, skipped when they are `None` as in a `--windowed` build. New `PanelHTTPServer(ThreadingHTTPServer)`: on `os.name == "nt"` it disables `allow_reuse_address` and sets `SO_EXCLUSIVEADDRUSE` before binding, which leaves POSIX unchanged. `main()` uses it. |
| `app.py` | Uses `M.PanelHTTPServer`. `_wait_for_server()` doesn't poll until `_runtime['server']` exists, i.e. its own bind succeeded. Removed the now unused `ThreadingHTTPServer` import. |
| `collect_licenses.py` | On `win32`, also collects pythonnet, clr_loader, cffi, pycparser and bottle. |
| `docs/WINDOWS_BUILD.md` | Setup filename corrected from 1.3.1 to 1.3.2. |

## For the Mac session to do

1. **Run `tests/run_all.py` on the Mac.** The tests aren't in the Windows kit, so they weren't
   run here. `app.py` and `mp32_gui.py` changed. The Mac code path is intended to behave the
   same, but the startup wait in `app.py` is shared code.
2. **Don't commit build output:** `.venv/`, `build/`, `dist/`, `build-log.txt`,
   `__pycache__/`. Check `.gitignore`.
3. **Add `.gitattributes`** for CRLF on `*.bat` / `*.iss` (finding 9).
4. **Update the docs that still say Windows is untested:**
   - `docs/WINDOWS_BUILD.md` → "What is still untested"
   - `LOE MIND.txt` → "This kit has never been built on Windows before"
   - `VERIFICATION.md` → the Windows build item is now done, unverified against hardware
   - Mention that `py -3` / the Python install manager is supported and that the build uses a
     private `.venv`.
5. `docs/WINDOWS_BUILD.md` says `build_windows.bat` builds a single file by default, while
   `BUILD MP32 Control.bat` defaults to a folder (`MP32_ONEFILE=1` switches it). Both are
   intended, but the doc only describes the older script.
6. Consider pinning minimum versions, e.g. `pythonnet>=3.2` for Python 3.14.

## Not verified

- Against a real MP32: preflight, live gain, phantom and VU, the multi-client session.
- Running the Setup.exe: install, shortcuts, firewall rules and their removal on uninstall.
  It needs administrator rights, so I didn't run it on this machine.
- Phones reaching the panel, `mp32-control.local` over mDNS on Windows, Mac↔Windows peer sync
  and web-host failover.
- Windows on ARM.
