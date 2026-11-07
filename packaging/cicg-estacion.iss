; Windows installer for the station: Start menu entry, optional desktop
; icon, and an uninstaller in "Apps" / "Add or remove programs". Per user, so
; it needs no administrator. The station's data stays in %USERPROFILE%\CICG
; and is never removed by the uninstaller.
;
; Built by .github/workflows/release.yml after PyInstaller:
;   ISCC /DAppVersion=0.2.0 packaging\cicg-estacion.iss
; Output: dist\EstacionCICG-<version>-Windows-Instalador.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E6C1A52-3F0B-4C1E-9B6A-2D4C7E1F0A93}
AppName=Estación CICG
AppVersion={#AppVersion}
AppVerName=Estación CICG {#AppVersion}
AppPublisher=Centro de Investigación Colibrí Gorriazul
DefaultDirName={autopf}\Estacion CICG
DefaultGroupName=Estación CICG
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=EstacionCICG-{#AppVersion}-Windows-Instalador
SetupIconFile=cicg-estacion.ico
UninstallDisplayIcon={app}\cicg-estacion.exe
UninstallDisplayName=Estación CICG
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\cicg-estacion.exe"; DestDir: "{app}"; Flags: ignoreversion
; Tells the station it is the installed copy, so its update notice can
; download and run the next installer (adapters/updates.py).
Source: "instalado.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Estación CICG"; Filename: "{app}\cicg-estacion.exe"
Name: "{group}\Desinstalar Estación CICG"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Estación CICG"; Filename: "{app}\cicg-estacion.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cicg-estacion.exe"; Description: "{cm:LaunchProgram,Estación CICG}"; Flags: nowait postinstall skipifsilent
