"""
Tailscale API client supporting both Personal API Access Tokens and OAuth clients.

All credentials stay on the server (never exposed to clients).
Supports:
- User invite creation: POST /api/v2/tailnet/{tailnet}/user-invites
- User invite status check: GET /api/v2/tailnet/{tailnet}/users
- User invite revocation: DELETE /api/v2/user-invites/{invite_id}
- Mock / test injection via set_client()
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests

TAILSCALE_API_BASE = os.getenv("TAILSCALE_API_BASE", "https://api.tailscale.com")
TAILSCALE_API_KEY = os.getenv("TAILSCALE_API_KEY", "")
TAILSCALE_CLIENT_ID = os.getenv("TAILSCALE_CLIENT_ID", "")
TAILSCALE_CLIENT_SECRET = os.getenv("TAILSCALE_CLIENT_SECRET", "")
TAILSCALE_TAILNET = os.getenv("TAILSCALE_TAILNET", "-")  # '-' represents default tailnet in API
TAILSCALE_SLOTS_MAX = int(os.getenv("TAILSCALE_SLOTS_MAX", "6"))

_client: Optional[TailscaleClient] = None


class TailscaleClient:
    def __init__(
        self,
        api_key: str = TAILSCALE_API_KEY,
        client_id: str = TAILSCALE_CLIENT_ID,
        client_secret: str = TAILSCALE_CLIENT_SECRET,
        tailnet: str = TAILSCALE_TAILNET,
        api_base: str = TAILSCALE_API_BASE,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.tailnet = tailnet
        self.api_base = api_base.rstrip("/")
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key or (self.client_id and self.client_secret))

    def _get_access_token(self) -> str:
        """Obtain or refresh OAuth access token if using OAuth client credentials."""
        if self.api_key:
            return self.api_key

        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        url = f"{self.api_base}/api/v2/oauth/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        expires_in = body.get("expires_in", 3600)
        self._token_expires_at = now + expires_in
        return self._access_token

    def _headers(self) -> dict[str, str]:
        token = self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def create_user_invite(self, email: str, role: str = "member") -> dict[str, Any]:
        """
        Create a Tailscale user invite for email.
        Returns: { "id": "...", "inviteUrl": "https://...", "email": email }
        """
        if not self.is_configured:
            # Fallback stub for dev / mock mode when no keys are configured
            return {
                "id": f"mock-invite-{int(time.time())}",
                "inviteUrl": f"https://login.tailscale.com/admin/invite/mock-{email}",
                "email": email,
            }

        url = f"{self.api_base}/api/v2/tailnet/{self.tailnet}/user-invites"
        payload = {"email": email, "role": role}
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_users(self) -> list[dict[str, Any]]:
        """List all users in the tailnet."""
        if not self.is_configured:
            return []

        url = f"{self.api_base}/api/v2/tailnet/{self.tailnet}/users"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("users", [])

    def is_user_in_tailnet(self, email: str) -> bool:
        """Check if a user with the given email exists and is active in the tailnet."""
        if not self.is_configured:
            return False

        users = self.get_users()
        email_clean = email.lower().strip()
        for u in users:
            login_name = u.get("loginName", "").lower().strip()
            user_status = u.get("status", "").lower()
            if login_name == email_clean and user_status in ("active", "signed_in", ""):
                return True
        return False

    def delete_user_invite(self, invite_id: str) -> bool:
        """Delete / revoke a pending user invite."""
        if not self.is_configured:
            return True

        url = f"{self.api_base}/api/v2/user-invites/{invite_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=10)
        return resp.status_code in (200, 204, 404)


def get_tailscale_client() -> TailscaleClient:
    global _client
    if _client is None:
        _client = TailscaleClient()
    return _client


def set_tailscale_client(client: Optional[TailscaleClient]) -> None:
    """Inject client for unit and integration testing."""
    global _client
    _client = client
