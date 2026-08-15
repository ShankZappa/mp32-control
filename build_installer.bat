@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  Build the Windows installer: MP32-Control-1.3.1-Setup.exe
REM  RUN THIS ON A WINDOWS MACHINE, after build_windows.bat has succeeded.
REM
REM  Needs Inno Setup 6 — free, from https://jrsoftware.org/isdl.php
REM
REM  The installer copies the app into Program Files, creates Start Menu and
REM  optional desktop shortcuts, adds the four firewall rules, and registers an
REM  uninstaller that removes those rules again.
REM ============================================================================
cd /d "%~dp0"

echo.
echo === Checking for a built app ===
if exist "dist\MP32 Control\MP32 Control.exe" (
  echo Found the folder build: dist\MP32 Control\
) else if exist "dist\MP32 Control.exe" (
  echo Found the single-file build: dist\MP32 Control.exe
  echo NOTE: a folder build starts faster inside an installed app.
  echo       Rebuild with MP32_ONEDIR=1 if you want that.
) else (
  echo ERROR: no built app found in dist\.
  echo Run build_windows.bat first.
  goto :fail
)

echo.
echo === Locating the Inno Setup compiler ===
set ISCC=
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do (
  if exist %%P set ISCC=%%P
)
if not defined ISCC (
  where ISCC.exe >nul 2>nul && for /f "delims=" %%I in ('where ISCC.exe') do set ISCC="%%I"
)
if not defined ISCC (
  echo ERROR: ISCC.exe was not found.
  echo Install Inno Setup 6 from https://jrsoftware.org/isdl.php and retry.
  goto :fail
)
echo Using !ISCC!

echo.
echo === Compiling the installer ===
!ISCC! "installer\MP32 Control.iss"
if errorlevel 1 (
  echo ERROR: the compiler reported a problem. The output above says why.
  goto :fail
)

echo.
echo ============================================================
echo  Done.  dist\MP32-Control-1.3.1-Setup.exe
echo.
echo  Copy that one file to each studio Windows machine and run it.
echo  It asks for Administrator rights, which it needs for the
echo  firewall rules and for installing into Program Files.
echo.
echo  Nothing else needs to be run on those machines: the installer
echo  does what windows_firewall.bat does.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo Installer not built.
echo.
pause
exit /b 1
