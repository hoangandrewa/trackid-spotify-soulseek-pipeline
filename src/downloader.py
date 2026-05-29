"""Download orchestrator: manages concurrent Soulseek downloads with
timeout, queue position, transfer speed monitoring, and retry logic."""

import logging
import os
import time
from pathlib import Path
from typing import Optional

from .config import DownloadConfig
from .models import Track, DownloadTask, DownloadStatus, SoulseekResult
from .slskd_client import SlskdClient
from .matcher import rank_candidates

logger = logging.getLogger(__name__)


class DownloadOrchestrator:
    """
    Manages the lifecycle of downloading tracks from Soulseek:
    1. Search for each track
    2. Rank candidates by format/duration/availability
    3. Queue the best candidate
    4. Monitor download progress
    5. Cancel + retry from next candidate if stalled/slow/queued too deep
    """

    def __init__(self, slskd: SlskdClient, config: DownloadConfig):
        self.slskd = slskd
        self.config = config
        self.tasks: list[DownloadTask] = []

        # Ensure output dir exists
        os.makedirs(config.output_dir, exist_ok=True)

    def prepare_tasks(self, tracks: list[Track]) -> list[DownloadTask]:
        """Create download tasks for a list of tracks."""
        self.tasks = [DownloadTask(track=t) for t in tracks]
        return self.tasks

    def search_and_rank(self, task: DownloadTask) -> bool:
        """Search Soulseek for a track and rank results. Returns True if candidates found."""
        task.status = DownloadStatus.SEARCHING
        track = task.track

        # Strip parentheticals and common noise for cleaner searches
        import re
        clean_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", track.title).strip()
        # For artists with commas (collabs), use just the first artist
        primary_artist = track.artist.split(",")[0].strip()

        # Multiple search strategies — broad to narrow
        # Soulseek search is a filename grep, so simpler queries = more results
        queries = [
            f"{primary_artist} {clean_title}",   # Artist + clean title
            clean_title,                           # Just the title
            primary_artist,                        # Just the artist (cast a wide net)
        ]
        # If title has remix/version info, also search with full title
        if clean_title != track.title:
            queries.insert(1, f"{primary_artist} {track.title}")

        all_results: list[SoulseekResult] = []
        seen_paths = set()

        for query in queries:
            results = self.slskd.search(query, timeout_sec=15.0)
            for r in results:
                if r.file_path not in seen_paths:
                    seen_paths.add(r.file_path)
                    all_results.append(r)
            # If we already have enough results, skip broader searches
            if len(all_results) > 20:
                break

        # Rank candidates
        task.candidates = rank_candidates(
            track=track,
            results=all_results,
            format_priority=self.config.format_priority,
            duration_tolerance_sec=self.config.duration_tolerance_sec,
            min_mp3_bitrate=self.config.min_mp3_bitrate,
            max_queue_position=self.config.max_queue_position,
        )
        task.candidate_index = 0

        if task.candidates:
            logger.info(
                f"  {len(task.candidates)} viable candidates for: {track.search_query}"
            )
            top = task.candidates[0]
            logger.info(
                f"  Top pick: {top.filename} [{top.file_format.value}] "
                f"from {top.username} (queue: {top.queue_length})"
            )
            return True
        else:
            logger.warning(f"  No viable candidates for: {track.search_query}")
            task.status = DownloadStatus.FAILED
            task.error = "No viable candidates found"
            return False

    def queue_next_candidate(self, task: DownloadTask) -> bool:
        """Queue the next untried candidate for download. Returns False if exhausted."""
        if task.candidate_index >= len(task.candidates):
            task.status = DownloadStatus.FAILED
            task.error = "All candidates exhausted"
            return False

        if task.attempts >= self.config.max_retries:
            task.status = DownloadStatus.FAILED
            task.error = f"Max retries ({self.config.max_retries}) reached"
            return False

        candidate = task.candidates[task.candidate_index]
        task.current_result = candidate
        task.candidate_index += 1
        task.attempts += 1

        try:
            transfer_id = self.slskd.queue_download(candidate)
            task.transfer_id = transfer_id
            task.status = DownloadStatus.QUEUED
            logger.info(
                f"  Queued candidate #{task.candidate_index}: "
                f"{candidate.filename} from {candidate.username}"
            )
            return True
        except Exception as e:
            logger.error(f"  Failed to queue: {e}")
            # Try next candidate
            return self.queue_next_candidate(task)

    def check_transfer(self, task: DownloadTask) -> DownloadStatus:
        """
        Check the status of an active download.
        Returns the updated status. Handles cancellation logic.
        """
        if not task.current_result:
            return task.status

        result = task.current_result
        transfer = self.slskd.get_transfer_by_filename(
            result.username, result.file_path
        )

        if transfer is None:
            # Transfer might not have registered yet
            return task.status

        state = transfer.get("state", "") or transfer.get("stateDescription", "")
        state_lower = state.lower()
        progress = transfer.get("percentComplete", 0)
        speed = transfer.get("averageSpeed", 0)  # bytes/sec

        # slskd states: "Requested", "Queued", "Initializing", "InProgress",
        # "Completed, Succeeded", "Completed, Cancelled", "Completed, TimedOut",
        # "Completed, Errored", "Completed, Rejected"

        if "succeeded" in state_lower:
            bytes_transferred = transfer.get("bytesTransferred", 0)
            size_mb = bytes_transferred / 1024 / 1024

            # Minimum viable size check based on format and expected duration
            min_bytes = 512 * 1024  # absolute minimum 512KB
            track_dur = task.track.best_duration_sec
            if track_dur and result.file_format:
                # Estimate minimum reasonable file size
                fmt = result.file_format.value
                if fmt in ("flac", "aiff", "wav"):
                    # Lossless: at least ~50KB/sec of audio
                    min_bytes = max(min_bytes, int(track_dur * 50000))
                elif fmt == "mp3":
                    # 128kbps MP3 = ~16KB/sec (floor for acceptable quality)
                    min_bytes = max(min_bytes, int(track_dur * 16000))

            if bytes_transferred < min_bytes:
                min_mb = min_bytes / 1024 / 1024
                logger.warning(
                    f"  File too small ({size_mb:.1f}MB, expected ≥{min_mb:.1f}MB): {result.filename}"
                )
                return self._cancel_and_retry(task, f"File too small ({size_mb:.1f}MB)")

            task.status = DownloadStatus.COMPLETE
            task.output_path = self._resolve_output_path(task)
            logger.info(f"  ✓ Complete: {result.filename} ({size_mb:.1f}MB)")
            return task.status

        if "errored" in state_lower or "rejected" in state_lower:
            logger.warning(f"  Transfer failed: {state}")
            return self._cancel_and_retry(task, f"Transfer state: {state}")

        if "cancelled" in state_lower or "timedout" in state_lower:
            logger.warning(f"  Transfer cancelled/timed out: {state}")
            return self._cancel_and_retry(task, f"Transfer state: {state}")

        if "inprogress" in state_lower.replace(" ", ""):
            task.status = DownloadStatus.DOWNLOADING
            speed_kbps = speed / 1024.0 if speed else 0
            if speed_kbps < self.config.min_transfer_speed_kbps and progress > 5:
                logger.warning(f"  Slow transfer: {speed_kbps:.1f} KB/s")
                return self._cancel_and_retry(task, f"Slow transfer: {speed_kbps:.1f} KB/s")

        return task.status

    def _cancel_and_retry(self, task: DownloadTask, reason: str) -> DownloadStatus:
        """Cancel current download and try next candidate."""
        logger.warning(f"  Cancelling: {reason}")

        if task.current_result and task.transfer_id:
            self.slskd.cancel_download(
                task.current_result.username,
                task.transfer_id,
                task.current_result.file_path,
            )

        task.status = DownloadStatus.CANCELLED

        # Try next candidate
        if self.queue_next_candidate(task):
            return task.status
        else:
            task.status = DownloadStatus.FAILED
            task.error = reason
            return task.status

    def _resolve_output_path(self, task: DownloadTask) -> str:
        """Build the final output path for a completed download."""
        result = task.current_result
        track = task.track
        ext = result.extension if result else "flac"
        safe_artist = "".join(c for c in track.artist if c.isalnum() or c in " -_").strip()
        safe_title = "".join(c for c in track.title if c.isalnum() or c in " -_").strip()
        filename = f"{safe_artist} - {safe_title}.{ext}"
        return os.path.join(self.config.output_dir, filename)

    # ── Main download loop ────────────────────────────────────────────

    def run(self, tracks: list[Track]) -> dict:
        """
        Main entry point. Searches, queues, monitors, and retries all tracks.

        Returns a summary dict with counts and lists of completed/failed tracks.
        """
        self.prepare_tasks(tracks)

        # Phase 1: Search and rank all tracks
        # Sequential — slskd limits concurrent searches to 2, and each track
        # does multiple queries internally, so parallel causes silent failures
        logger.info("=" * 60)
        logger.info("Phase 1: Searching Soulseek for all tracks...")
        logger.info("=" * 60)

        for i, task in enumerate(self.tasks):
            try:
                self.search_and_rank(task)
            except Exception as e:
                logger.error(f"  Search failed for {task.track.search_query}: {e}")
                task.status = DownloadStatus.FAILED
                task.error = str(e)
            if i < len(self.tasks) - 1:
                time.sleep(2)

        # Phase 2: Queue initial downloads (up to max_concurrent)
        logger.info("=" * 60)
        logger.info("Phase 2: Queueing downloads...")
        logger.info("=" * 60)
        active: list[DownloadTask] = []
        pending = [t for t in self.tasks if t.candidates]

        def fill_slots():
            while len(active) < self.config.max_concurrent and pending:
                task = pending.pop(0)
                if self.queue_next_candidate(task):
                    active.append(task)

        fill_slots()

        # Phase 3: Monitor loop
        logger.info("=" * 60)
        logger.info("Phase 3: Monitoring downloads...")
        logger.info("=" * 60)
        start_times: dict[int, float] = {}  # task id -> time when queued

        while active:
            time.sleep(self.config.poll_interval_sec)

            for task in list(active):
                task_key = id(task)

                # Track when this task was queued
                if task_key not in start_times:
                    start_times[task_key] = time.time()

                # Check for start timeout
                elapsed_min = (time.time() - start_times[task_key]) / 60.0
                if (
                    task.status == DownloadStatus.QUEUED
                    and elapsed_min > self.config.start_timeout_min
                ):
                    logger.warning(
                        f"  Start timeout ({self.config.start_timeout_min} min): "
                        f"{task.track.search_query}"
                    )
                    self._cancel_and_retry(task, "Start timeout")
                    start_times[task_key] = time.time()  # Reset timer for retry

                # Check transfer status
                status = self.check_transfer(task)

                if status in (DownloadStatus.COMPLETE, DownloadStatus.FAILED):
                    active.remove(task)
                    fill_slots()

            # Progress report
            complete = sum(1 for t in self.tasks if t.status == DownloadStatus.COMPLETE)
            failed = sum(1 for t in self.tasks if t.status == DownloadStatus.FAILED)
            total = len(self.tasks)
            logger.info(
                f"  Progress: {complete}/{total} complete, "
                f"{failed} failed, {len(active)} active"
            )

        # Summary
        completed = [t for t in self.tasks if t.status == DownloadStatus.COMPLETE]
        failed = [t for t in self.tasks if t.status == DownloadStatus.FAILED]
        no_results = [t for t in self.tasks if not t.candidates and t.status == DownloadStatus.FAILED]

        # Flatten downloads — move files from subfolders to output root
        self.flatten_downloads()

        return {
            "total": len(self.tasks),
            "completed": len(completed),
            "failed": len(failed),
            "no_results": len(no_results),
            "completed_tracks": completed,
            "failed_tracks": failed,
        }

    def flatten_downloads(self):
        """
        Move all audio files from subdirectories into the output root.
        slskd saves files in subfolders mirroring the remote user's structure;
        this flattens everything into one directory.
        """
        import glob
        import shutil

        output_dir = self.config.output_dir
        audio_extensions = (".flac", ".aiff", ".aif", ".wav", ".mp3")
        moved = 0

        for root, dirs, files in os.walk(output_dir):
            if root == output_dir:
                continue  # Skip files already in the root
            for filename in files:
                if filename.lower().endswith(audio_extensions):
                    src = os.path.join(root, filename)
                    dst = os.path.join(output_dir, filename)
                    # Avoid overwriting
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(filename)
                        dst = os.path.join(output_dir, f"{base}_{moved}{ext}")
                    shutil.move(src, dst)
                    moved += 1

        # Clean up empty subdirectories
        for root, dirs, files in os.walk(output_dir, topdown=False):
            if root == output_dir:
                continue
            try:
                os.rmdir(root)  # Only removes if empty
            except OSError:
                pass

        if moved:
            logger.info(f"  Flattened {moved} files into {output_dir}")
