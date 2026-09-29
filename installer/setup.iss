; Inno Setup Script for Minecraft Rotating-Host P2P Client
; Silent installation, Tailscale installer bundling/download, shortcuts, and TLauncher detection.

#define MyAppName "Minecraft Rotating-Host P2P"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Minecraft P2P Team"
#define MyAppExeName "MinecraftP2P.exe"

[Setup]
AppId={{9B7E319E-5201-4D6F-9C1C-624D0F8CE7A2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist_installer
OutputBaseFilename=MinecraftP2P_Installer_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; The compiled PyInstaller executable
Source: "..\dist\MinecraftP2P.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Check & install Tailscale silently if not already installed
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) { Write-Host 'Installing Tailscale...'; Invoke-WebRequest -Uri 'https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe' -OutFile '$env:TEMP\tailscale-setup.exe'; Start-Process '$env:TEMP\tailscale-setup.exe' -ArgumentList '/quiet /norestart' -Wait; Start-Process 'tailscale' -ArgumentList 'login' }"""; StatusMsg: "Ensuring Tailscale is installed and configured..."; Flags: runhidden

; Launch app after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Detect if TLauncher or standard Minecraft is installed
function InitializeSetup(): Boolean;
var
  AppData: String;
  TLauncherFound: Boolean;
begin
  AppData := ExpandConstant('{userappdata}');
  TLauncherFound := DirExists(AppData + '\.tlauncher') or DirExists(AppData + '\TLauncher');
  if not TLauncherFound then
  begin
    Log('Notice: TLauncher directory not detected in %APPDATA%. The player will be able to select their Minecraft directory.');
  end;
  Result := True;
end;
