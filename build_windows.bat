@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  Build MP32 Control.exe.  RUN THIS ON A WINDOWS MACHINE.
REM  PyInstaller cannot cross-compile, so the .exe must be built on Windows.
REM
REM  Needs: Python 3.9+ with "Add Python to PATH" ticked at install time.
REM  See docs\WINDOWS_BUILD.md for the full walkthrough.
REM
REM  Optional environment variables:
REM    MP32_ONEDIR=1            build a folder instead of one .exe (starts faster)
REM    MP32_SKIP_DEVICE_TEST=1  skip the physical MP32 check (produces an UNVERIFIED build)
REM    MP32_CERT_SHA1=<thumb>   sign with a code-signing certificate
REM ============================================================================
cd /d "%~dp0"

echo.
echo === Checking Python ===
python --version >nul 2>nul
if errorlevel 1 (
  echo ERROR: "python" was not found on PATH.
  echo Install Python 3.9 or newer from python.org and tick "Add Python to PATH".
  goto :fail
)
python --version
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.9 or newer is required.
  goto :fail
)

echo.
echo === Checking project files ===
for %%F in (app.py mp32_gui.py mp32_protocol.py version_info.txt) do (
  if not exist "%%F" (
    echo ERROR: %%F is missing. Copy the WHOLE project folder to this machine.
    goto :fail
  )
)
if not exist "assets\mp32-control.ico" (
  echo ERROR: assets\mp32-control.ico is missing. The assets folder must be copied too.
  goto :fail
)

echo.
echo === Installing build tools ===
REM pythonnet is what lets pywebview use the modern Edge WebView2 engine on Windows.
REM Without it pywebview falls back to a legacy engine and the panel renders incorrectly.
python -m pip install --upgrade pip
python -m pip install pywebview pythonnet pyinstaller "zeroconf>=0.32"
if errorlevel 1 (
  echo ERROR: installing dependencies failed. Check the network connection and retry.
  goto :fail
)

echo.
echo === Checking the Edge WebView2 runtime ===
set WV2=0
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set WV2=1
reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set WV2=1
if "!WV2!"=="0" (
  echo WARNING: the Edge WebView2 runtime was not detected.
  echo          The app will build, but the window may be blank or render incorrectly.
  echo          Install "Microsoft Edge WebView2 Runtime" ^(Evergreen Standalone Installer^)
  echo          from Microsoft, then run this script again.
  echo.
) else (
  echo WebView2 runtime found.
)

echo.
echo === Physical MP32 preflight ===
if not defined MP32_SKIP_DEVICE_TEST (
  python device_preflight.py
  if errorlevel 1 (
    echo.
    echo Preflight failed, so no release build was produced.
    echo The unit must be reachable on this network. See docs\WINDOWS_BUILD.md.
    goto :fail
  )
) else (
  echo WARNING: skipped by MP32_SKIP_DEVICE_TEST. This build is UNVERIFIED.
)

echo.
echo === Building ===
if defined MP32_ONEDIR (
  set PKGMODE=--onedir
  echo Mode: folder ^(faster startup^)
) else (
  set PKGMODE=--onefile
  echo Mode: single file ^(set MP32_ONEDIR=1 for a faster-starting folder build^)
)

python -m PyInstaller --windowed !PKGMODE! --noconfirm --clean ^
  --name "MP32 Control" ^
  --icon "assets\mp32-control.ico" ^
  --version-file "version_info.txt" ^
  --collect-all zeroconf ^
  --collect-all webview ^
  --add-data "assets;assets" ^
  --add-data "LICENSE;legal" ^
  --add-data "NOTICE;legal" ^
  --add-data "THIRD_PARTY_NOTICES.md;legal" ^
  app.py
if errorlevel 1 (
  echo ERROR: PyInstaller failed. The output above says why.
  goto :fail
)

echo.
echo === Signing ===
if defined MP32_CERT_SHA1 (
  where signtool >nul 2>nul
  if errorlevel 1 (
    echo WARNING: MP32_CERT_SHA1 is set but signtool.exe is not on PATH.
    echo          Install the Windows SDK, or run this from a Developer Command Prompt.
  ) else (
    signtool sign /sha1 %MP32_CERT_SHA1% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 "dist\MP32 Control.exe"
    if errorlevel 1 echo WARNING: signing failed. The build is still usable but unsigned.
  )
) else (
  echo Unsigned build. Set MP32_CERT_SHA1 to a certificate thumbprint to sign it.
  echo On first run Windows SmartScreen shows a warning: "More info" then "Run anyway".
)

echo.
echo ============================================================
if defined MP32_ONEDIR (
  echo  Done.  Folder:  dist\MP32 Control\
  echo  Copy the WHOLE folder. "MP32 Control.exe" inside it is the app.
) else (
  echo  Done.  File:  dist\MP32 Control.exe
  echo  Self-contained. Copy it anywhere and double-click.
)
echo.
echo  NEXT STEP: run windows_firewall.bat as Administrator once on each
echo  machine, so phones and other controllers can reach this one.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo Build cancelled.
echo.
pause
exit /b 1
