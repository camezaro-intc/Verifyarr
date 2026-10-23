# Security Policy

## Supported versions

Security fixes are released for the **latest published release** only. Please update to the newest version before reporting.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for security problems.**

Report privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability** (GitHub private vulnerability reporting).
3. Describe the issue, how to reproduce it, and the impact.

If private reporting is not enabled on the repository, open a public issue that says only that you need a private security contact (no details), and a maintainer will arrange one.

Please include:

- Verifyarr version and installation method (Docker, Windows NSSM, Linux systemd)
- A minimal reproduction (a sanitized webhook payload and `config.yaml` excerpt are ideal)
- What an attacker could achieve

You can expect an acknowledgement within **7 days** and a status update within **14 days**. We will credit reporters in the release notes unless you prefer to stay anonymous.

## Scope for Vulnerability Reports

The following categories outline valid vulnerability types that security researchers are invited to report. **Note:** These are **not active defects or known unfixed bugs** in Verifyarr; rather, they represent the critical security boundaries the project is actively designed to defend:

- **API key or secret exposure:** Any unintended disclosure of Radarr/Sonarr/Jellyfin API keys, qBittorrent credentials, or Discord webhook URLs in application logs, database exports, error traces, or notifications.
- **Arbitrary command or argument injection:** Any bypass in `ffmpeg`, `ffprobe`, or `tesseract` process invocation (e.g., specially crafted torrent titles, file names, or webhook payloads that alter command execution).
- **Path traversal or unintended file access:** Any bypass allowing directory traversal via webhook path translation or the server-side filesystem navigator.
- **Unauthorized remediation:** Any mechanism allowing unauthorized deletion or blocklisting of media files outside the configured verification rules.
- **Container security issues:** Any vulnerability within the official Docker image or container execution environment.

## Out of Scope & Known Design Limitations

The following items are known architectural design choices and are **not** considered reportable security vulnerabilities:

- **No built-in authentication:** The webhook and dashboard (including Force Approve) have no built-in password or session authentication. Verifyarr is designed for trusted local networks. If remote access is required, place an authenticating reverse proxy (such as Authelia, Authentik, Caddy, or Nginx with Basic Auth) in front of port 7979.
- **Exposure from public listening:** Running Verifyarr directly exposed to the public internet without network firewalling or reverse-proxy protection.
- **Local machine access:** Physical or administrator-level access to the machine where `config.yaml`, `.env`, or the SQLite database resides.

## Security Design & Defense Mechanisms

- **Safe Subprocess Execution:** External binaries (`ffmpeg`, `ffprobe`, `tesseract`) are spawned using direct argument vectors (`subprocess.run(args, ...)`) with `stdin=subprocess.DEVNULL` and `shell=False`. Webhook and file values are never evaluated through a system shell.
- **Secret Masking:** Sensitive credentials (API keys, passwords, webhook tokens) are managed using Pydantic `SecretStr` models, prevented from leaking into history tables or logs, and sent only over dedicated request headers.
- **Strict File Browser Lock:** The server-side filesystem explorer only displays and allows selection of designated binary executables (`ffprobe`, `ffmpeg`, `tesseract`), preventing navigation or selection of arbitrary files.
- **Safe Path Resolution:** Path mapping uses component-based `PurePath` traversal protection to prevent arbitrary filesystem escapes.
