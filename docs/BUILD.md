# MP32 Control — building a real app (no terminal/cmd window)

The app wraps the web server in a native window (pywebview) and bundles it with
PyInstaller. No terminal/cmd opens. Phones/iPads connect over Wi‑Fi to the URL shown
under the **⚙ Device** tab.

Both build scripts run `device_preflight.py` first. A release build is cancelled unless a
physical MP32 passes discovery, TCP, validated 32-channel config, and cyclic/VU checks.
For packaging-only CI without hardware, `MP32_SKIP_DEVICE_TEST=1` bypasses the check; such
an artifact is unverified and must not be published as a tested release.

## macOS (.app)
    ./build_mac.sh
Result: `dist/MP32 Control.app` — double-click to run. (Unsigned, so first launch:
right-click → Open → Open. No Apple Developer account needed.)

The bundle uses `assets/mp32-control.icns`, version 1.3.1 metadata and an ad-hoc code
signature. For a publicly trusted Developer ID build, install your Apple certificate and run:

    MP32_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./build_mac.sh

For normal distribution, notarize that signed build with Apple's notary service afterward.

## Windows (.exe) — build ON Windows

**See [WINDOWS_BUILD.md](WINDOWS_BUILD.md) for the full walkthrough**, including the two
prerequisites that are easy to miss and produce confusing failures: "Add Python to PATH" at
install time, and the Microsoft Edge WebView2 runtime, without which the window renders blank.

Short version, on a Windows machine:
1. Copy the whole project folder, including `assets/`.
2. Install Python 3.9+ with "Add Python to PATH" ticked.
3. Install the Microsoft Edge WebView2 Runtime.
4. Double-click `build_windows.bat`.
5. Right-click `windows_firewall.bat` and "Run as administrator" — once per machine, or
   phones and other controllers cannot reach it.

Result: `dist\MP32 Control.exe`. Unsigned, so SmartScreen on first run: "More info" →
"Run anyway". Set `MP32_ONEDIR=1` before building for a folder build that starts faster.

The executable uses `assets\mp32-control.ico` and `version_info.txt`. If a trusted
code-signing certificate is installed, open a Developer Command Prompt and set its SHA-1
thumbprint before building:

    set MP32_CERT_SHA1=YOUR_CERTIFICATE_THUMBPRINT
    build_windows.bat

Without publisher certificates, neither operating system can be made to publicly trust a
new build automatically; the build files are prepared to use them when available.

## Notes
- `DEMO_MODE` in mp32_gui.py must be `False` for the real device.
- The app listens on `0.0.0.0:8765` so phones on the same Wi‑Fi can connect.
- The stable LAN URL is `http://mp32-control.local:8765`; one Mac or Windows host
  advertises it through mDNS and another running controller takes over after host loss.
- Windows may request Private Network firewall access on first launch. Allow it so HTTP
  port 8765 and mDNS/UDP 5353 can be reached from the iPad and other controllers.
- The current host's direct IP remains visible under ⚙ Device as a fallback.
