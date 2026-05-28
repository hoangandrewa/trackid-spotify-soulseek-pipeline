"""Load and validate config from YAML."""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import yaml


@dataclass
class SpotifyConfig:
    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8888/callback"
    playlist_name_template: str = "TrackID: {set_name}"


@dataclass
class SlskdConfig:
    base_url: str = "http://localhost:5030"
    api_key: str = ""
    username: str = ""
    password: str = ""


@dataclass
class DownloadConfig:
    output_dir: str = "~/Music/TrackID Downloads"
    format_priority: list[str] = None
    min_mp3_bitrate: int = 320
    duration_tolerance_sec: float = 5.0
    start_timeout_min: float = 5.0
    max_queue_position: int = 50
    min_transfer_speed_kbps: float = 20.0
    speed_floor_duration_sec: float = 60.0
    max_concurrent: int = 3
    poll_interval_sec: float = 10.0
    max_retries: int = 3

    def __post_init__(self):
        if self.format_priority is None:
            self.format_priority = ["flac", "aiff", "wav", "mp3"]
        self.output_dir = str(Path(self.output_dir).expanduser())


@dataclass
class Config:
    spotify: SpotifyConfig
    slskd: SlskdConfig
    download: DownloadConfig


def load_config(path: Optional[str] = None) -> Config:
    """Load config from YAML file. Checks ./config.yaml by default."""
    if path is None:
        path = os.environ.get("TRACKID_CONFIG", "config.yaml")

    with open(path) as f:
        raw = yaml.safe_load(f)

    return Config(
        spotify=SpotifyConfig(**raw.get("spotify", {})),
        slskd=SlskdConfig(**raw.get("slskd", {})),
        download=DownloadConfig(**raw.get("download", {})),
    )