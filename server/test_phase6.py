"""
Phase 6 server tests: Hardening, rate limiting, and admin lockout.
"""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from server.main import (
    _ADMIN_FAILURES,
    _PHASE1_TOKEN,
    _RATE_LIMIT_WINDOWS,
    _hash,
    _players_registry,
    _token_hash_to_player_id,
    app,
)


@pytest.fixture(autouse=True)
def clean_hardening_state(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", "super-admin-secret")
    monkeypatch.setenv("PLAYER_TOKEN", _PHASE1_TOKEN)
    _RATE_LIMIT_WINDOWS.clear()
    _ADMIN_FAILURES.clear()

    # Register admin player
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


@pytest.fixture
def client():
    return TestClient(app)


def test_admin_lockout_after_repeated_wrong_secrets(client):
    """
    Scenario (plan.md Section 6B):
    Repeated wrong admin secret attempts must lock out the caller with 429 Too Many Requests.
    """
    bad_headers = {
        "x-api-token": _PHASE1_TOKEN,
        "x-admin-secret": "wrong-secret",
    }

    # First 4 attempts return 403 Forbidden
    for _ in range(4):
        r = client.post("/admin/invite-code", json={"ttl_hours": 1, "uses": 1}, headers=bad_headers)
        assert r.status_code == 403

    # 5th attempt triggers lockout
    r5 = client.post("/admin/invite-code", json={"ttl_hours": 1, "uses": 1}, headers=bad_headers)
    assert r5.status_code == 403

    # Subsequent attempts (even with correct secret) are now locked out with 429!
    correct_headers = {
        "x-api-token": _PHASE1_TOKEN,
        "x-admin-secret": "super-admin-secret",
    }
    r_locked = client.post("/admin/invite-code", json={"ttl_hours": 1, "uses": 1}, headers=correct_headers)
    assert r_locked.status_code == 429
    assert "locked out" in r_locked.json()["detail"].lower()


def test_join_rate_limiting(client):
    """
    Scenario (plan.md Section 8):
    Rate limit /join to prevent brute-forcing.
    """
    # Exceed limit in quick succession
    for _ in range(20):
        client.post("/join", json={"invite_code": "fake", "email": "a@b.com", "display_name": "x"})

    # 21st attempt returns 429 Too Many Requests
    r = client.post("/join", json={"invite_code": "fake", "email": "a@b.com", "display_name": "x"})
    assert r.status_code == 429
    assert "too many requests" in r.json()["detail"].lower()
