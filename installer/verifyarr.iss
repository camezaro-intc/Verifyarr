#ifndef AppVersion
  #define AppVersion "1.5.4"
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
; Overwrite application binaries on update
Source: "..\dist\verifyarr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.yaml,.env,data\*,*.db,*.log"
Source: "..\dist\verifyarr\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; Install config.example.yaml always for reference
Source: "..\config.example.yaml"; DestDir: "{app}"; DestName: "config.example.yaml"; Flags: ignoreversion

; ONLY install default config.yaml if the user doesn't already have one
Source: "..\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
; Keep the data directory intact across uninstalls/reinstalls
Name: "{app}\data"; Flags: uninsneveruninstall

[Tasks]
Name: "installservice"; Description: "Register and start Verifyarr as a background Windows Service (NSSM)"
Name: "installtesseract"; Description: "Download and install Tesseract OCR engine (Recommended for Tier 3B Mockbuster OCR)"; GroupDescription: "Optional OCR Utilities:"; Flags: unchecked; Check: ShouldOfferTesseract

[Run]
Filename: "{app}\scripts\windows\install-service.bat"; Parameters: "--no-pause"; Description: "Installing service"; Flags: runhidden; Tasks: installservice
Filename: "net.exe"; Parameters: "start Verifyarr"; Description: "Starting Verifyarr service"; Flags: runhidden
Filename: "http://localhost:7979"; Flags: shellexec runasoriginaluser postinstall nowait; Description: "Open Verifyarr Web Dashboard"

[Code]
function IsTesseractInstalled(): Boolean;
var
  RegPath: string;
begin
  Result := False;
  if FileExists('C:\Program Files\Tesseract-OCR\tesseract.exe') then
  begin
    Result := True;
    Exit;
  end;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Tesseract-OCR', 'InstallDir', RegPath) then
  begin
    if FileExists(AddBackslash(RegPath) + 'tesseract.exe') then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function ShouldOfferTesseract(): Boolean;
begin
  Result := not IsTesseractInstalled();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // 1. Stop existing service
  Exec('net', 'stop Verifyarr', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if FileExists(ExpandConstant('{app}\nssm.exe')) then
  begin
    Exec(ExpandConstant('{app}\nssm.exe'), 'stop Verifyarr', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  // 2. Kill running processes holding file locks
  Exec('taskkill', '/F /IM verifyarr.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);

  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  ConfigFile: string;
  Lines: TArrayOfString;
  I: Integer;
  FFprobePath, FFmpegPath, TesseractPath: string;
begin
  if CurStep = ssPostInstall then
  begin
    // If requested, silently install Tesseract via winget
    if WizardIsTaskSelected('installtesseract') then
    begin
      Exec('winget', 'install --id UB-Mannheim.TesseractOCR -e --silent --accept-source-agreements --accept-package-agreements', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;

    // Discover binary paths across standard locations with forward slashes
    FFprobePath := '';
    if FileExists('C:\Program Files\Jellyfin\Server\ffprobe.exe') then
      FFprobePath := 'C:/Program Files/Jellyfin/Server/ffprobe.exe'
    else if FileExists('C:\ffmpeg\bin\ffprobe.exe') then
      FFprobePath := 'C:/ffmpeg/bin/ffprobe.exe';

    FFmpegPath := '';
    if FileExists('C:\Program Files\Jellyfin\Server\ffmpeg.exe') then
      FFmpegPath := 'C:/Program Files/Jellyfin/Server/ffmpeg.exe'
    else if FileExists('C:\ffmpeg\bin\ffmpeg.exe') then
      FFmpegPath := 'C:/ffmpeg/bin/ffmpeg.exe';

    TesseractPath := '';
    if FileExists('C:\Program Files\Tesseract-OCR\tesseract.exe') then
      TesseractPath := 'C:/Program Files/Tesseract-OCR/tesseract.exe';

    // Ensure config.yaml exists
    ConfigFile := ExpandConstant('{app}\config.yaml');
    if not FileExists(ConfigFile) then
    begin
      if FileExists(ExpandConstant('{app}\config.example.yaml')) then
        FileCopy(ExpandConstant('{app}\config.example.yaml'), ConfigFile, False);

      // Write discovered binary paths into config.yaml only for new installations
      if FileExists(ConfigFile) and ((FFprobePath <> '') or (FFmpegPath <> '') or (TesseractPath <> '')) then
      begin
        if LoadStringsFromFile(ConfigFile, Lines) then
        begin
          for I := 0 to GetArrayLength(Lines) - 1 do
          begin
            if (FFprobePath <> '') and (Pos('ffprobe_path:', Lines[I]) > 0) then
              Lines[I] := '  ffprobe_path: "' + FFprobePath + '"';
            if (FFmpegPath <> '') and (Pos('ffmpeg_path:', Lines[I]) > 0) then
              Lines[I] := '  ffmpeg_path: "' + FFmpegPath + '"';
            if (TesseractPath <> '') and (Pos('tesseract_path:', Lines[I]) > 0) then
              Lines[I] := '  tesseract_path: "' + TesseractPath + '"';
          end;
          SaveStringsToFile(ConfigFile, Lines, False);
        end;
      end;
    end;

    // Ensure the Windows service is started on upgrade or fresh installation
    Exec('net.exe', 'start Verifyarr', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
