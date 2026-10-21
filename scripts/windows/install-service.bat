@echo off
setlocal EnableDelayedExpansion

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges required. Right-click and select "Run as administrator".
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

cd /d "%~dp0"
if exist "..\..\verifyarr.exe" cd ..\..
set "APP_DIR=%CD%"
set "APP_EXE=%APP_DIR%\verifyarr.exe"

if not exist "%APP_EXE%" (
    echo [ERROR] Could not find verifyarr.exe in "%APP_DIR%"
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

:: Locate or download NSSM
set "NSSM_EXE="
if exist "%APP_DIR%\nssm.exe" (
    "%APP_DIR%\nssm.exe" version >nul 2>&1
    if !errorLevel! equ 0 set "NSSM_EXE=%APP_DIR%\nssm.exe"
)
if not defined NSSM_EXE (
    if exist "%~dp0nssm.exe" (
        "%~dp0nssm.exe" version >nul 2>&1
        if !errorLevel! equ 0 set "NSSM_EXE=%~dp0nssm.exe"
    )
)
if not defined NSSM_EXE (
    for %%X in (nssm.exe) do (
        "%%~$PATH:X" version >nul 2>&1
        if !errorLevel! equ 0 set "NSSM_EXE=%%~$PATH:X"
    )
)
if not defined NSSM_EXE (
    if exist "C:\Bazarr\nssm.exe" (
        "C:\Bazarr\nssm.exe" version >nul 2>&1
        if !errorLevel! equ 0 set "NSSM_EXE=C:\Bazarr\nssm.exe"
    )
)
if not defined NSSM_EXE (
    echo [INFO] Downloading NSSM Win64...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $zip = Join-Path $env:TEMP 'nssm.zip'; $extract = Join-Path $env:TEMP 'nssm_extracted'; Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $zip -TimeoutSec 15; Expand-Archive -Path $zip -DestinationPath $extract -Force; Copy-Item (Join-Path $extract 'nssm-2.24\win64\nssm.exe') '%APP_DIR%\nssm.exe' -Force; Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue"
    if exist "%APP_DIR%\nssm.exe" (
        "%APP_DIR%\nssm.exe" version >nul 2>&1
        if !errorLevel! equ 0 set "NSSM_EXE=%APP_DIR%\nssm.exe"
    )
)

if not defined NSSM_EXE (
    echo [ERROR] Failed to obtain working nssm.exe.
    if "%~1" neq "--no-pause" pause
    exit /b 1
)

echo ============================================================================
echo  Installing / Updating Verifyarr Service
echo  Directory: %APP_DIR%
echo ============================================================================

:: 1. Terminate running process and remove old service registration
taskkill /F /IM verifyarr.exe /T >nul 2>&1
"%NSSM_EXE%" stop Verifyarr >nul 2>&1
"%NSSM_EXE%" remove Verifyarr confirm >nul 2>&1

if not exist "%APP_DIR%\data" mkdir "%APP_DIR%\data"

:: Ensure default config exists
if not exist "%APP_DIR%\config.yaml" (
    if exist "%APP_DIR%\config.example.yaml" (
        copy /Y "%APP_DIR%\config.example.yaml" "%APP_DIR%\config.yaml" >nul
    )
)

:: 2. Register service
"%NSSM_EXE%" install Verifyarr "%APP_EXE%"
"%NSSM_EXE%" set Verifyarr AppDirectory "%APP_DIR%"
"%NSSM_EXE%" set Verifyarr AppStdout "%APP_DIR%\data\verifyarr.log"
"%NSSM_EXE%" set Verifyarr AppStderr "%APP_DIR%\data\verifyarr-error.log"
"%NSSM_EXE%" set Verifyarr Start SERVICE_AUTO_START
"%NSSM_EXE%" set Verifyarr AppRestartDelay 3000

:: 3. Start service
"%NSSM_EXE%" start Verifyarr

:: 4. Health Check
echo Checking service status on port 7979...
powershell -NoProfile -Command "Start-Sleep -Seconds 3"
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:7979/' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"

if %errorLevel% equ 0 (
    echo [SUCCESS] Verifyarr is active and listening at http://localhost:7979/
) else (
    echo [WARNING] Service started but port 7979 did not respond yet.
    echo Printing last 20 lines of %APP_DIR%\data\verifyarr-error.log:
    echo ----------------------------------------------------------------------------
    if exist "%APP_DIR%\data\verifyarr-error.log" (
        powershell -NoProfile -Command "Get-Content '%APP_DIR%\data\verifyarr-error.log' -Tail 20"
    ) else (
        echo Error log file was not created.
    )
    echo ----------------------------------------------------------------------------
)

if "%~1" neq "--no-pause" pause
