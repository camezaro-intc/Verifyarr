"""Webhook schemas for Radarr and Sonarr (v3/v4/v5 payloads) and the normalised job model.

Servarr payloads differ by application, version, and trigger type (``Download``, ``Upgrade``,
``MovieFileImport``, ``FileImport``, ``Rename``, ``MovieAdded``), and several fields Verifyarr
uses (``runtime``, ``originalLanguage``) are not present in every version. All secondary fields
are therefore optional with safe defaults, path resolution falls back across ``movieFile.path``,
``movie.folderPath``, and root ``path``, and the verifier enriches missing metadata via the
Servarr REST API when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from src.utils.path_mapper import is_windows_style, join_remote

AppName = Literal["radarr", "sonarr"]


class EventType(StrEnum):
    DOWNLOAD = "Download"
    UPGRADE = "Upgrade"
    MOVIE_FILE_IMPORT = "MovieFileImport"
    FILE_IMPORT = "FileImport"
    EPISODE_FILE_IMPORT = "EpisodeFileImport"
    RENAME = "Rename"
    MOVIE_ADDED = "MovieAdded"
    TEST = "Test"


_IGNORED_EVENT_TYPES = frozenset(
    {
        "grab",
        "moviedelete",
        "moviefiledelete",
        "seriesdelete",
        "episodefiledelete",
        "health",
        "healthissue",
        "healthrestored",
        "applicationupdate",
    }
)

_IMPORT_EVENT_TYPES = frozenset(
    {
        "download",
        "upgrade",
        "moviefileimport",
        "fileimport",
        "episodefileimport",
        "rename",
        "movieadded",
    }
)


def _flatten_name(value: Any) -> str | None:
    """Flatten nested Servarr quality/language objects or plain strings into ``str | None``."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "name" in value and value["name"] is not None:
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


class MovieModel(_CamelModel):
    id: int | None = None
    title: str = "Unknown"
    year: int | None = None
    runtime: int | None = None  # minutes
    tmdb_id: int | None = None
    imdb_id: str | None = None
    original_language: FlexibleName = None
    folder_path: str | None = None
    path: str | None = None


class SeriesModel(_CamelModel):
    id: int | None = None
    title: str = "Unknown"
    year: int | None = None
    runtime: int | None = None  # minutes per episode
    path: str | None = None
    folder_path: str | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    original_language: FlexibleName = None


class Episode(_CamelModel):
    id: int | None = None
    episode_number: int | None = None
    season_number: int | None = None
    title: str | None = None


class MovieFileModel(_CamelModel):
    """``movieFile`` / ``episodeFile`` payload with all fields optional for safety."""

    id: int | None = None
    relative_path: str | None = None
    path: str | None = None
    scene_name: str | None = None
    quality: FlexibleName = None
    release_group: str | None = None
    size: int | None = None


# Backwards-compatible aliases used across the codebase.
Movie = MovieModel
Series = SeriesModel
EpisodeFileModel = MovieFileModel
MediaFile = MovieFileModel


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

    event_type: str = EventType.DOWNLOAD.value
    instance_name: str | None = None
    is_upgrade: bool = False
    download_id: str | None = None
    release: Release | None = None

    # Root-level fallbacks for synthetic or custom payloads
    title: str | None = None
    runtime: int | None = None
    path: str | None = None

    movie: MovieModel | None = None
    movie_file: MovieFileModel | None = None
    renamed_movie_files: list[MovieFileModel] = []

    series: SeriesModel | None = None
    episodes: list[Episode] = []
    episode_file: EpisodeFileModel | None = None
    episode_files: list[EpisodeFileModel] = []  # Sonarr v4 season-pack style payloads
    renamed_episode_files: list[EpisodeFileModel] = []

    @model_validator(mode="after")
    def _resolve_paths(self) -> ServarrWebhookPayload:
        """Resolve media file paths across ``movieFile.path``, ``movie.folderPath``, or ``path``."""
        # If neither movie nor series is set, promote root-level path/title into a Radarr movie.
        if self.movie is None and self.series is None and self.path:
            self.movie = MovieModel(title=self.title or "Unknown", runtime=self.runtime)

        if self.movie is not None:
            if self.movie_file is None and self.renamed_movie_files:
                self.movie_file = self.renamed_movie_files[0]

            folder = self.movie.folder_path or self.movie.path
            file_path = self.movie_file.path if self.movie_file else None
            rel_path = self.movie_file.relative_path if self.movie_file else None

            resolved = file_path
            if not resolved and folder and rel_path:
                resolved = join_remote(folder, rel_path)
            if not resolved:
                resolved = self.path or folder

            if resolved:
                if self.movie_file is None:
                    self.movie_file = MovieFileModel(path=resolved)
                elif not self.movie_file.path:
                    self.movie_file.path = resolved

        if self.series is not None:
            if not self.episode_files and self.renamed_episode_files:
                self.episode_files = list(self.renamed_episode_files)

            series_folder = self.series.path or self.series.folder_path
            for ef in self.episode_files:
                if not ef.path and ef.relative_path and series_folder:
                    ef.path = join_remote(series_folder, ef.relative_path)

            if self.episode_file is not None:
                if not self.episode_file.path and self.episode_file.relative_path and series_folder:
                    self.episode_file.path = join_remote(
                        series_folder, self.episode_file.relative_path
                    )
                if not self.episode_file.path and (self.path or series_folder):
                    self.episode_file.path = self.path or series_folder
            elif not self.episode_files and (self.path or series_folder):
                self.episode_file = EpisodeFileModel(path=self.path or series_folder)

        return self

    @property
    def is_test(self) -> bool:
        return self.event_type.strip().lower() == EventType.TEST.value.lower()

    @property
    def has_media_path(self) -> bool:
        if self.movie_file and self.movie_file.path:
            return True
        if self.episode_file and self.episode_file.path:
            return True
        if any(ef.path for ef in self.episode_files):
            return True
        return bool(self.path)

    @property
    def is_import_event(self) -> bool:
        ev = self.event_type.strip().lower()
        if ev in _IGNORED_EVENT_TYPES:
            return False
        return ev in _IMPORT_EVENT_TYPES or self.has_media_path

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
            if self.episode_file is not None and all(
                (f.id is None or f.id != self.episode_file.id)
                and (f.path is None or f.path != self.episode_file.path)
                for f in files
            ):
                files.insert(0, self.episode_file)
            return [self._sonarr_job(self.series, f, len(files)) for f in files]
        return []

    # -- helpers -----------------------------------------------------------------

    def _release_title(self, media_file: MovieFileModel, path: str | None) -> str:
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

    def _quality_text(self, media_file: MovieFileModel) -> str | None:
        return (self.release.quality if self.release else None) or media_file.quality

    def _radarr_job(self, movie: MovieModel, media_file: MovieFileModel) -> VerificationJob:
        path = media_file.path
        folder = movie.folder_path or movie.path
        if not path and folder and media_file.relative_path:
            path = join_remote(folder, media_file.relative_path)
        if not path:
            path = self.path or folder
        title = f"{movie.title} ({movie.year})" if movie.year else movie.title
        runtime = movie.runtime if movie.runtime is not None else self.runtime
        return VerificationJob(
            app="radarr",
            entity_id=movie.id or 0,
            file_id=media_file.id,
            title=title,
            release_title=self._release_title(media_file, path),
            remote_path=path,
            expected_runtime_min=float(runtime) if runtime else None,
            original_language=self._language_name(movie.original_language),
            quality=self._quality_text(media_file),
            is_upgrade=self.is_upgrade
            or self.event_type.lower() == EventType.UPGRADE.value.lower(),
            download_id=self.download_id,
            indexer=self.release.indexer if self.release else None,
            size=(self.release.size if self.release else None) or media_file.size,
        )

    def _sonarr_job(
        self, series: SeriesModel, media_file: EpisodeFileModel, file_count: int
    ) -> VerificationJob:
        path = media_file.path
        series_folder = series.path or series.folder_path
        if not path and series_folder and media_file.relative_path:
            path = join_remote(series_folder, media_file.relative_path)
        if not path:
            path = self.path or series_folder

        title = series.title
        episode_ids: list[int] = []
        if file_count == 1 and self.episodes:
            episode_ids = [e.id for e in self.episodes if e.id is not None]
            labels = [
                f"S{e.season_number:02d}E{e.episode_number:02d}"
                for e in self.episodes
                if e.season_number is not None and e.episode_number is not None
            ]
            if labels:
                title = f"{series.title} - {labels[0]}" + (
                    f"-{labels[-1]}" if len(labels) > 1 else ""
                )

        runtime = series.runtime if series.runtime is not None else self.runtime
        expected = float(runtime) if runtime else None
        if expected and len(episode_ids) > 1:
            # A multi-episode file (e.g. S01E01-E02) holds several episodes back-to-back.
            expected *= len(episode_ids)

        return VerificationJob(
            app="sonarr",
            entity_id=series.id or 0,
            file_id=media_file.id,
            episode_ids=episode_ids,
            title=title,
            release_title=self._release_title(media_file, path),
            remote_path=path,
            expected_runtime_min=expected,
            original_language=self._language_name(series.original_language),
            quality=self._quality_text(media_file),
            is_upgrade=self.is_upgrade
            or self.event_type.lower() == EventType.UPGRADE.value.lower(),
            download_id=self.download_id,
            indexer=self.release.indexer if self.release else None,
            size=(self.release.size if self.release else None) or media_file.size,
        )


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_fallback_jobs(body: dict[str, Any]) -> list[VerificationJob]:
    """Fallback extractor when Pydantic validation fails on unexpected Servarr fields.

    If ``body`` contains ``movie`` (or ``series``) and a resolvable media file path,
    extracts ``title``, ``runtime`` and ``path`` so the verification pipeline still runs.
    """
    raw_movie = body.get("movie")
    raw_series = body.get("series")
    movie = _as_dict(raw_movie) if isinstance(raw_movie, dict) else None
    series = _as_dict(raw_series) if isinstance(raw_series, dict) else None
    movie_file = _as_dict(body.get("movieFile"))
    episode_file = _as_dict(body.get("episodeFile"))
    release = _as_dict(body.get("release"))

    if movie is not None:
        folder = movie.get("folderPath") or movie.get("path")
        rel_path = movie_file.get("relativePath")
        path = (
            movie_file.get("path")
            or (join_remote(str(folder), str(rel_path)) if folder and rel_path else None)
            or body.get("path")
            or folder
        )
        if not path:
            return []
        path_str = str(path)
        pure = PureWindowsPath(path_str) if is_windows_style(path_str) else PurePosixPath(path_str)
        title = str(movie.get("title") or body.get("title") or pure.stem or "Unknown")
        year = _safe_int(movie.get("year"))
        if year:
            title = f"{title} ({year})"
        runtime = _safe_float(movie.get("runtime") or body.get("runtime"))
        release_title = str(
            release.get("releaseTitle") or movie_file.get("sceneName") or pure.stem or "unknown"
        )
        return [
            VerificationJob(
                app="radarr",
                entity_id=_safe_int(movie.get("id")) or 0,
                file_id=_safe_int(movie_file.get("id")),
                title=title,
                release_title=release_title,
                remote_path=path_str,
                expected_runtime_min=runtime,
                original_language=_flatten_name(movie.get("originalLanguage")),
                quality=_flatten_name(release.get("quality") or movie_file.get("quality")),
            )
        ]

    if series is not None:
        folder = series.get("path") or series.get("folderPath")
        rel_path = episode_file.get("relativePath")
        path = (
            episode_file.get("path")
            or (join_remote(str(folder), str(rel_path)) if folder and rel_path else None)
            or body.get("path")
            or folder
        )
        if not path:
            return []
        path_str = str(path)
        pure = PureWindowsPath(path_str) if is_windows_style(path_str) else PurePosixPath(path_str)
        title = str(series.get("title") or body.get("title") or pure.stem or "Unknown")
        runtime = _safe_float(series.get("runtime") or body.get("runtime"))
        release_title = str(
            release.get("releaseTitle") or episode_file.get("sceneName") or pure.stem or "unknown"
        )
        return [
            VerificationJob(
                app="sonarr",
                entity_id=_safe_int(series.get("id")) or 0,
                file_id=_safe_int(episode_file.get("id")),
                title=title,
                release_title=release_title,
                remote_path=path_str,
                expected_runtime_min=runtime,
                original_language=_flatten_name(series.get("originalLanguage")),
                quality=_flatten_name(release.get("quality") or episode_file.get("quality")),
            )
        ]

    return []


@dataclass(slots=True)
class VerificationJob:
    """One media file to verify, independent of the originating application."""

    app: AppName
    entity_id: int  # Radarr movie id / Sonarr series id (0 when unknown)
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
