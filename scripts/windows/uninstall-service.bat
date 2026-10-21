@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
:: Verifyarr - Windows NSSM Service Uninstaller
:: ============================================================================

set "SERVICE_NAME=Verifyarr"
set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%verifyarr.exe" (
    set "APP_DIR=%SCRIPT_DIR%"
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
    pause
    exit /b 1
)

:: Locate NSSM
set "NSSM_CMD=nssm"
where nssm >nul 2>&1
if %errorLevel% neq 0 (
    if exist "%APP_DIR%nssm.exe" (
        set "NSSM_CMD=%APP_DIR%nssm.exe"
    ) else if exist "%SCRIPT_DIR%nssm.exe" (
        set "NSSM_CMD=%SCRIPT_DIR%nssm.exe"
    ) else (
        echo [ERROR] NSSM was not found in PATH or "%APP_DIR%".
        pause
        exit /b 1
    )
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

pause
endlocal

