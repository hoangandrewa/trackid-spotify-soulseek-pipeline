# TrackID → Spotify → Soulseek Pipeline

Takes a TrackID.net CSV export, creates a Spotify preview playlist, then downloads
high-quality files via Soulseek (slskd) with format preferences and queue management.

## Prerequisites

1. **Spotify Developer App** — https://developer.spotify.com/dashboard
   - Create an app, get client ID + secret
   - Set redirect URI to `http://localhost:8888/callback`

2. **slskd** — https://github.com/slskd/slskd
   - Self-hosted Soulseek client with REST API
   - Run via Docker: `docker run -p 5030:5030 -p 5031:5031 slskd/slskd`
   - Get your API key from slskd settings

3. **Python 3.11+**

## Setup

```bash
cd trackid-pipeline
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials
```

## Usage

```bash
# Full pipeline from a TrackID CSV
python -m src.pipeline run --csv "path/to/trackid_export.csv"

# Full pipeline from a Spotify playlist
python -m src.pipeline run --playlist "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

# Soulseek download only (either input)
python -m src.pipeline download --csv "path/to/trackid_export.csv"
python -m src.pipeline download --playlist "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"

# Spotify preview playlist from CSV (skipped when input is already a playlist)
python -m src.pipeline spotify --csv "path/to/trackid_export.csv"

# Dry run — just show parsed tracklist
python -m src.pipeline list --csv "path/to/export.csv"
python -m src.pipeline list --playlist "https://open.spotify.com/playlist/..."

# Retry failed downloads from last run
python -m src.pipeline retry

# Check slskd connection
python -m src.pipeline status
```

Every command accepts either `--csv` or `--playlist` (not both). When the source is
a Spotify playlist, tracks arrive with duration and URI pre-populated — no Spotify
search step needed, and the duration data feeds directly into Soulseek matching.
