"""Verification orchestrator: evaluates Tiers 1-3, records history, remediates and notifies.

Disk-heavy work (ffprobe / ffmpeg / OCR) runs inside an ``asyncio.Semaphore`` sized by
``verification.max_concurrent_probes`` so a Sonarr season-pack import cannot exhaust disk I/O.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path

from src.config import Settings
from src.models.history import CheckResult, HistoryEntry, HistoryStore, VerificationStatus
from src.models.servarr import VerificationJob
from src.services.notifier import Notifier
from src.services.prober import (
    ProbeResult,
    ToolError,
    check_audio_language,
    check_channel_sanity,
    probe_file,
)
from src.services.remediator import RemediationError, Remediator
from src.services.visual_check import detect_freeze_frames, find_keywords, ocr_scan_frames
from src.utils.path_mapper import PathMapper

logger = logging.getLogger(__name__)

# OCR windows: two frames in the opening (00:30-01:30) and two in the last three minutes.
_OPENING_TIMESTAMPS = (30, 90)
_ENDING_OFFSETS_FROM_CREDITS_START = (0, 60)
_ENDING_WINDOW_SECONDS = 180
_VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".webm"})


def _resolve_video_file(path: Path) -> Path | None:
    """Return ``path`` if it is a file, or the largest video file inside ``path`` if a directory."""
    if path.is_file():
        return path
    if path.is_dir():
        candidates = [
            child
            for child in path.rglob("*")
            if child.is_file() and child.suffix.lower() in _VIDEO_EXTENSIONS
        ]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_size)
    return None


class Verifier:
    def __init__(
        self,
        settings: Settings,
        history: HistoryStore,
        remediator: Remediator,
        notifier: Notifier,
        path_mapper: PathMapper | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = settings.verification
        self._history = history
        self._remediator = remediator
        self._notifier = notifier
        self._mapper = path_mapper or PathMapper(settings.path_mappings)
        self._semaphore = asyncio.Semaphore(self._cfg.max_concurrent_probes)
        self._tasks: set[asyncio.Task[HistoryEntry]] = set()

        self._ffmpeg = settings.binaries.resolve("ffmpeg")
        self._ffprobe = settings.binaries.resolve("ffprobe")
        self._tesseract = settings.binaries.resolve("tesseract")

    def reload_settings(self, settings: Settings) -> None:
        """Hot-reload active configuration across Verifier, Remediator, and Notifier."""
        old_concurrency = self._cfg.max_concurrent_probes
        self._settings = settings
        self._cfg = settings.verification
        self._mapper = PathMapper(settings.path_mappings)
        if self._cfg.max_concurrent_probes != old_concurrency:
            self._semaphore = asyncio.Semaphore(self._cfg.max_concurrent_probes)
        self._ffmpeg = settings.binaries.resolve("ffmpeg")
        self._ffprobe = settings.binaries.resolve("ffprobe")
        self._tesseract = settings.binaries.resolve("tesseract")
        if hasattr(self._remediator, "reload_settings"):
            self._remediator.reload_settings(settings)
        if hasattr(self._notifier, "reload_settings"):
            self._notifier.reload_settings(settings)
        logger.info(
            "Hot-reloaded Verifier settings: mode=%s, runtime_delta=%g min, max_probes=%d",
            "DRY-RUN" if self._cfg.dry_run else "ACTIVE",
            self._cfg.runtime_delta_threshold_minutes,
            self._cfg.max_concurrent_probes,
        )

    # -- lifecycle -----------------------------------------------------------------

    def log_environment(self) -> None:
        for name, path in (("ffmpeg", self._ffmpeg), ("ffprobe", self._ffprobe)):
            if path:
                logger.info("Using %s at %s", name, path)
            else:
                logger.error("%s not found on PATH and binaries.%s_path is unset", name, name)
        if self._cfg.enable_ocr_credit_check:
            if self._tesseract:
                logger.info("Using tesseract at %s", self._tesseract)
            else:
                logger.warning("OCR credit check enabled but tesseract was not found on PATH")
        logger.info(
            "Verification mode: %s (max %d concurrent probes, runtime tolerance %g min)",
            (
                "DRY-RUN (no remediation)"
                if self._cfg.dry_run
                else "ACTIVE (will delete/blocklist/re-search)"
            ),
            self._cfg.max_concurrent_probes,
            self._cfg.runtime_delta_threshold_minutes,
        )

    def submit(self, jobs: Iterable[VerificationJob]) -> int:
        """Queue jobs for background verification; returns how many were queued."""
        count = 0
        for job in jobs:
            task = asyncio.create_task(self.verify_job(job), name=f"verify:{job.release_title}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            count += 1
        return count

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- main flow -----------------------------------------------------------------

    async def verify_job(self, job: VerificationJob) -> HistoryEntry:
        """Verify one file end-to-end. Never raises: unexpected failures become ERROR entries."""
        try:
            return await self._verify(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - last-resort guard around a background task
            logger.exception("Unexpected error verifying %s", job.release_title)
            return await self._record_error(job, f"Unexpected error: {exc!r}")

    async def _verify(self, job: VerificationJob) -> HistoryEntry:
        logger.info(
            "Verifying [%s] %s (release=%s, path=%s)",
            job.app,
            job.title,
            job.release_title,
            job.remote_path,
        )

        if await self._history.is_whitelisted(job.release_title):
            entry = await self._history.add(
                self._entry(
                    job,
                    VerificationStatus.PASSED,
                    checks=[
                        CheckResult(
                            tier=0,
                            name="Whitelist",
                            passed=True,
                            detail="Release was force-approved earlier; checks skipped",
                        )
                    ],
                    whitelisted=True,
                )
            )
            logger.info("PASSED (whitelisted) %s", job.release_title)
            await self._safe_refresh_jellyfin()
            return entry

        await self._enrich(job)

        if not job.remote_path:
            return await self._record_error(job, "Webhook payload did not include a file path")
        mapped_path = self._mapper.to_local(job.remote_path)
        local_path = await asyncio.to_thread(_resolve_video_file, mapped_path)
        if local_path is None:
            return await self._record_error(
                job,
                f"File not found: {mapped_path} (from {job.remote_path}); "
                "check path_mappings and volume mounts",
            )
        if local_path != mapped_path and job.release_title in ("unknown", mapped_path.stem):
            job.release_title = local_path.stem

        try:
            async with self._semaphore:
                checks, probe = await self._run_pipeline(job, local_path)
        except ToolError as exc:
            return await self._record_error(job, f"Probe failed: {exc}")

        failed = [c for c in checks if not c.passed]
        if not failed:
            entry = await self._history.add(
                self._entry(
                    job,
                    VerificationStatus.PASSED,
                    checks=checks,
                    probe=probe,
                    file_path=str(local_path),
                )
            )
            logger.info(
                "PASSED %s (probed %.1f min / expected %s min)",
                job.release_title,
                probe.duration_minutes,
                f"{job.expected_runtime_min:.0f}" if job.expected_runtime_min else "n/a",
            )
            await self._safe_refresh_jellyfin()
            return entry

        reasons = [f"Tier {c.tier} {c.name}: {c.detail}" for c in failed]
        for reason in reasons:
            logger.warning("FAILED %s -> %s", job.release_title, reason)

        if self._cfg.dry_run:
            entry = await self._history.add(
                self._entry(
                    job,
                    VerificationStatus.DRY_RUN,
                    checks=checks,
                    probe=probe,
                    reasons=reasons,
                    remediation=["Dry-run: file kept, nothing deleted or blocklisted"],
                    file_path=str(local_path),
                )
            )
        else:
            actions = await self._remediate(job)
            entry = await self._history.add(
                self._entry(
                    job,
                    VerificationStatus.REJECTED,
                    checks=checks,
                    probe=probe,
                    reasons=reasons,
                    remediation=actions,
                    file_path=str(local_path),
                )
            )
        await self._safe_notify_failure(entry)
        return entry

    # -- tiers ---------------------------------------------------------------------

    async def _run_pipeline(
        self, job: VerificationJob, path: Path
    ) -> tuple[list[CheckResult], ProbeResult]:
        cfg = self._cfg
        checks: list[CheckResult] = []

        # Tier 1: runtime delta (immediate gate)
        probe = await probe_file(path, self._ffprobe, timeout=cfg.probe_timeout_seconds)
        probed_min = probe.duration_minutes
        if job.expected_runtime_min:
            delta = abs(probed_min - job.expected_runtime_min)
            ok = delta <= cfg.runtime_delta_threshold_minutes
            detail = (
                f"probed {probed_min:.1f} min vs expected {job.expected_runtime_min:.0f} min "
                f"(delta {delta:.1f} min, limit {cfg.runtime_delta_threshold_minutes:g})"
            )
            checks.append(
                CheckResult(
                    tier=1,
                    name="Runtime Mismatch" if not ok else "Runtime Delta",
                    passed=ok,
                    detail=detail,
                )
            )
            if not ok:
                return checks, probe  # REJECTED (Runtime Mismatch): skip remaining tiers
        else:
            checks.append(
                CheckResult(
                    tier=1,
                    name="Runtime Delta",
                    passed=True,
                    skipped=True,
                    detail=f"no expected runtime available (probed {probed_min:.1f} min)",
                )
            )

        # Tier 2: language + channel sanity
        if cfg.enable_audio_sanity_check:
            checks.append(
                self._tier2_check(
                    "Original Language", *check_audio_language(probe, job.original_language)
                )
            )
            advertised = (
                f"{job.release_title} {job.quality or ''} {Path(job.remote_path or '').name}"
            )
            checks.append(
                self._tier2_check("Channel Sanity", *check_channel_sanity(probe, advertised))
            )

        # Tier 3: visual & content integrity
        if cfg.enable_freeze_detection:
            checks.append(await self._freeze_check(path, probe))
        if cfg.enable_ocr_credit_check:
            checks.append(await self._ocr_check(path, probe))
        return checks, probe

    @staticmethod
    def _tier2_check(name: str, result: bool | None, detail: str) -> CheckResult:
        return CheckResult(
            tier=2, name=name, passed=result is not False, skipped=result is None, detail=detail
        )

    async def _freeze_check(self, path: Path, probe: ProbeResult) -> CheckResult:
        cfg = self._cfg
        total = int(probe.duration_seconds)
        sample = min(cfg.freeze_sample_duration_seconds, total)
        if sample < 10:
            return CheckResult(
                tier=3,
                name="Freeze Detection",
                passed=True,
                skipped=True,
                detail="file too short to sample",
            )
        # Slide the window back for short files so we always sample a full window if possible.
        start = max(0, min(cfg.freeze_sample_start_seconds, total - sample))
        try:
            result = await detect_freeze_frames(
                path,
                start_time=start,
                duration=sample,
                min_freeze_seconds=cfg.freeze_min_duration_seconds,
                ffmpeg=self._ffmpeg,
                timeout=cfg.probe_timeout_seconds * 3,
            )
        except ToolError as exc:
            logger.warning("Freeze detection skipped for %s: %s", path.name, exc)
            return CheckResult(
                tier=3, name="Freeze Detection", passed=True, skipped=True, detail=f"skipped: {exc}"
            )
        ok = result.frozen_ratio <= cfg.freeze_ratio_threshold
        detail = (
            f"{result.frozen_seconds:.0f}s of {sample}s frozen ({result.frozen_ratio:.0%}) "
            f"in window {start}-{start + sample}s; limit {cfg.freeze_ratio_threshold:.0%}"
        )
        return CheckResult(
            tier=3,
            name="Freeze/Loop Trap" if not ok else "Freeze Detection",
            passed=ok,
            detail=detail,
        )

    def _ocr_timestamps(self, probe: ProbeResult) -> list[int]:
        total = int(probe.duration_seconds)
        end_start = total - _ENDING_WINDOW_SECONDS
        candidates = list(_OPENING_TIMESTAMPS) + [
            end_start + o for o in _ENDING_OFFSETS_FROM_CREDITS_START
        ]
        return sorted({t for t in candidates if 0 <= t < total - 1})

    async def _ocr_check(self, path: Path, probe: ProbeResult) -> CheckResult:
        cfg = self._cfg
        timestamps = self._ocr_timestamps(probe)
        if not timestamps or not cfg.mockbuster_keywords:
            return CheckResult(
                tier=3, name="Credits OCR", passed=True, skipped=True, detail="nothing to scan"
            )
        try:
            texts = await ocr_scan_frames(
                path,
                timestamps,
                ffmpeg=self._ffmpeg,
                tesseract=self._tesseract,
                timeout=cfg.probe_timeout_seconds,
            )
        except ToolError as exc:
            logger.warning("OCR credit check skipped for %s: %s", path.name, exc)
            return CheckResult(
                tier=3, name="Credits OCR", passed=True, skipped=True, detail=f"skipped: {exc}"
            )
        hits = find_keywords(texts, cfg.mockbuster_keywords)
        if hits:
            return CheckResult(
                tier=3,
                name="Mockbuster Credits",
                passed=False,
                detail=f"blacklisted text found in credits: {', '.join(hits)}",
            )
        return CheckResult(
            tier=3,
            name="Credits OCR",
            passed=True,
            detail=f"scanned {len(timestamps)} frames, no blacklisted text",
        )

    # -- helpers -------------------------------------------------------------------

    async def _enrich(self, job: VerificationJob) -> None:
        """Fill in runtime / original language / file path from Servarr when webhook lacks them."""
        if not job.entity_id or job.entity_id <= 0:
            return
        need_runtime = not job.expected_runtime_min
        need_language = self._cfg.enable_audio_sanity_check and not job.original_language
        need_file = job.file_id is None or not job.remote_path
        if not (need_runtime or need_language or need_file):
            return
        try:
            meta = await self._remediator.get_media_metadata(job.app, job.entity_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch metadata for %s: %s", job.title, exc)
            return
        if need_runtime and meta.get("runtime"):
            runtime = float(meta["runtime"])
            if len(job.episode_ids) > 1:
                runtime *= len(job.episode_ids)
            job.expected_runtime_min = runtime
        if need_language:
            language = meta.get("originalLanguage")
            job.original_language = language.get("name") if isinstance(language, dict) else language
        movie_file = meta.get("movieFile")
        if isinstance(movie_file, dict):
            if job.file_id is None and movie_file.get("id"):
                job.file_id = int(movie_file["id"])
            if movie_file.get("path"):
                job.remote_path = str(movie_file["path"])

    async def _remediate(self, job: VerificationJob) -> list[str]:
        """Delete file -> blocklist release -> trigger replacement search (each step is logged)."""
        actions: list[str] = []
        delete_ok = blocklist_ok = False

        async def step(label: str, coro) -> bool:  # noqa: ANN001 - small local helper
            try:
                actions.append(await coro)
                return True
            except RemediationError as exc:
                logger.error("%s failed for %s: %s", label, job.release_title, exc)
                actions.append(f"{label} FAILED: {exc}")
                return False

        if job.file_id is None:
            actions.append("Delete file FAILED: webhook payload had no file id")
        else:
            delete_ok = await step(
                "Delete file", self._remediator.delete_file(job.app, job.file_id)
            )
        blocklist_ok = await step(
            "Blocklist release",
            self._remediator.blocklist_release(
                job.app,
                job.release_title,
                entity_id=job.entity_id,
                download_id=job.download_id,
                indexer=job.indexer,
                size=job.size,
                episode_ids=job.episode_ids,
            ),
        )
        # Searching before the bad release is blocklisted would just grab it again.
        if delete_ok and blocklist_ok:
            await step(
                "Trigger search",
                self._remediator.trigger_search(
                    job.app, job.entity_id, episode_ids=job.episode_ids
                ),
            )
        else:
            actions.append("Replacement search skipped because delete or blocklist did not succeed")
        for action in actions:
            logger.info("Remediation [%s]: %s", job.release_title, action)
        return actions

    @staticmethod
    def _entry(
        job: VerificationJob,
        status: VerificationStatus,
        *,
        checks: list[CheckResult],
        probe: ProbeResult | None = None,
        reasons: list[str] | None = None,
        remediation: list[str] | None = None,
        file_path: str | None = None,
        whitelisted: bool = False,
    ) -> HistoryEntry:
        return HistoryEntry(
            app=job.app,
            title=job.title,
            release_name=job.release_title,
            file_path=file_path or job.remote_path,
            file_id=job.file_id,
            expected_runtime_min=job.expected_runtime_min,
            probed_runtime_min=probe.duration_minutes if probe else None,
            status=status,
            failure_reasons=reasons or [],
            checks=checks,
            remediation=remediation or [],
            whitelisted=whitelisted,
            download_hash=job.download_id,
        )

    async def _record_error(self, job: VerificationJob, message: str) -> HistoryEntry:
        logger.error("ERROR %s: %s", job.release_title, message)
        entry = await self._history.add(
            self._entry(
                job,
                VerificationStatus.ERROR,
                checks=[],
                reasons=[message],
                remediation=["No action taken (file could not be verified)"],
            )
        )
        try:
            await self._notifier.notify_error(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error notification failed: %s", exc)
        return entry

    async def _safe_refresh_jellyfin(self) -> None:
        try:
            await self._notifier.refresh_jellyfin()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Jellyfin refresh failed: %s", exc)

    async def _safe_notify_failure(self, entry: HistoryEntry) -> None:
        try:
            await self._notifier.notify_failure(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failure notification failed: %s", exc)
