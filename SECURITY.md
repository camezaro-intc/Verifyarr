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

## In scope

- **API key or secret exposure**: Radarr/Sonarr/Jellyfin API keys or the Discord webhook URL appearing in logs, the dashboard, error responses, history entries or notifications.
- **Arbitrary command or argument injection** through `ffmpeg`, `ffprobe` or `tesseract` invocations, for example crafted file paths, release titles or webhook fields that alter the command that is executed.
- **Path traversal or unintended file access** through webhook paths or path mappings.
- Unauthorized remediation, such as triggering deletion/blocklisting through crafted requests beyond the documented behavior.
- Vulnerabilities in the Docker image or its default configuration.

## Design notes and known limitations

- External programs are started with `asyncio.create_subprocess_exec` and an argument list. No shell is involved, and webhook-provided values are never interpolated into a shell command line.
- API keys are held as secrets in configuration and sent only in the `X-Api-Key` / `X-Emby-Token` headers of requests to the configured Servarr and Jellyfin URLs.
- **The webhook and dashboard (including Force Approve) have no built-in authentication.** Run Verifyarr on a trusted network, do not expose port 7979 to the internet, and put an authenticating reverse proxy in front of it if you need remote access. Reports that only state this are a known limitation, not a vulnerability.
- Keep `config.yaml` and `.env` out of version control and readable only by the service account.
