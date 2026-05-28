"""Spotify integration: search tracks, ingest playlists, create preview playlists."""

import logging
import re
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from .config import SpotifyConfig
from .models import Track
from .matcher import normalize_for_matching

logger = logging.getLogger(__name__)


def parse_playlist_id(url_or_uri: str) -> str:
    """
    Extract a Spotify playlist ID from any common format:
      - https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123
      - spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
      - 37i9dQZF1DXcBWIGoYBM5M  (bare ID)
    """
    url_or_uri = url_or_uri.strip()

    # URL format
    match = re.search(r"playlist/([a-zA-Z0-9]+)", url_or_uri)
    if match:
        return match.group(1)

    # URI format
    if url_or_uri.startswith("spotify:playlist:"):
        return url_or_uri.split(":")[-1]

    # Assume bare ID
    return url_or_uri


class SpotifyClient:
    def __init__(self, config: SpotifyConfig):
        self.config = config
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri=config.redirect_uri,
            scope="playlist-modify-public playlist-modify-private",
        ))
        self._user_id: Optional[str] = None

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = self.sp.current_user()["id"]
        return self._user_id

    # ── Playlist ingestion ────────────────────────────────────────────

    def get_playlist_tracks(self, url_or_uri: str) -> tuple[str, list[Track]]:
        """
        Fetch all tracks from an existing Spotify playlist.
        Returns (playlist_name, list[Track]) with duration pre-populated.

        Handles pagination — Spotify returns max 100 items per request.
        """
        playlist_id = parse_playlist_id(url_or_uri)
        playlist = self.sp.playlist(playlist_id)
        playlist_name = playlist.get("name", "Unknown Playlist")

        # Spotify returns the paginated tracks object under "tracks" or "items"
        # The paginated object has keys: href, items, limit, next, offset, total
        tracks_obj = playlist.get("tracks", {})
        if not isinstance(tracks_obj, dict) or "items" not in tracks_obj:
            tracks_obj = playlist.get("items", {})
        if not isinstance(tracks_obj, dict) or "items" not in tracks_obj:
            logger.error(f"Cannot find tracks in playlist response. Keys: {list(playlist.keys())}")
            return playlist_name, []

        total = tracks_obj.get("total", 0)
        items = tracks_obj.get("items", [])

        logger.info(f"Fetching playlist: {playlist_name} ({total} tracks)")

        tracks: list[Track] = []

        def process_items(items_list):
            if not isinstance(items_list, list):
                return
            for item in items_list:
                if not isinstance(item, dict):
                    continue
                # Spotify puts track data under "item" or "track" depending on API version
                sp_track = None
                for key in ("item", "track"):
                    candidate = item.get(key)
                    if isinstance(candidate, dict):
                        sp_track = candidate
                        break
                if sp_track is None:
                    continue
                if sp_track.get("uri") is None:
                    continue

                artist = ", ".join(a["name"] for a in sp_track.get("artists", []))
                title = sp_track.get("name", "")
                duration_ms = sp_track.get("duration_ms")
                album = sp_track.get("album", {}).get("name")

                track = Track(
                    artist=artist,
                    title=title,
                    duration_sec=duration_ms / 1000.0 if duration_ms else None,
                    label=album,
                    spotify_uri=sp_track.get("uri"),
                    spotify_duration_ms=duration_ms,
                )
                tracks.append(track)

        # Process first page from the initial response
        process_items(items)

        # Paginate if there are more tracks
        while tracks_obj.get("next"):
            tracks_obj = self.sp.next(tracks_obj)
            if not tracks_obj:
                break
            process_items(tracks_obj.get("items", []))

        logger.info(f"  Loaded {len(tracks)} tracks from playlist")
        return playlist_name, tracks

    def search_track(self, track: Track) -> Optional[str]:
        """
        Search Spotify for a track. Returns the Spotify URI if found.
        Also populates track.spotify_uri and track.spotify_duration_ms.
        """
        # Try precise search first
        query = f"artist:{track.artist} track:{track.title}"
        results = self.sp.search(q=query, type="track", limit=5)
        uri = self._best_match(track, results)

        if not uri:
            # Fallback: looser search
            query = f"{track.artist} {track.title}"
            results = self.sp.search(q=query, type="track", limit=10)
            uri = self._best_match(track, results)

        if not uri:
            # Last resort: just the title (helps with remixes where artist is the remixer)
            results = self.sp.search(q=track.title, type="track", limit=10)
            uri = self._best_match(track, results)

        return uri

    def _best_match(self, track: Track, results: dict) -> Optional[str]:
        """Find the best matching track from Spotify search results."""
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return None

        target_artist = normalize_for_matching(track.artist)
        target_title = normalize_for_matching(track.title)

        best_uri = None
        best_score = 0

        for item in items:
            sp_artists = " ".join(a["name"] for a in item["artists"])
            sp_title = item["name"]

            from thefuzz import fuzz
            artist_score = fuzz.token_sort_ratio(
                target_artist, normalize_for_matching(sp_artists)
            )
            title_score = fuzz.token_sort_ratio(
                target_title, normalize_for_matching(sp_title)
            )

            # Weight title match more — artist names can vary a lot in electronic music
            combined = (artist_score * 0.4) + (title_score * 0.6)

            # Duration sanity check if we have TrackID duration
            if track.duration_sec and item.get("duration_ms"):
                sp_dur = item["duration_ms"] / 1000.0
                if abs(sp_dur - track.duration_sec) > 30:
                    combined *= 0.5  # Penalize big duration mismatches

            if combined > best_score and combined > 55:
                best_score = combined
                best_uri = item["uri"]
                # Store the Spotify data on the track
                track.spotify_uri = item["uri"]
                track.spotify_duration_ms = item.get("duration_ms")

        return best_uri

    def create_playlist(self, name: str, track_uris: list[str]) -> str:
        """Create a Spotify playlist and add tracks. Returns the playlist URL."""
        # Use /me/playlists instead of /users/{id}/playlists
        playlist = self.sp._post(
            "me/playlists",
            payload={
                "name": name,
                "public": False,
                "description": "Auto-generated from TrackID.net export",
            },
        )

        # Spotify API accepts max 100 tracks per request
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i + 100]
            self.sp.playlist_add_items(playlist["id"], batch)

        url = playlist["external_urls"]["spotify"]
        logger.info(f"Created playlist: {url} ({len(track_uris)} tracks)")
        return url

    def search_and_create_playlist(
        self, tracks: list[Track], set_name: str
    ) -> tuple[str, list[Track], list[Track]]:
        """
        Search for all tracks and create a playlist.
        Returns (playlist_url, found_tracks, missing_tracks).
        """
        found = []
        missing = []
        uris = []

        for track in tracks:
            logger.info(f"Searching Spotify: {track.search_query}")
            uri = self.search_track(track)
            if uri:
                found.append(track)
                uris.append(uri)
                logger.info(f"  ✓ Found: {uri}")
            else:
                missing.append(track)
                logger.warning(f"  ✗ Not found on Spotify")

        playlist_name = self.config.playlist_name_template.format(set_name=set_name)
        url = self.create_playlist(playlist_name, uris) if uris else ""

        return url, found, missing
