@echo off
REM ============================================================================
REM  Open the ports MP32 Control needs on this Windows machine.
REM  RUN AS ADMINISTRATOR: right-click this file, "Run as administrator".
REM
REM  Without these rules the app still controls the device and its own window
REM  works, but nothing else can reach it: phones cannot open the panel and
REM  other controllers see this machine as unreachable — which also makes it
REM  lose the web-host role to a machine that is reachable.
REM
REM  Run this ONCE per machine. Re-running is harmless: old rules are replaced.
REM ============================================================================
cd /d "%~dp0"

net session >nul 2>nul
if errorlevel 1 (
  echo ERROR: this must run as Administrator.
  echo Right-click windows_firewall.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo Removing any previous MP32 Control rules...
netsh advfirewall firewall delete rule name="MP32 Control HTTP 8765" >nul 2>nul
netsh advfirewall firewall delete rule name="MP32 Control mDNS 5353" >nul 2>nul
netsh advfirewall firewall delete rule name="MP32 Control peer sync 5009" >nul 2>nul
netsh advfirewall firewall delete rule name="MP32 Control device discovery 5008" >nul 2>nul

echo.
echo Adding rules for the private network profile only...

REM The panel itself, reached by phones, tablets and other controllers.
netsh advfirewall firewall add rule name="MP32 Control HTTP 8765" ^
  dir=in action=allow protocol=TCP localport=8765 profile=private
if errorlevel 1 goto :fail

REM mDNS, which publishes the stable http://mp32-control.local:8765 address.
netsh advfirewall firewall add rule name="MP32 Control mDNS 5353" ^
  dir=in action=allow protocol=UDP localport=5353 profile=private
if errorlevel 1 goto :fail

REM Controller-to-controller presence and shared metadata (names, colours,
REM groups, stereo links, shared cards).
netsh advfirewall firewall add rule name="MP32 Control peer sync 5009" ^
  dir=in action=allow protocol=UDP localport=5009 profile=private
if errorlevel 1 goto :fail

REM Device discovery announcements from the machine the unit is plugged into.
netsh advfirewall firewall add rule name="MP32 Control device discovery 5008" ^
  dir=in action=allow protocol=UDP localport=5008 profile=private
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  Done. Four inbound rules added for the private profile:
echo    TCP 8765  panel and API
echo    UDP 5353  mDNS, publishes mp32-control.local
echo    UDP 5009  controller presence and shared metadata
echo    UDP 5008  device discovery
echo.
echo  IMPORTANT: this network must be set to "Private" in Windows,
echo  not "Public". Settings then Network ^& Internet, pick the
echo  network, choose "Private network".
echo ============================================================
echo.
echo Current rules:
netsh advfirewall firewall show rule name=all | findstr /C:"MP32 Control"
echo.
pause
exit /b 0

:fail
echo.
echo ERROR: adding a rule failed. The output above says why.
echo.
pause
exit /b 1
