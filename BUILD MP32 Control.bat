@echo off
setlocal enabledelayedexpansion
title MP32 Control - Windows build
cd /d "%~dp0"

REM ============================================================================
REM  MP32 Control - one-click Windows build.
REM
REM  Double-click this file. It checks the machine, installs what it needs,
REM  builds the application, builds the installer if Inno Setup is present, and
REM  offers to open the firewall ports. Nothing else has to be run by hand.
REM
REM  Everything is written to build-log.txt as well as to this window, so a
REM  failure can be sent on without retyping it.
REM
REM  Optional, set before running if you need them:
REM    MP32_SKIP_DEVICE_TEST=1  skip the physical MP32 check (UNVERIFIED build)
REM    MP32_ONEFILE=1           build one .exe instead of a folder
REM    MP32_CERT_SHA1=<thumb>   sign with a code-signing certificate
REM    MP32_UNATTENDED=1        never ask anything; for running over SSH, where a prompt
REM                             would simply hang. Takes the sensible default at every
REM                             question and skips the firewall step, whose elevation
REM                             prompt appears on the console, not the remote session.
REM ============================================================================

set "LOG=%~dp0build-log.txt"
echo MP32 Control build log - %DATE% %TIME% > "%LOG%"

cls
echo.
echo   ==========================================================
echo     MP32 Control - Windows build
echo   ==========================================================
echo.
echo   This builds the application on this machine. PyInstaller
echo   cannot cross-compile, which is why it has to happen here
echo   rather than on the Mac the project is developed on.
echo.
echo   Log: build-log.txt
echo.
echo   ----------------------------------------------------------

REM -------------------------------------------------------------- 1. Python --
echo.
echo   [1/6] Python
REM  "python" alone is not a reliable test on Windows: with no python.org install on PATH it
REM  resolves to the Microsoft Store alias, which prints "Python was not found" and exits
REM  non-zero. The py launcher (python.org installer and the Python install manager) comes
REM  first, and each candidate has to actually run code before it counts.
REM  Box-drawing and emoji in the app's console output would otherwise crash a print() into
REM  the log on a legacy code page.
set "PYTHONUTF8=1"
set "PY="
for %%C in ("py -3" "python" "python3") do if not defined PY (
  %%~C -c "import sys" >nul 2>nul && set "PY=%%~C"
)
if not defined PY goto :no_python
for /f "tokens=*" %%V in ('!PY! --version 2^>^&1') do set "PYVER=%%V"
!PY! -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
echo         !PYVER!  ^(!PY!^)  OK
echo Python: !PYVER! via !PY! >> "%LOG%"

REM ------------------------------------------------------- 2. Project files --
echo.
echo   [2/6] Project files
set "MISSING="
for %%F in (app.py mp32_gui.py mp32_protocol.py device_preflight.py collect_licenses.py version_info.txt) do (
  if not exist "%%F" set "MISSING=!MISSING! %%F"
)
if not exist "assets\mp32-control.ico" set "MISSING=!MISSING! assets\mp32-control.ico"
if defined MISSING goto :missing_files
echo         all present  OK

REM -------------------------------------------------------- 3. Dependencies --
echo.
echo   [3/6] Dependencies  ^(this can take a few minutes the first time^)
REM  A private environment next to this file, so the build neither depends on nor changes
REM  whatever else is installed in the system Python. Recreated if it was copied from
REM  another machine and no longer runs.
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "!VPY!" (
  "!VPY!" -c "import sys" >nul 2>nul || rmdir /s /q "%~dp0.venv"
)
if not exist "!VPY!" (
  echo         creating the build environment .venv ...
  !PY! -m venv "%~dp0.venv" >> "%LOG%" 2>&1
  if errorlevel 1 goto :venv_failed
)
echo         installing pywebview, pythonnet, pyinstaller, zeroconf ...
"!VPY!" -m pip install --upgrade pip >> "%LOG%" 2>&1
"!VPY!" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_failed
"!VPY!" -c "import webview, clr, zeroconf, PyInstaller" >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_failed
echo         OK

echo.
echo         Edge WebView2 runtime:
set "WV2=0"
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set "WV2=1"
reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set "WV2=1"
if "!WV2!"=="1" (
  echo         found  OK
  echo WebView2: found >> "%LOG%"
) else (
  echo         NOT FOUND.
  echo.
  echo         The app will build and run, but its window will be blank or
  echo         render wrongly until this is installed. It is a free Microsoft
  echo         download - search for "Edge WebView2 Runtime" and take the
  echo         Evergreen Standalone Installer.
  echo.
  echo WebView2: NOT FOUND >> "%LOG%"
  if defined MP32_UNATTENDED (
    echo         carrying on anyway ^(unattended^)
  ) else (
    set /p "GOON=        Carry on building anyway? [Y/n] "
    if /i "!GOON!"=="n" goto :cancelled
  )
)

REM ------------------------------------------------------------ 4. Preflight --
echo.
echo   [4/6] Physical MP32 check
if defined MP32_SKIP_DEVICE_TEST goto :skip_preflight
echo         looking for the unit on this network ...
"!VPY!" device_preflight.py >> "%LOG%" 2>&1
if not errorlevel 1 (
  echo         found, 32 channels, live telemetry  OK
  goto :after_preflight
)
echo.
echo         The MP32 was NOT reachable from this machine.
echo.
echo         This gate exists so a release build cannot be made against
echo         hardware nobody checked. You can still build without it, but
echo         the result is marked unverified and should not be handed on
echo         as a release.
echo.
if defined MP32_UNATTENDED (
  echo         continuing unverified ^(unattended^)
) else (
  set /p "UNVER=        Build anyway, unverified? [y/N] "
  if /i not "!UNVER!"=="y" goto :preflight_failed
)
set "MP32_SKIP_DEVICE_TEST=1"
echo         continuing UNVERIFIED
echo Preflight: FAILED, continued unverified >> "%LOG%"
goto :after_preflight

:skip_preflight
echo         skipped by MP32_SKIP_DEVICE_TEST - this build is UNVERIFIED
echo Preflight: skipped by env >> "%LOG%"

:after_preflight

REM --------------------------------------------------------------- 5. Build --
echo.
echo   [5/6] Building the application
"!VPY!" collect_licenses.py "build\legal-third-party" >> "%LOG%" 2>&1
if errorlevel 1 goto :licenses_failed
echo         third-party license texts collected  OK

if defined MP32_ONEFILE (
  set "PKGMODE=--onefile"
  echo         mode: single .exe
) else (
  set "PKGMODE=--onedir"
  echo         mode: folder ^(starts faster, and what the installer wants^)
)

echo         running PyInstaller ... this takes a minute or two
"!VPY!" -m PyInstaller --windowed !PKGMODE! --noconfirm --clean ^
  --name "MP32 Control" ^
  --icon "assets\mp32-control.ico" ^
  --version-file "version_info.txt" ^
  --collect-all zeroconf ^
  --collect-all webview ^
  --add-data "assets;assets" ^
  --add-data "LICENSE;legal" ^
  --add-data "NOTICE;legal" ^
  --add-data "THIRD_PARTY_NOTICES.md;legal" ^
  --add-data "build\legal-third-party;legal\third-party" ^
  app.py >> "%LOG%" 2>&1
if errorlevel 1 goto :build_failed

set "APPPATH="
if exist "dist\MP32 Control\MP32 Control.exe" set "APPPATH=dist\MP32 Control\MP32 Control.exe"
if exist "dist\MP32 Control.exe" set "APPPATH=dist\MP32 Control.exe"
if not defined APPPATH goto :build_failed
echo         built: !APPPATH!  OK
echo Built: !APPPATH! >> "%LOG%"

if defined MP32_CERT_SHA1 (
  where signtool >nul 2>nul
  if errorlevel 1 (
    echo         signing skipped: signtool.exe is not on PATH
  ) else (
    signtool sign /sha1 %MP32_CERT_SHA1% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 "!APPPATH!" >> "%LOG%" 2>&1
    if errorlevel 1 (
      echo         WARNING: signing failed, build is usable but unsigned
    ) else (
      echo         signed  OK
    )
  )
)

REM ----------------------------------------------------------- 6. Installer --
echo.
echo   [6/6] Installer
REM  %ProgramFiles(x86)% carries literal parentheses in its name, which break an
REM  if-statement that cmd is already parsing for parentheses. Copy it to a plain
REM  variable first and the problem disappears.
set "ISCC="
set "PF86=%ProgramFiles(x86)%"
set "PF64=%ProgramFiles%"
if exist "!PF86!\Inno Setup 6\ISCC.exe" set "ISCC=!PF86!\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "!PF64!\Inno Setup 6\ISCC.exe" set "ISCC=!PF64!\Inno Setup 6\ISCC.exe"
REM  A per-user install (winget --scope user, or "Install for me only") lands here.
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"

if not defined ISCC (
  echo         Inno Setup 6 is not installed, so no installer was built.
  echo.
  echo         You do not need it to use the app - the build above runs as
  echo         it is. It is worth installing if you are putting this on
  echo         several machines: it produces one Setup.exe that installs
  echo         the app, makes the shortcuts and adds the firewall rules,
  echo         and removes the rules again when uninstalled.
  echo         Free, from jrsoftware.org/isdl.php - then run this again.
  echo Installer: Inno Setup not found >> "%LOG%"
  set "SETUPEXE="
) else (
  echo         using !ISCC!
  "!ISCC!" "installer\MP32 Control.iss" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo         WARNING: the installer failed to compile. See build-log.txt.
    echo         The application above is still fine.
    set "SETUPEXE="
  ) else (
    set "SETUPEXE="
    for %%S in ("dist\MP32-Control-*-Setup.exe") do set "SETUPEXE=dist\%%~nxS"
    echo         built: !SETUPEXE!  OK
    echo Installer: !SETUPEXE! >> "%LOG%"
  )
)

REM ------------------------------------------------------------- Firewall ---
echo.
echo   ----------------------------------------------------------
echo.
set "FWNOW=n"
if defined MP32_UNATTENDED goto :fw_unattended
if defined SETUPEXE goto :fw_installer
goto :fw_plain

:fw_unattended
echo   Firewall step skipped. Its Administrator prompt appears on the machine's
echo   own screen, not in a remote session. Run windows_firewall.bat as
echo   Administrator there, or install the Setup.exe, which adds the rules itself.
echo Firewall: skipped, unattended >> "%LOG%"
goto :fw_ask

:fw_installer
echo   The installer adds the firewall rules itself, so there is
echo   nothing further to run on the machines you install it on.
echo.
set /p "FWNOW=  Open the ports on THIS machine too, to test it here? [y/N] "
goto :fw_ask

:fw_plain
echo   Phones and other controllers can only reach this machine
echo   once four ports are open on the private network profile.
echo   Without them the app still controls the device and its own
echo   window works, but nothing else can see it.
echo.
set /p "FWNOW=  Open them now? Needs an Administrator prompt. [Y/n] "
if "!FWNOW!"=="" set "FWNOW=y"

:fw_ask
if /i "!FWNOW!"=="y" (
  echo.
  echo   Asking for Administrator rights ...
  powershell -NoProfile -Command "Start-Process -FilePath '%~dp0windows_firewall.bat' -Verb RunAs" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo   Could not elevate. Right-click windows_firewall.bat and choose
    echo   "Run as administrator" instead.
  ) else (
    echo   A second window handles it. Approve the prompt there.
  )
)

REM -------------------------------------------------------------- Summary ---
echo.
echo   ==========================================================
echo     Done
echo   ==========================================================
echo.
echo     Application:  !APPPATH!
if defined SETUPEXE echo     Installer:    !SETUPEXE!
echo.
if defined MP32_SKIP_DEVICE_TEST (
  echo     NOTE: built WITHOUT the physical device check. Usable for
  echo     testing, but do not hand this on as a release.
  echo.
)
if defined SETUPEXE (
  echo     To put this on the studio machines: copy the one Setup.exe
  echo     and run it there. It handles shortcuts and firewall rules.
) else (
  echo     To put this on another machine: copy the whole dist folder.
)
echo.
echo     On first run Windows SmartScreen may warn, because the build
echo     is unsigned: "More info" then "Run anyway".
echo.
echo     The network must be set to Private in Windows, not Public.
echo.
if not defined MP32_UNATTENDED pause
exit /b 0

REM ============================================================== failures ===
:no_python
echo         NOT FOUND
echo Python: NOT FOUND ^(tried py -3, python, python3^) >> "%LOG%"
echo.
echo   Python is not installed, or was installed without being added to PATH.
echo   A "Python was not found" message from the Microsoft Store shortcut
echo   means the same thing: no real Python is installed.
echo.
echo   Install Python 3.9 or newer from python.org, and on the FIRST page of
echo   the installer tick "Add python.exe to PATH" before clicking Install.
echo   That checkbox is the single most common reason this step fails.
echo.
echo   Then run this file again.
goto :die

:old_python
echo         !PYVER! is too old
echo.
echo   Python 3.9 or newer is required. Install a current version from
echo   python.org, ticking "Add python.exe to PATH", then run this again.
goto :die

:missing_files
echo         MISSING:!MISSING!
echo.
echo   Copy the WHOLE unzipped folder to this machine, not just this file.
echo   The build needs the source, the assets and the installer script.
goto :die

:venv_failed
echo         FAILED
echo.
echo   The private build environment .venv could not be created. Delete the
echo   .venv folder next to this file if it exists, then run this again.
echo   The detail is in build-log.txt.
goto :die

:pip_failed
echo         FAILED
echo.
echo   Installing the build tools failed. The usual causes are no internet
echo   access, or a proxy that blocks pypi.org. The detail is in
echo   build-log.txt.
goto :die

:preflight_failed
echo.
echo   Stopped: the MP32 was not reachable and an unverified build was declined.
echo.
echo   Things worth checking: the unit is powered and plugged into a computer
echo   on this network, that computer is awake, and this machine is on the same
echo   subnet rather than a guest network. Then run this again.
goto :die

:licenses_failed
echo         FAILED
echo.
echo   The third-party license texts could not be collected, and a binary
echo   build must not be distributed without them. Detail in build-log.txt.
goto :die

:build_failed
echo         FAILED
echo.
echo   PyInstaller did not produce an application. The reason is in
echo   build-log.txt - the last twenty lines usually say it plainly.
goto :die

:cancelled
echo.
echo   Cancelled.
goto :die

:die
echo Result: FAILED, nothing was built >> "%LOG%"
echo.
echo   Nothing was built. build-log.txt has the full output.
echo.
if not defined MP32_UNATTENDED pause
exit /b 1
