"""
Phase 3 acceptance tests.

Acceptance criterion (plan.md):
  "A full host session works end to end, including force-killing the game
   and the app (lock expires, next host gets the last good version)."

Test structure
--------------
1.  Unit: HeartbeatThread – sends heartbeat, calls on_lock_lost on 410, stops cleanly.
2.  Unit: LAN sniffer parsing – pure function, no socket needed.
3.  Unit: APIClient – retries on 503, raises on 409/410.
4.  Integration: run_host_session orchestration – all external I/O mocked.
5.  Integration: force-kill scenario – server-side, uses TestClient + moto.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import boto3
import pytest
import responses as resp_lib
from moto import mock_aws
from fastapi.testclient import TestClient

# ---------- server imports (for the force-kill integration test) ----------
import server.r2 as r2
from server.main import (
    _PHASE1_TOKEN,
    _lock,
    _host,
    _world_versions,
    _pending_uploads,
    WorldVersion,
    app as server_app,
)

# ---------- client imports ------------------------------------------------
from client.api_client import APIClient, APIError
from client.heartbeat import HeartbeatThread
from client.lan_sniffer import parse_lan_announcement

SERVER_HEADERS = {"x-api-token": _PHASE1_TOKEN}
BUCKET = "minecraft-p2p"


# ==========================================================================
# Shared fixtures
# ==========================================================================


def _reset_server_state():
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
def clean_server_state():
    _reset_server_state()
    yield
    _reset_server_state()


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        r2.set_client(client)
        r2.BUCKET = BUCKET
        yield client


@pytest.fixture
def api_test_client():
    return TestClient(server_app)


# ==========================================================================
# 1. Unit: HeartbeatThread
# ==========================================================================


class TestHeartbeatThread:

    def test_calls_heartbeat_fn_at_each_interval(self):
        calls = []

        def fake_hb(token):
            calls.append(token)
            return {"expires_at": time.time() + 90}

        hb = HeartbeatThread(fake_hb, "tok", on_lock_lost=lambda: None, interval=0.05)
        hb.start()
        time.sleep(0.22)   # ~4 intervals
        hb.stop()
        hb.join(timeout=1)
        assert len(calls) >= 3

    def test_on_lock_lost_called_when_410_raised(self):
        lost = []

        def fake_hb(token):
            err = APIError(410, "Lock lost")
            raise err

        hb = HeartbeatThread(fake_hb, "tok", on_lock_lost=lambda: lost.append(True), interval=0.05)
        hb.start()
        time.sleep(0.15)
        hb.join(timeout=1)
        assert lost  # on_lock_lost was called

    def test_stop_prevents_further_heartbeats(self):
        calls = []

        def fake_hb(token):
            calls.append(1)

        hb = HeartbeatThread(fake_hb, "tok", on_lock_lost=lambda: None, interval=0.05)
        hb.start()
        time.sleep(0.08)
        hb.stop()
        hb.join(timeout=1)
        count_at_stop = len(calls)
        time.sleep(0.15)
        assert len(calls) == count_at_stop  # no more calls after stop

    def test_transient_error_does_not_kill_thread(self):
        """A non-410 exception should be swallowed; thread keeps running."""
        calls = []
        attempt = [0]

        def fake_hb(token):
            attempt[0] += 1
            if attempt[0] == 1:
                raise ConnectionError("blip")
            calls.append(1)

        hb = HeartbeatThread(fake_hb, "tok", on_lock_lost=lambda: None, interval=0.05)
        hb.start()
        time.sleep(0.2)
        hb.stop()
        hb.join(timeout=1)
        assert len(calls) >= 1  # recovered after the blip


# ==========================================================================
# 2. Unit: LAN sniffer parsing
# ==========================================================================


class TestLanSniffer:

    def test_parses_standard_announcement(self):
        msg = "[MOTD]Tejas's World[/MOTD][AD]25565[/AD]"
        assert parse_lan_announcement(msg) == 25565

    def test_parses_random_port(self):
        msg = "[MOTD]Test[/MOTD][AD]54321[/AD]"
        assert parse_lan_announcement(msg) == 54321

    def test_returns_none_for_invalid_message(self):
        assert parse_lan_announcement("no port here") is None

    def test_rejects_port_zero(self):
        assert parse_lan_announcement("[AD]0[/AD]") is None

    def test_rejects_port_above_65535(self):
        assert parse_lan_announcement("[AD]99999[/AD]") is None


# ==========================================================================
# 3. Unit: APIClient retries and error handling
# ==========================================================================


class TestAPIClient:

    @pytest.fixture
    def client(self):
        return APIClient("http://fake-server", "test-token")

    @resp_lib.activate
    def test_retries_on_503_then_succeeds(self, client):
        resp_lib.add(resp_lib.POST, "http://fake-server/lock/acquire", status=503)
        resp_lib.add(resp_lib.POST, "http://fake-server/lock/acquire", status=503)
        resp_lib.add(
            resp_lib.POST,
            "http://fake-server/lock/acquire",
            json={"owner_token": "tok", "expires_at": 9999.0, "holder_name": "p1", "download_url": None, "world_version_key": None},
            status=200,
        )
        # Patch sleep so the test doesn't actually wait
        with patch("client.api_client.time.sleep"):
            result = client.acquire_lock()
        assert result["owner_token"] == "tok"

    @resp_lib.activate
    def test_raises_api_error_on_409(self, client):
        resp_lib.add(
            resp_lib.POST,
            "http://fake-server/lock/acquire",
            json={"detail": {"holder_name": "player2"}},
            status=409,
        )
        with pytest.raises(APIError) as exc_info:
            client.acquire_lock()
        assert exc_info.value.status_code == 409
        assert "player2" in exc_info.value.detail

    @resp_lib.activate
    def test_raises_api_error_on_410_heartbeat(self, client):
        resp_lib.add(
            resp_lib.POST,
            "http://fake-server/lock/heartbeat",
            json={"detail": "Lock lost"},
            status=410,
        )
        with pytest.raises(APIError) as exc_info:
            client.heartbeat("some-token")
        assert exc_info.value.status_code == 410


# ==========================================================================
# 4. Integration: run_host_session orchestration (all I/O mocked)
# ==========================================================================


class TestHostSessionOrchestration:
    """
    Patches all external I/O in host_session and verifies the correct
    sequence of steps: download → launch → wait_mc → sniff → set_address
    → heartbeat → wait_exit → upload.
    """

    def _make_fake_process(self):
        p = MagicMock()
        p.pid = 1234
        p.wait.return_value = 0
        return p

    @patch("client.host_session.cmd_upload")
    @patch("client.host_session.cmd_download")
    @patch("client.host_session.wait_for_exit")
    @patch("client.host_session.wait_for_minecraft")
    @patch("client.host_session.launch_tlauncher")
    @patch("client.host_session.sniff_port", return_value=25565)
    @patch("client.host_session.get_tailscale_ip", return_value="100.64.0.1")
    @patch("client.host_session.APIClient")
    def test_full_session_happy_path(
        self,
        MockAPIClient,
        mock_ts_ip,
        mock_sniff,
        mock_launch,
        mock_wait_mc,
        mock_wait_exit,
        mock_download,
        mock_upload,
    ):
        from client.host_session import run_host_session

        # Set up mock API client
        mock_api = MagicMock()
        mock_api.set_host_address.return_value = {"stored": True}
        mock_api.heartbeat.return_value = {"expires_at": time.time() + 90}
        MockAPIClient.return_value = mock_api

        # set_for_minecraft returns a fake process
        fake_proc = self._make_fake_process()
        mock_wait_mc.return_value = fake_proc

        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            world_dir = Path(tmp) / "OurWorld"
            world_dir.mkdir()

            # cmd_download needs to write the session file
            session_file = world_dir.parent / f".{world_dir.name}_session"
            session_file.write_text("fake-owner-token")

            run_host_session(
                "http://fake-server",
                "test-token",
                world_dir,
                log=logs.append,
            )

        # Verify the key steps happened in order
        mock_download.assert_called_once()
        mock_launch.assert_called_once()
        mock_wait_mc.assert_called_once()
        mock_sniff.assert_called_once()
        mock_api.set_host_address.assert_called_once_with(
            "fake-owner-token", "100.64.0.1", 25565
        )
        mock_wait_exit.assert_called_once_with(fake_proc)
        mock_upload.assert_called_once()

        # Verify heartbeat ran (it's a daemon thread; check that start was effective)
        assert any("Heartbeat" in msg for msg in logs)

    @patch("client.host_session.cmd_upload")
    @patch("client.host_session.cmd_download")
    @patch("client.host_session.wait_for_exit")
    @patch("client.host_session.wait_for_minecraft")
    @patch("client.host_session.launch_tlauncher")
    @patch("client.host_session.sniff_port", return_value=None)   # ← timeout
    @patch("client.host_session.get_tailscale_ip", return_value="100.1.2.3")
    @patch("client.host_session.APIClient")
    @patch("builtins.input", return_value="25600")                 # ← manual entry
    def test_manual_port_fallback(
        self,
        mock_input,
        MockAPIClient,
        mock_ts_ip,
        mock_sniff,
        mock_launch,
        mock_wait_mc,
        mock_wait_exit,
        mock_download,
        mock_upload,
    ):
        from client.host_session import run_host_session

        mock_api = MagicMock()
        mock_api.set_host_address.return_value = {"stored": True}
        mock_api.heartbeat.return_value = {"expires_at": time.time() + 90}
        MockAPIClient.return_value = mock_api
        mock_wait_mc.return_value = self._make_fake_process()

        with tempfile.TemporaryDirectory() as tmp:
            world_dir = Path(tmp) / "OurWorld"
            world_dir.mkdir()
            session_file = world_dir.parent / f".{world_dir.name}_session"
            session_file.write_text("fake-owner-token")

            run_host_session("http://fake-server", "test-token", world_dir, log=lambda _: None)

        # Should have used the manually-entered port 25600
        mock_api.set_host_address.assert_called_once_with("fake-owner-token", "100.1.2.3", 25600)


# ==========================================================================
# 5. Integration: force-kill scenario (server-side with TestClient + moto)
# ==========================================================================


class TestForceKillScenario:
    """
    Simulates:
      1. Player 1 hosts, commits world v1.
      2. Player 1's app is force-killed (heartbeat stops → lock TTL expires).
      3. Player 2 acquires the lock and receives a download URL pointing to v1.
      4. Verified: the last good version is still current.
    """

    def test_force_kill_lock_expires_next_host_gets_good_version(self, api_test_client, s3):
        client = api_test_client
        from server.main import _PLAYERS
        _PLAYERS["player2-token"] = "player2"

        try:
            # ── Player 1 acquires lock ─────────────────────────────────────
            r = client.post("/lock/acquire", headers={"x-api-token": _PHASE1_TOKEN})
            assert r.status_code == 200
            token1 = r.json()["owner_token"]

            # ── Player 1 uploads & commits world v1 ───────────────────────
            content_v1 = b"world-data-v1"
            sha_v1 = "sha256-v1"

            r_url = client.post(
                "/world/upload-url",
                json={"owner_token": token1, "size": len(content_v1), "sha256": sha_v1},
            )
            assert r_url.status_code == 200
            key_v1 = r_url.json()["version_key"]

            # Put the object into the moto bucket
            s3.put_object(Bucket=BUCKET, Key=key_v1, Body=content_v1)

            r_commit = client.post(
                "/world/commit",
                json={"owner_token": token1, "version_key": key_v1, "sha256": sha_v1},
            )
            assert r_commit.status_code == 200

            # ── Force-kill: expire the lock (heartbeat stopped) ───────────
            _lock.expires_at = time.time() - 1   # simulate TTL expiry

            # ── Player 2 acquires lock → must get download_url for v1 ─────
            r2_acq = client.post("/lock/acquire", headers={"x-api-token": "player2-token"})
            assert r2_acq.status_code == 200, r2_acq.text
            data2 = r2_acq.json()

            assert data2["holder_name"] == "player2"
            assert data2["world_version_key"] == key_v1
            assert data2["download_url"] is not None

            # ── World version integrity: v1 is still current ───────────────
            r_cfg = client.get("/config", headers={"x-api-token": "player2-token"})
            assert r_cfg.json()["current_world_version"] == key_v1

        finally:
            _PLAYERS.pop("player2-token", None)
