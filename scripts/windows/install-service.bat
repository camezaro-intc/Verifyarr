@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
:: Verifyarr - Windows NSSM Service Installer (Standalone / Installer Package)
:: ============================================================================

set "SERVICE_NAME=Verifyarr"

:: Check for non-interactive / silent execution flags
set "NO_PAUSE="
for %%A in (%*) do (
    if /i "%%A"=="--no-pause" set "NO_PAUSE=1"
    if /i "%%A"=="/no-pause" set "NO_PAUSE=1"
    if /i "%%A"=="--silent" set "NO_PAUSE=1"
    if /i "%%A"=="/silent" set "NO_PAUSE=1"
)

:: Resolve application root directory (supports running from root or scripts\windows\)
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%verifyarr.exe" (
    set "APP_DIR=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%..\..\verifyarr.exe" (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
) else if exist "C:\Verifyarr\verifyarr.exe" (
    set "APP_DIR=C:\Verifyarr\"
) else (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
)

:: Trim trailing backslash for NSSM AppDirectory
set "APP_DIR_CLEAN=%APP_DIR:~0,-1%"
set "EXE_PATH=%APP_DIR%verifyarr.exe"

echo ============================================================================
echo  Installing %SERVICE_NAME% Windows Service
echo  Application Directory: %APP_DIR_CLEAN%
echo  Executable Path      : %EXE_PATH%
echo ============================================================================

:: Verify Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges are required to install a Windows service.
    echo         Please right-click install-service.bat and select "Run as administrator".
    if not defined NO_PAUSE pause
    exit /b 1
)

:: Locate or download NSSM (Non-Sucking Service Manager)
set "NSSM_CMD="
if exist "%APP_DIR%nssm.exe" (
    set "NSSM_CMD=%APP_DIR%nssm.exe"
) else if exist "%SCRIPT_DIR%nssm.exe" (
    set "NSSM_CMD=%SCRIPT_DIR%nssm.exe"
) else if exist "C:\Verifyarr\nssm.exe" (
    set "NSSM_CMD=C:\Verifyarr\nssm.exe"
) else if exist "C:\Bazarr\nssm.exe" (
    set "NSSM_CMD=C:\Bazarr\nssm.exe"
) else (
    where nssm >nul 2>&1
    if !errorLevel! equ 0 set "NSSM_CMD=nssm"
)

:: If NSSM is still missing, attempt automated download via PowerShell
if not defined NSSM_CMD (
    echo [INFO] NSSM not found locally. Downloading NSSM 2.24 via PowerShell...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
        "$zip = Join-Path $env:TEMP 'nssm-2.24.zip'; $extract = Join-Path $env:TEMP 'nssm_extracted'; " ^
        "$urls = @('https://nssm.cc/release/nssm-2.24.zip', 'https://github.com/kirillkovalenko/nssm/releases/download/2.24/nssm-2.24.zip'); " ^
        "$ok = $false; " ^
        "foreach ($u in $urls) { try { Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 15; $ok = $true; break } catch {} }; " ^
        "if ($ok) { Expand-Archive -Path $zip -DestinationPath $extract -Force; " ^
        "Copy-Item (Join-Path $extract 'nssm-2.24\win64\nssm.exe') '%APP_DIR%nssm.exe' -Force; " ^
        "Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue }" >nul 2>&1

    if exist "%APP_DIR%nssm.exe" (
        set "NSSM_CMD=%APP_DIR%nssm.exe"
        echo [INFO] NSSM successfully downloaded to %APP_DIR%nssm.exe.
    ) else (
        echo [ERROR] NSSM could not be found or downloaded automatically.
        echo         Please place nssm.exe into "%APP_DIR%" or install it via "winget install NSSM.NSSM".
        if not defined NO_PAUSE pause
        exit /b 1
    )
)

:: Verify standalone executable exists
if not exist "%EXE_PATH%" (
    echo [ERROR] Could not find "%EXE_PATH%".
    echo         Ensure this script is executed from an extracted Verifyarr standalone release.
    if not defined NO_PAUSE pause
    exit /b 1
)

:: Bootstrap config.yaml from config.example.yaml if missing
if not exist "%APP_DIR%config.yaml" (
    if exist "%APP_DIR%config.example.yaml" (
        echo [INFO] Creating default config.yaml from config.example.yaml...
        copy /Y "%APP_DIR%config.example.yaml" "%APP_DIR%config.yaml" >nul
    )
)

:: Create logs and data directories
if not exist "%APP_DIR%logs" mkdir "%APP_DIR%logs"
if not exist "%APP_DIR%data" mkdir "%APP_DIR%data"

:: Install and configure the service via NSSM
echo [INFO] Configuring %SERVICE_NAME% with NSSM (%NSSM_CMD%)...
"%NSSM_CMD%" status "%SERVICE_NAME%" >nul 2>&1
if %errorLevel% equ 0 (
    echo [INFO] %SERVICE_NAME% service is already registered. Updating configuration...
) else (
    echo [INFO] Registering new %SERVICE_NAME% service...
    "%NSSM_CMD%" install "%SERVICE_NAME%" "%EXE_PATH%"
)

"%NSSM_CMD%" set "%SERVICE_NAME%" Application "%EXE_PATH%"
"%NSSM_CMD%" set "%SERVICE_NAME%" AppDirectory "%APP_DIR_CLEAN%"
"%NSSM_CMD%" set "%SERVICE_NAME%" DisplayName "Verifyarr Media Verification Service"
"%NSSM_CMD%" set "%SERVICE_NAME%" Description "Multi-tier media verification companion for Radarr, Sonarr, and Jellyfin."
"%NSSM_CMD%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START
"%NSSM_CMD%" set "%SERVICE_NAME%" AppStdout "%APP_DIR%logs\service-stdout.log"
"%NSSM_CMD%" set "%SERVICE_NAME%" AppStderr "%APP_DIR%logs\service-stderr.log"
"%NSSM_CMD%" set "%SERVICE_NAME%" AppRotateFiles 1
"%NSSM_CMD%" set "%SERVICE_NAME%" AppRotateOnline 1
"%NSSM_CMD%" set "%SERVICE_NAME%" AppRotateBytes 10485760

echo [INFO] Starting %SERVICE_NAME% service...
"%NSSM_CMD%" start "%SERVICE_NAME%" >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Service start returned non-zero (it may already be running). Attempting restart...
    "%NSSM_CMD%" restart "%SERVICE_NAME%" >nul 2>&1
)

echo.
echo [SUCCESS] %SERVICE_NAME% service installed and started!
echo           Dashboard URL: http://localhost:7979
echo           Config File  : %APP_DIR%config.yaml
echo           Service Logs : %APP_DIR%logs\

if not defined NO_PAUSE pause
endlocal
