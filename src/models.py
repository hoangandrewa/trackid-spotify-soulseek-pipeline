"""Data models for the pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DownloadStatus(Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileFormat(Enum):
    FLAC = "flac"
    AIFF = "aiff"
    WAV = "wav"
    MP3 = "mp3"
    UNKNOWN = "unknown"

    @classmethod
    def from_extension(cls, ext: str) -> "FileFormat":
        ext = ext.lower().lstrip(".")
        if ext in ("aif", "aiff"):
            return cls.AIFF
        return cls._value2member_map_.get(ext, cls.UNKNOWN)


@dataclass
class Track:
    """A track parsed from the TrackID CSV export."""
    artist: str
    title: str
    duration_sec: Optional[float] = None
    bpm: Optional[float] = None
    label: Optional[str] = None
    # Set after Spotify lookup
    spotify_uri: Optional[str] = None
    spotify_duration_ms: Optional[int] = None

    @property
    def search_query(self) -> str:
        return f"{self.artist} - {self.title}"

    @property
    def best_duration_sec(self) -> Optional[float]:
        """Best known duration — prefer TrackID, fall back to Spotify."""
        if self.duration_sec:
            return self.duration_sec
        if self.spotify_duration_ms:
            return self.spotify_duration_ms / 1000.0
        return None


@dataclass
class SoulseekResult:
    """A single file result from a Soulseek search."""
    username: str
    filename: str
    file_path: str
    size_bytes: int
    bitrate: Optional[int] = None
    duration_sec: Optional[float] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    file_format: FileFormat = FileFormat.UNKNOWN
    # slskd internal IDs
    search_id: Optional[str] = None
    file_id: Optional[str] = None
    # User's queue depth / free slots
    free_upload_slots: bool = False
    queue_length: int = 0

    @property
    def extension(self) -> str:
        return self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""


@dataclass
class DownloadTask:
    """Tracks the state of downloading a specific track."""
    track: Track
    status: DownloadStatus = DownloadStatus.PENDING
    current_result: Optional[SoulseekResult] = None
    # All candidates ranked by quality
    candidates: list[SoulseekResult] = field(default_factory=list)
    # Index into candidates — which one we're currently trying
    candidate_index: int = 0
    attempts: int = 0
    # slskd transfer ID once queued
    transfer_id: Optional[str] = None
    output_path: Optional[str] = None
    error: Optional[str] = None
