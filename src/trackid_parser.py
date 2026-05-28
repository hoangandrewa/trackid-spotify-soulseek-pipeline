"""Parse TrackID.net CSV exports into Track objects."""

import csv
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from .models import Track


def parse_duration(raw: str) -> Optional[float]:
    """Parse duration strings like '6:42' or '06:42' into seconds."""
    if not raw or pd.isna(raw):
        return None
    raw = str(raw).strip()
    # MM:SS or HH:MM:SS
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    # Maybe it's already in seconds
    try:
        return float(raw)
    except ValueError:
        return None


def normalize_column_name(col: str) -> str:
    """Normalize column names to snake_case for flexible CSV parsing."""
    col = col.strip().lower()
    col = re.sub(r"[^a-z0-9]+", "_", col)
    return col.strip("_")


def parse_csv(csv_path: str) -> list[Track]:
    """
    Parse a TrackID.net CSV export.

    TrackID CSVs vary in column names, but typically include:
    - Artist / DJ / Performer
    - Title / Track / Track Name
    - Duration / Length / Time
    - BPM / Tempo (optional)
    - Label (optional)

    This parser normalizes column names and maps flexibly.
    """
    df = pd.read_csv(csv_path)

    # Normalize column names
    col_map = {col: normalize_column_name(col) for col in df.columns}
    df = df.rename(columns=col_map)

    # Flexible column detection
    def find_col(*candidates: str) -> Optional[str]:
        for c in candidates:
            for col in df.columns:
                if c in col:
                    return col
        return None

    artist_col = find_col("artist", "dj", "performer")
    title_col = find_col("title", "track_name", "track", "name")
    duration_col = find_col("duration", "length", "time")
    bpm_col = find_col("bpm", "tempo")
    label_col = find_col("label", "imprint")

    if not artist_col or not title_col:
        # Fallback: maybe it's "Artist - Title" in a single column
        single_col = find_col("track", "name", "title")
        if single_col:
            return _parse_combined_column(df, single_col, duration_col, bpm_col, label_col)
        raise ValueError(
            f"Cannot find artist/title columns in CSV. Found: {list(df.columns)}"
        )

    tracks = []
    for _, row in df.iterrows():
        artist = str(row[artist_col]).strip() if pd.notna(row[artist_col]) else ""
        title = str(row[title_col]).strip() if pd.notna(row[title_col]) else ""

        if not artist or not title:
            continue

        track = Track(
            artist=artist,
            title=title,
            duration_sec=parse_duration(row.get(duration_col, "")) if duration_col else None,
            bpm=float(row[bpm_col]) if bpm_col and pd.notna(row.get(bpm_col)) else None,
            label=str(row[label_col]).strip() if label_col and pd.notna(row.get(label_col)) else None,
        )
        tracks.append(track)

    return tracks


def _parse_combined_column(
    df: pd.DataFrame,
    track_col: str,
    duration_col: Optional[str],
    bpm_col: Optional[str],
    label_col: Optional[str],
) -> list[Track]:
    """Parse 'Artist - Title' style combined columns."""
    tracks = []
    for _, row in df.iterrows():
        raw = str(row[track_col]).strip()
        # Try common separators: " - ", " – ", " — "
        for sep in [" - ", " – ", " — "]:
            if sep in raw:
                parts = raw.split(sep, 1)
                artist, title = parts[0].strip(), parts[1].strip()
                break
        else:
            # Can't split — use whole thing as title, unknown artist
            artist, title = "Unknown", raw

        track = Track(
            artist=artist,
            title=title,
            duration_sec=parse_duration(row.get(duration_col, "")) if duration_col else None,
            bpm=float(row[bpm_col]) if bpm_col and pd.notna(row.get(bpm_col)) else None,
            label=str(row[label_col]).strip() if label_col and pd.notna(row.get(label_col)) else None,
        )
        tracks.append(track)
    return tracks


def get_set_name(csv_path: str) -> str:
    """Extract a human-readable set name from the CSV filename."""
    return Path(csv_path).stem.replace("_", " ").replace("-", " ")
