"""Post-download verification: confirm file integrity, duration, and format."""

import logging
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from .models import Track, DownloadTask

logger = logging.getLogger(__name__)


def verify_download(task: DownloadTask, tolerance_sec: float = 5.0) -> bool:
    """
    Verify a completed download:
    1. File exists and is not empty
    2. File is a valid audio file (parseable by mutagen)
    3. Duration matches expected (within tolerance)

    Returns True if verified, False if suspicious.
    """
    if not task.output_path:
        logger.error("No output path set on task")
        return False

    path = Path(task.output_path)
    if not path.exists():
        logger.error(f"File not found: {path}")
        return False

    if path.stat().st_size < 1024:  # Less than 1KB is suspicious
        logger.error(f"File suspiciously small ({path.stat().st_size} bytes): {path}")
        return False

    # Try to parse with mutagen
    try:
        audio = MutagenFile(str(path))
        if audio is None:
            logger.error(f"Mutagen could not parse: {path}")
            return False
    except Exception as e:
        logger.error(f"Error reading audio file: {e}")
        return False

    # Check duration
    file_duration = get_duration(str(path))
    expected_duration = task.track.best_duration_sec

    if file_duration and expected_duration:
        diff = abs(file_duration - expected_duration)
        if diff > tolerance_sec:
            logger.warning(
                f"Duration mismatch: file={file_duration:.1f}s, "
                f"expected={expected_duration:.1f}s (diff={diff:.1f}s)"
            )
            return False
        logger.info(f"  Duration verified: {file_duration:.1f}s (±{diff:.1f}s)")

    logger.info(f"  ✓ Verified: {path.name}")
    return True


def get_duration(file_path: str) -> Optional[float]:
    """Get the duration of an audio file in seconds."""
    try:
        audio = MutagenFile(file_path)
        if audio and audio.info:
            return audio.info.length
    except Exception:
        pass
    return None


def get_file_info(file_path: str) -> dict:
    """Get detailed info about a downloaded audio file."""
    info = {
        "path": file_path,
        "size_bytes": Path(file_path).stat().st_size,
        "format": Path(file_path).suffix.lstrip(".").upper(),
    }

    try:
        audio = MutagenFile(file_path)
        if audio and audio.info:
            info["duration_sec"] = audio.info.length
            info["sample_rate"] = getattr(audio.info, "sample_rate", None)
            info["channels"] = getattr(audio.info, "channels", None)
            info["bitrate"] = getattr(audio.info, "bitrate", None)
            info["bits_per_sample"] = getattr(audio.info, "bits_per_sample", None)
    except Exception:
        pass

    return info
