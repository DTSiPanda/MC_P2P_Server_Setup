"""
Phase 2 acceptance tests.

Acceptance criteria from plan.md:
  1. A kill mid-upload leaves the previous version intact.
  2. A corrupted upload is rejected at commit (sha256 mismatch).

Also tests the new endpoints:
  - GET /config
  - POST /world/upload-url
  - POST /world/commit
  - lock/acquire now returns download_url for existing world
"""

import time

import boto3
import pytest
from moto import mock_aws
from fastapi.testclient import TestClient

import server.r2 as r2
from server.main import (
    _PHASE1_TOKEN,
    _lock,
    _host,
    _world_versions,
    _pending_uploads,
    WorldVersion,
    app,
)

HEADERS = {"x-api-token": _PHASE1_TOKEN}
BUCKET = "minecraft-p2p"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _reset_state():
    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0
    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None
    _world_versions.clear()
    _pending_uploads.clear()
    r2.set_client(None)


@pytest.fixture(autouse=True)
def clean_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def s3():
    """Provide a moto-backed S3 client and inject it into the r2 module."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        r2.set_client(client)
        r2.BUCKET = BUCKET
        yield client


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def token(client):
    """Acquire a lock and return the owner_token."""
    r = client.post("/lock/acquire", headers=HEADERS)
    assert r.status_code == 200
    return r.json()["owner_token"]


# ---------------------------------------------------------------------------
# Helper: put a fake world zip into the mock bucket
# ---------------------------------------------------------------------------


def _put_fake_world(s3_client, key: str, content: bytes = b"fake-world-data"):
    s3_client.put_object(Bucket=BUCKET, Key=key, Body=content)


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_config_returns_fields(client):
    r = client.get("/config", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "min_client_version" in data
    assert "required_mc_version" in data
    assert data["current_world_version"] is None  # no world yet


def test_config_shows_current_world_version(client):
    # Seed a current version directly
    _world_versions.append(
        WorldVersion(
            key="worlds/abc.zip",
            sha256="aabbcc",
            size=100,
            created_by="player1",
            created_at=time.time(),
            is_current=True,
        )
    )
    r = client.get("/config", headers=HEADERS)
    assert r.json()["current_world_version"] == "worlds/abc.zip"


# ---------------------------------------------------------------------------
# lock/acquire returns download URL when a world exists
# ---------------------------------------------------------------------------


def test_lock_acquire_no_world_returns_no_download_url(client, s3):
    r = client.post("/lock/acquire", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["download_url"] is None


def test_lock_acquire_with_world_returns_download_url(client, s3):
    key = "worlds/v1.zip"
    _put_fake_world(s3, key)
    _world_versions.append(
        WorldVersion(
            key=key,
            sha256="deadbeef",
            size=14,
            created_by="player1",
            created_at=time.time(),
            is_current=True,
        )
    )
    r = client.post("/lock/acquire", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["download_url"] is not None
    assert data["world_version_key"] == key


# ---------------------------------------------------------------------------
# POST /world/upload-url
# ---------------------------------------------------------------------------


def test_upload_url_returns_url_and_key(client, s3, token):
    r = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": 1024, "sha256": "abc123"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "upload_url" in data
    assert data["version_key"].startswith("worlds/")


def test_upload_url_wrong_token_rejected(client, s3):
    r = client.post(
        "/world/upload-url",
        json={"owner_token": "wrong", "size": 1024, "sha256": "abc123"},
    )
    assert r.status_code in (403, 410)


# ---------------------------------------------------------------------------
# POST /world/commit
# ---------------------------------------------------------------------------


def test_commit_success(client, s3, token):
    content = b"world-data"
    sha = "sha256-of-world"

    # Get upload URL
    r_url = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": len(content), "sha256": sha},
    )
    key = r_url.json()["version_key"]

    # Simulate upload by putting the object directly into moto
    _put_fake_world(s3, key, content)

    # Commit
    r_commit = client.post(
        "/world/commit",
        json={"owner_token": token, "version_key": key, "sha256": sha},
    )
    assert r_commit.status_code == 200
    assert r_commit.json()["committed"] is True

    # /config should now reflect the new current version
    r_cfg = client.get("/config", headers=HEADERS)
    assert r_cfg.json()["current_world_version"] == key


# ---------------------------------------------------------------------------
# Acceptance test 1: kill mid-upload leaves previous version intact
# ---------------------------------------------------------------------------


def test_kill_mid_upload_leaves_previous_version_intact(client, s3, token):
    """
    Scenario: a good world (v1) is committed. The host starts a new session,
    gets an upload URL (v2), but the app is killed before /world/commit is called.
    The current version must still be v1.
    """
    # --- Commit v1 first ---
    content_v1 = b"world-v1"
    sha_v1 = "sha-v1"

    r_url1 = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": len(content_v1), "sha256": sha_v1},
    )
    key_v1 = r_url1.json()["version_key"]
    _put_fake_world(s3, key_v1, content_v1)
    client.post(
        "/world/commit",
        json={"owner_token": token, "version_key": key_v1, "sha256": sha_v1},
    )

    # Verify v1 is current
    assert client.get("/config", headers=HEADERS).json()["current_world_version"] == key_v1

    # --- Start v2 upload, but "kill" before commit ---
    r_url2 = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": 9, "sha256": "sha-v2"},
    )
    assert r_url2.status_code == 200
    # (intentionally never call /world/commit for v2)

    # Previous version must still be current
    assert client.get("/config", headers=HEADERS).json()["current_world_version"] == key_v1


# ---------------------------------------------------------------------------
# Acceptance test 2: corrupted upload rejected at commit
# ---------------------------------------------------------------------------


def test_corrupted_upload_rejected_at_commit(client, s3, token):
    """
    Scenario: client declares sha256='good-hash' when getting the upload URL,
    but calls /world/commit with sha256='bad-hash'. Must be rejected with 422.
    The current version must remain unchanged (None here since no prior commit).
    """
    declared_sha = "correct-sha256"

    r_url = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": 100, "sha256": declared_sha},
    )
    key = r_url.json()["version_key"]
    _put_fake_world(s3, key, b"data")

    r_commit = client.post(
        "/world/commit",
        json={"owner_token": token, "version_key": key, "sha256": "WRONG-sha256"},
    )
    assert r_commit.status_code == 422
    assert "mismatch" in r_commit.json()["detail"]

    # No version should have been committed
    assert client.get("/config", headers=HEADERS).json()["current_world_version"] is None


def test_commit_without_upload_rejected(client, s3, token):
    """Commit with a key that was never registered via upload-url must fail."""
    r = client.post(
        "/world/commit",
        json={"owner_token": token, "version_key": "worlds/ghost.zip", "sha256": "abc"},
    )
    assert r.status_code == 400


def test_commit_object_not_in_r2_rejected(client, s3, token):
    """upload-url was called but the client never actually PUT the object."""
    r_url = client.post(
        "/world/upload-url",
        json={"owner_token": token, "size": 100, "sha256": "myhash"},
    )
    key = r_url.json()["version_key"]
    # Do NOT put the object into moto

    r_commit = client.post(
        "/world/commit",
        json={"owner_token": token, "version_key": key, "sha256": "myhash"},
    )
    assert r_commit.status_code == 422
    assert "not found" in r_commit.json()["detail"]
