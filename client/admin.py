"""
admin.py – Admin panel CLI and client operations.

Admin authentication rules (plan.md Section 6B):
- ADMIN_SECRET is stored in Windows Credential Manager or passed via flag.
- Sends x-admin-secret together with x-api-token.
- Constant-time verification on server.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

import requests

from client.api_client import APIError
from client.auth import get_api_token

ADMIN_SERVICE_NAME = "MinecraftP2P_Admin"
ADMIN_USERNAME = "admin_secret"


def save_admin_secret(secret: str) -> None:
    try:
        import keyring
        keyring.set_password(ADMIN_SERVICE_NAME, ADMIN_USERNAME, secret)
    except Exception:
        pass


def get_admin_secret() -> Optional[str]:
    env_secret = os.getenv("ADMIN_SECRET")
    if env_secret:
        return env_secret.strip()
    try:
        import keyring
        secret = keyring.get_password(ADMIN_SERVICE_NAME, ADMIN_USERNAME)
        if secret:
            return secret.strip()
    except Exception:
        pass
    return None


class AdminClient:
    def __init__(self, api_url: str, admin_secret: str, api_token: str):
        self.api_url = api_url.rstrip("/")
        self.admin_secret = admin_secret
        self.api_token = api_token

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-token": self.api_token,
            "x-admin-secret": self.admin_secret,
        }

    def create_invite_code(self, ttl_hours: int = 48, uses: int = 1) -> dict[str, Any]:
        """Create a new single-use app invite code."""
        url = f"{self.api_url}/admin/invite-code"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"ttl_hours": ttl_hours, "uses": uses},
            timeout=10,
        )
        if resp.status_code != 200:
            raise APIError(resp.status_code, resp.text)
        return resp.json()

    def list_players(self) -> dict[str, Any]:
        """List all players and Tailscale slot usage."""
        url = f"{self.api_url}/admin/players"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        if resp.status_code != 200:
            raise APIError(resp.status_code, resp.text)
        return resp.json()

    def revoke_player(self, player_id: str) -> dict[str, Any]:
        """Revoke a player's access and delete pending Tailscale invite."""
        url = f"{self.api_url}/admin/revoke"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"player_id": player_id},
            timeout=10,
        )
        if resp.status_code != 200:
            raise APIError(resp.status_code, resp.text)
        return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Minecraft P2P Admin CLI")
    parser.add_argument("--api-url", default=os.getenv("API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-secret", default=get_admin_secret())
    parser.add_argument("--token", default=get_api_token())

    sub = parser.add_subparsers(dest="cmd", required=True)

    # invite-code
    p_inv = sub.add_parser("create-invite", help="Generate a new app invite code")
    p_inv.add_argument("--ttl-hours", type=int, default=48)
    p_inv.add_argument("--uses", type=int, default=1)

    # list-players
    sub.add_parser("list-players", help="List players and slot usage")

    # revoke
    p_rev = sub.add_parser("revoke", help="Revoke player access")
    p_rev.add_argument("--player-id", required=True)

    args = parser.parse_args()

    if not args.admin_secret:
        print("ERROR: Admin secret required. Pass --admin-secret or set ADMIN_SECRET.", file=sys.stderr)
        sys.exit(1)
    if not args.token:
        print("ERROR: Player API token required. Pass --token or set PLAYER_TOKEN.", file=sys.stderr)
        sys.exit(1)

    client = AdminClient(args.api_url, args.admin_secret, args.token)

    try:
        if args.cmd == "create-invite":
            res = client.create_invite_code(args.ttl_hours, args.uses)
            print(f"Created Invite Code: {res['code']}")
            print(f"Uses Left: {res['uses_left']}")
            print(f"Expires at epoch: {res['expires_at']}")
        elif args.cmd == "list-players":
            res = client.list_players()
            print(f"Slots: {res['slots_used']} / {res['slots_max']}")
            print("Players:")
            for p in res.get("players", []):
                rev_mark = "[REVOKED]" if p.get("revoked") else "[ACTIVE]"
                print(f"  - {p['name']} ({p.get('email', 'no email')}) ID={p['id']} {rev_mark}")
        elif args.cmd == "revoke":
            res = client.revoke_player(args.player_id)
            print(f"Player {args.player_id} revoked successfully.")
    except APIError as exc:
        print(f"Admin Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
