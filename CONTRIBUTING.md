# Contributing to Verifyarr

Thanks for helping improve Verifyarr! Bug reports, verification ideas, docs and code are all welcome. By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). For security issues, see [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Local setup

Requirements: Python 3.11+, and `ffmpeg`/`ffprobe` on `PATH` if you want to run the service against real files (the unit tests do not need them). Tesseract is only needed for OCR work.

```bash
git clone <your-fork-url> verifyarr
cd verifyarr

python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Linux/macOS:          source .venv/bin/activate

pip install -r requirements-dev.txt     # runtime deps + pytest, ruff, black, mypy

cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml
python -m src.main                      # dashboard on http://localhost:7979
```

Keep `verification.dry_run: true` while developing so a failing check can never delete a real file. `config.yaml`, `.env` and `data/` are git-ignored.

Dependency management is plain `pip` + `requirements*.txt`; there is no Poetry project. If you prefer Poetry locally, you can use it as a personal wrapper around those files, but please do not commit Poetry-specific files.

## Code style and static analysis

All of the following must pass before a PR is merged. The settings live in [`pyproject.toml`](pyproject.toml).

```bash
black .            # formatting (line length 100); use `black --check .` to verify only
ruff check .       # linting and import sorting; add `--fix` for safe auto-fixes
mypy               # type checking of src/ (configured in pyproject.toml)
```

Conventions:

- Python 3.11+ type hints on all new functions and methods.
- Use `pathlib.Path` for filesystem paths, never string concatenation.
- Everything on the request path must be non-blocking: subprocesses via `asyncio.create_subprocess_exec` (never `shell=True`), HTTP via `httpx.AsyncClient`, blocking file or SQLite work via `asyncio.to_thread`.
- Never log or return API keys or webhook URLs.
- Add or update docs (`README.md`, `config.example.yaml`) when you change behavior or configuration.

## Running the tests

```bash
pytest                          # whole suite
pytest tests/test_runtime_delta.py -k mockbuster
```

Tests must not need real media, `ffmpeg`, `ffprobe`, Tesseract or network access. Fake `ffprobe` output through the `mock_ffprobe` fixture in [`tests/conftest.py`](tests/conftest.py), and use `httpx.MockTransport` for Servarr/Jellyfin/Discord calls. Every bug fix should come with a regression test, and every new verification check needs a passing and a failing case.

## Branching strategy

- `main` is always releasable. Do not commit to it directly.
- Create a short-lived branch from an up-to-date `main`:
  - `feature/<short-description>` for new functionality
  - `fix/<short-description>` for bug fixes
  - `docs/<short-description>` for documentation only
- Releases are cut from `main` by publishing a GitHub release with a `vMAJOR.MINOR.PATCH` tag. Publishing the release runs the Docker workflow, which tests the code and pushes multi-arch images to GHCR.

## Pull request guidelines

1. Open an issue first for anything larger than a small fix, so the approach can be agreed before you invest time.
2. Keep PRs focused: one logical change per PR.
3. Write clear commit messages in the imperative mood (`Fix runtime delta for multi-episode files`). [Conventional Commits](https://www.conventionalcommits.org) prefixes are welcome but not required.
4. Fill in the pull request template and make sure `black`, `ruff`, `mypy` and `pytest` pass.
5. Describe how you tested it: which environments (Docker, Windows service, Linux systemd) and whether you ran in dry-run or live mode.
6. Be responsive to review feedback. PRs are squash-merged, so your branch history does not need to be pristine.

Changes that make Verifyarr more destructive (anything that deletes, blocklists or otherwise changes a user's library) must stay behind `dry_run` and be called out clearly in the PR description.
