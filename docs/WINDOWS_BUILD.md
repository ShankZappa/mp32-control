# Building and running MP32 Control on Windows

PyInstaller cannot cross-compile, so the `.exe` has to be built on a Windows machine. The Mac
build in `dist/` is useless on Windows and vice versa.

Three scripts, depending on how many machines you are setting up:

| Script | What it does | Run as |
|---|---|---|
| `build_windows.bat` | builds the app | normal user |
| `build_installer.bat` | wraps the built app into one Setup .exe | normal user |
| `windows_firewall.bat` | opens the ports — **not needed if you use the installer** | **Administrator** |

For a single machine, `build_windows.bat` + `windows_firewall.bat` is enough. For several
machines, build the installer once and run that Setup on each — it handles the firewall
itself. See "Making an installer" below.

## Before you start

1. **Python 3.9 or newer** from python.org. At install time **tick "Add Python to PATH"** —
   almost every failure of this build is that box being left unticked.
2. **Microsoft Edge WebView2 Runtime** — the "Evergreen Standalone Installer" from Microsoft.
   This is the part that is easy to miss and produces the most confusing failure: the app
   builds and starts, but the window is blank or the layout is broken. pywebview uses WebView2
   to render the panel, and without it falls back to a legacy engine that cannot display it.
   Windows 11 usually has it already; Windows 10 often does not.
3. **The whole project folder**, not just the `.py` files. `assets\` must come along — the
   build stops with a clear error if the icon is missing, but the images the panel uses would
   otherwise be absent from a build that appears to succeed.
4. **The MP32 reachable on the same network.** The build runs a physical check and refuses to
   produce a release build if the unit cannot be reached. That is deliberate.

## Building

Double-click `build_windows.bat`, or run it from a terminal to keep the output visible.

It installs `pywebview`, `pythonnet`, `pyinstaller` and `zeroconf`, checks for the WebView2
runtime, runs the physical preflight, then packages the app.

`pythonnet` is not optional on Windows even though it is not needed on macOS: it is the bridge
pywebview uses to reach WebView2.

Result: **`dist\MP32 Control.exe`** — self-contained, copy it anywhere.

### Options

Set these before running the script (`set NAME=1` in the same terminal, or as environment
variables):

- **`MP32_ONEDIR=1`** — build a folder instead of a single file. The single `.exe` unpacks
  itself into a temporary directory on every launch, which adds a few seconds of startup and
  occasionally attracts antivirus attention. The folder build starts immediately. Copy the
  whole `dist\MP32 Control\` folder; the `.exe` inside it is the app. Worth using on machines
  where the app is opened daily.
- **`MP32_CERT_SHA1=<thumbprint>`** — sign the build with a code-signing certificate. Needs
  `signtool.exe`, which comes with the Windows SDK. Without a certificate the build is
  unsigned and SmartScreen shows a warning on first run: **More info → Run anyway**. This is
  expected and not a sign of a broken build.
- **`MP32_SKIP_DEVICE_TEST=1`** — skip the physical check. Produces an **unverified** build.
  Only for packaging work when no unit is available; never for something that will be used in
  a session.

## Making an installer instead (recommended for several machines)

The two scripts above are fine for one machine. For a studio with several Windows computers
there is a third option that does everything in one step, including the firewall.

**On the machine where you build**, once:

1. Install **Inno Setup 6** — free, from jrsoftware.org.
2. Build the app first (`build_windows.bat`). Prefer `MP32_ONEDIR=1`; an installed app should
   not unpack itself on every launch.
3. Double-click **`build_installer.bat`**.

Result: **`dist\MP32-Control-1.3.1-Setup.exe`** — one file.

**On every other machine**, just run that Setup once. It asks for Administrator rights, which
it genuinely needs, and then:

- installs into `Program Files\MP32 Control`
- creates a Start Menu entry, an optional desktop shortcut, and a shortcut that opens
  `http://mp32-control.local:8765` in a browser
- **adds the four firewall rules itself** — `windows_firewall.bat` is not needed
- registers a normal uninstaller in Add/Remove Programs
- **removes the firewall rules again when uninstalled**, leaving the machine as it was found

It also checks for the WebView2 runtime **before** installing and offers to open the download
page, rather than letting you discover the problem as a blank window afterwards.

Re-running the installer to upgrade is safe: the firewall rules are deleted before being
re-added, so they never accumulate.

The installer script is `installer\MP32 Control.iss` if you need to change the version, the
shortcut set, or the ports.

## After building — open the ports

*(Skip this if you used the installer — it does this for you.)*

Right-click **`windows_firewall.bat`** and choose **Run as administrator**. Once per machine.

Without it the app still connects to the device and its own window works fine, so the problem
is not obvious. What breaks is everything else: phones cannot open the panel, and other
controllers see this machine as unreachable — which also makes it lose the web-host role to a
machine that can be reached, since a host that nobody can talk to is not useful as a host.

Four inbound rules, private profile only:

| Port | Purpose |
|---|---|
| TCP 8765 | the panel and its API |
| UDP 5353 | mDNS, publishes `http://mp32-control.local:8765` |
| UDP 5009 | controller presence and shared metadata — names, colours, groups, stereo links, cards |
| UDP 5008 | device discovery announcements |

**The network must be set to Private, not Public.** The rules only apply to the private
profile, which is the safe choice for a studio LAN. Check under Settings → Network & Internet
→ pick the network → Private network. A studio network showing as Public is a common reason
for "it works on the Mac but not on Windows".

## First run

Double-click the `.exe`. On an unsigned build, SmartScreen: **More info → Run anyway**.

Windows may also ask whether to allow network access. Allow it for **private networks**. If
you have already run `windows_firewall.bat`, the rules are in place regardless of what you
answer here.

The window should show the channel strips within a second or two. If it is blank, the WebView2
runtime is missing — install it and restart the app; no rebuild is needed.

## Checking it works

1. The header shows **Online** and a channel count, with real gain values on the strips.
2. Open `http://mp32-control.local:8765` on a phone on the same Wi-Fi. If the name does not
   resolve, use the direct address shown under ⚙ Device.
3. With a Mac controller also running, exactly one of them shows **Web Host**, and both show
   the same values read from the hardware.
4. Make a group on one machine and confirm it appears on the other.

## Where the app keeps its state

`%APPDATA%\MP32 Control\`

That holds the last known device address and the webview's local storage — channel names,
colours, groups, local notes, and the metadata clock. Deleting it resets the panel to a clean
state without touching the device.

## Troubleshooting

**"python was not found"** — PATH. Reinstall Python with "Add Python to PATH" ticked, or use
the full path to `python.exe`.

**Blank or broken window** — WebView2 runtime missing. Install it; no rebuild needed.

**Preflight fails, no build produced** — the unit was not reachable. The device port moves when
the host's Antelope server restarts, and discovery has to find it again. Confirm the unit is
powered and on the same subnet, then retry.

**Phone cannot reach the panel** — in order: firewall script not run as Administrator, network
set to Public instead of Private, or the phone on a different Wi-Fi band or a guest network
with client isolation.

**Slow startup** — the single-file build unpacking itself. Rebuild with `MP32_ONEDIR=1`.

**Antivirus quarantines the exe** — common for unsigned PyInstaller single-file builds. Sign
it, or use `MP32_ONEDIR=1`, or add an exclusion.

## What is still untested

No Windows build of this version has been produced or run yet. The scripts are written from
the macOS build, which is verified, plus the platform differences above. The verification
matrix in `VERIFICATION.md` still lists building and smoke-testing on a clean Windows
machine, and Mac↔Windows web-host failover from an installed iPad PWA, as open items.
