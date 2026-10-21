#ifndef AppVersion
  #define AppVersion "1.3.0"
#endif

[Setup]
AppName=Verifyarr
AppVersion={#AppVersion}
DefaultDirName=C:\Verifyarr
DisableDirPage=no
DirExistsWarning=no
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=Verifyarr-windows-x64-Setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

[Files]
; Source files compiled by PyInstaller and bundled NSSM
Source: "..\dist\verifyarr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\verifyarr\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Tasks]
Name: "installservice"; Description: "Register and start Verifyarr as a background Windows Service (NSSM)"; Flags: checkedonce

[Run]
Filename: "{app}\scripts\windows\install-service.bat"; Parameters: "--no-pause"; Description: "Installing service"; Flags: runhidden; Tasks: installservice
Filename: "http://localhost:7979"; Flags: shellexec runasoriginaluser postinstall nowait; Description: "Open Verifyarr Web Dashboard"
