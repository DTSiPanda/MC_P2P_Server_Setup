"""
Phase 5 server acceptance tests.

Acceptance criteria (plan.md Phase 5):
1. A new friend joins using app invite code + email -> gets API token + Tailscale invite.
2. A used or expired app code fails.
3. A revoked player is rejected with 403 Forbidden.
4. Admin endpoints require valid ADMIN_SECRET (tested constant-time, bad secret rejected).
5. Revoking a player holding the lock releases the lock.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import server.r2 as r2
import server.tailscale as tailscale_mod
from server.main import (
    _PHASE1_TOKEN,
    _hash,
    _invites,
    _lock,
    _host,
    _players_registry,
    _token_hash_to_player_id,
    _PLAYERS,
    app,
)

BUCKET = "minecraft-p2p"
ADMIN_SECRET = "test-admin-secret"
ADMIN_HEADERS = {
    "x-api-token": _PHASE1_TOKEN,
    "x-admin-secret": ADMIN_SECRET,
}


@pytest.fixture(autouse=True)
def setup_env_and_state(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("PLAYER_TOKEN", _PHASE1_TOKEN)

    # Reset state
    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0
    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None

    _invites.clear()
    _players_registry.clear()
    _token_hash_to_player_id.clear()
    _PLAYERS.clear()

    # Re-register default admin player
    from server.main import PlayerRecord
    admin_p = PlayerRecord(
        id="admin-id",
        name="player1",
        email="admin@example.com",
        token_hash=_hash(_PHASE1_TOKEN),
        created_at=time.time(),
        revoked=False,
    )
    _players_registry[admin_p.id] = admin_p
    _token_hash_to_player_id[admin_p.token_hash] = admin_p.id
    _PLAYERS[_PHASE1_TOKEN] = admin_p.name

    # Mock Tailscale client
    mock_ts = MagicMock()
    mock_ts.is_configured = True
    mock_ts.create_user_invite.return_value = {
        "id": "invite-123",
        "inviteUrl": "https://login.tailscale.com/invite-test",
        "email": "friend@example.com",
    }
    mock_ts.is_user_in_tailnet.return_value = False
    mock_ts.delete_user_invite.return_value = True
    tailscale_mod.set_tailscale_client(mock_ts)

    yield

    tailscale_mod.set_tailscale_client(None)


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Admin: Create invite code
# ---------------------------------------------------------------------------


def test_admin_create_invite_code(client):
    r = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "code" in data
    assert data["code"].startswith("MC-")
    assert data["uses_left"] == 1
    assert data["expires_at"] > time.time()


def test_admin_endpoints_reject_wrong_secret(client):
    bad_headers = {
        "x-api-token": _PHASE1_TOKEN,
        "x-admin-secret": "wrong-secret",
    }
    r = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=bad_headers)
    assert r.status_code == 403
    assert "Invalid admin secret" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. Join flow
# ---------------------------------------------------------------------------


def test_join_with_valid_invite_code(client):
    # 1. Admin generates invite code
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]

    # 2. Friend joins
    join_payload = {
        "invite_code": code,
        "email": "friend@example.com",
        "display_name": "FriendPlayer",
    }
    r_join = client.post("/join", json=join_payload)
    assert r_join.status_code == 200
    join_data = r_join.json()
    assert "api_token" in join_data
    assert "player_id" in join_data
    assert join_data["tailscale_invite_url"] == "https://login.tailscale.com/invite-test"

    # 3. Friend can use their new API token immediately
    friend_token = join_data["api_token"]
    r_host = client.get("/host", headers={"x-api-token": friend_token})
    assert r_host.status_code == 200
    assert r_host.json()["hosting"] is False


def test_used_invite_code_fails(client):
    # Generate single-use code
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]

    # First join succeeds
    r1 = client.post("/join", json={"invite_code": code, "email": "p1@test.com", "display_name": "P1"})
    assert r1.status_code == 200

    # Second join with same code fails
    r2 = client.post("/join", json={"invite_code": code, "email": "p2@test.com", "display_name": "P2"})
    assert r2.status_code == 400
    assert "Invalid or expired" in r2.json()["detail"]


def test_expired_invite_code_fails(client):
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]

    # Expire the code manually
    code_h = _hash(code)
    _invites[code_h].expires_at = time.time() - 10

    r = client.post("/join", json={"invite_code": code, "email": "p@test.com", "display_name": "P"})
    assert r.status_code == 400
    assert "Invalid or expired" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Join status polling
# ---------------------------------------------------------------------------


def test_join_status_polling(client):
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]
    r_join = client.post("/join", json={"invite_code": code, "email": "friend@example.com", "display_name": "Friend"})
    token = r_join.json()["api_token"]

    # Initially tailscale says not accepted
    mock_ts = tailscale_mod.get_tailscale_client()
    mock_ts.is_user_in_tailnet.return_value = False

    r_status1 = client.get("/join/status", headers={"x-api-token": token})
    assert r_status1.status_code == 200
    assert r_status1.json()["accepted"] is False

    # Simulate user accepted email invite
    mock_ts.is_user_in_tailnet.return_value = True

    r_status2 = client.get("/join/status", headers={"x-api-token": token})
    assert r_status2.status_code == 200
    assert r_status2.json()["accepted"] is True


# ---------------------------------------------------------------------------
# 4. Admin: List and Revoke player
# ---------------------------------------------------------------------------


def test_admin_list_players(client):
    r = client.get("/admin/players", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["slots_max"] == 6
    assert data["slots_used"] >= 1
    assert len(data["players"]) >= 1


def test_revoked_player_is_rejected(client):
    # Register new player
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]
    r_join = client.post("/join", json={"invite_code": code, "email": "bad@test.com", "display_name": "BadGuy"})
    token = r_join.json()["api_token"]
    player_id = r_join.json()["player_id"]

    # Verify they can currently access
    assert client.get("/host", headers={"x-api-token": token}).status_code == 200

    # Admin revokes player
    r_rev = client.post("/admin/revoke", json={"player_id": player_id}, headers=ADMIN_HEADERS)
    assert r_rev.status_code == 200
    assert r_rev.json()["revoked"] is True

    # Tailscale delete invite was called
    mock_ts = tailscale_mod.get_tailscale_client()
    mock_ts.delete_user_invite.assert_called_with("invite-123")

    # Subsequent access by revoked player must return 403 Forbidden
    r_blocked = client.get("/host", headers={"x-api-token": token})
    assert r_blocked.status_code == 403
    assert "revoked" in r_blocked.json()["detail"].lower()


def test_revoking_lock_holder_releases_lock(client):
    # Player joins
    r_inv = client.post("/admin/invite-code", json={"ttl_hours": 24, "uses": 1}, headers=ADMIN_HEADERS)
    code = r_inv.json()["code"]
    r_join = client.post("/join", json={"invite_code": code, "email": "holder@test.com", "display_name": "LockHolder"})
    token = r_join.json()["api_token"]
    player_id = r_join.json()["player_id"]

    # Player acquires lock
    r_acq = client.post("/lock/acquire", headers={"x-api-token": token})
    assert r_acq.status_code == 200

    # Verify lock is held
    assert _lock.holder_name == "LockHolder"

    # Admin revokes player
    client.post("/admin/revoke", json={"player_id": player_id}, headers=ADMIN_HEADERS)

    # Lock must be cleared immediately
    assert _lock.holder_name is None
    assert _lock.owner_token_hash is None
