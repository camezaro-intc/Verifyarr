## Summary

<!-- What does this PR change and why? Link the issue: Fixes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New verification check / tier
- [ ] New integration or notification target
- [ ] Documentation / CI / tooling only
- [ ] Breaking change (config or behavior change users must act on)

## Checklist

### Code quality
- [ ] `black .` leaves no changes
- [ ] `ruff check .` passes
- [ ] `mypy` passes
- [ ] New code has Python 3.11+ type hints and uses `pathlib.Path`
- [ ] Subprocess, HTTP and file work is non-blocking (`asyncio.create_subprocess_exec`, `httpx.AsyncClient`, `asyncio.to_thread`) and never uses a shell

### Tests
- [ ] Tests added or updated (new checks have a passing **and** a failing case)
- [ ] `pytest` passes locally
- [ ] Tests do not require real media, ffmpeg/ffprobe, Tesseract or network access

### Safety and docs
- [ ] Anything that deletes/blocklists/searches stays behind `dry_run`
- [ ] No API keys, tokens or webhook URLs in code, tests, logs or screenshots
- [ ] `README.md` and `config.example.yaml` updated for any new behavior or option

## Tested environments

<!-- Tick everything you actually ran Verifyarr in, and note dry-run vs live. -->

- [ ] Docker / Docker Compose
- [ ] Windows (NSSM service or foreground)
- [ ] Linux (systemd or foreground)
- [ ] Not run end-to-end (unit tests only)

Radarr / Sonarr version(s) tested: <!-- e.g. Radarr 5.14, Sonarr 4.0.10 -->
Mode: <!-- dry-run / live -->

## Screenshots or logs (optional)
