# Verifyarr - Automated Media Verification & Mockbuster Guard for the Servarr Stack

[![GitHub release](https://img.shields.io/github/v/release/camezaro-intc/Verifyarr?logo=github)](https://github.com/camezaro-intc/Verifyarr/releases)
[![Docker image](https://img.shields.io/badge/docker-ghcr.io%2Fcamezaro-intc%2Fverifyarr-2496ED?logo=docker&logoColor=white)](https://github.com/camezaro-intc/Verifyarr/pkgs/container/verifyarr)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)

Verifyarr is a companion service for **Radarr**, **Sonarr** and **Jellyfin**. It receives a webhook every time a file is imported or upgraded, inspects the file with `ffprobe`/`ffmpeg`, and only lets media into your library if it is what it claims to be. Bad files are logged (dry-run) or automatically deleted, blocklisted and re-searched.

```text
[ Radarr / Sonarr ] --(Webhook: On Grab / On Import / On Upgrade)--> [ Verifyarr :7979 ]
     0. Tier 0 Pre-Download Manifest Inspection (qBittorrent Web API)
          MALICIOUS / FAKE RELEASE        CLEAN VIDEO / IN PROGRESS
             |                                     |
          Purge from qBit, remove from      Allow torrent download to finish
          Servarr queue, blocklist, re-search      |
                                                   v
     1. Path translation (Windows E:\ <-> Docker /media/)
     2. Concurrency throttle (asyncio.Semaphore)
     3. Tier 1 runtime delta -> Tier 2 audio sanity -> Tier 3 freeze + OCR
          PASSED                                  FAILED
            |                                       |
   Jellyfin library refresh                dry_run: log + alert (Discord)
                                           live:    delete file, blocklist release,
                                                    trigger replacement search, alert
```

## Table of contents

- [Why Verifyarr?](#why-verifyarr)
- [Features](#features)
- [Quick start: 1-Click Windows Installer](#quick-start-1-click-windows-installer-recommended)
- [Quick start: Docker Compose](#quick-start-docker-compose)
- [Quick start: Linux (systemd & tarball)](#quick-start-linux-systemd--portable-tarball)
- [Web Settings Management](#web-settings-management)
- [Radarr & Sonarr setup](#radarr--sonarr-setup)
- [Configuration reference](#configuration-reference)
- [How results work](#how-results-work)
- [API](#api)
- [Contributing, security and license](#contributing-security-and-license)

## Why Verifyarr?

Radarr and Sonarr decide what to grab from the **release title and metadata**, not from the video inside the file. Some releases are deliberately named to collide with big theatrical titles:

- A studio such as **The Asylum** (or any "mockbuster" label) sells a cheap knock-off of a blockbuster under a confusingly similar name. An **86-minute** low-budget film can end up imported in place of the **173-minute** theatrical epic you actually wanted, because the title and year match.
- Fake uploads loop a few seconds of video, or a still frame, to pad the file out to the expected length and size.
- Releases advertise **Atmos / TrueHD / 7.1** but ship a plain stereo track, or carry no audio in the movie's original language.

The most reliable tell is the **runtime delta**: the container duration compared with the runtime Radarr/Sonarr expect. Verifyarr checks that first, then looks deeper only if the file is plausible. You find out about a mockbuster before the family sits down to watch it.

## Features

**Multi-tier verification pipeline** (checks run in order; earlier tier failure stops the pipeline immediately)

| Tier | Check | What it does |
|------|-------|--------------|
| 0 | **Pre-Download Manifest Inspection** (qBittorrent) | Intercepts Sonarr/Radarr **On Grab** webhooks, inspects internal torrent file manifest via qBittorrent Web API, and purges fake releases (`.7z`, `.zip`, `.rar`, `.exe`, `.iso`, or releases with zero video files) before bytes download to disk. Blocklists the release and triggers an automatic replacement search. |
| 1 | **Runtime delta** | `ffprobe` container duration vs. the expected runtime. Fails if the difference exceeds `runtime_delta_threshold_minutes` (default 15). Multi-episode Sonarr files are scaled by episode count. |
| 2 | **Stream and channel verification** | At least one audio stream must match the movie's original language, and releases advertising Atmos, 7.1 or TrueHD must contain more than a stereo track. |
| 3 | **Freeze-frame / loop detection** | `ffmpeg freezedetect` on a 3-minute sample (minutes 10-13). Fails if more than 30% of the sample is frozen. |
| 3 | **Credits OCR** (optional) | Reads 2 opening and 2 ending frames with Tesseract and rejects blacklisted strings such as "The Asylum" or "Syfy Channel". |

**Operations & User Experience**

- **Modern Web Dashboard** at `/`: real-time view of recent verifications with timestamp, title, release name, probed vs. expected runtime, check breakdown, and failure reasons.
- **Arr-Style Settings UI** at `/settings`: configure verification engine, analysis tiers, Servarr connections, qBittorrent download client, notifications, and path mappings with live in-memory hot reloading.
- **Tiered Binary Resolution & Locked File Browser**: Auto-detects `ffprobe`, `ffmpeg`, and `tesseract` across system `PATH` and common vendor directories (Jellyfin Server, Chocolatey, Program Files, Linux `/usr`), supports manual overrides, and provides a restricted server-side filesystem navigator.
- **Connection Testers**: real-time API status validation for Radarr, Sonarr, Jellyfin, and qBittorrent with password masking toggles.
- **Force Approve**: one-click whitelist for false positives such as director's cuts and extended editions. It also triggers a Jellyfin refresh.
- **Dry-run mode** (on by default): runs every check and records the results, but never deletes or blocklists anything.
- **Safe remediation** (live mode): delete the file, blocklist the release, then trigger a replacement search. The search only runs if the first two steps succeeded.
- **Path mapping**: translate Servarr paths to container/host paths (`E:\Temp_Flex_Media\` to `/media/`), by prefix or regex.
- **Concurrency throttling**: `max_concurrent_probes` limits simultaneous ffmpeg/ffprobe runs so a season-pack import cannot exhaust disk I/O.
- **Multi-Platform Distributions**: 1-Click Windows Setup (`.exe`), Windows Portable (`.zip`), Linux Portable (`.tar.gz`), and multi-arch Docker images (`linux/amd64`, `linux/arm64`).

---

## Quick start: 1-Click Windows Installer (Recommended)

Verifyarr provides a standalone 1-click Windows installer that requires no Python installation:

1. Download **`Verifyarr-windows-x64-Setup.exe`** from the [Latest GitHub Release](https://github.com/camezaro-intc/Verifyarr/releases/latest).
2. Right-click and run as Administrator.
3. The installer extracts all dependencies to `C:\Verifyarr`, bundles `nssm.exe`, automatically registers and starts the background Windows Service, and opens the dashboard at <http://localhost:7979>.
4. Navigate to <http://localhost:7979/settings> to configure your Radarr/Sonarr URLs and API keys directly in the browser.

### Windows Portable Archive (`.zip`)
If you prefer not to use the installer:
1. Download **`Verifyarr-windows-x64.zip`** and extract to `C:\Verifyarr`.
2. Right-click `install-service.bat` and select **Run as Administrator** to register the service via NSSM.
3. To run manually in a console window instead, run `run-standalone.bat`.
4. To remove the service later, run `uninstall-service.bat` as Administrator.

### Running from Source (Developers)
```powershell
git clone https://github.com/camezaro-intc/Verifyarr.git C:\verifyarr
cd C:\verifyarr
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe -m src.main
```

---

## Quick start: Docker Compose

1. Create a folder for Verifyarr and download `docker-compose.yml` and `config.example.yaml` from this repository.
2. Initialize your configuration and data directory:

   ```bash
   cp config.example.yaml config.yaml
   mkdir -p data
   ```

3. Configure your media mounts and Servarr connections in `docker-compose.yml`:

   ```yaml
   services:
     verifyarr:
       image: ghcr.io/camezaro-intc/verifyarr:latest
       container_name: verifyarr
       restart: unless-stopped
       user: "1000:1000"                  # must be able to READ your media
       environment:
         TZ: "UTC"
       ports:
         - "7979:7979"
       volumes:
         - ./config.yaml:/app/config.yaml # mounted rw if you use the /settings web UI
         - ./data:/app/data               # verification history (SQLite)
         - /path/to/media:/media:ro       # Windows host: "E:/Temp_Flex_Media:/media:ro"
       extra_hosts:
         - "host.docker.internal:host-gateway"
       networks:
         - servarr

   networks:
     servarr:
       driver: bridge
   ```

4. Start Verifyarr and open the dashboard:

   ```bash
   docker compose up -d
   docker compose logs -f verifyarr
   # Browse to http://localhost:7979
   ```

---

## Quick start: Linux (systemd & portable tarball)

1. Download **`Verifyarr-linux-x64.tar.gz`** from the [Latest GitHub Release](https://github.com/camezaro-intc/Verifyarr/releases/latest).
2. Extract to `/opt/verifyarr`:

   ```bash
   sudo mkdir -p /opt/verifyarr
   sudo tar -xzvf Verifyarr-linux-x64.tar.gz -C /opt/
   cd /opt/verifyarr
   sudo cp config.example.yaml config.yaml
   ```

3. Install the included systemd service unit:

   ```bash
   sudo cp scripts/systemd/verifyarr.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now verifyarr
   ```

4. Check service status:

   ```bash
   sudo systemctl status verifyarr
   # Open http://localhost:7979 in your browser
   ```

---

## Web Settings Management

Verifyarr includes an Arr-style web settings interface at <http://localhost:7979/settings>:

- **Verification Engine**: Toggle Dry-Run mode, customize runtime delta tolerance (minutes), concurrency limits, and probe timeouts.
- **Analysis Tiers**: Enable/disable audio language/channel sanity checks, freeze-frame loop detection, and OCR mockbuster blacklist keywords.
- **Integrations**: Configure Radarr, Sonarr, and Jellyfin connections with inline "Test Connection" validation buttons and show/hide API key toggles.
- **Notifications**: Discord webhook integration for instant failure and error notifications.
- **System & Binaries**: Customize `ffprobe`, `ffmpeg`, and `tesseract` paths, and manage path mappings (`From` -> `To`) dynamically.
- **Hot Reloading**: Changes submitted via "Save Changes" are persisted atomically to `config.yaml` and hot-reloaded into memory immediately without restarting the service.

---

## Radarr & Sonarr setup

Configure webhooks in **Radarr**, and repeat in **Sonarr**:

1. Open Radarr/Sonarr in your browser and click **Settings** > **Connect**.
2. Click the **+** (Add Connection) tile and select **Webhook**.
3. Fill in the connection form:
   - **Name:** `Verifyarr`
   - **Notification Triggers:**
     - Tick **On Download** and **On Upgrade** (newer versions label these **On File Import** and **On File Upgrade**).
     - Tick **On Grab** if you use qBittorrent and want **Tier 0 Pre-Download Inspection** (instantly purges archives, `.exe`, and zero-video fake releases before bytes download).
     - Leave **On Rename** unticked.
   - **URL:** `http://localhost:7979/api/v1/webhook` (or `http://verifyarr:7979/api/v1/webhook` if using Docker container names).
   - **Method:** `POST`
4. Click **Test**. Verifyarr will return HTTP 200 with `{"status": "ok", "message": "Verifyarr webhook test successful"}`.
5. Click **Save**.

Import or re-import a movie/episode to observe real-time verification on the dashboard at <http://localhost:7979>.

---

## Configuration reference

Settings can be edited via the [Web UI](#web-settings-management) or in `config.yaml`. Environment variables use the `VERIFYARR_` prefix with `__` for nested keys (e.g. `VERIFYARR_RADARR__API_KEY=...`).

### `server`
| Key | Default | Description |
|-----|---------|-------------|
| `server.host` | `"0.0.0.0"` | Interface to listen on. |
| `server.port` | `7979` | HTTP port for the webhook, dashboard, and settings UI. |
| `server.log_level` | `"info"` | Logging verbosity: `info`, `debug`, `warning`, `error`. |

### `verification`
| Key | Default | Description |
|-----|---------|-------------|
| `verification.dry_run` | `true` | When `true`, verifications are recorded as `DRY_RUN` without deleting/blocklisting files. |
| `verification.max_concurrent_probes` | `2` | Maximum concurrent ffmpeg/ffprobe processes to protect disk I/O. |
| `verification.runtime_delta_threshold_minutes` | `15` | Tier 1: Max allowed difference between probed and expected duration (minutes). |
| `verification.enable_audio_sanity_check` | `true` | Tier 2: Validates audio language and channel layout against advertised metadata. |
| `verification.enable_freeze_detection` | `true` | Tier 3A: Scans sample window for frozen/loop frame traps using ffmpeg. |
| `verification.freeze_threshold_percent` | `30` | Maximum allowable static percentage in sample window before rejection. |
| `verification.enable_ocr_credit_check` | `false` | Tier 3B: Uses Tesseract OCR to flag mockbuster studios in opening/closing credits. |
| `verification.mockbuster_keywords` | `["The Asylum", "Asylum Presents", "Syfy Channel"]` | Blacklisted keywords flagged during OCR scanning. |

### `radarr`, `sonarr`, `jellyfin`
| Key | Default | Description |
|-----|---------|-------------|
| `radarr.enabled` | `true` | Process Radarr webhooks and enable Radarr v3 API remediation. |
| `radarr.url` | `"http://localhost:7878"` | Base URL of Radarr. |
| `radarr.api_key` | `""` | Radarr API key. |
| `sonarr.enabled` | `false` | Process Sonarr webhooks and enable Sonarr v3 API remediation. |
| `sonarr.url` | `"http://localhost:8989"` | Base URL of Sonarr. |
| `sonarr.api_key` | `""` | Sonarr API key. |
| `jellyfin.enabled` | `false` | Trigger a debounced library scan when imports pass verification. |
| `jellyfin.url` | `"http://localhost:8096"` | Base URL of Jellyfin. |
| `jellyfin.api_key` | `""` | Jellyfin API token. |

### `qbittorrent`
| Key | Default | Description |
|-----|---------|-------------|
| `qbittorrent.enabled` | `false` | Enable Tier 0 pre-download torrent inspection. |
| `qbittorrent.url` | `"http://localhost:8080"` | Base URL of the qBittorrent Web UI. |
| `qbittorrent.username` | `"admin"` | Username for qBittorrent Web API authentication. |
| `qbittorrent.password` | `""` | Password for qBittorrent Web API. |
| `qbittorrent.inspect_on_grab` | `true` | Intercept Servarr **On Grab** webhooks to inspect new torrent contents. |
| `qbittorrent.metadata_timeout_seconds` | `30` | Maximum wait time for torrent file tree manifest to resolve from peers. |
| `qbittorrent.blocked_extensions` | `[".7z", ".zip", ".rar", ".exe", ".iso", ".bat"]` | Blacklisted file extensions that trigger immediate purge and Servarr blocklisting. |

### `binaries`
| Key | Default | Description |
|-----|---------|-------------|
| `binaries.ffmpeg_path` | `""` | Path to `ffmpeg` executable (leave empty for automatic detection). |
| `binaries.ffprobe_path` | `""` | Path to `ffprobe` executable (leave empty for automatic detection). |
| `binaries.tesseract_path` | `""` | Path to `tesseract` executable (leave empty for automatic detection). |

---

## How results work

| Result | Meaning |
|--------|---------|
| `PASSED` | All checks passed. A Jellyfin refresh is requested (coalesced for season packs). |
| `DRY_RUN` | Checks failed but `dry_run` is on, so the file was kept and nothing was blocklisted. |
| `REJECTED` | Post-import checks failed in live mode: the file was deleted, the release blocklisted, and a replacement search triggered. |
| `REJECTED_PRE_DOWNLOAD` | Tier 0 check failed: torrent contained blacklisted archives/executables or zero video containers. Purged from qBittorrent, removed from the Servarr queue, blocklisted, and a replacement search triggered before downloading to disk. |
| `ERROR` | File could not be verified (file not found, `ffprobe` failure). Never remediated automatically. |

**Force Approve**: Click to whitelist any false positive (e.g. extended cuts). Whitelisted releases bypass future verification and trigger a Jellyfin library scan.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Web dashboard displaying recent verifications |
| `GET` | `/settings` | Interactive tabbed settings management interface |
| `GET` | `/api/v1/settings` | Retrieve active configuration as JSON |
| `POST` | `/api/v1/settings` | Save settings atomically to disk and hot-reload in memory |
| `POST` | `/api/v1/settings/test-connection` | Test credentials against Radarr, Sonarr, or Jellyfin |
| `POST` | `/api/v1/settings/test-qbittorrent` | Test authentication against qBittorrent Web API |
| `POST` | `/api/v1/settings/auto-detect-binaries` | Auto-detect installed paths for `ffprobe`, `ffmpeg`, and `tesseract` |
| `GET` | `/api/v1/settings/browse-fs` | Server-side filesystem navigator restricted to specific binaries |
| `POST` | `/api/v1/webhook` | Servarr webhook ingestion endpoint |
| `POST` | `/api/v1/whitelist/{item_id}` | Force-approve a rejected release and refresh Jellyfin |
| `GET` | `/health` | Application health and mode status probe |

---

## Contributing, security and license

- Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Report security issues privately as described in [SECURITY.md](SECURITY.md).
- Distributed under the [MIT License](LICENSE).
