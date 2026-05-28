"""CLI entry point for the TrackID → Spotify → Soulseek pipeline.

Accepts either a TrackID CSV export or a Spotify playlist URL as input.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click

from .config import load_config
from .trackid_parser import parse_csv, get_set_name
from .spotify_client import SpotifyClient
from .slskd_client import SlskdClient
from .downloader import DownloadOrchestrator
from .verify import verify_download, get_file_info
from .models import Track, DownloadStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STATE_FILE = "pipeline_state.json"
LOGS_DIR = "logs"
HISTORY_FILE = "download_history.json"


# ── Shared input resolution ──────────────────────────────────────────

def resolve_input(
    config,
    csv_path: Optional[str] = None,
    playlist: Optional[str] = None,
) -> tuple[list[Track], str, bool]:
    """
    Resolve tracks from either a CSV file or a Spotify playlist URL.

    Returns (tracks, set_name, from_spotify).
    from_spotify=True means we already have Spotify metadata — skip search.
    """
    if csv_path and playlist:
        raise click.UsageError("Provide either --csv or --playlist, not both.")
    if not csv_path and not playlist:
        raise click.UsageError("Provide either --csv or --playlist.")

    if csv_path:
        tracks = parse_csv(csv_path)
        set_name = get_set_name(csv_path)
        logger.info(f"Parsed CSV: {len(tracks)} tracks — \"{set_name}\"")
        return tracks, set_name, False

    # Spotify playlist
    spotify = SpotifyClient(config.spotify)
    set_name, tracks = spotify.get_playlist_tracks(playlist)
    logger.info(f"Loaded Spotify playlist: {len(tracks)} tracks — \"{set_name}\"")
    return tracks, set_name, True


def print_tracklist(tracks: list[Track]):
    for i, t in enumerate(tracks, 1):
        dur = ""
        if t.duration_sec:
            m, s = divmod(int(t.duration_sec), 60)
            dur = f" [{m}:{s:02d}]"
        logger.info(f"  {i:3d}. {t.artist} - {t.title}{dur}")


def save_state(tasks, set_name: str = "", path: str = STATE_FILE):
    """
    Save pipeline results three ways:
    1. pipeline_state.json — latest run (used by retry command)
    2. logs/YYYY-MM-DD_HHMMSS_{set_name}.json — timestamped run log
    3. download_history.json — persistent append-only history across all runs
    """
    from datetime import datetime
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")

    run_data = [
        {
            "artist": t.track.artist,
            "title": t.track.title,
            "status": t.status.value,
            "error": t.error,
            "output_path": t.output_path,
        }
        for t in tasks
    ]

    # 1. Latest state (for retry)
    with open(path, "w") as f:
        json.dump(run_data, f, indent=2)

    # 2. Timestamped log
    os.makedirs(LOGS_DIR, exist_ok=True)
    safe_name = "".join(c for c in set_name if c.isalnum() or c in " -_").strip().replace(" ", "_")
    log_filename = f"{timestamp}_{safe_name}.json" if safe_name else f"{timestamp}.json"
    log_path = os.path.join(LOGS_DIR, log_filename)

    run_summary = {
        "timestamp": now.isoformat(),
        "set_name": set_name,
        "total": len(run_data),
        "completed": sum(1 for t in run_data if t["status"] == "complete"),
        "failed": sum(1 for t in run_data if t["status"] == "failed"),
        "tracks": run_data,
    }
    with open(log_path, "w") as f:
        json.dump(run_summary, f, indent=2)
    logger.info(f"  Run log saved: {log_path}")

    # 3. Append to persistent history
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []

    for t in run_data:
        entry = {
            "timestamp": now.isoformat(),
            "set_name": set_name,
            **t,
        }
        history.append(entry)

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def filter_already_downloaded(tracks: list[Track]) -> tuple[list[Track], list[Track]]:
    """
    Check download history and remove tracks that were previously completed.
    Returns (new_tracks, skipped_tracks).
    """
    if not os.path.exists(HISTORY_FILE):
        return tracks, []

    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except (json.JSONDecodeError, IOError):
        return tracks, []

    # Build set of previously completed "artist - title" keys (lowercased)
    completed = set()
    for entry in history:
        if entry.get("status") == "complete":
            key = f"{entry.get('artist', '').lower()} - {entry.get('title', '').lower()}"
            completed.add(key)

    new_tracks = []
    skipped = []
    for track in tracks:
        key = f"{track.artist.lower()} - {track.title.lower()}"
        if key in completed:
            skipped.append(track)
        else:
            new_tracks.append(track)

    if skipped:
        logger.info(f"  Skipping {len(skipped)} already-downloaded tracks")
        for t in skipped:
            logger.info(f"    ✓ {t.artist} - {t.title}")

    return new_tracks, skipped


# ── Input options shared across commands ─────────────────────────────

_input_options = [
    click.option("--csv", "csv_path", default=None, help="Path to TrackID.net CSV export"),
    click.option("--playlist", default=None, help="Spotify playlist URL or URI"),
]


def input_options(fn):
    """Decorator that adds --csv and --playlist options."""
    for opt in reversed(_input_options):
        fn = opt(fn)
    return fn


# ── CLI ──────────────────────────────────────────────────────────────

@click.group()
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
@click.pass_context
def cli(ctx, config_path):
    """Track acquisition pipeline: CSV or Spotify → Soulseek download."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@input_options
@click.pass_context
def run(ctx, csv_path: Optional[str], playlist: Optional[str]):
    """Full pipeline: parse input → Spotify playlist (if CSV) → Soulseek download."""
    config = ctx.obj["config"]

    # Step 1: Load tracks
    logger.info("=" * 60)
    logger.info("Loading tracks...")
    logger.info("=" * 60)
    tracks, set_name, from_spotify = resolve_input(config, csv_path, playlist)
    print_tracklist(tracks)

    # Step 2: Spotify — only needed if source is CSV (playlist input already has metadata)
    playlist_url = ""
    if from_spotify:
        logger.info("")
        logger.info("Source is Spotify — skipping playlist creation (you already have it).")
        # Tracks already have spotify_uri and spotify_duration_ms populated
    else:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Creating Spotify preview playlist...")
        logger.info("=" * 60)
        spotify = SpotifyClient(config.spotify)
        playlist_url, found, missing = spotify.search_and_create_playlist(tracks, set_name)

        if playlist_url:
            logger.info(f"\n  Spotify playlist: {playlist_url}")
        logger.info(f"  Found: {len(found)}/{len(tracks)}")
        if missing:
            logger.info("  Missing from Spotify:")
            for t in missing:
                logger.info(f"    - {t.search_query}")

    # Step 3: Soulseek download
    logger.info("")
    logger.info("=" * 60)
    logger.info("Downloading from Soulseek...")
    logger.info("=" * 60)
    tracks, skipped = filter_already_downloaded(tracks)
    if not tracks:
        logger.info("All tracks already downloaded!")
        return
    slskd = SlskdClient(config.slskd)
    orchestrator = DownloadOrchestrator(slskd, config.download)
    summary = orchestrator.run(tracks)

    # Step 4: Verify downloads
    logger.info("")
    logger.info("=" * 60)
    logger.info("Verifying downloads...")
    logger.info("=" * 60)
    verified = 0
    for task in orchestrator.tasks:
        if task.status == DownloadStatus.COMPLETE:
            if verify_download(task, config.download.duration_tolerance_sec):
                verified += 1
                info = get_file_info(task.output_path)
                logger.info(
                    f"    {info['format']} | "
                    f"{info.get('sample_rate', '?')}Hz | "
                    f"{info.get('bits_per_sample', '?')}bit | "
                    f"{info['size_bytes'] / 1024 / 1024:.1f}MB"
                )

    # Final summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Total tracks:     {summary['total']}")
    logger.info(f"  Downloaded:       {summary['completed']}")
    logger.info(f"  Verified:         {verified}")
    logger.info(f"  Failed:           {summary['failed']}")
    logger.info(f"  No results:       {summary['no_results']}")
    if playlist_url:
        logger.info(f"  Spotify playlist: {playlist_url}")
    logger.info(f"  Output dir:       {config.download.output_dir}")

    if summary["failed_tracks"]:
        logger.info("\n  Failed tracks:")
        for task in summary["failed_tracks"]:
            logger.info(f"    - {task.track.search_query}: {task.error}")

    save_state(orchestrator.tasks, set_name)


@cli.command()
@input_options
@click.pass_context
def spotify(ctx, csv_path: Optional[str], playlist: Optional[str]):
    """Spotify only: create a preview playlist (CSV input) or show playlist info."""
    config = ctx.obj["config"]
    tracks, set_name, from_spotify = resolve_input(config, csv_path, playlist)

    if from_spotify:
        # Already have everything — just print the tracklist
        logger.info(f"Playlist: {set_name}")
        print_tracklist(tracks)
        logger.info(f"\n{len(tracks)} tracks — all have Spotify metadata, ready for download.")
    else:
        client = SpotifyClient(config.spotify)
        playlist_url, found, missing = client.search_and_create_playlist(tracks, set_name)
        logger.info(f"\nPlaylist: {playlist_url}")
        logger.info(f"Found: {len(found)}/{len(tracks)}")
        if missing:
            logger.info("\nNot found on Spotify:")
            for t in missing:
                logger.info(f"  - {t.search_query}")


@cli.command()
@input_options
@click.pass_context
def download(ctx, csv_path: Optional[str], playlist: Optional[str]):
    """Soulseek only: download tracks from CSV or Spotify playlist."""
    config = ctx.obj["config"]
    tracks, set_name, _ = resolve_input(config, csv_path, playlist)
    tracks, skipped = filter_already_downloaded(tracks)
    if not tracks:
        logger.info("All tracks already downloaded!")
        return
    logger.info(f"Downloading {len(tracks)} tracks...")

    slskd = SlskdClient(config.slskd)
    orchestrator = DownloadOrchestrator(slskd, config.download)
    summary = orchestrator.run(tracks)

    logger.info(f"\nCompleted: {summary['completed']}/{summary['total']}")
    logger.info(f"Failed: {summary['failed']}")
    save_state(orchestrator.tasks, set_name)


@cli.command()
@click.pass_context
def retry(ctx):
    """Retry failed downloads from the last run."""
    config = ctx.obj["config"]

    if not Path(STATE_FILE).exists():
        logger.error(f"No state file found ({STATE_FILE}). Run the pipeline first.")
        sys.exit(1)

    with open(STATE_FILE) as f:
        state = json.load(f)

    failed_tracks = [
        Track(artist=s["artist"], title=s["title"])
        for s in state
        if s["status"] in ("failed", "cancelled")
    ]

    if not failed_tracks:
        logger.info("No failed tracks to retry.")
        return

    logger.info(f"Retrying {len(failed_tracks)} failed tracks...")
    slskd = SlskdClient(config.slskd)
    orchestrator = DownloadOrchestrator(slskd, config.download)
    summary = orchestrator.run(failed_tracks)

    logger.info(f"\nCompleted: {summary['completed']}/{summary['total']}")
    logger.info(f"Still failed: {summary['failed']}")
    save_state(orchestrator.tasks, "retry")


@cli.command(name="list")
@input_options
@click.pass_context
def list_tracks(ctx, csv_path: Optional[str], playlist: Optional[str]):
    """Parse and display the tracklist (dry run)."""
    config = ctx.obj["config"]
    tracks, set_name, from_spotify = resolve_input(config, csv_path, playlist)

    source = "Spotify" if from_spotify else "CSV"
    print(f"\n{set_name}  ({source}, {len(tracks)} tracks)\n")

    for i, t in enumerate(tracks, 1):
        parts = [f"{i:3d}. {t.artist} - {t.title}"]
        if t.duration_sec:
            m, s = divmod(int(t.duration_sec), 60)
            parts.append(f"[{m}:{s:02d}]")
        if t.bpm:
            parts.append(f"{t.bpm:.0f}bpm")
        if t.label:
            parts.append(f"({t.label})")
        if from_spotify and t.spotify_uri:
            parts.append("✓ Spotify")
        print("  ".join(parts))


@cli.command()
@click.pass_context
def status(ctx):
    """Check slskd connection and active downloads."""
    config = ctx.obj["config"]
    slskd = SlskdClient(config.slskd)

    try:
        downloads = slskd.get_all_downloads()
        logger.info(f"slskd connected at {config.slskd.base_url}")
        logger.info(f"Active download groups: {len(downloads)}")
    except Exception as e:
        logger.error(f"Cannot connect to slskd: {e}")
        sys.exit(1)


@cli.command()
@click.option("--failed-only", is_flag=True, help="Show only failed tracks")
def history(failed_only):
    """View download history across all runs."""
    if not os.path.exists(HISTORY_FILE):
        print("No download history yet. Run the pipeline first.")
        return

    with open(HISTORY_FILE) as f:
        entries = json.load(f)

    if failed_only:
        entries = [e for e in entries if e["status"] != "complete"]

    if not entries:
        print("No matching entries.")
        return

    # Group by run
    from collections import defaultdict
    runs = defaultdict(list)
    for e in entries:
        key = f"{e.get('timestamp', '?')[:16]} — {e.get('set_name', '?')}"
        runs[key].append(e)

    for run_key, tracks in runs.items():
        complete = sum(1 for t in tracks if t["status"] == "complete")
        failed = sum(1 for t in tracks if t["status"] == "failed")
        print(f"\n{run_key}  ({complete} ok, {failed} failed, {len(tracks)} total)")
        for t in tracks:
            icon = "✓" if t["status"] == "complete" else "✗"
            line = f"  {icon} {t['artist']} - {t['title']}"
            if t.get("error"):
                line += f"  [{t['error']}]"
            print(line)


@cli.command()
def logs():
    """List all run logs."""
    if not os.path.exists(LOGS_DIR):
        print("No logs yet. Run the pipeline first.")
        return

    log_files = sorted(Path(LOGS_DIR).glob("*.json"), reverse=True)
    if not log_files:
        print("No logs yet.")
        return

    for lf in log_files:
        try:
            with open(lf) as f:
                data = json.load(f)
            c = data.get("completed", 0)
            fail = data.get("failed", 0)
            total = data.get("total", 0)
            name = data.get("set_name", "?")
            print(f"  {lf.name:45s}  {name:30s}  {c}/{total} ok, {fail} failed")
        except (json.JSONDecodeError, IOError):
            print(f"  {lf.name} (corrupt)")


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
