"""Webhook schemas for Radarr and Sonarr (v3/v4/v5 payloads) and the normalised job model.

Servarr payloads differ by application and version, and several fields Verifyarr wants
(``runtime``, ``originalLanguage``) are not present in every version. Everything beyond the
event type is therefore optional; the verifier enriches missing metadata through the
Servarr REST API when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_camel

from src.utils.path_mapper import is_windows_style, join_remote

AppName = Literal["radarr", "sonarr"]


class EventType(StrEnum):
    DOWNLOAD = "Download"
    UPGRADE = "Upgrade"
    TEST = "Test"


def _flatten_name(value: Any) -> str | None:
    """Servarr sends quality as a plain string in webhooks but as nested objects in the API."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        if "quality" in value:
            return _flatten_name(value["quality"])
    return str(value)


FlexibleName = Annotated[str | None, BeforeValidator(_flatten_name)]


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class Language(_CamelModel):
    id: int | None = None
    name: str | None = None


class Movie(_CamelModel):
    id: int
    title: str = "Unknown"
    year: int | None = None
    runtime: int | None = None  # minutes
    tmdb_id: int | None = None
    imdb_id: str | None = None
    original_language: Language | str | None = None
    folder_path: str | None = None


class Series(_CamelModel):
    id: int
    title: str = "Unknown"
    year: int | None = None
    runtime: int | None = None  # minutes per episode
    path: str | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    original_language: Language | str | None = None


class Episode(_CamelModel):
    id: int
    episode_number: int | None = None
    season_number: int | None = None
    title: str | None = None


class MediaFile(_CamelModel):
    """``movieFile`` / ``episodeFile`` payload."""

    id: int
    relative_path: str | None = None
    path: str | None = None
    scene_name: str | None = None
    quality: FlexibleName = None
    release_group: str | None = None
    size: int | None = None


class Release(_CamelModel):
    release_title: str | None = None
    indexer: str | None = None
    quality: FlexibleName = None
    release_group: str | None = None
    size: int | None = None


class ServarrWebhookPayload(_CamelModel):
    """Union-shaped payload.

    Radarr sets ``movie``/``movieFile``; Sonarr sets ``series``/``episodeFile``.
    """

    event_type: str
    instance_name: str | None = None
    is_upgrade: bool = False
    download_id: str | None = None
    release: Release | None = None

    movie: Movie | None = None
    movie_file: MediaFile | None = None

    series: Series | None = None
    episodes: list[Episode] = []
    episode_file: MediaFile | None = None
    episode_files: list[MediaFile] = []  # Sonarr v4 season-pack style payloads

    @property
    def is_test(self) -> bool:
        return self.event_type.lower() == EventType.TEST.value.lower()

    @property
    def is_import_event(self) -> bool:
        return self.event_type in (EventType.DOWNLOAD, EventType.UPGRADE)

    @property
    def app(self) -> AppName | None:
        if self.movie is not None:
            return "radarr"
        if self.series is not None:
            return "sonarr"
        return None

    def to_jobs(self) -> list[VerificationJob]:
        """Normalise the payload into one job per imported media file."""
        if self.movie is not None and self.movie_file is not None:
            return [self._radarr_job(self.movie, self.movie_file)]
        if self.series is not None:
            files = list(self.episode_files)
            if self.episode_file is not None and all(f.id != self.episode_file.id for f in files):
                files.insert(0, self.episode_file)
            return [self._sonarr_job(self.series, f, len(files)) for f in files]
        return []

    # -- helpers -----------------------------------------------------------------

    def _release_title(self, media_file: MediaFile, path: str | None) -> str:
        if self.release and self.release.release_title:
            return self.release.release_title
        if media_file.scene_name:
            return media_file.scene_name
        name_source = path or media_file.relative_path or ""
        pure = (
            PureWindowsPath(name_source)
            if is_windows_style(name_source)
            else PurePosixPath(name_source)
        )
        return pure.stem or "unknown"

    @staticmethod
    def _language_name(language: Language | str | None) -> str | None:
        if isinstance(language, Language):
            return language.name
        return language

    def _quality_text(self, media_file: MediaFile) -> str | None:
        return (self.release.quality if self.release else None) or media_file.quality

    def _radarr_job(self, movie: Movie, media_file: MediaFile) -> VerificationJob:
        path = media_file.path
        if not path and movie.folder_path and media_file.relative_path:
            path = join_remote(movie.folder_path, media_file.relative_path)
        title = f"{movie.title} ({movie.year})" if movie.year else movie.title
        return VerificationJob(
            app="radarr",
            entity_id=movie.id,
            file_id=media_file.id,
            title=title,
            release_title=self._release_title(media_file, path),
            remote_path=path,
            expected_runtime_min=float(movie.runtime) if movie.runtime else None,
            original_language=self._language_name(movie.original_language),
            quality=self._quality_text(media_file),
            is_upgrade=self.is_upgrade or self.event_type == EventType.UPGRADE,
            download_id=self.download_id,
            indexer=self.release.indexer if self.release else None,
            size=(self.release.size if self.release else None) or media_file.size,
        )

    def _sonarr_job(
        self, series: Series, media_file: MediaFile, file_count: int
    ) -> VerificationJob:
        path = media_file.path
        if not path and series.path and media_file.relative_path:
            path = join_remote(series.path, media_file.relative_path)

        title = series.title
        episode_ids: list[int] = []
        if file_count == 1 and self.episodes:
            episode_ids = [e.id for e in self.episodes]
            labels = [
                f"S{e.season_number:02d}E{e.episode_number:02d}"
                for e in self.episodes
                if e.season_number is not None and e.episode_number is not None
            ]
            if labels:
                title = f"{series.title} - {labels[0]}" + (
                    f"-{labels[-1]}" if len(labels) > 1 else ""
                )

        expected = float(series.runtime) if series.runtime else None
        if expected and len(episode_ids) > 1:
            # A multi-episode file (e.g. S01E01-E02) holds several episodes back-to-back.
            expected *= len(episode_ids)

        return VerificationJob(
            app="sonarr",
            entity_id=series.id,
            file_id=media_file.id,
            episode_ids=episode_ids,
            title=title,
            release_title=self._release_title(media_file, path),
            remote_path=path,
            expected_runtime_min=expected,
            original_language=self._language_name(series.original_language),
            quality=self._quality_text(media_file),
            is_upgrade=self.is_upgrade or self.event_type == EventType.UPGRADE,
            download_id=self.download_id,
            indexer=self.release.indexer if self.release else None,
            size=(self.release.size if self.release else None) or media_file.size,
        )


@dataclass(slots=True)
class VerificationJob:
    """One media file to verify, independent of the originating application."""

    app: AppName
    entity_id: int  # Radarr movie id / Sonarr series id
    file_id: int | None
    title: str
    release_title: str
    remote_path: str | None
    expected_runtime_min: float | None = None
    original_language: str | None = None
    quality: str | None = None
    is_upgrade: bool = False
    download_id: str | None = None
    indexer: str | None = None
    size: int | None = None
    episode_ids: list[int] = field(default_factory=list)
