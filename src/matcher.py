"""Fuzzy matching and candidate ranking for Soulseek results."""

import re
from typing import Optional

from thefuzz import fuzz

from .models import Track, SoulseekResult, FileFormat


# Noise that appears in track names but doesn't help matching
STRIP_PATTERNS = [
    r"\(original\s*mix\)",
    r"\(clip\)",
    r"\(preview\)",
    r"\(master\)",
    r"\(digital\)",
    r"\[.*?\]",  # Anything in square brackets
    r"feat\.?\s+.*$",  # featuring credits
]


def normalize_for_matching(text: str) -> str:
    """Strip noise, lowercase, collapse whitespace for fuzzy comparison."""
    text = text.lower().strip()
    for pattern in STRIP_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # Remove non-alphanumeric except spaces
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_score(track: Track, result: SoulseekResult) -> int:
    """
    Score how well a Soulseek result matches the target track.
    Returns 0-100. Higher is better.
    """
    # Build the expected string from the track
    expected = normalize_for_matching(f"{track.artist} {track.title}")

    # Build the candidate string from the filename (strip path + extension)
    filename_clean = result.filename.rsplit("/", 1)[-1]
    filename_clean = filename_clean.rsplit("\\", 1)[-1]
    filename_clean = filename_clean.rsplit(".", 1)[0]
    # Replace underscores and hyphens with spaces
    filename_clean = filename_clean.replace("_", " ").replace("-", " ")
    candidate = normalize_for_matching(filename_clean)

    # Also try matching against the full path (sometimes has artist/album folders)
    path_clean = result.file_path.replace("\\", "/").replace("_", " ").replace("-", " ")
    path_candidate = normalize_for_matching(path_clean)

    # Take the best score between filename and full path
    score = max(
        fuzz.token_sort_ratio(expected, candidate),
        fuzz.token_sort_ratio(expected, path_candidate),
        fuzz.partial_ratio(expected, candidate),
    )

    return score


def duration_matches(
    track: Track,
    result: SoulseekResult,
    tolerance_sec: float = 5.0,
) -> Optional[bool]:
    """
    Check if the duration of a Soulseek result is within tolerance.
    Returns None if we can't determine (no duration data on either side).
    """
    track_dur = track.best_duration_sec
    result_dur = result.duration_sec

    if track_dur is None or result_dur is None:
        return None  # Can't verify — not a rejection, just unknown

    return abs(track_dur - result_dur) <= tolerance_sec


def rank_candidates(
    track: Track,
    results: list[SoulseekResult],
    format_priority: list[str],
    duration_tolerance_sec: float = 5.0,
    min_match_score: int = 60,
    min_mp3_bitrate: int = 320,
    max_queue_position: int = 50,
) -> list[SoulseekResult]:
    """
    Filter and rank Soulseek results for a track.

    Scoring weights:
    - Format preference (highest priority)
    - Duration match
    - Fuzzy name match
    - Queue position / availability
    """
    scored: list[tuple[float, SoulseekResult]] = []

    for result in results:
        # === Hard filters ===

        # Must meet minimum name match
        name_score = match_score(track, result)
        if name_score < min_match_score:
            continue

        # Duration check (skip mismatches, allow unknowns through)
        dur_ok = duration_matches(track, result, duration_tolerance_sec)
        if dur_ok is False:
            continue

        # Skip low-bitrate MP3s
        if result.file_format == FileFormat.MP3:
            if result.bitrate and result.bitrate < min_mp3_bitrate:
                continue

        # Skip users with massive queues
        if result.queue_length > max_queue_position:
            continue

        # === Scoring ===
        total = 0.0

        # Format score: 0-35 points
        fmt = result.file_format.value
        if fmt in format_priority:
            idx = format_priority.index(fmt)
            total += 35 - (idx * (25 / max(len(format_priority) - 1, 1)))
        else:
            total += 0

        # Audio quality: 0-25 points
        quality = 0.0

        # Bit depth: 24-bit = 10pts, 16-bit = 5pts
        if result.bit_depth:
            if result.bit_depth >= 24:
                quality += 10
            elif result.bit_depth >= 16:
                quality += 5

        # Sample rate: 96k+ = 8pts, 48k = 5pts, 44.1k = 3pts
        if result.sample_rate:
            if result.sample_rate >= 96000:
                quality += 8
            elif result.sample_rate >= 48000:
                quality += 5
            elif result.sample_rate >= 44100:
                quality += 3

        # Bitrate for lossy: 320 = 5pts, 256 = 3pts, lower = 0
        if result.bitrate:
            if result.bitrate >= 320:
                quality += 5
            elif result.bitrate >= 256:
                quality += 3

        # File size as quality proxy (bigger = better for same format/duration)
        # Only useful when bit depth/sample rate aren't available
        if quality < 5 and result.size_bytes and track.best_duration_sec:
            # bytes per second — higher is better quality
            bps = result.size_bytes / max(track.best_duration_sec, 1)
            if result.file_format in (FileFormat.FLAC, FileFormat.AIFF, FileFormat.WAV):
                # Lossless: >200KB/s is likely 24-bit, >100KB/s is 16-bit
                if bps > 200000:
                    quality += 7
                elif bps > 100000:
                    quality += 3
            elif result.file_format == FileFormat.MP3:
                if bps > 38000:  # ~304kbps
                    quality += 4

        total += min(quality, 25)

        # Name match: 0-20 points
        total += (name_score / 100) * 20

        # Duration verified: 0 or 10 points
        if dur_ok is True:
            total += 10
        elif dur_ok is None:
            total += 3

        # Availability: 0-10 points
        if result.free_upload_slots:
            total += 7
        queue_penalty = min(result.queue_length, max_queue_position) / max_queue_position
        total += (1 - queue_penalty) * 3

        scored.append((total, result))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]
