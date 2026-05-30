"""Two-stage matching and ranking for Soulseek results.

Stage A — Identity: Is this actually the correct track?
  - Artist token coverage
  - Title token coverage
  - Duration verification
  - Negative keyword rejection

Stage B — Quality: Among confirmed matches, which is the best copy?
  - Format preference
  - Audio quality (bit depth, sample rate, bitrate)
  - Availability (queue position, free slots)
"""

import re
from typing import Optional

from thefuzz import fuzz

from .models import Track, SoulseekResult, FileFormat


# ── Constants ─────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {"flac", "aiff", "aif", "wav", "mp3", "ogg", "wma", "m4a", "aac"}

STRIP_PATTERNS = [
    r"\(original\s*mix\)",
    r"\(clip\)",
    r"\(preview\)",
    r"\(master\)",
    r"\(digital\)",
    r"\[.*?\]",
    r"feat\.?\s+.*$",
]

STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "mix", "original", "pt", "part", "vol", "version",
}

# Hard reject — files containing these terms are never viable
NEGATIVE_KEYWORDS_HARD = [
    "sample pack", "samplepack", "stems", "acapella", "a cappella",
    "tutorial", "masterclass", "ableton", "fl studio", "logic pro",
    "midi pack", "drum kit", "loop pack", "sound design",
    "dj set", "dj mix", "continuous mix", "mixed by",
    "karaoke", "instrumental version", "backing track",
]

# Soft penalty — these terms reduce confidence but don't reject
NEGATIVE_KEYWORDS_SOFT = [
    "vinyl rip", "youtube rip", "radio rip", "radio edit",
    "bootleg", "unofficial", "re-edit",
    "live at", "live from", "live version",
    "preview", "snippet", "clip",
]


# ── Text normalization ────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, transliterate accents, strip noise, collapse spaces."""
    import unicodedata
    text = text.lower().strip()
    # Transliterate accented characters to ASCII (ø→o, å→a, ā→a, é→e)
    text = text.replace("ø", "o")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Apostrophes: remove entirely (hangin' → hangin, don't → dont)
    text = re.sub(r"['\u2018\u2019\u201c\u201d`]", "", text)
    # Hyphens, dashes, periods: replace with space
    text = re.sub(r"[-–—.]", " ", text)
    # Brackets/parens: remove, keep content
    text = re.sub(r"[\[\](){}]", " ", text)
    for pattern in STRIP_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Backward-compatible alias
normalize_for_matching = normalize


def extract_words(text: str, remove_stopwords: bool = True) -> set[str]:
    """Normalize text and split into a set of meaningful words."""
    words = set(normalize(text).split())
    if remove_stopwords:
        words = words - STOPWORDS
    return words


def get_candidate_text(result: SoulseekResult) -> tuple[str, str]:
    """Extract normalized filename and path text from a result."""
    filename = result.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    filename_norm = normalize(filename.replace("_", " ").replace("-", " "))
    path_norm = normalize(result.file_path.replace("\\", "/").replace("_", " ").replace("-", " "))
    return filename_norm, path_norm


# ── Stage A: Identity matching ────────────────────────────────────────

def check_identity(
    track: Track,
    result: SoulseekResult,
    duration_tolerance_sec: float = 5.0,
) -> tuple[bool, float]:
    """
    Stage A: Determine if this result is actually the correct track.

    Returns (passes, confidence) where:
      - passes: True if this result should be considered
      - confidence: 0.0-1.0 identity confidence score
    """
    filename_norm, path_norm = get_candidate_text(result)
    candidate_words = set(filename_norm.split()) | set(path_norm.split())
    candidate_text = filename_norm + " " + path_norm

    # ── Hard reject: negative keywords ──
    for keyword in NEGATIVE_KEYWORDS_HARD:
        if keyword in candidate_text:
            return False, 0.0

    # ── Audio file check ──
    if result.extension not in AUDIO_EXTENSIONS:
        return False, 0.0

    # ── Artist matching ──
    artist_words = extract_words(track.artist)
    # For collabs, also try just the primary artist
    primary_artist = track.artist.split(",")[0].strip()
    primary_words = extract_words(primary_artist)

    if artist_words:
        artist_matches = artist_words & candidate_words
        primary_matches = primary_words & candidate_words
        # Use whichever gives better coverage
        best_artist_coverage = max(
            len(artist_matches) / len(artist_words) if artist_words else 0,
            len(primary_matches) / len(primary_words) if primary_words else 0,
        )
    else:
        best_artist_coverage = 0.0

    # ── Title matching ──
    # Strip parentheticals for a clean core title
    core_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", track.title).strip()
    title_words = extract_words(core_title)

    # If core title is empty after stripping, use the full title
    if not title_words:
        title_words = extract_words(track.title)

    if title_words:
        title_matches = title_words & candidate_words
        title_coverage = len(title_matches) / len(title_words)
    else:
        title_coverage = 0.0

    # ── Identity gate ──
    # Title is required — must have strong coverage
    if title_coverage < 0.5:
        return False, 0.0

    # Artist should have some presence (but be lenient for compilations)
    if artist_words and best_artist_coverage < 0.3:
        # Check if artist appears in the path (folder name)
        artist_in_path = any(w in path_norm for w in primary_words if len(w) > 2)
        if not artist_in_path:
            return False, 0.0

    # ── Duration as identity signal ──
    duration_confidence = 0.5  # default: unknown
    track_dur = track.best_duration_sec
    result_dur = result.duration_sec

    if track_dur and result_dur:
        diff = abs(track_dur - result_dur)
        if diff <= 1:
            duration_confidence = 1.0
        elif diff <= 3:
            duration_confidence = 0.9
        elif diff <= 5:
            duration_confidence = 0.7
        else:
            # Duration mismatch > 5 sec — reject
            return False, 0.0

    # ── Compute identity confidence ──
    # Weighted: title 40%, artist 25%, duration 35%
    confidence = (
        title_coverage * 0.40
        + best_artist_coverage * 0.25
        + duration_confidence * 0.35
    )

    # ── Soft negative keyword penalty ──
    for keyword in NEGATIVE_KEYWORDS_SOFT:
        if keyword in candidate_text:
            confidence *= 0.7
            break  # Only penalize once

    # Minimum confidence threshold
    if confidence < 0.35:
        return False, 0.0

    return True, confidence


# ── Stage B: Quality ranking ──────────────────────────────────────────

def score_quality(
    track: Track,
    result: SoulseekResult,
    identity_confidence: float,
    format_priority: list[str],
    min_mp3_bitrate: int = 320,
    max_queue_position: int = 50,
) -> float:
    """
    Stage B: Among confirmed identity matches, score by quality.

    Returns a total score. Higher is better.
    Only called on results that passed Stage A.
    """
    total = 0.0

    # ── Identity confidence bonus: 0-20 pts ──
    # Reward higher-confidence matches even in quality ranking
    total += identity_confidence * 20

    # ── Format preference: 0-30 pts ──
    fmt = result.file_format.value
    if fmt in format_priority:
        idx = format_priority.index(fmt)
        total += 30 - (idx * (20 / max(len(format_priority) - 1, 1)))

    # ── Audio quality: 0-25 pts ──
    quality = 0.0

    if result.bit_depth:
        if result.bit_depth >= 24:
            quality += 10
        elif result.bit_depth >= 16:
            quality += 5

    if result.sample_rate:
        if result.sample_rate >= 96000:
            quality += 8
        elif result.sample_rate >= 48000:
            quality += 5
        elif result.sample_rate >= 44100:
            quality += 3

    if result.bitrate:
        if result.bitrate >= 320:
            quality += 5
        elif result.bitrate >= 256:
            quality += 3

    # File size as quality proxy when metadata isn't available
    if quality < 5 and result.size_bytes and track.best_duration_sec:
        bps = result.size_bytes / max(track.best_duration_sec, 1)
        if result.file_format in (FileFormat.FLAC, FileFormat.AIFF, FileFormat.WAV):
            if bps > 200000:
                quality += 7
            elif bps > 100000:
                quality += 3
        elif result.file_format == FileFormat.MP3:
            if bps > 38000:
                quality += 4

    total += min(quality, 25)

    # ── Availability: 0-10 pts ──
    if result.free_upload_slots:
        total += 7
    if max_queue_position > 0:
        queue_penalty = min(result.queue_length, max_queue_position) / max_queue_position
        total += (1 - queue_penalty) * 3

    # ── MP3 bitrate penalty ──
    if result.file_format == FileFormat.MP3:
        if result.bitrate and result.bitrate < min_mp3_bitrate:
            total -= 15

    return total


# ── Main entry point ──────────────────────────────────────────────────

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
    Two-stage matching pipeline:
      Stage A — filter to only correct tracks (identity)
      Stage B — rank correct tracks by quality

    Returns results sorted best-first.
    """
    scored: list[tuple[float, SoulseekResult]] = []

    for result in results:
        # Stage A: Identity — is this the right track?
        passes, confidence = check_identity(
            track, result, duration_tolerance_sec
        )
        if not passes:
            continue

        # Stage B: Quality — how good is this copy?
        quality_score = score_quality(
            track, result, confidence,
            format_priority=format_priority,
            min_mp3_bitrate=min_mp3_bitrate,
            max_queue_position=max_queue_position,
        )

        scored.append((quality_score, result))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]
