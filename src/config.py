"""Verifyarr configuration: Pydantic v2 models, YAML loader, atomic persistence and hot-reload.

Precedence (highest first): init kwargs, environment variables, ``.env`` file, YAML file.
Environment variables use the ``VERIFYARR_`` prefix and ``__`` as the nested delimiter,
e.g. ``VERIFYARR_RADARR__API_KEY``. The YAML location comes from ``VERIFYARR_CONFIG``
(default: ``./config.yaml``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
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


def normalize_path(path_str: str | None) -> str | None:
    """Normalize file/folder paths to use forward slashes and strip extra whitespace."""
    if not path_str or not isinstance(path_str, str):
        return path_str
    return path_str.replace("\\", "/").strip()


def load_yaml_resilient(file_path: Path) -> dict[str, Any]:
    """Read a YAML file and sanitize unescaped Windows paths before safe_load."""
    if not file_path.is_file():
        return {}
    try:
        raw_content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Could not read configuration file %s: %s", file_path, exc)
        return {}

    # Convert drive letters followed by single backslashes (e.g., C:\) to forward slashes
    sanitized = re.sub(r"([A-Za-z]:)\\(?!\\)", r"\1/", raw_content)
    # Convert any remaining single backslashes in path-like strings (e.g. \Program Files\...)
    sanitized = re.sub(r"\\([A-Za-z0-9_.\- ])", r"/\1", sanitized)
    try:
        data = yaml.safe_load(sanitized)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        try:
            data = yaml.safe_load(raw_content)
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError as exc:
            logger.error("Failed parsing YAML in %s: %s", file_path, exc)
            return {}


class ResilientYamlConfigSettingsSource(YamlConfigSettingsSource):
    """Pydantic Settings source using resilient YAML loading to prevent Windows escape crashes."""

    def _read_file(self, file_path: Path | Any) -> dict[str, Any]:
        return load_yaml_resilient(Path(str(file_path)))


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    max_concurrent_probes: int = Field(default=2, ge=1)
    runtime_delta_threshold_minutes: float = Field(default=15, ge=1)
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

    @model_validator(mode="before")
    @classmethod
    def _convert_freeze_percent(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        if "freeze_threshold_percent" in copied:
            raw_pct = copied.pop("freeze_threshold_percent")
            pct = float(raw_pct)
            if pct < 0 or pct > 100:
                raise ValueError("freeze_threshold_percent must be between 0 and 100")
            copied["freeze_ratio_threshold"] = round(pct / 100.0, 4)
        return copied

    @property
    def freeze_threshold_percent(self) -> float:
        return round(self.freeze_ratio_threshold * 100.0, 2)


class ArrConfig(BaseModel):
    """Connection details for a Servarr application (Radarr / Sonarr)."""

    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    discord_webhook_url: str = ""


class BinariesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    tesseract_path: str = ""

    @field_validator("ffmpeg_path", "ffprobe_path", "tesseract_path", mode="before")
    @classmethod
    def _sanitize_binary_path(cls, v: Any) -> str:
        if isinstance(v, str):
            return v.strip()
        return "" if v is None else str(v).strip()

    def resolve(self, name: BinaryName) -> Path | None:
        """Locate a binary using tiered hierarchy: manual override -> PATH -> common dirs."""
        from src.services.binary_resolver import resolve_binary

        override = getattr(self, f"{name}_path", "").strip()
        return resolve_binary(name, override)


class PathMapping(BaseModel):
    """Translate a path as seen by Servarr (remote) to one Verifyarr can open (local)."""

    model_config = ConfigDict(extra="forbid")

    remote_path: str
    local_path: str
    use_regex: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_from_to_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        if "from" in copied and "remote_path" not in copied:
            copied["remote_path"] = copied.pop("from")
        if "from_path" in copied and "remote_path" not in copied:
            copied["remote_path"] = copied.pop("from_path")
        if "to" in copied and "local_path" not in copied:
            copied["local_path"] = copied.pop("to")
        if "to_path" in copied and "local_path" not in copied:
            copied["local_path"] = copied.pop("to_path")
        if "regex" in copied and "use_regex" not in copied:
            copied["use_regex"] = copied.pop("regex")
        return copied

    @field_validator("remote_path", "local_path", mode="before")
    @classmethod
    def sanitize_path(cls, v: Any) -> str:
        if isinstance(v, str):
            s = v.strip()
            # Preserve drive roots like C:\ or C:/ and root / or \
            if re.match(r"^[A-Za-z]:[\\/]$", s):
                return s[:2] + "\\"
            if s in ("/", "\\"):
                return s
            # Strip trailing slashes to prevent escape sequences in JSON/YAML
            return s.rstrip("/\\")
        return "" if v is None else str(v).strip().rstrip("/\\")

    @property
    def from_path(self) -> str:
        return self.remote_path

    @property
    def to_path(self) -> str:
        return self.local_path


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
        yaml_source = ResilientYamlConfigSettingsSource(
            settings_cls, yaml_file=get_config_path(), yaml_file_encoding="utf-8"
        )
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)

    def to_dict(self, *, include_computed: bool = False) -> dict[str, Any]:
        """Serialize configuration to a plain dict with unveiled API keys for YAML / admin UI."""
        verification_dict = self.verification.model_dump()
        if include_computed:
            verification_dict["freeze_threshold_percent"] = (
                self.verification.freeze_threshold_percent
            )
        return {
            "server": self.server.model_dump(),
            "verification": verification_dict,
            "radarr": {
                "enabled": self.radarr.enabled,
                "url": self.radarr.url,
                "api_key": self.radarr.api_key.get_secret_value(),
            },
            "sonarr": {
                "enabled": self.sonarr.enabled,
                "url": self.sonarr.url,
                "api_key": self.sonarr.api_key.get_secret_value(),
            },
            "jellyfin": {
                "enabled": self.jellyfin.enabled,
                "url": self.jellyfin.url,
                "api_key": self.jellyfin.api_key.get_secret_value(),
            },
            "notifications": self.notifications.model_dump(),
            "binaries": self.binaries.model_dump(),
            "path_mappings": [m.model_dump() for m in self.path_mappings],
            "storage": self.storage.model_dump(),
        }

    def update_in_place(self, other: Settings) -> None:
        """Mutate this Settings instance in-place so all held references see new values."""
        self.server = other.server
        self.verification = other.verification
        self.radarr = other.radarr
        self.sonarr = other.sonarr
        self.jellyfin = other.jellyfin
        self.notifications = other.notifications
        self.binaries = other.binaries
        self.path_mappings = list(other.path_mappings)
        self.storage = other.storage


class AppConfig(BaseModel):
    """Strict validation schema for settings payloads submitted via the Web UI / REST API."""

    model_config = ConfigDict(extra="forbid")

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

    def to_settings(self) -> Settings:
        return Settings(
            server=self.server,
            verification=self.verification,
            radarr=self.radarr,
            sonarr=self.sonarr,
            jellyfin=self.jellyfin,
            notifications=self.notifications,
            binaries=self.binaries,
            path_mappings=self.path_mappings,
            storage=self.storage,
        )


def get_config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser()
    cwd_candidate = Path(DEFAULT_CONFIG_FILE).expanduser()
    if cwd_candidate.is_file() or not getattr(sys, "frozen", False):
        return cwd_candidate
    exe_dir = Path(sys.executable).resolve().parent
    exe_candidate = exe_dir / DEFAULT_CONFIG_FILE
    if not exe_candidate.is_file():
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        for example in (exe_dir / "config.example.yaml", meipass / "config.example.yaml"):
            if example.is_file():
                shutil.copyfile(example, exe_candidate)
                logger.info("Created default %s from %s", exe_candidate, example)
                break
    return exe_candidate


CONFIG_PATH = Path(DEFAULT_CONFIG_FILE)


def save_config_atomic(config_dict: dict[str, Any], target_path: Path) -> None:
    """Atomically write ``config_dict`` to ``target_path`` via a sibling ``.tmp`` file."""
    import copy

    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_path.with_suffix(target_path.suffix + ".tmp")

    cleaned_dict = copy.deepcopy(config_dict)
    if "binaries" in cleaned_dict and isinstance(cleaned_dict["binaries"], dict):
        for k in ("ffprobe_path", "ffmpeg_path", "tesseract_path"):
            if k in cleaned_dict["binaries"] and isinstance(cleaned_dict["binaries"][k], str):
                cleaned_dict["binaries"][k] = normalize_path(cleaned_dict["binaries"][k]) or ""
    if "path_mappings" in cleaned_dict and isinstance(cleaned_dict["path_mappings"], list):
        for m in cleaned_dict["path_mappings"]:
            if isinstance(m, dict):
                for k in ("remote_path", "local_path", "from", "to", "from_path", "to_path"):
                    if k in m and isinstance(m[k], str):
                        cleaned_val = normalize_path(m[k])
                        if cleaned_val:
                            # Preserve drive roots like C:/ and root /
                            if not re.match(r"^[A-Za-z]:/$", cleaned_val) and cleaned_val != "/":
                                cleaned_val = cleaned_val.rstrip("/")
                        m[k] = cleaned_val or ""

    with open(temp_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(cleaned_dict, f, sort_keys=False, indent=2, allow_unicode=True)
    temp_file.replace(target_path)


def save_config(new_config: Settings | AppConfig, path: Path | None = None) -> Path:
    """Persist ``new_config`` atomically to ``path`` (defaults to ``get_config_path()``)."""
    target = Path(path) if path is not None else get_config_path()
    settings_obj = new_config.to_settings() if isinstance(new_config, AppConfig) else new_config
    save_config_atomic(settings_obj.to_dict(include_computed=False), target)
    logger.info("Configuration saved atomically to %s", target)
    return target


def load_settings() -> Settings:
    """Load settings from YAML + environment. A missing YAML file falls back to defaults."""
    path = get_config_path()
    if not path.is_file():
        logger.warning("Config file %s not found; using defaults and environment only", path)
    try:
        return Settings()
    except Exception as exc:
        logger.error(
            "Failed to parse settings from %s (%s); falling back to default settings", path, exc
        )
        return Settings()
