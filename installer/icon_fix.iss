; ============================================================
; WxReadAssistant v2.2.5 Icon-Fix Mini Installer (Inno Setup 6)
;
; Purpose: patch EXISTING v2.2.5 installations (built before the
; app icon was embedded) by replacing ONLY WxReadAssistant.exe.
;   - Icon lives in the EXE resource; shortcuts / uninstall entry
;     point at the exe, so they all pick up the new icon.
;   - The exe also carries the icon_store.py fix (white "W").
;   - Same AppId as the main installer -> in-place upgrade,
;     previous install dir reused, uninstall log appended.
;
; Build:
;   iscc /DDistDir=<full dir> /DOutputDir=<release dir> installer\icon_fix.iss
; ============================================================

#define MyAppName "WxReadAssistant"
#define MyAppExeName "WxReadAssistant.exe"

[Setup]
; MUST match the main installer's AppId for in-place upgrade.
AppId={{7F3A2B9C-1D4E-4A8B-B5C6-2E9F1A3D4B5E}
AppName={#MyAppName}
AppVersion=2.2.5
AppVerName={#MyAppName} v2.2.5 Icon Fix
AppPublisher={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
; Reuse the directory chosen by the main installer.
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir={#OutputDir}
OutputBaseFilename=WxReadAssistant-v2.2.5-iconfix-setup
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Languages]
; SourcePath = the directory containing this .iss (trailing backslash).
#define CsIsl SourcePath + "Languages\ChineseSimplified.isl"
#if FileExists(CsIsl)
Name: "chinesesimplified"; MessagesFile: {#CsIsl}
#elif FileExists(CompilerPath + "\Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
; Only the exe: icon resource + all pure-python app code live inside it.
Source: "{#DistDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Code]
// Braces in the strings below are literal: Inno does not expand
// constants inside [Code], and ISPP only reacts to "{#...}".
function IsAppInstalled(): Boolean;
begin
  Result :=
    RegKeyExists(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{7F3A2B9C-1D4E-4A8B-B5C6-2E9F1A3D4B5E}_is1') or
    RegKeyExists(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{7F3A2B9C-1D4E-4A8B-B5C6-2E9F1A3D4B5E}_is1');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsAppInstalled() then
  begin
    MsgBox('WxReadAssistant is not detected on this computer.' #13#10 +
           'Please run the full installer (WxReadAssistant-v2.2.5-setup.exe) first.',
           mbError, MB_OK);
    Result := False;
  end;
end;

[UninstallRun]
; Nothing here on purpose: user data in %APPDATA%\WxReadAssistant must survive uninstall.
