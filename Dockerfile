FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg (ffmpeg + ffprobe), tesseract for credit OCR, curl for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src

# Run unprivileged; compose can override with `user:` so the UID can read the media mount.
# /app/data holds the SQLite history and must be writable by whichever UID is used.
RUN useradd --create-home --uid 1000 verifyarr \
    && mkdir -p /app/data \
    && chown -R verifyarr:verifyarr /app \
    && chmod 0777 /app/data
USER verifyarr

# config.yaml is mounted at runtime (see docker-compose.yml): ./config.yaml -> /app/config.yaml
ENV VERIFYARR_CONFIG=/app/config.yaml

EXPOSE 7979

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:7979/health || exit 1

CMD ["python", "-m", "src.main"]
