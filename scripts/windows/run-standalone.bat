@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
:: Verifyarr - Standalone Console Runner
:: ============================================================================

set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%verifyarr.exe" (
    set "APP_DIR=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%..\..\verifyarr.exe" (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
) else (
    for %%I in ("%SCRIPT_DIR%..\..") do set "APP_DIR=%%~fI\"
)

cd /d "%APP_DIR%"

:: Bootstrap config.yaml from config.example.yaml if missing
if not exist "%APP_DIR%config.yaml" (
    if exist "%APP_DIR%config.example.yaml" (
        echo [INFO] Creating default config.yaml from config.example.yaml...
        copy /Y "%APP_DIR%config.example.yaml" "%APP_DIR%config.yaml" >nul
    )
)

:: Ensure runtime directories exist
if not exist "%APP_DIR%logs" mkdir "%APP_DIR%logs"
if not exist "%APP_DIR%data" mkdir "%APP_DIR%data"

if exist "%APP_DIR%verifyarr.exe" (
    echo [INFO] Starting Verifyarr standalone executable...
    "%APP_DIR%verifyarr.exe"
) else if exist "%APP_DIR%.venv\Scripts\python.exe" (
    echo [INFO] Standalone executable not found; launching via local .venv Python...
    "%APP_DIR%.venv\Scripts\python.exe" -m src.main
) else (
    echo [INFO] Standalone executable not found; launching via system Python...
    python -m src.main
)

if %errorLevel% neq 0 (
    echo.
    echo [ERROR] Verifyarr exited with code %errorLevel%.
    pause
)

endlocal

