"""
Phase 6 client tests: Hardening, local conflict resolution (Section 6A),
and failure scenarios (Section 7).

Acceptance criteria:
1. Kill mid-upload -> next session retries the upload and never downloads over the only good copy.
2. Solo play detection -> backup created in app's backup folder, rotated to max 5.
3. Autosave thread periodically commits snapshots during play.
4. Version check blocks outdated client or mismatched Minecraft version.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import responses

from client.api_client import APIClient, check_compatibility
from client.autosave import AutosaveThread
from client.conflict_manager import (
    BACKUPS_DIR,
    MARKERS_DIR,
    check_and_resolve_conflict,
    create_local_backup,
    hash_world_folder,
    load_marker,
    save_marker,
    set_upload_pending,
)
from client.world_sync import cmd_download


@pytest.fixture(autouse=True)
def redirect_app_dirs(monkeypatch, tmp_path):
    # Redirect markers and backups to a temp test directory
    test_app_dir = tmp_path / "app_data"
    markers = test_app_dir / "markers"
    backups = test_app_dir / "backups"
    markers.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("client.conflict_manager.APP_DATA_DIR", test_app_dir)
    monkeypatch.setattr("client.conflict_manager.MARKERS_DIR", markers)
    monkeypatch.setattr("client.conflict_manager.BACKUPS_DIR", backups)


# ---------------------------------------------------------------------------
# 1. Section 6A: Mid-upload kill and recovery (MUST-TEST)
# ---------------------------------------------------------------------------


def test_kill_mid_upload_recovery(tmp_path):
    """
    Scenario (plan.md 6A / 7):
    A host starts session, modifies the world, begins upload, but the app is killed mid-upload.
    `upload_pending` remains set.
    The next session (cmd_download) must retry the upload first and never download over the only good copy!
    """
    world_dir = tmp_path / "OurWorld"
    world_dir.mkdir()
    (world_dir / "level.dat").write_text("my-precious-local-progress")

    # Set marker indicating previous session was killed mid-upload
    save_marker("OurWorld", version_id="worlds/old.zip", folder_hash="old-hash", upload_pending=True)

    upload_retried = []

    def fake_upload(api_url, token, w_dir):
        upload_retried.append(True)
        # Clear pending flag upon successful upload
        set_upload_pending("OurWorld", False)

    with patch("client.world_sync.cmd_upload", side_effect=fake_upload):
        with patch("client.world_sync._api") as mock_api:
            mock_api.return_value.status_code = 200
            mock_api.return_value.json.return_value = {
                "owner_token": "new-tok",
                "download_url": None,  # or fresh
                "world_version_key": None,
            }

            cmd_download("http://fake-api", "test-token", world_dir)

    # Verify that the upload was retried first before proceeding
    assert upload_retried, "Must have retried the pending upload before downloading!"
    assert load_marker("OurWorld")["upload_pending"] is False


# ---------------------------------------------------------------------------
# 2. Section 6A: Solo play detection and backup rotation
# ---------------------------------------------------------------------------


def test_solo_play_creates_backup_and_rotates(tmp_path):
    world_dir = tmp_path / "OurWorld"
    world_dir.mkdir()
    (world_dir / "region.mca").write_text("solo-changes")

    # Save marker with a different previous hash
    save_marker("OurWorld", version_id="worlds/cloud.zip", folder_hash="different-synced-hash", upload_pending=False)

    logs = []
    res = check_and_resolve_conflict(world_dir, "OurWorld", log=logs.append)
    assert res == "BACKED_UP"
    assert any("Local solo changes detected" in l for l in logs)

    import client.conflict_manager as cm
    # Check backup file was created in backups dir
    backups = list((cm.BACKUPS_DIR / "OurWorld").glob("OurWorld_overridden_*.zip"))
    assert len(backups) == 1

    # Verify rotation: create 6 backups -> must prune down to max 5
    for i in range(6):
        time.sleep(0.01)
        create_local_backup(world_dir, "OurWorld")

    remaining_backups = list((cm.BACKUPS_DIR / "OurWorld").glob("OurWorld_overridden_*.zip"))
    assert len(remaining_backups) <= 5


# ---------------------------------------------------------------------------
# 3. Autosave Thread
# ---------------------------------------------------------------------------


def test_autosave_thread():
    saves = []

    def mock_save():
        saves.append(time.time())

    th = AutosaveThread(autosave_fn=mock_save, interval=0.05)
    th.start()
    time.sleep(0.18)
    th.stop()
    th.join(timeout=1)

    assert len(saves) >= 2, "Autosave thread should have triggered at least twice"


# ---------------------------------------------------------------------------
# 4. Version Check & Compatibility
# ---------------------------------------------------------------------------


@responses.activate
def test_version_check_outdated_client_raises():
    responses.add(
        responses.GET,
        "http://fake-api/config",
        json={
            "min_client_version": "1.0.0",
            "required_mc_version": "1.20.1",
            "current_world_version": None,
        },
        status=200,
    )

    client = APIClient("http://fake-api", "token")
    with pytest.raises(RuntimeError) as exc_info:
        check_compatibility(client, current_client_version="0.1.0")

    assert "outdated" in str(exc_info.value).lower()


@responses.activate
def test_version_check_minecraft_mismatch_raises():
    responses.add(
        responses.GET,
        "http://fake-api/config",
        json={
            "min_client_version": "0.1.0",
            "required_mc_version": "1.20.1",
            "current_world_version": None,
        },
        status=200,
    )

    client = APIClient("http://fake-api", "token")
    with pytest.raises(RuntimeError) as exc_info:
        check_compatibility(client, current_client_version="0.1.0", local_mc_version="1.19.2")

    assert "mismatch" in str(exc_info.value).lower()
