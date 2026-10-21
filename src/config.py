"""Verifyarr configuration: Pydantic v2 models, YAML loader and environment overrides.

Precedence (highest first): init kwargs, environment variables, ``.env`` file, YAML file.
Environment variables use the ``VERIFYARR_`` prefix and ``__`` as the nested delimiter,
e.g. ``VERIFYARR_RADARR__API_KEY``. The YAML location comes from ``VERIFYARR_CONFIG``
(default: ``./config.yaml``).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "VERIFYARR_CONFIG"
DEFAULT_CONFIG_FILE = "config.yaml"

BinaryName = Literal["ffmpeg", "ffprobe", "tesseract"]


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=7979, ge=1, le=65535)
    log_level: str = "info"

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"critical", "error", "warning", "info", "debug", "trace"}:
            raise ValueError(f"unsupported log level: {value!r}")
        return value


class VerificationConfig(BaseModel):
    dry_run: bool = True
    max_concurrent_probes: int = Field(default=2, ge=1)
    runtime_delta_threshold_minutes: float = Field(default=15, ge=0)
    enable_audio_sanity_check: bool = True
    enable_freeze_detection: bool = True
    enable_ocr_credit_check: bool = False
    mockbuster_keywords: list[str] = Field(
        default_factory=lambda: ["The Asylum", "Asylum Presents", "Syfy Channel"]
    )
    # Optional tuning knobs for Tier 3.
    freeze_min_duration_seconds: float = Field(default=10, gt=0)
    freeze_ratio_threshold: float = Field(default=0.30, ge=0, le=1)
    freeze_sample_start_seconds: int = Field(default=600, ge=0)
    freeze_sample_duration_seconds: int = Field(default=180, gt=0)
    probe_timeout_seconds: float = Field(default=120, gt=0)


class ArrConfig(BaseModel):
    """Connection details for a Servarr application (Radarr / Sonarr)."""

    enabled: bool = False
    url: str = ""
    api_key: SecretStr = SecretStr("")

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")


class JellyfinConfig(ArrConfig):
    """Jellyfin shares the url/api_key/enabled shape of the Servarr config."""


class NotificationsConfig(BaseModel):
    discord_webhook_url: str = ""


class BinariesConfig(BaseModel):
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    tesseract_path: str = ""

    def resolve(self, name: BinaryName) -> Path | None:
        """Locate a binary: explicit config override first, then ``shutil.which``.

        ``shutil.which`` honours ``PATHEXT`` on Windows, so ``ffmpeg`` finds ``ffmpeg.exe``.
        """
        override = getattr(self, f"{name}_path").strip()
        if override:
            candidate = Path(override).expanduser()
            if candidate.is_file():
                return candidate
            # Allow a bare command name or a directory-less override.
            found = shutil.which(override)
            if found:
                return Path(found)
            logger.warning("Configured %s path %s does not exist", name, override)
            return None
        found = shutil.which(name)
        return Path(found) if found else None


class PathMapping(BaseModel):
    """Translate a path as seen by Servarr (remote) to one Verifyarr can open (local)."""

    remote_path: str
    local_path: str
    use_regex: bool = False


class StorageConfig(BaseModel):
    database_path: str = "data/verifyarr.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VERIFYARR_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    radarr: ArrConfig = Field(
        default_factory=lambda: ArrConfig(enabled=True, url="http://localhost:7878")
    )
    sonarr: ArrConfig = Field(default_factory=lambda: ArrConfig(url="http://localhost:8989"))
    jellyfin: JellyfinConfig = Field(
        default_factory=lambda: JellyfinConfig(url="http://localhost:8096")
    )
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    binaries: BinariesConfig = Field(default_factory=BinariesConfig)
    path_mappings: list[PathMapping] = Field(default_factory=list)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = YamlConfigSettingsSource(
            settings_cls, yaml_file=get_config_path(), yaml_file_encoding="utf-8"
        )
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


def get_config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_FILE)).expanduser()


def load_settings() -> Settings:
    """Load settings from YAML + environment. A missing YAML file falls back to defaults."""
    path = get_config_path()
    if not path.is_file():
        logger.warning("Config file %s not found; using defaults and environment only", path)
    return Settings()
