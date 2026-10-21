@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
:: Verifyarr - Windows NSSM Service Uninstaller
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

echo ============================================================================
echo  Uninstalling %SERVICE_NAME% Windows Service
echo ============================================================================

:: Verify Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges are required to remove a Windows service.
    echo         Please right-click uninstall-service.bat and select "Run as administrator".
    if not defined NO_PAUSE pause
    exit /b 1
)

:: Locate NSSM
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

if not defined NSSM_CMD (
    echo [ERROR] NSSM was not found in PATH or "%APP_DIR%".
    if not defined NO_PAUSE pause
    exit /b 1
)

echo [INFO] Stopping %SERVICE_NAME% service...
"%NSSM_CMD%" stop "%SERVICE_NAME%" >nul 2>&1

echo [INFO] Removing %SERVICE_NAME% service...
"%NSSM_CMD%" remove "%SERVICE_NAME%" confirm

if %errorLevel% equ 0 (
    echo [SUCCESS] %SERVICE_NAME% service has been stopped and removed.
) else (
    echo [WARNING] Could not remove %SERVICE_NAME% ^(it may not be installed^).
)

if not defined NO_PAUSE pause
endlocal
