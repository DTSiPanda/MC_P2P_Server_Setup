"""
Phase 1 acceptance tests.
Tests that the plan requires:
  1. Two clients cannot hold the lock at once.
  2. The lock expires if heartbeats stop.
  3. A wrong owner token is rejected.

Run with:
  pip install pytest httpx fastapi
  pytest server/test_phase1.py -v
"""

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.main import _PHASE1_TOKEN, _hash, _lock, _host, app

HEADERS = {"x-api-token": _PHASE1_TOKEN}
HEADERS2 = {"x-api-token": "another-player-token"}  # second player


def _reset_state():
    """Reset shared in-memory state between tests."""
    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0
    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None


@pytest.fixture(autouse=True)
def clean_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register_second_player():
    """Add a second player token to the in-memory registry."""
    from server.main import _PLAYERS
    _PLAYERS["another-player-token"] = "player2"
    yield
    _PLAYERS.pop("another-player-token", None)


# ---------------------------------------------------------------------------
# Lock acquire
# ---------------------------------------------------------------------------


def test_acquire_lock_success(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "owner_token" in data
    assert data["holder_name"] == "player1"
    assert data["expires_at"] > time.time()


def test_acquire_lock_without_token_returns_401(client):
    r = client.post("/lock/acquire")
    assert r.status_code == 422  # missing required header → unprocessable or 401


def test_acquire_lock_with_wrong_token_returns_401(client):
    r = client.post("/lock/acquire", headers={"x-api-token": "bad-token"})
    assert r.status_code == 401


def test_two_clients_cannot_hold_lock_at_once(client):
    """The plan's primary acceptance criterion for Phase 1."""
    # Register a second player inline
    from server.main import _PLAYERS
    _PLAYERS["another-player-token"] = "player2"

    try:
        # player1 acquires
        r1 = client.post("/lock/acquire", headers=HEADERS)
        assert r1.status_code == 200

        # player2 tries to acquire → must get 409
        r2 = client.post("/lock/acquire", headers=HEADERS2)
        assert r2.status_code == 409
        assert r2.json()["detail"]["holder_name"] == "player1"
    finally:
        _PLAYERS.pop("another-player-token", None)


def test_expired_lock_can_be_acquired(client):
    """If the lock has expired (TTL elapsed), another player may acquire it."""
    from server.main import _PLAYERS
    _PLAYERS["another-player-token"] = "player2"

    try:
        r1 = client.post("/lock/acquire", headers=HEADERS)
        assert r1.status_code == 200

        # Expire the lock manually
        _lock.expires_at = time.time() - 1

        r2 = client.post("/lock/acquire", headers=HEADERS2)
        assert r2.status_code == 200
        assert r2.json()["holder_name"] == "player2"
    finally:
        _PLAYERS.pop("another-player-token", None)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_extends_lock(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]
    old_expires = r.json()["expires_at"]

    # Travel a little forward in time
    _lock.expires_at = time.time() + 5  # shrink TTL artificially

    r2 = client.post("/lock/heartbeat", json={"owner_token": token})
    assert r2.status_code == 200
    assert r2.json()["expires_at"] > _lock.expires_at - 1  # extended


def test_heartbeat_with_wrong_token_returns_403(client):
    client.post("/lock/acquire", headers=HEADERS)

    r = client.post("/lock/heartbeat", json={"owner_token": "wrong-token"})
    assert r.status_code == 403


def test_heartbeat_after_lock_expires_returns_410(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]

    # Expire the lock
    _lock.expires_at = time.time() - 1

    r2 = client.post("/lock/heartbeat", json={"owner_token": token})
    assert r2.status_code == 410


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_clears_lock(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]

    r2 = client.post("/lock/release", json={"owner_token": token})
    assert r2.status_code == 200

    # Should now be acquirable again
    r3 = client.post("/lock/acquire", headers=HEADERS)
    assert r3.status_code == 200


def test_release_with_wrong_token_returns_403(client):
    client.post("/lock/acquire", headers=HEADERS)

    r = client.post("/lock/release", json={"owner_token": "wrong"})
    assert r.status_code == 403


def test_release_clears_host_address(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]

    client.post(
        "/host/address",
        json={"owner_token": token, "tailscale_ip": "100.1.2.3", "port": 25565},
    )

    client.post("/lock/release", json={"owner_token": token})

    r2 = client.get("/host", headers=HEADERS)
    assert r2.json()["hosting"] is False


# ---------------------------------------------------------------------------
# Host address
# ---------------------------------------------------------------------------


def test_set_and_get_host_address(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]

    r2 = client.post(
        "/host/address",
        json={"owner_token": token, "tailscale_ip": "100.64.0.1", "port": 12345},
    )
    assert r2.status_code == 200

    r3 = client.get("/host", headers=HEADERS)
    data = r3.json()
    assert data["hosting"] is True
    assert data["ip"] == "100.64.0.1"
    assert data["port"] == 12345
    assert data["host_name"] == "player1"


def test_set_host_address_with_wrong_token_returns_403(client):
    client.post("/lock/acquire", headers=HEADERS)

    r = client.post(
        "/host/address",
        json={"owner_token": "bad", "tailscale_ip": "100.64.0.1", "port": 12345},
    )
    assert r.status_code == 403


def test_get_host_returns_not_hosting_when_no_lock(client):
    r = client.get("/host", headers=HEADERS)
    assert r.json()["hosting"] is False


def test_get_host_returns_not_hosting_after_lock_expires(client):
    r = client.post("/lock/acquire", headers=HEADERS)
    token = r.json()["owner_token"]

    client.post(
        "/host/address",
        json={"owner_token": token, "tailscale_ip": "100.64.0.1", "port": 12345},
    )

    # Expire the lock
    _lock.expires_at = time.time() - 1

    r2 = client.get("/host", headers=HEADERS)
    assert r2.json()["hosting"] is False
