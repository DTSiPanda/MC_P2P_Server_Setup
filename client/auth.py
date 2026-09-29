"""
auth.py – Authentication and Onboarding Wizard helpers.

Handles:
- Storing/retrieving API token in Windows Credential Manager (via keyring)
  with fallback to ~/.minecraft_p2p/token.
- POST /join to register an invite code and request a Tailscale invite.
- Polling /join/status until the Tailscale invite is accepted.
- Polling local Tailscale status (`tailscale status`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from client.api_client import APIClient, APIError

KEYRING_SERVICE_NAME = "MinecraftP2P"
KEYRING_USERNAME = "api_token"
FALLBACK_TOKEN_DIR = Path.home() / ".minecraft_p2p"
FALLBACK_TOKEN_FILE = FALLBACK_TOKEN_DIR / "token"


def save_api_token(token: str) -> None:
    """Save the API token in Windows Credential Manager or fallback file."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME, token)
        return
    except Exception:
        pass

    FALLBACK_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    FALLBACK_TOKEN_FILE.write_text(token.strip(), encoding="utf-8")


def get_api_token() -> Optional[str]:
    """Retrieve the API token from Windows Credential Manager or fallback file."""
    # 1. Environment variable override
    env_token = os.getenv("PLAYER_TOKEN")
    if env_token:
        return env_token.strip()

    # 2. Windows Credential Manager (keyring)
    try:
        import keyring
        token = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
        if token:
            return token.strip()
    except Exception:
        pass

    # 3. Fallback file
    if FALLBACK_TOKEN_FILE.exists():
        try:
            return FALLBACK_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return None


def clear_api_token() -> None:
    """Remove stored API token."""
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
    except Exception:
        pass

    if FALLBACK_TOKEN_FILE.exists():
        try:
            FALLBACK_TOKEN_FILE.unlink()
        except Exception:
            pass


def join_network(
    api_url: str,
    invite_code: str,
    email: str,
    display_name: str,
) -> dict[str, Any]:
    """
    Call POST /join, save the returned API token, and return response dict.
    Raises APIError or requests.HTTPError on failure.
    """
    url = f"{api_url.rstrip('/')}/join"
    payload = {
        "invite_code": invite_code.strip(),
        "email": email.strip(),
        "display_name": display_name.strip(),
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
        raise APIError(resp.status_code, str(detail))

    data = resp.json()
    token = data["api_token"]
    save_api_token(token)
    return data


def poll_join_status(
    api_url: str,
    token: str,
    timeout: float = 300,
    poll_interval: float = 5,
    log=print,
) -> bool:
    """
    Poll GET /join/status until accepted == True or timeout.
    Returns True if accepted, False if timed out.
    """
    client = APIClient(api_url, token)
    deadline = time.time() + timeout
    log("[onboarding] Waiting for Tailscale invite acceptance...")

    while time.time() < deadline:
        try:
            res = client._request("GET", "/join/status")
            if res.status_code == 200:
                data = res.json()
                if data.get("accepted"):
                    log("[onboarding] Tailscale invite accepted! Device is now authorized.")
                    return True
        except Exception as exc:
            log(f"  [onboarding] Poll status error: {exc}")

        time.sleep(poll_interval)

    log("[onboarding] Timed out waiting for Tailscale invite acceptance.")
    return False


def is_tailscale_logged_in() -> bool:
    """
    Check if this PC is connected to Tailscale.
    """
    from client.lan_sniffer import find_tailscale_cli, get_tailscale_ip
    if get_tailscale_ip():
        return True
    try:
        cli = find_tailscale_cli() or "tailscale"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.run(
            [cli, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        if proc.returncode == 0 and "BackendState" in proc.stdout:
            # BackendState == "Running" means active and authenticated
            return '"BackendState":"Running"' in proc.stdout or '"BackendState": "Running"' in proc.stdout
    except Exception:
        pass
    return False


def launch_tailscale_login() -> bool:
    """Run `tailscale login` to prompt browser sign-in."""
    from client.lan_sniffer import find_tailscale_cli
    try:
        cli = find_tailscale_cli() or "tailscale"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen([cli, "login"], creationflags=flags)
        return True
    except Exception:
        return False

