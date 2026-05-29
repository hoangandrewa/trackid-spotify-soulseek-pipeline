"""slskd REST API client for Soulseek search and download management."""

import logging
import time
from typing import Optional

import requests

from .config import SlskdConfig
from .models import SoulseekResult, FileFormat

logger = logging.getLogger(__name__)


class SlskdClient:
    """
    Client for the slskd REST API.
    API docs: https://github.com/slskd/slskd/blob/master/docs/api.md

    Core endpoints used:
      POST   /api/v0/searches            — start a search
      GET    /api/v0/searches/{id}        — get search results
      DELETE /api/v0/searches/{id}        — cancel/delete a search
      POST   /api/v0/transfers/downloads/{username}  — queue a download
      GET    /api/v0/transfers/downloads/{username}   — get download status
      DELETE /api/v0/transfers/downloads/{username}/{id} — cancel a download
    """

    def __init__(self, config: SlskdConfig):
        self.base_url = config.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
        })

        # Authenticate: try API key first, fall back to username/password login
        if config.api_key:
            self.session.headers["X-API-Key"] = config.api_key
        elif config.username and config.password:
            self._login(config.username, config.password)
        else:
            logger.warning("No slskd credentials configured — API calls may fail")

    def _login(self, username: str, password: str):
        """Authenticate with slskd using username/password to get a session token."""
        resp = self.session.post(
            f"{self.base_url}/api/v0/session",
            json={"username": username, "password": password},
        )
        resp.raise_for_status()
        token = resp.json().get("token")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            logger.info("Authenticated with slskd via session token")
        else:
            logger.warning("Login succeeded but no token returned")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v0{path}"

    def _request(self, method: str, path: str, max_retries: int = 3, **kwargs):
        """Make an HTTP request with retry logic for rate limits."""
        for attempt in range(max_retries + 1):
            resp = self.session.request(method, self._url(path), **kwargs)
            if resp.status_code == 429:
                wait = min(5 * (attempt + 1), 30)
                logger.warning(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()  # Raise the last 429

    def _get(self, path: str, **kwargs) -> dict:
        return self._request("GET", path, **kwargs).json()

    def _post(self, path: str, json: dict = None, **kwargs) -> dict:
        return self._request("POST", path, json=json, **kwargs).json()

    def _delete(self, path: str, **kwargs) -> None:
        self._request("DELETE", path, **kwargs)

    # ── Search ────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        timeout_sec: float = 15.0,
        poll_interval: float = 2.0,
    ) -> list[SoulseekResult]:
        """
        Run a Soulseek search and wait for results.

        slskd searches are async — we start one, then poll the status
        endpoint until complete, then fetch results from /responses.
        Always fetches partial results even if the search times out.
        """
        logger.info(f"Searching Soulseek: '{query}'")
        search = self._post("/searches", json={"searchText": query})
        search_id = search["id"]

        # slskd completes searches at ~18s. /responses is empty until then.
        # Just wait for completion.
        deadline = time.time() + 25  # hard cap slightly above slskd's ~18s timeout
        completed = False

        while time.time() < deadline:
            time.sleep(poll_interval)
            state = self._get(f"/searches/{search_id}")
            state_str = state.get("state", "")

            if state.get("isComplete", False) or "Completed" in state_str:
                completed = True
                break

        # Always fetch results — even on timeout, partial results may exist
        try:
            responses = self._get(f"/searches/{search_id}/responses")
        except Exception as e:
            logger.error(f"  Failed to fetch responses for '{query}': {e}")
            responses = []

        if isinstance(responses, list):
            logger.info(f"  Raw responses: {len(responses)} entries")
        else:
            logger.info(f"  Raw responses type: {type(responses)}")

        results = self._parse_search_results(responses, search_id)

        status = "complete" if completed else "partial/timeout"
        logger.info(f"  Found {len(results)} results for '{query}' ({status})")

        if not results and isinstance(responses, list) and len(responses) > 0:
            # We got responses but parsing produced 0 results — log why
            logger.warning(f"  Responses exist ({len(responses)}) but 0 parsed — check parse logic")
            sample = responses[0] if responses else {}
            logger.warning(f"  Sample response keys: {list(sample.keys()) if isinstance(sample, dict) else type(sample)}")

        return results

    def _parse_search_results(self, responses: list, search_id: str) -> list[SoulseekResult]:
        """Parse slskd search responses into SoulseekResult objects."""
        results = []

        if not isinstance(responses, list):
            responses = responses.get("responses", [])

        for response in responses:
            username = response.get("username", "")
            free_slots = response.get("hasFreeUploadSlot", False)
            queue_length = response.get("queueLength", 0)

            for file_info in response.get("files", []):
                filename = file_info.get("filename", "")
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

                bitrate = file_info.get("bitRate") or file_info.get("bitrate")
                duration = file_info.get("length")  # in seconds
                size = file_info.get("size", 0)
                bit_depth = file_info.get("bitDepth")
                sample_rate = file_info.get("sampleRate")

                result = SoulseekResult(
                    username=username,
                    filename=filename.rsplit("\\", 1)[-1] if "\\" in filename else filename.rsplit("/", 1)[-1],
                    file_path=filename,
                    size_bytes=size,
                    bitrate=bitrate,
                    duration_sec=float(duration) if duration else None,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    file_format=FileFormat.from_extension(ext),
                    search_id=search_id,
                    free_upload_slots=free_slots,
                    queue_length=queue_length,
                )
                results.append(result)

        return results

    # ── Downloads ─────────────────────────────────────────────────────

    def queue_download(self, result: SoulseekResult) -> str:
        """
        Queue a file for download from a specific user.
        Returns a transfer ID for tracking.
        """
        logger.info(f"Queueing download: {result.filename} from {result.username}")

        payload = [
            {
                "filename": result.file_path,
                "size": result.size_bytes,
            }
        ]

        resp = self._post(f"/transfers/downloads/{result.username}", json=payload)

        # Response is {"enqueued": [...]}
        enqueued = resp.get("enqueued", [])
        transfer_id = enqueued[0].get("id") if enqueued else result.file_path
        return transfer_id

    def get_download_status(self, username: str) -> list[dict]:
        """Get all download transfer statuses for a user."""
        try:
            return self._get(f"/transfers/downloads/{username}")
        except requests.HTTPError:
            return []

    def get_transfer_by_filename(self, username: str, file_path: str) -> Optional[dict]:
        """Find a specific transfer by username and file path."""
        # Try user-specific endpoint first
        try:
            user_data = self._get(f"/transfers/downloads/{username}")
        except requests.HTTPError:
            user_data = None

        # If that didn't work, search all downloads
        if not user_data:
            try:
                all_downloads = self._get("/transfers/downloads")
                for entry in all_downloads:
                    if entry.get("username") == username:
                        user_data = entry
                        break
            except requests.HTTPError:
                return None

        if not user_data:
            return None

        # Navigate: directories > files structure
        directories = []
        if isinstance(user_data, dict):
            directories = user_data.get("directories", [])
        elif isinstance(user_data, list):
            directories = user_data

        for directory in directories:
            if not isinstance(directory, dict):
                continue
            for f in directory.get("files", []):
                if f.get("filename") == file_path:
                    return f

        return None

    def cancel_download(self, username: str, transfer_id: str, file_path: str) -> None:
        """Cancel a specific download by its transfer ID."""
        logger.warning(f"Cancelling download: {file_path} from {username}")
        try:
            self._delete(f"/transfers/downloads/{username}/{transfer_id}")
        except requests.HTTPError as e:
            logger.error(f"Failed to cancel download: {e}")

    def get_all_downloads(self) -> list[dict]:
        """Get all active downloads across all users."""
        try:
            return self._get("/transfers/downloads")
        except requests.HTTPError:
            return []
