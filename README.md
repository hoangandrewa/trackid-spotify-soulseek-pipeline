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
  -e SLSKD_DIRECTORIES__SHARED=/music \
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
cd trackid-pipeline
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
3. **Soulseek search** — multiple query strategies per track (artist+title, title only, artist only) to maximize results
4. **Rank candidates** — scores by format preference, audio quality (bit depth, sample rate, bitrate), name match, duration match, and queue availability
5. **Download** — queues top candidate, monitors progress, cancels and retries from next candidate if stalled, rejected, or too slow
6. **Flatten** — moves all downloaded files from subfolders into a single output directory

## Configuration

See `config.example.yaml` for all options. Key settings:

| Setting | Default | Description |
|---|---|---|
| `format_priority` | flac, aiff, wav, mp3 | Preferred formats in order |
| `min_mp3_bitrate` | 320 | Reject MP3s below this kbps |
| `duration_tolerance_sec` | 5 | How close duration must match (seconds) |
| `max_queue_position` | 50 | Cancel if queued behind this many users |
| `start_timeout_min` | 5 | Cancel if download doesn't start in time |
| `min_transfer_speed_kbps` | 20 | Cancel if speed drops below this |
| `max_concurrent` | 3 | Simultaneous downloads |
| `max_retries` | 3 | Different users to try before giving up |

## Troubleshooting

**"Completed, Rejected" on most downloads**
Users are blocking you because you're not sharing files. Make sure your Docker
command mounts a music folder with `-v "/path/to/music:/music:ro"` and sets
`SLSKD_DIRECTORIES__SHARED=/music`.

**"429 Too Many Requests"**
slskd rate-limits concurrent searches. The pipeline handles this with automatic
retry and backoff. If you see many 429s, the delays between searches may need
increasing.

**"No viable candidates" for a track**
The fuzzy matcher couldn't find a close enough match. Try searching manually in
the slskd web UI — the track may be listed under a different name or artist.

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
