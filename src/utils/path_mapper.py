"""Remote-to-local path translation (e.g. Windows ``E:\\Temp_Flex_Media\\`` <-> Docker ``/media/``).

Servarr reports paths in *its* filesystem namespace. When Verifyarr runs elsewhere (a Docker
container, another host) those paths must be translated before they can be opened.
Matching is done on path components via ``pure`` paths so it is independent of the
platform Verifyarr runs on: Windows-style paths match case-insensitively and accept either
separator, POSIX-style paths match case-sensitively.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from src.config import PathMapping

logger = logging.getLogger(__name__)

_DRIVE_OR_UNC = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_windows_style(path: str) -> bool:
    """Heuristic: drive letter / UNC prefix, or backslashes without any forward slash."""
    return bool(_DRIVE_OR_UNC.match(path)) or ("\\" in path and "/" not in path)


def _pure(path: str, *, windows: bool | None = None) -> PurePath:
    if windows is None:
        windows = is_windows_style(path)
    return PureWindowsPath(path) if windows else PurePosixPath(path)


def join_remote(base: str, relative: str) -> str:
    """Join a Servarr base folder and a relative path, honouring the base's path style."""
    windows = is_windows_style(base)
    rel_parts = _pure(relative.replace("\\", "/"), windows=False).parts
    return str(_pure(base, windows=windows).joinpath(*rel_parts))


def _remainder(path: str, prefix: str) -> tuple[str, ...] | None:
    """Return path components of ``path`` below ``prefix`` or ``None`` if not under it."""
    windows = is_windows_style(prefix) or is_windows_style(path)
    path_parts = _pure(path, windows=windows).parts
    prefix_parts = _pure(prefix, windows=windows).parts
    if len(path_parts) < len(prefix_parts):
        return None
    fold = str.casefold if windows else str
    if all(fold(a) == fold(b) for a, b in zip(path_parts, prefix_parts, strict=False)):
        return tuple(path_parts[len(prefix_parts) :])
    return None


class PathMapper:
    """Applies configured prefix / regex replacements; unmapped paths pass through as-is."""

    def __init__(self, mappings: Sequence[PathMapping]) -> None:
        prefix_maps = [m for m in mappings if not m.use_regex]
        # Longest remote prefix wins so nested mappings behave predictably.
        self._prefix_maps = sorted(prefix_maps, key=lambda m: len(m.remote_path), reverse=True)
        self._regex_maps = [
            (re.compile(m.remote_path, re.IGNORECASE), m.local_path)
            for m in mappings
            if m.use_regex
        ]

    def to_local(self, remote: str) -> Path:
        """Translate a Servarr-side path into a path Verifyarr can open."""
        for mapping in self._prefix_maps:
            rest = _remainder(remote, mapping.remote_path)
            if rest is not None:
                return Path(mapping.local_path).joinpath(*rest)

        for pattern, replacement in self._regex_maps:
            match = pattern.match(remote)
            if match:
                result = match.expand(replacement) + remote[match.end() :]
                if result.startswith("/"):
                    result = result.replace("\\", "/")
                return Path(result)

        logger.debug("No path mapping matched %s; using it unchanged", remote)
        return Path(remote)

    def to_remote(self, local: str | Path) -> str:
        """Translate a local path back to the Servarr namespace (prefix mappings only)."""
        local_str = str(local)
        for mapping in sorted(self._prefix_maps, key=lambda m: len(m.local_path), reverse=True):
            rest = _remainder(local_str, mapping.local_path)
            if rest is not None:
                return str(_pure(mapping.remote_path).joinpath(*rest))
        return local_str
