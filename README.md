# Verifyarr - Automated Media Verification & Mockbuster Guard for the Servarr Stack

[![GitHub release](https://img.shields.io/github/v/release/OWNER/verifyarr?logo=github)](https://github.com/OWNER/verifyarr/releases)
[![Docker image](https://img.shields.io/badge/docker-ghcr.io%2FOWNER%2Fverifyarr-2496ED?logo=docker&logoColor=white)](https://github.com/OWNER/verifyarr/pkgs/container/verifyarr)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)

Verifyarr is a companion service for **Radarr**, **Sonarr** and **Jellyfin**. It receives a webhook every time a file is imported or upgraded, inspects the file with `ffprobe`/`ffmpeg`, and only lets media into your library if it is what it claims to be. Bad files are logged (dry-run) or automatically deleted, blocklisted and re-searched.

> **Before you start:** replace `OWNER` in the badges and image names with your GitHub user or organisation.

```text
[ Radarr / Sonarr ] --(Webhook: On Import / On Upgrade)--> [ Verifyarr :7979 ]
     1. Path translation (Windows E:\ <-> Docker /media/)
     2. Concurrency throttle (asyncio.Semaphore)
     3. Tier 1 runtime delta -> Tier 2 audio sanity -> Tier 3 freeze + OCR
          PASSED                          FAILED
            |                               |
   Jellyfin library refresh        dry_run: log + alert (Discord)
                                   live:    delete file, blocklist release,
                                            trigger replacement search, alert
```

## Table of contents

- [Why Verifyarr?](#why-verifyarr)
- [Features](#features)
- [Quick start: Docker Compose](#quick-start-docker-compose)
- [Quick start: native Windows (NSSM)](#quick-start-native-windows-nssm)
- [Radarr & Sonarr setup](#radarr--sonarr-setup)
- [Configuration reference](#configuration-reference)
- [How results work](#how-results-work)
- [Linux (systemd)](#linux-systemd)
- [API](#api)
- [Contributing, security and license](#contributing-security-and-license)

## Why Verifyarr?

Radarr and Sonarr decide what to grab from the **release title and metadata**, not from the video inside the file. Some releases are deliberately named to collide with big theatrical titles:

- A studio such as **The Asylum** (or any "mockbuster" label) sells a cheap knock-off of a blockbuster under a confusingly similar name. An **86-minute** low-budget film can end up imported in place of the **173-minute** theatrical epic you actually wanted, because the title and year match.
- Fake uploads loop a few seconds of video, or a still frame, to pad the file out to the expected length and size.
- Releases advertise **Atmos / TrueHD / 7.1** but ship a plain stereo track, or carry no audio in the movie's original language.

The most reliable tell is the **runtime delta**: the container duration compared with the runtime Radarr/Sonarr expect. Verifyarr checks that first, then looks deeper only if the file is plausible. You find out about a mockbuster before the family sits down to watch it.

## Features

**Multi-tier verification pipeline** (checks run in order; Tier 1 failure stops the pipeline immediately)

| Tier | Check | What it does |
|------|-------|--------------|
| 1 | **Runtime delta** | `ffprobe` container duration vs. the expected runtime. Fails if the difference exceeds `runtime_delta_threshold_minutes` (default 15). Multi-episode Sonarr files are scaled by episode count. |
| 2 | **Stream and channel verification** | At least one audio stream must match the movie's original language, and releases advertising Atmos, 7.1 or TrueHD must contain more than a stereo track. |
| 3 | **Freeze-frame / loop detection** | `ffmpeg freezedetect` on a 3-minute sample (minutes 10-13). Fails if more than 30% of the sample is frozen. |
| 3 | **Credits OCR** (optional) | Reads 2 opening and 2 ending frames with Tesseract and rejects blacklisted strings such as "The Asylum" or "Syfy Channel". |

**Operations**

- **Web dashboard** at `/`: the last 50 verifications with timestamp, title, release name, probed vs. expected runtime and result (`PASSED`, `REJECTED`, `DRY_RUN`, `ERROR`), including the reason for each failure.
- **Force Approve**: one-click whitelist for false positives such as director's cuts and extended editions. It also triggers a Jellyfin refresh.
- **Dry-run mode** (on by default): runs every check and records the results, but never deletes or blocklists anything.
- **Safe remediation** (live mode): delete the file, blocklist the release, then trigger a replacement search. The search only runs if the first two steps succeeded.
- **Path mapping**: translate Servarr paths to container/host paths (`E:\Temp_Flex_Media\` to `/media/`), by prefix or regex.
- **Concurrency throttling**: `max_concurrent_probes` limits simultaneous ffmpeg/ffprobe runs so a season-pack import cannot exhaust disk I/O. Webhooks are acknowledged immediately and verified in the background.
- **Notifications**: Jellyfin library refresh for good files, Discord messages for failures.
- **Runs anywhere** from one codebase: Docker, a Windows service (NSSM) or a Linux systemd service. `ffmpeg`/`ffprobe` are found automatically on `PATH`.

## Quick start: Docker Compose

1. Create a folder for Verifyarr and download `docker-compose.yml` and `config.example.yaml` from this repository (or clone it).
2. Create your config and the data folder:

   ```bash
   cp config.example.yaml config.yaml
   mkdir -p data
   ```

3. Edit `config.yaml`. At minimum set the Radarr (and/or Sonarr) `url` and `api_key`, and point `path_mappings` at the folder you mount as `/media`:

   ```yaml
   radarr:
     enabled: true
     url: "http://host.docker.internal:7878"   # or http://radarr:7878 on a shared Docker network
     api_key: "YOUR_RADARR_API_KEY"

   path_mappings:
     - remote_path: "E:\\Temp_Flex_Media\\"    # the path as Radarr/Sonarr report it
       local_path: "/media/"                   # the same folder inside this container
   ```

4. Use a compose file like this (production snippet; pulls the published image):

   ```yaml
   services:
     verifyarr:
       image: ghcr.io/OWNER/verifyarr:latest
       container_name: verifyarr
       restart: unless-stopped
       user: "1000:1000"                  # must be able to READ your media
       environment:
         TZ: "UTC"
       ports:
         - "7979:7979"
       volumes:
         - ./config.yaml:/app/config.yaml:ro
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

   The repository's own [`docker-compose.yml`](docker-compose.yml) builds the image locally instead and also reads an optional `.env` file (see [`.env.example`](.env.example)).

5. Start it and open the dashboard:

   ```bash
   docker compose up -d
   docker compose logs -f verifyarr
   # http://localhost:7979
   ```

Notes:
- Media is mounted **read-only**; Verifyarr only reads files. Deletions go through the Radarr/Sonarr API.
- Inside a container `localhost` means the container itself. Use `host.docker.internal` for apps on the Docker host, or service names when they share the network.
- Secrets can be supplied as environment variables instead of YAML, e.g. `VERIFYARR_RADARR__API_KEY` (see [Configuration reference](#configuration-reference)).

## Quick start: native Windows (NSSM)

This runs Verifyarr as a background Windows service on port **7979**.

1. **Install prerequisites** (PowerShell):

   ```powershell
   winget install Python.Python.3.11
   winget install Gyan.FFmpeg
   winget install NSSM.NSSM
   # Optional, only for the credits OCR check:
   winget install UB-Mannheim.TesseractOCR
   ```

   Open a **new** terminal afterwards and check `python --version`, `ffmpeg -version` and `ffprobe -version`.

2. **Get the code and install dependencies:**

   ```powershell
   git clone https://github.com/OWNER/verifyarr.git C:\verifyarr
   cd C:\verifyarr
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item config.example.yaml config.yaml
   ```

3. **Edit `C:\verifyarr\config.yaml`**: set the Radarr/Sonarr `url` and `api_key`. Radarr and Verifyarr share a filesystem here, so the default identity `path_mappings` entry (`E:\Temp_Flex_Media\` to `E:\Temp_Flex_Media\`) is fine; change it to your media drive. Leave `dry_run: true` for now.

4. **Test it in the foreground:**

   ```powershell
   .\.venv\Scripts\python.exe -m src.main
   ```

   Browse to <http://localhost:7979>, then stop it with `Ctrl+C`.

5. **Install the service** (run PowerShell **as Administrator**):

   ```powershell
   New-Item -ItemType Directory -Force C:\verifyarr\logs | Out-Null
   nssm install Verifyarr "C:\verifyarr\.venv\Scripts\python.exe" "-m src.main"
   nssm set Verifyarr AppDirectory "C:\verifyarr"
   nssm set Verifyarr DisplayName "Verifyarr"
   nssm set Verifyarr Description "Media verification companion for Radarr/Sonarr"
   nssm set Verifyarr Start SERVICE_AUTO_START
   nssm set Verifyarr AppStdout "C:\verifyarr\logs\verifyarr.log"
   nssm set Verifyarr AppStderr "C:\verifyarr\logs\verifyarr.log"
   nssm set Verifyarr AppRotateFiles 1
   nssm set Verifyarr AppRotateBytes 5242880
   nssm start Verifyarr
   ```

6. **Allow the port** if Radarr/Sonarr run on another machine:

   ```powershell
   New-NetFirewallRule -DisplayName "Verifyarr" -Direction Inbound -Protocol TCP -LocalPort 7979 -Action Allow
   ```

7. **Check it:** open <http://localhost:7979> and `Get-Content C:\verifyarr\logs\verifyarr.log -Tail 20`.

Windows tips:
- The service runs as `LocalSystem` by default, which may not see the same `PATH` or network drives as you. If `ffprobe` is not found, set `binaries.ffmpeg_path` / `binaries.ffprobe_path` in `config.yaml` to full paths. Mapped drive letters (like `Z:`) are not visible to services; use the real drive or a UNC path (`\\NAS\media`), and run the service under an account that can read it (`nssm set Verifyarr ObjectName ...`).
- After editing `config.yaml`, restart with `nssm restart Verifyarr`.
- Remove the service with `nssm remove Verifyarr confirm`.

## Radarr & Sonarr setup

Do this in **Radarr**, and again in **Sonarr** if you use it (and set `sonarr.enabled: true` plus its `url` and `api_key` in `config.yaml`).

1. Open Radarr/Sonarr in your browser and click **Settings** in the left sidebar.
2. Click **Connect**.
3. Click the **+** (Add Connection) tile and choose **Webhook**.
4. Fill in the form:
   - **Name:** `Verifyarr`
   - **Notification Triggers:** tick **On Download** and **On Upgrade**, and leave everything else unticked.

     > Newer versions of Radarr and Sonarr label these **On File Import** (the `Download` event) and **On File Upgrade** (the `Upgrade` event). Tick those. Leave **On Grab** and **On Import Complete** off: they are not verified.
   - **URL:** `http://localhost:7979/api/v1/webhook`
     (If Radarr/Sonarr run on another machine or in a container, replace `localhost` with the Verifyarr host or container name, e.g. `http://verifyarr:7979/api/v1/webhook`.)
   - **Method:** `POST`
   - **Username / Password:** leave empty (Verifyarr has no authentication)
   - **Tags:** leave empty to verify everything, or add tags to limit which items are checked.
5. Click **Test**. A green check means Verifyarr answered `{"status": "ok", "message": "Verifyarr webhook test successful"}`.
6. Click **Save**.

Import something (or re-import a file) and watch it appear on the dashboard at <http://localhost:7979>. Webhooks return immediately and verification runs in the background.

Run in **dry-run** until you have seen a few runs and are confident there are no false positives, then set `verification.dry_run: false` and restart.

## Configuration reference

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml`. The file location can be changed with the `VERIFYARR_CONFIG` environment variable.

Every value can also be set through environment variables prefixed with `VERIFYARR_`, using `__` between levels (for example `VERIFYARR_VERIFICATION__DRY_RUN=false`, `VERIFYARR_RADARR__API_KEY=...`). Environment variables take precedence over `config.yaml`.

### `server`

| Key | Default | Description |
|-----|---------|-------------|
| `server.host` | `"0.0.0.0"` | Interface to listen on. Use `127.0.0.1` to accept local connections only. |
| `server.port` | `7979` | HTTP port for the webhook and dashboard. |
| `server.log_level` | `"info"` | `critical`, `error`, `warning`, `info`, `debug` or `trace`. Use `debug` when reporting bugs. |

### `verification`

| Key | Default | Description |
|-----|---------|-------------|
| `verification.dry_run` | `true` | When `true`, all checks run and are recorded but nothing is deleted or blocklisted. |
| `verification.max_concurrent_probes` | `2` | Maximum simultaneous ffmpeg/ffprobe passes (disk I/O throttle for season packs). |
| `verification.runtime_delta_threshold_minutes` | `15` | Tier 1: reject when the probed and expected runtimes differ by more than this many minutes. |
| `verification.enable_audio_sanity_check` | `true` | Tier 2: original-language audio and Atmos/7.1/TrueHD channel checks. |
| `verification.enable_freeze_detection` | `true` | Tier 3: freeze-frame and loop detection on a 3-minute sample. |
| `verification.enable_ocr_credit_check` | `false` | Tier 3: OCR of opening/ending frames. Requires Tesseract on the host (included in the Docker image). |
| `verification.mockbuster_keywords` | `["The Asylum", "Asylum Presents", "Syfy Channel"]` | Case-insensitive strings that fail the OCR check when found in the credits. |
| `verification.freeze_min_duration_seconds` | `10` | Minimum length of a still segment to count as frozen (`freezedetect` `d=`). |
| `verification.freeze_ratio_threshold` | `0.30` | Fail when more than this fraction of the sample is frozen. |
| `verification.freeze_sample_start_seconds` | `600` | Start of the sample window (minute 10). Moved earlier automatically for short files. |
| `verification.freeze_sample_duration_seconds` | `180` | Length of the sample window. |
| `verification.probe_timeout_seconds` | `120` | Timeout for a single ffprobe/ffmpeg call (freeze detection gets 3x). |

### `radarr`, `sonarr`, `jellyfin`

| Key | Default | Description |
|-----|---------|-------------|
| `radarr.enabled` | `true` | Process Radarr webhooks and allow Radarr API calls. |
| `radarr.url` | `"http://localhost:7878"` | Base URL of Radarr, without a trailing path. |
| `radarr.api_key` | `""` | Radarr API key (Settings > General). Needed for delete, blocklist, search and metadata lookups. |
| `sonarr.enabled` | `false` | Process Sonarr webhooks and allow Sonarr API calls. |
| `sonarr.url` | `"http://localhost:8989"` | Base URL of Sonarr. |
| `sonarr.api_key` | `""` | Sonarr API key. |
| `jellyfin.enabled` | `false` | Refresh the Jellyfin library after a file passes (or is force-approved). |
| `jellyfin.url` | `"http://localhost:8096"` | Base URL of Jellyfin. |
| `jellyfin.api_key` | `""` | Jellyfin API key (Dashboard > API Keys). |

### `notifications`

| Key | Default | Description |
|-----|---------|-------------|
| `notifications.discord_webhook_url` | `""` | Discord webhook URL for failure/error summaries. Empty disables Discord. |

### `binaries`

| Key | Default | Description |
|-----|---------|-------------|
| `binaries.ffmpeg_path` | `""` | Full path to `ffmpeg`. Empty auto-detects it on `PATH`. |
| `binaries.ffprobe_path` | `""` | Full path to `ffprobe`. Empty auto-detects it on `PATH`. |
| `binaries.tesseract_path` | `""` | Full path to `tesseract` (OCR only). Empty auto-detects it on `PATH`. |

### `path_mappings`

A list; each entry translates a path as Servarr sees it into a path Verifyarr can open. Longest matching prefix wins; unmapped paths are used unchanged.

| Key | Default | Description |
|-----|---------|-------------|
| `path_mappings[].remote_path` | (required) | Path prefix as reported by Radarr/Sonarr, e.g. `"E:\\Temp_Flex_Media\\"`. Windows paths match case-insensitively with either slash. A regular expression when `use_regex` is true. |
| `path_mappings[].local_path` | (required) | Replacement path as seen by Verifyarr, e.g. `"/media/"`. With `use_regex` it may use group references such as `\1`. |
| `path_mappings[].use_regex` | `false` | Treat `remote_path` as a regular expression instead of a prefix. |

### `storage`

| Key | Default | Description |
|-----|---------|-------------|
| `storage.database_path` | `"data/verifyarr.db"` | SQLite file for history and the whitelist. Use `":memory:"` for a non-persistent history. In Docker keep it under the mounted `/app/data` folder. |

## How results work

| Result | Meaning |
|--------|---------|
| `PASSED` | All checks passed. A Jellyfin refresh is requested (coalesced for season packs). |
| `DRY_RUN` | Checks failed but `dry_run` is on, so the file was kept and nothing was blocklisted. |
| `REJECTED` | Checks failed in live mode: the file was deleted, the release blocklisted and a replacement search triggered. |
| `ERROR` | The file could not be verified (not found, `ffprobe` failure). **Never** remediated. Usually a `path_mappings` problem. |

A check that cannot give a verdict (no expected runtime, untagged audio, tool missing) is recorded as *skipped* and never causes a rejection.

**Force Approve** whitelists the release name so future imports of it skip verification, and requests a Jellyfin refresh. A `REJECTED` file has already been deleted, so approving it does not bring it back: use dry-run first to find false positives.

> **Security note:** the webhook and dashboard have no authentication. Keep port 7979 on a trusted network or behind an authenticating reverse proxy. See [SECURITY.md](SECURITY.md).

## Linux (systemd)

`/etc/systemd/system/verifyarr.service`:

```ini
[Unit]
Description=Verifyarr media verification service
After=network-online.target

[Service]
User=media
WorkingDirectory=/opt/verifyarr
ExecStart=/opt/verifyarr/.venv/bin/python -m src.main
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now verifyarr
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/webhook` | Radarr/Sonarr ingestion (`Test` returns 200, imports return 202) |
| `GET` | `/` | Dashboard: last 50 verifications |
| `POST` | `/api/v1/whitelist/{item_id}` | Force-approve a `REJECTED`/`DRY_RUN` entry and refresh Jellyfin |
| `GET` | `/health` | Liveness probe |

## Contributing, security and license

- Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
- Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Released under the [MIT License](LICENSE).
