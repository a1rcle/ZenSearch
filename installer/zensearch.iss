; ZenSearch Inno Setup Installer Script
; Version: 2.0.0
; Architecture: x86_64-pc-windows-msvc

#define MyAppName "ZenSearch"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "ZenSearch Team"
#define MyAppURL "https://github.com/ZenSearch/ZenSearch"
#define MyAppExeName "zensearch.exe"

[Setup]
AppId={{C8D5E6A3-2026-47A1-88E2-2B47F03D9F88}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=..\target\installer
OutputBaseFilename=ZenSearch_Setup_v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=commandline dialog
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\assets\logo.ico
UninstallIconFile=..\assets\logo.ico

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "开机自动以最高权限启动 (免 UAC 穿透 UIPI，推荐)"; GroupDescription: "启动选项:"; Flags: checkedonce

[Files]
; Main executable
Source: "..\target\release\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Native Everything64 C FFI dynamic library
Source: "..\tools\Everything64.dll"; DestDir: "{app}"; Flags: ignoreversion
; Bundled silent Everything daemon
Source: "..\tools\Everything.exe"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\tools\Everything64.dll"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Setup highest-privilege task scheduler autostart if requested and running elevated
Filename: "schtasks"; Parameters: "/Create /F /TN ""ZenSearch_Startup"" /TR """"{app}\{#MyAppExeName}"" --silent"" /SC ONLOGON /RL HIGHEST"; Tasks: autostart; Check: IsAdminLoggedOn; Flags: runhidden
; Fallback to registry Run if not running elevated
Filename: "{app}\{#MyAppExeName}"; Parameters: "--silent"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove scheduled task cleanly on uninstall
Filename: "schtasks"; Parameters: "/Delete /F /TN ""ZenSearch_Startup"""; Flags: runhidden; RunOnceId: "DelTaskZenSearch"

[Code]
// Clean up background processes before installing or upgrading
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM zensearch.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM zensearch.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
