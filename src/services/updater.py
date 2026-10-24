"""Manual update service: checks GitHub releases and applies standalone updates."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from src import __version__

logger = logging.getLogger(__name__)


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Parse a version string (e.g. 'v1.6.0' or '1.5.6') into a comparable integer tuple."""
    cleaned = version_str.lstrip("vV").strip()
    parts = re.findall(r"\d+", cleaned)
    return tuple(int(p) for p in parts) if parts else (0,)


class UpdateManager:
    """Manages update checks against GitHub and self-update script execution on Windows."""

    CURRENT_VERSION: str = __version__
    REPO_API_URL: str = (
        os.getenv("VERIFYARR_REPO_API_URL")
        or "https://api.github.com/repos/camezaro-intc/Verifyarr/releases/latest"
    )

    def __init__(
        self,
        current_version: str | None = None,
        repo_api_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.current_version = current_version or self.CURRENT_VERSION
        self.repo_api_url = repo_api_url or self.REPO_API_URL
        self._http_client = http_client
        self._cached_result: dict[str, Any] | None = None
        self._cache_time: float = 0.0

    async def _check_web_redirect_fallback(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> dict[str, Any] | None:
        """Fallback to GitHub HTML release redirect when api.github.com is rate-limited."""
        web_url = "https://github.com/camezaro-intc/Verifyarr/releases/latest"
        try:
            req_headers = {
                "User-Agent": headers.get("User-Agent", f"Verifyarr/{self.current_version}")
            }
            fb_resp = await client.get(web_url, headers=req_headers, follow_redirects=False)
            if fb_resp.status_code in (301, 302, 303, 307, 308):
                loc = fb_resp.headers.get("location", "")
                tag_name = loc.rstrip("/").split("/")[-1].lstrip("vV").strip()
                if tag_name:
                    curr_tuple = parse_semver(self.current_version)
                    latest_tuple = parse_semver(tag_name)
                    download_url = (
                        f"https://github.com/camezaro-intc/Verifyarr/releases/download/"
                        f"v{tag_name}/Verifyarr-windows-x64.zip"
                    )
                    return {
                        "update_available": latest_tuple > curr_tuple,
                        "current_version": self.current_version,
                        "latest_version": tag_name,
                        "release_url": loc,
                        "changelog": f"Release v{tag_name}. See release page for notes.",
                        "download_url": download_url,
                    }
        except Exception as exc:
            logger.warning("GitHub web redirect fallback failed: %s", exc)
        return None

    async def check_for_updates(self, force: bool = False) -> dict[str, Any]:
        """Query GitHub API for the latest release, compare versions, and return update metadata."""
        now = time.time()
        if not force and self._cached_result and (now - self._cache_time) < 60.0:
            return self._cached_result

        github_token = (
            os.getenv("GITHUB_TOKEN")
            or os.getenv("GH_TOKEN")
            or os.getenv("VERIFYARR_GITHUB_TOKEN")
        )
        headers = {
            "User-Agent": f"Verifyarr/{self.current_version}",
            "Accept": "application/vnd.github.v3+json",
        }
        if github_token and github_token.strip():
            headers["Authorization"] = f"Bearer {github_token.strip()}"

        client_to_close: httpx.AsyncClient | None = None
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            client_to_close = client

        try:
            resp = await client.get(self.repo_api_url, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "GitHub release check API returned status %d; attempting web redirect fallback",
                    resp.status_code,
                )
                fallback = await self._check_web_redirect_fallback(client, headers)
                if fallback:
                    self._cached_result = fallback
                    self._cache_time = now
                    return fallback

                return {
                    "update_available": False,
                    "current_version": self.current_version,
                    "latest_version": self.current_version,
                    "release_url": "",
                    "changelog": "",
                    "download_url": "",
                }

            data = resp.json()
            if not isinstance(data, dict):
                fallback = await self._check_web_redirect_fallback(client, headers)
                if fallback:
                    self._cached_result = fallback
                    self._cache_time = now
                    return fallback

                return {
                    "update_available": False,
                    "current_version": self.current_version,
                    "latest_version": self.current_version,
                    "release_url": "",
                    "changelog": "",
                    "download_url": "",
                }

            tag_name = str(data.get("tag_name", "")).lstrip("vV").strip()
            html_url = str(data.get("html_url", ""))
            body = str(data.get("body", ""))

            download_url = ""
            assets = data.get("assets", [])
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict):
                        name = str(asset.get("name", "")).lower()
                        if name.endswith(".zip"):
                            download_url = str(asset.get("browser_download_url", ""))
                            break

            if not download_url:
                download_url = str(data.get("zipball_url", ""))

            curr_tuple = parse_semver(self.current_version)
            latest_tuple = parse_semver(tag_name)
            update_available = latest_tuple > curr_tuple

            result = {
                "update_available": update_available,
                "current_version": self.current_version,
                "latest_version": tag_name,
                "release_url": html_url,
                "changelog": body,
                "download_url": download_url,
            }
            self._cached_result = result
            self._cache_time = now
            return result
        except Exception as exc:
            logger.warning("Error checking for updates from %s: %s", self.repo_api_url, exc)
            fallback = await self._check_web_redirect_fallback(client, headers)
            if fallback:
                self._cached_result = fallback
                self._cache_time = now
                return fallback

            return {
                "update_available": False,
                "current_version": self.current_version,
                "latest_version": self.current_version,
                "release_url": "",
                "changelog": "",
                "download_url": "",
            }
        finally:
            if client_to_close:
                await client_to_close.aclose()

    def apply_update(self, download_url: str, exit_process: bool = True) -> None:
        """Download update package, extract to staging, write updater script, spawn and exit."""
        target_dir = Path(os.getenv("VERIFYARR_INSTALL_DIR") or r"C:\Verifyarr")
        temp_dir = Path(os.getenv("VERIFYARR_TEMP_DIR") or target_dir / "temp")
        staging_dir = temp_dir / "staging"
        zip_path = temp_dir / "update.zip"
        updater_script = temp_dir / "updater.ps1"

        # 1. Prepare temp and staging folders (clean old staging if exists)
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

        # 2. Download zip asset to C:\Verifyarr\temp\update.zip
        logger.info("Downloading update asset from %s to %s", download_url, zip_path)
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            resp = client.get(download_url)
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                f.write(resp.content)

        # 3. Extract zip to C:\Verifyarr\temp\staging
        logger.info("Extracting update zip to %s", staging_dir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(staging_dir)

        # 4. Write C:\Verifyarr\temp\updater.ps1 matching Sonarr/Radarr service update architecture
        ps_content = f"""# Verifyarr Updater Script (Sonarr/Radarr Lifecycle Architecture)
$ErrorActionPreference = 'Continue'
$target = "{target_dir}"
$staging = "{staging_dir}"
$logsDir = Join-Path $target "logs"
if (-not (Test-Path $logsDir)) {{
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}}
$logFile = Join-Path $logsDir "updater.log"

function Log-Update($msg) {{
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$timestamp] [Verifyarr Updater] $msg"
    Write-Output $line
    try {{
        Add-Content -Path $logFile -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
    }} catch {{}}
}}

Log-Update "Starting self-update..."
Log-Update "Target directory: $target"
Log-Update "Staging directory: $staging"

# 1. Un-nest staging directory if wrapped in a single root folder
$items = Get-ChildItem -Path $staging
if ($items.Count -eq 1 -and $items[0].PSIsContainer) {{
    $staging = $items[0].FullName
    Log-Update "Un-nested staging folder: $staging"
}}

# 2. Stop Verifyarr Windows service cleanly via ServiceController and NSSM
$nssm = Join-Path $target "nssm.exe"
$svc = Get-Service -Name "Verifyarr" -ErrorAction SilentlyContinue
if ($svc) {{
    Log-Update "Verifyarr service detected (Status: $($svc.Status)). Stopping service..."
    if (Test-Path $nssm) {{
        & $nssm stop Verifyarr 2>&1 | Out-Null
    }}
    if ($svc.Status -ne 'Stopped') {{
        Stop-Service -Name "Verifyarr" -Force -ErrorAction SilentlyContinue
        try {{
            $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
            Log-Update "Verifyarr service stopped successfully."
        }} catch {{
            Log-Update "Wait for service stop timed out ($($svc.Status))."
        }}
    }}
}} else {{
    Log-Update "No Verifyarr service found. Proceeding with process termination."
}}

# 3. Terminate any lingering verifyarr.exe processes
$procs = Get-Process -Name "verifyarr" -ErrorAction SilentlyContinue
if ($procs) {{
    Log-Update "Terminating lingering verifyarr processes..."
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}}

$maxWait = 10
while ((Get-Process -Name "verifyarr" -ErrorAction SilentlyContinue) -and ($maxWait -gt 0)) {{
    Start-Sleep -Milliseconds 500
    Get-Process -Name "verifyarr" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    $maxWait--
}}

# 4. Remove any existing .old files
Get-ChildItem -Path $target -Filter "*.old" -Recurse -Force -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 5. Mirror updated files into target using Robocopy
Log-Update "Synchronizing updated files from staging to target..."
$robocopyArgs = @(
    $staging, $target, "/E", "/R:3", "/W:1",
    "/XF", "nssm.exe", "config.yaml", ".env",
    "/XD", "data", "logs", "temp", ".git"
)
& robocopy @robocopyArgs 2>&1 | Out-Null
$rc = $LASTEXITCODE
Log-Update "Robocopy exit code: $rc"

# If robocopy failed (exit code >= 8), run fallback copy
if ($rc -ge 8) {{
    Log-Update "Robocopy reported errors ($rc). Executing Copy-Item fallback..."
    Get-ChildItem -Path $staging | ForEach-Object {{
        if (@("data", "config.yaml", ".env", "logs", "temp", "nssm.exe") -notcontains $_.Name) {{
            Copy-Item -Path $_.FullName -Destination $target -Recurse -Force `
                -ErrorAction Continue
        }}
    }}
}}

# 6. Restart Windows service or launch standalone application
if ($svc) {{
    Log-Update "Restarting Verifyarr Windows service..."
    if (Test-Path $nssm) {{
        & $nssm start Verifyarr 2>&1 | Out-Null
    }}
    Start-Service -Name "Verifyarr" -ErrorAction SilentlyContinue
    try {{
        $svc.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
        Log-Update "Verifyarr service is now Running."
    }} catch {{
        Log-Update "Wait for service start timed out (Status: $($svc.Status))."
    }}
}} else {{
    $targetExe = Join-Path $target "verifyarr.exe"
    if (Test-Path $targetExe) {{
        Log-Update "Restarting standalone application: $targetExe"
        Start-Process -FilePath $targetExe -WorkingDirectory $target
    }}
}}

# 7. Clean up staging & archive
Log-Update "Cleaning up temporary staging directory and archive..."
Remove-Item -Path $staging -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path "{zip_path}" -Force -ErrorAction SilentlyContinue

Log-Update "Update completed successfully."
"""
        with open(updater_script, "w", encoding="utf-8") as f:
            f.write(ps_content)

        # 5. Spawn updater.ps1 with attached log output handle and process group breakaway
        logs_dir = target_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = logs_dir / "updater.log"

        creationflags = (
            subprocess.CREATE_BREAKAWAY_FROM_JOB
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ) if os.name == "nt" else 0

        ps_exe = (
            shutil.which("powershell.exe")
            or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        ps_cmd = [
            ps_exe,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(updater_script),
        ]
        logger.info("Spawning updater process: %s", updater_script)
        try:
            with open(log_file_path, "a", encoding="utf-8") as lf:
                subprocess.Popen(
                    ps_cmd,
                    creationflags=creationflags,
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                )
        except Exception as exc:
            logger.error("Failed to spawn updater process: %s", exc)
            raise

        # 6. Service lifecycle management is delegated entirely to updater.ps1
        # (matching Sonarr/Radarr's external updater lifecycle to prevent NSSM crash-restart loops)
        logger.info("Update package extracted and updater script spawned successfully")
