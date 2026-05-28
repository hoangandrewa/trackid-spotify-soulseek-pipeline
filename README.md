# TrackID -> Spotify -> Soulseek Pipeline

Takes a TrackID.net CSV export or Spotify playlist, creates a Spotify preview
playlist, then downloads high-quality files via Soulseek (slskd) with format
preferences, quality ranking, and queue management.

## Prerequisites

- **Docker Desktop** — [download here](https://docker.com/products/docker-desktop)
- **Spotify account** + developer app (free)
- **Python 3.11+**

## Setup

### 1. Start Docker

Open Docker Desktop and wait for the whale icon to appear in your menu bar.
Docker must be running before any `docker` commands will work.

### 2. Run slskd

slskd is a self-hosted Soulseek client with a web UI and REST API.

```bash
docker run -d \
  --name slskd \
  -p 5030:5030 \
  -p 5031:5031 \
  -v ~/Music/slskd:/app/data \
  -v "$HOME/Music/TrackID Downloads:/app/downloads" \
  -v "/path/to/your/music:/music:ro" \
  -e SLSKD_REMOTE_CONFIGURATION=true \
  -e SLSKD_SLSK_USERNAME=your_soulseek_username \
  -e SLSKD_SLSK_PASSWORD=your_soulseek_password \
  -e "SLSKD_SHARED_DIR=/music" \
  slskd/slskd
```

**Important:**
- Replace `/path/to/your/music` with your music collection. If the path has
  spaces, wrap it in quotes: `"/Users/you/My Music:/music:ro"`
- Soulseek accounts are created automatically on first connect — just pick a
  unique username. If you get `INVALIDPASS`, the username is taken; try another.
- **You must share files.** Soulseek users will block downloads from people who
  don't share anything. Point the shared directory at your existing music.
- Downloads land in `~/Music/TrackID Downloads/`

Open `http://localhost:5030` to verify slskd is running. Default login is
`slskd` / `slskd`. You should see "Connected" in the top right once it
connects to the Soulseek network.

### 3. Create a Spotify Developer App

1. Go to https://developer.spotify.com/dashboard and create an app
2. Check **Web API** under "Which API/SDKs are you planning to use?"
3. Set the redirect URI to `http://127.0.0.1:8888/callback` (not `localhost` —
   Spotify may reject it as insecure)
4. After creating, go to **Settings → User Management** and add the email
   address you use to log into Spotify. Without this, playlist creation will
   fail with a 403 error.
5. Copy the **Client ID** and **Client Secret** from the app dashboard

### 4. Install the pipeline

```bash
git clone https://github.com/hoangandrewa/trackid-spotify-soulseek-pipeline.git
cd trackid-spotify-soulseek-pipeline
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your Spotify credentials and slskd login:

```yaml
spotify:
  client_id: "your_client_id"
  client_secret: "your_client_secret"
  redirect_uri: "http://127.0.0.1:8888/callback"

slskd:
  base_url: "http://localhost:5030"
  username: "slskd"
  password: "slskd"
```

### 5. Verify the connection

```bash
python -m src.pipeline status
```

You should see `slskd connected` and `Active download groups: 0`. If this
fails, check that Docker is running and slskd shows "Connected" in its web UI.

## Usage

**First run — the Spotify OAuth flow:**

The first time you run any command that touches Spotify, it will open your
browser for authorization. Log in, authorize the app, and you'll be redirected
to a URL that looks like `http://127.0.0.1:8888/callback?code=...`. The token
is cached after this, so you only do it once.

```bash
# Dry run — parse and display a tracklist without downloading
python -m src.pipeline list --playlist "https://open.spotify.com/playlist/..."
python -m src.pipeline list --csv "path/to/trackid_export.csv"

# Full pipeline: Spotify playlist + Soulseek download
python -m src.pipeline run --playlist "https://open.spotify.com/playlist/..."
python -m src.pipeline run --csv "path/to/trackid_export.csv"

# Soulseek download only (skip Spotify playlist creation)
python -m src.pipeline download --playlist "https://open.spotify.com/playlist/..."
python -m src.pipeline download --csv "path/to/export.csv"

# Create Spotify preview playlist from CSV (no download)
python -m src.pipeline spotify --csv "path/to/export.csv"

# Retry failed downloads from last run
python -m src.pipeline retry

# Check slskd connection
python -m src.pipeline status
```

Every command accepts either `--csv` or `--playlist`. When the source is a
Spotify playlist, tracks arrive with duration pre-populated, which improves
Soulseek matching accuracy.

## How it works

1. **Parse input** — reads a TrackID CSV or fetches tracks from a Spotify playlist
2. **Spotify search** (CSV only) — fuzzy-matches tracks and creates a preview playlist
3. **Soulseek search** — multiple query strategies per track (artist+title, title only, artist only), 2 concurrent searches, 15s timeout per query
4. **Stage A — Identity match** — filters results to only correct tracks (see below)
5. **Stage B — Quality ranking** — among confirmed matches, picks the best copy
6. **Download** — queues top candidate, monitors progress, cancels and retries from next candidate if stalled, rejected, or too slow
7. **Flatten** — moves all downloaded files from subfolders into a single output directory

## Matching architecture

The matcher uses a two-stage pipeline to prevent wrong tracks from ever being
downloaded, regardless of how high-quality they are.

### Stage A — Identity ("Is this the right track?")

Every search result must pass all of these checks:

- **Audio file filter** — only flac, aiff, wav, mp3, ogg, m4a, aac
- **Negative keyword rejection** — instant reject for "sample pack", "tutorial",
  "dj set", "stems", "acapella", etc. Soft penalty for "vinyl rip", "bootleg",
  "radio edit", "live version"
- **Title coverage** — at least 50% of the track title's key words must appear in
  the filename or path
- **Artist coverage** — at least 30% of the artist's name must appear in the
  filename or path (lenient for compilations)
- **Duration gate** — ±1 sec: high confidence, ±3 sec: strong, ±5 sec: moderate,
  >5 sec: **rejected**. Unknown duration gets a neutral score, not a pass.

Identity confidence is weighted: title (40%) + artist (25%) + duration (35%).
Results below 0.35 confidence are rejected.

### Stage B — Quality ("Which copy is best?")

Only runs on results that passed Stage A. Scores by:

- **Identity confidence** (0-20 pts) — higher confidence matches rank higher
- **Format preference** (0-30 pts) — based on your `format_priority` config
- **Audio quality** (0-25 pts) — bit depth, sample rate, bitrate, with file size
  as a proxy when metadata is unavailable
- **Availability** (0-10 pts) — free upload slots, queue position

A correct 320kbps MP3 will always beat a wrong 24-bit FLAC because wrong tracks
are eliminated before quality scoring begins.

## Configuration

See `config.example.yaml` for all options. Key settings:

| Setting | Default | Description |
|---|---|---|
| `format_priority` | flac, aiff, wav, mp3 | Preferred formats in order |
| `min_mp3_bitrate` | 320 | Reject MP3s below this kbps |
| `duration_tolerance_sec` | 5 | Max duration difference before rejecting (seconds) |
| `max_queue_position` | 50 | Cancel if queued behind this many users |
| `start_timeout_min` | 5 | Cancel if download doesn't start in time |
| `min_transfer_speed_kbps` | 20 | Cancel if speed drops below this |
| `max_concurrent` | 3 | Simultaneous downloads |
| `max_retries` | 3 | Different users to try before giving up |

## Troubleshooting

**"Completed, Rejected" on most downloads**
Users are blocking you because you're not sharing files. Make sure your Docker
command mounts a music folder with `-v "/path/to/music:/music:ro"` and sets
`SLSKD_SHARED_DIR=/music`.

**"429 Too Many Requests"**
slskd limits concurrent searches to 2. The pipeline handles this with automatic
retry and backoff.

**"No viable candidates" for a track**
The identity matcher couldn't confirm any results as the correct track. Common
causes: artist or title is spelled differently on Soulseek, the track is very
new/obscure, or all results failed the duration check. Try searching manually
in the slskd web UI.

**Spotify 403 Forbidden when creating playlists**
Go to your Spotify developer app → Settings → User Management and add the email
address associated with your Spotify account. Apps in development mode only work
for explicitly listed users.

**Downloads complete but files are empty/missing**
Check `~/Music/TrackID Downloads/`. The pipeline auto-flattens files from
subfolders after each run. If files are missing, check `docker logs slskd` for
errors.

**"Failed to reconnect: INVALIDPASS"**
The Soulseek username you chose is already taken. Recreate the container with a
different username.

**Check what downloaded and what failed**
```bash
cat pipeline_state.json | python -m json.tool
```

## Docker reference

```bash
docker ps              # is slskd running?
docker stop slskd      # stop
docker start slskd     # start again
docker logs slskd      # check connection/errors
docker rm -f slskd     # remove (recreate with docker run)
```
