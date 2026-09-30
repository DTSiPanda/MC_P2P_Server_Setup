"""
api_client.py – HTTP wrapper around the P2P API with retry, backoff, and version validation.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

REQUEST_TIMEOUT = 15          # seconds per attempt
MAX_RETRIES = 6
RETRY_BACKOFF_START = 2.0     # doubles each attempt
COLD_START_CODES = {502, 503, 504}


class APIError(Exception):
    """Raised when the server returns an application-level error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


def parse_version_tuple(v: str) -> tuple[int, ...]:
    clean = v.split("-")[0].split("+")[0]
    return tuple(int(x) for x in clean.split(".") if x.isdigit())


def check_compatibility(
    client: APIClient,
    current_client_version: str = "0.1.0",
    local_mc_version: Optional[str] = None,
) -> dict[str, Any]:
    """
    Checks /config and validates client and Minecraft version compatibility.
    Raises RuntimeError if version is incompatible.
    """
    cfg = client.get_config()
    min_client_v = cfg.get("min_client_version", "0.1.0")
    required_mc_v = cfg.get("required_mc_version", "1.20.1")

    if parse_version_tuple(current_client_version) < parse_version_tuple(min_client_v):
        raise RuntimeError(
            f"Client version {current_client_version} is outdated. "
            f"Minimum required version is {min_client_v}. Please update the app."
        )

    if local_mc_version and local_mc_version.strip() != required_mc_v.strip():
        raise RuntimeError(
            f"Minecraft version mismatch: Installed {local_mc_version}, required {required_mc_v}."
        )

    return cfg


class APIClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"x-api-token": token})

    # ------------------------------------------------------------------
    # Core transport
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        delay = RETRY_BACKOFF_START
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
                if r.status_code in COLD_START_CODES and attempt < MAX_RETRIES:
                    print(f"  [api] Server waking up, retrying in {delay:.0f}s…")
                    time.sleep(delay)
                    delay *= 2
                    continue
                return r
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    print(
                        f"  [api] Connection failed, retrying in {delay:.0f}s "
                        f"(attempt {attempt}/{MAX_RETRIES})…"
                    )
                    time.sleep(delay)
                    delay *= 2

        raise last_exc or RuntimeError("Max retries exceeded")

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    def acquire_lock(self) -> dict:
        r = self._request("POST", "/lock/acquire")
        if r.status_code == 409:
            detail = r.json().get("detail", {})
            raise APIError(409, f"Lock held by {detail.get('holder_name', 'someone')}")
        r.raise_for_status()
        return r.json()

    def heartbeat(self, owner_token: str) -> dict:
        r = self._request("POST", "/lock/heartbeat", json={"owner_token": owner_token})
        if r.status_code == 410:
            raise APIError(410, "Lock lost")
        r.raise_for_status()
        return r.json()

    def release_lock(self, owner_token: str) -> dict:
        r = self._request("POST", "/lock/release", json={"owner_token": owner_token})
        r.raise_for_status()
        return r.json()

    def set_host_address(self, owner_token: str, tailscale_ip: str, port: int) -> dict:
        r = self._request(
            "POST", "/host/address",
            json={"owner_token": owner_token, "tailscale_ip": tailscale_ip, "port": port},
        )
        r.raise_for_status()
        return r.json()

    def get_host(self) -> dict:
        r = self._request("GET", "/host")
        r.raise_for_status()
        return r.json()

    def get_config(self) -> dict:
        r = self._request("GET", "/config")
        r.raise_for_status()
        return r.json()

    def get_upload_url(self, owner_token: str, size: int, sha256: str) -> dict:
        r = self._request(
            "POST", "/world/upload-url",
            json={"owner_token": owner_token, "size": size, "sha256": sha256},
        )
        r.raise_for_status()
        return r.json()

    def commit_world(self, owner_token: str, version_key: str, sha256: str) -> dict:
        r = self._request(
            "POST", "/world/commit",
            json={"owner_token": owner_token, "version_key": version_key, "sha256": sha256},
        )
        r.raise_for_status()
        return r.json()

    def get_tailnet_info(self) -> dict:
        """Fetch the expected tailnet name/domain from the server."""
        try:
            r = self._request("GET", "/tailnet-info")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {"tailnet_name": ""}
