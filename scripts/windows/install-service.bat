@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
:: Verifyarr - Windows NSSM Service Installer (Standalone Distribution)
:: ============================================================================

set "SERVICE_NAME=Verifyarr"

:: Resolve application root directory (supports running from root or scripts\windows\)
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%verifyarr.exe" (
    set "APP_DIR=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%..\..\verifyarr.exe" (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
) else (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
)

:: Trim trailing backslash for NSSM AppDirectory
set "APP_DIR_CLEAN=%APP_DIR:~0,-1%"
set "EXE_PATH=%APP_DIR%verifyarr.exe"

echo ============================================================================
echo  Installing %SERVICE_NAME% Windows Service
echo  Application Directory: %APP_DIR_CLEAN%
echo ============================================================================

:: Verify Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges are required to install a Windows service.
    echo         Please right-click install-service.bat and select "Run as administrator".
    pause
    exit /b 1
)

:: Check for NSSM in PATH or inside the application directory
set "NSSM_CMD=nssm"
where nssm >nul 2>&1
if %errorLevel% neq 0 (
    if exist "%APP_DIR%nssm.exe" (
        set "NSSM_CMD=%APP_DIR%nssm.exe"
    ) else if exist "%SCRIPT_DIR%nssm.exe" (
        set "NSSM_CMD=%SCRIPT_DIR%nssm.exe"
    ) else (
        echo [ERROR] NSSM ^(Non-Sucking Service Manager^) was not found in PATH or "%APP_DIR%".
        echo         Install NSSM via "winget install NSSM.NSSM" or "choco install nssm",
        echo         or place nssm.exe next to verifyarr.exe.
        pause
        exit /b 1
    )
)

:: Verify standalone executable exists
if not exist "%EXE_PATH%" (
    echo [ERROR] Could not find "%EXE_PATH%".
    echo         Ensure this script is executed from an extracted Verifyarr standalone release.
    pause
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
echo [INFO] Registering %SERVICE_NAME% with NSSM...
"%NSSM_CMD%" install "%SERVICE_NAME%" "%EXE_PATH%"
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
"%NSSM_CMD%" start "%SERVICE_NAME%"

echo.
echo [SUCCESS] %SERVICE_NAME% service installed and started!
echo           Dashboard URL: http://localhost:7979
echo           Config File  : %APP_DIR%config.yaml
echo           Service Logs : %APP_DIR%logs\
pause
endlocal

