; ============================================================================
;  MP32 Control — Windows installer
;
;  Build with Inno Setup 6 (free, from jrsoftware.org). Either double-click
;  ..\build_installer.bat, or open this file in the Inno Setup Compiler and
;  press F9.
;
;  What it produces: one Setup .exe that installs the app, creates shortcuts,
;  adds the four firewall rules, registers an uninstaller, and removes the
;  firewall rules again when uninstalled.
;
;  Requires the app to be built FIRST — run ..\build_windows.bat.
;  Both build shapes work: the folder build (MP32_ONEDIR=1) is preferred
;  because it starts faster, and the single .exe is used if that is what exists.
; ============================================================================

#define AppName        "MP32 Control"
#define AppVersion     "1.3.2"
#define AppPublisher   "Independent MP32 Control Project"
#define AppExeName     "MP32 Control.exe"
#define SrcDir         "..\dist"

; Prefer the folder build; fall back to the single-file build.
#if DirExists(SrcDir + "\MP32 Control")
  #define OneDirBuild
#elif !FileExists(SrcDir + "\MP32 Control.exe")
  #error Build the app first: run build_windows.bat. Nothing found in dist\.
#endif

[Setup]
AppId={{8F3C1A62-5D74-4B9E-9C2E-7A1B4C0D5E6F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=..\dist
OutputBaseFilename=MP32-Control-{#AppVersion}-Setup
SetupIconFile=..\assets\mp32-control.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE

; Administrator rights are required, not optional: the firewall rules and a
; Program Files install both need them.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "firewall"; Description: "Allow other controllers, phones and tablets to reach this computer"; GroupDescription: "Network:"; Flags: checkedonce

[Files]
#ifdef OneDirBuild
Source: "{#SrcDir}\MP32 Control\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
Source: "{#SrcDir}\MP32 Control.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs
#endif
Source: "..\LICENSE"; DestDir: "{app}\legal"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\NOTICE"; DestDir: "{app}\legal"; DestName: "NOTICE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\legal"; Flags: ignoreversion
Source: "..\docs\WINDOWS_BUILD.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Open the panel in a browser"; Filename: "http://mp32-control.local:8765"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Idempotent: delete any rule of the same name before adding it, so reinstalling
; or upgrading never leaves duplicates behind.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control HTTP 8765"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control mDNS 5353"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control peer sync 5009"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control device discovery 5008"""; Flags: runhidden; Tasks: firewall

; The panel and its API, reached by phones, tablets and other controllers.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""MP32 Control HTTP 8765"" dir=in action=allow protocol=TCP localport=8765 profile=private"; Flags: runhidden; StatusMsg: "Adding firewall rules..."; Tasks: firewall
; mDNS, which publishes the stable http://mp32-control.local:8765 address.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""MP32 Control mDNS 5353"" dir=in action=allow protocol=UDP localport=5353 profile=private"; Flags: runhidden; Tasks: firewall
; Controller presence and shared metadata: names, colours, groups, stereo links, cards.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""MP32 Control peer sync 5009"" dir=in action=allow protocol=UDP localport=5009 profile=private"; Flags: runhidden; Tasks: firewall
; Device discovery announcements from the machine the unit is plugged into.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""MP32 Control device discovery 5008"" dir=in action=allow protocol=UDP localport=5008 profile=private"; Flags: runhidden; Tasks: firewall

Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Leave the machine as it was found.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control HTTP 8765"""; Flags: runhidden; RunOnceId: "DelFwHttp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control mDNS 5353"""; Flags: runhidden; RunOnceId: "DelFwMdns"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control peer sync 5009"""; Flags: runhidden; RunOnceId: "DelFwPeer"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MP32 Control device discovery 5008"""; Flags: runhidden; RunOnceId: "DelFwDisc"

[Code]
function WebView2Installed(): Boolean;
var
  Key: String;
begin
  { The Evergreen runtime registers itself under EdgeUpdate Clients with this GUID.
    Checked in both views, because a 64-bit runtime on a 64-bit OS lands under
    WOW6432Node while some machines carry the native-view entry instead. }
  Key := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := RegKeyExists(HKEY_LOCAL_MACHINE, Key);
  if not Result then
  begin
    Key := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
    Result := RegKeyExists(HKEY_LOCAL_MACHINE, Key);
  end;
end;

function InitializeSetup(): Boolean;
var
  Reply: Integer;
  ErrCode: Integer;
begin
  Result := True;
  if WebView2Installed() then
    Exit;

  { Without this runtime the app installs and starts, but its window renders
    blank. Better to say so before installing than to leave someone debugging
    an empty window. }
  Reply := MsgBox(
    'The Microsoft Edge WebView2 Runtime was not found on this computer.' + #13#10 + #13#10 +
    'MP32 Control uses it to draw its window. Without it the app will start ' +
    'but the window will be blank.' + #13#10 + #13#10 +
    'Open the Microsoft download page now?' + #13#10 +
    'Choose No to install anyway and add the runtime later — no reinstall is needed.',
    mbConfirmation, MB_YESNOCANCEL);

  if Reply = IDYES then
  begin
    ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/',
              '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
    Result := False;   { let them install the runtime first, then run setup again }
  end
  else if Reply = IDCANCEL then
    Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { The firewall rules only apply to the private profile, which is the right
    scope for a studio LAN — the HTTP API has no authentication and must not be
    reachable on a public network. That does mean a network marked Public in
    Windows will still block everything, which is worth saying out loud. }
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('firewall') then
    MsgBox('Firewall rules added for the private network profile.' + #13#10 + #13#10 +
           'For phones and other controllers to reach this computer, this network must be ' +
           'set to Private in Windows, not Public.' + #13#10 + #13#10 +
           'Settings > Network & Internet > select the network > Private network.',
           mbInformation, MB_OK);
end;
