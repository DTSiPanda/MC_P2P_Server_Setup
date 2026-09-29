"""
Phase 5 client acceptance tests.

Acceptance criteria (plan.md Phase 5):
1. A new friend can go from installer to connected using only an app code, an email, and one sign-in.
2. Token saving in credential manager / fallback.
3. Polling join status until accepted.
4. AdminClient operations (create-invite, list-players, revoke).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import responses

from client.admin import AdminClient
from client.api_client import APIError
from client.auth import (
    clear_api_token,
    get_api_token,
    join_network,
    poll_join_status,
    save_api_token,
)


@pytest.fixture(autouse=True)
def clean_token(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYER_TOKEN", raising=False)
    # Redirect fallback file to tmp_path
    monkeypatch.setattr("client.auth.FALLBACK_TOKEN_DIR", tmp_path)
    monkeypatch.setattr("client.auth.FALLBACK_TOKEN_FILE", tmp_path / "token")
    clear_api_token()
    yield
    clear_api_token()


def test_token_save_and_retrieve():
    save_api_token("my-secret-test-token")
    assert get_api_token() == "my-secret-test-token"
    clear_api_token()
    assert get_api_token() is None


@responses.activate
def test_join_network_saves_token():
    responses.add(
        responses.POST,
        "http://fake-api/join",
        json={
            "api_token": "newly-issued-token-xyz",
            "player_id": "player-99",
            "tailscale_invite_url": "https://login.tailscale.com/invite/123",
        },
        status=200,
    )

    res = join_network(
        api_url="http://fake-api",
        invite_code="MC-1234",
        email="friend@example.com",
        display_name="Friend",
    )

    assert res["api_token"] == "newly-issued-token-xyz"
    assert res["tailscale_invite_url"] == "https://login.tailscale.com/invite/123"

    # Token must have been automatically saved locally
    assert get_api_token() == "newly-issued-token-xyz"


@responses.activate
def test_poll_join_status_succeeds_when_accepted():
    responses.add(
        responses.GET,
        "http://fake-api/join/status",
        json={"accepted": False, "email": "friend@example.com", "status": "invited"},
        status=200,
    )
    responses.add(
        responses.GET,
        "http://fake-api/join/status",
        json={"accepted": True, "email": "friend@example.com", "status": "active"},
        status=200,
    )

    logs = []
    accepted = poll_join_status(
        api_url="http://fake-api",
        token="test-token",
        timeout=10,
        poll_interval=0.01,
        log=logs.append,
    )

    assert accepted is True
    assert any("accepted" in m.lower() for m in logs)


@responses.activate
def test_admin_client_operations():
    admin = AdminClient("http://fake-api", "secret-admin", "admin-token")

    # 1. create_invite_code
    responses.add(
        responses.POST,
        "http://fake-api/admin/invite-code",
        json={"code": "MC-AAAA", "expires_at": 9999.0, "uses_left": 1},
        status=200,
    )
    inv = admin.create_invite_code(ttl_hours=12, uses=1)
    assert inv["code"] == "MC-AAAA"

    # 2. list_players
    responses.add(
        responses.GET,
        "http://fake-api/admin/players",
        json={
            "players": [{"id": "p1", "name": "Player 1", "email": "p1@test.com", "created_at": 1000.0, "revoked": False}],
            "slots_used": 1,
            "slots_max": 6,
        },
        status=200,
    )
    plist = admin.list_players()
    assert plist["slots_used"] == 1
    assert len(plist["players"]) == 1

    # 3. revoke_player
    responses.add(
        responses.POST,
        "http://fake-api/admin/revoke",
        json={"revoked": True, "player_id": "p1"},
        status=200,
    )
    rev = admin.revoke_player("p1")
    assert rev["revoked"] is True
