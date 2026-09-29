"""
Phase 4 acceptance tests.

Acceptance criterion (plan.md):
  "A second PC joins with no manual typing (or one copy-paste)."

Tests
-----
1. write_server_entry creates a fresh servers.dat when none exists.
2. write_server_entry prepends the entry so it appears first.
3. write_server_entry deduplicates – no double entries on repeated calls.
4. cmd_join aborts cleanly when nobody is hosting.
5. Integration: cmd_join fetches /host and writes the correct address
   into servers.dat – the acceptance test proving no manual typing needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import nbtlib
import pytest
from fastapi.testclient import TestClient

# Server imports (for the integration test)
import server.r2 as r2
from server.main import (
    _PHASE1_TOKEN,
    _lock,
    _host,
    _world_versions,
    _pending_uploads,
    app as server_app,
)

# Client imports
from client.guest_connect import SERVER_NAME, cmd_join, write_server_entry

SERVER_HEADERS = {"x-api-token": _PHASE1_TOKEN}


# ------------------------------------------------------------------
# Shared fixtures
# ------------------------------------------------------------------


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
def api_test_client():
    return TestClient(server_app)


@pytest.fixture
def tmp_servers_dat(tmp_path):
    """Return a Path for a fresh servers.dat in a temp directory."""
    return tmp_path / "servers.dat"


# ------------------------------------------------------------------
# Unit: write_server_entry
# ------------------------------------------------------------------


class TestWriteServerEntry:

    def test_creates_file_when_none_exists(self, tmp_servers_dat):
        assert not tmp_servers_dat.exists()
        write_server_entry(tmp_servers_dat, "100.64.0.1", 25565)
        assert tmp_servers_dat.exists()

    def test_entry_has_correct_address(self, tmp_servers_dat):
        write_server_entry(tmp_servers_dat, "100.64.0.1", 12345)
        nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
        servers = nbt["servers"]
        assert len(servers) == 1
        assert str(servers[0]["ip"]) == "100.64.0.1:12345"
        assert str(servers[0]["name"]) == SERVER_NAME

    def test_entry_prepended_to_existing_list(self, tmp_servers_dat):
        """Our entry must appear first so it shows at top of Multiplayer list."""
        # Create a servers.dat with an existing server
        existing = nbtlib.File({
            "servers": nbtlib.List([
                nbtlib.Compound({
                    "name": nbtlib.String("Friend's Server"),
                    "ip": nbtlib.String("192.168.1.1:25565"),
                })
            ])
        })
        existing.save(str(tmp_servers_dat), gzipped=False)

        write_server_entry(tmp_servers_dat, "100.64.0.1", 25565)

        nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
        servers = nbt["servers"]
        assert len(servers) == 2
        # Our entry must be first
        assert str(servers[0]["name"]) == SERVER_NAME
        # Original entry preserved
        assert str(servers[1]["name"]) == "Friend's Server"

    def test_deduplicates_on_repeated_calls(self, tmp_servers_dat):
        """Calling twice must not create two entries."""
        write_server_entry(tmp_servers_dat, "100.64.0.1", 25565)
        write_server_entry(tmp_servers_dat, "100.64.0.2", 25566)  # updated host

        nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
        servers = nbt["servers"]
        our_entries = [s for s in servers if str(s.get("name", "")) == SERVER_NAME]
        assert len(our_entries) == 1
        # Address should be the latest one
        assert str(our_entries[0]["ip"]) == "100.64.0.2:25566"

    def test_preserves_other_servers_on_update(self, tmp_servers_dat):
        """Updating our entry must not delete the user's other servers."""
        initial = nbtlib.File({
            "servers": nbtlib.List([
                nbtlib.Compound({"name": nbtlib.String("My SMP"), "ip": nbtlib.String("1.2.3.4:25565")}),
                nbtlib.Compound({"name": nbtlib.String(SERVER_NAME), "ip": nbtlib.String("old:1")}),
            ])
        })
        initial.save(str(tmp_servers_dat), gzipped=False)

        write_server_entry(tmp_servers_dat, "100.64.0.5", 30000)

        nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
        servers = nbt["servers"]
        names = [str(s["name"]) for s in servers]
        assert "My SMP" in names
        assert SERVER_NAME in names
        assert len(servers) == 2  # no duplicates


# ------------------------------------------------------------------
# Unit: cmd_join
# ------------------------------------------------------------------


class TestCmdJoin:

    def test_aborts_when_not_hosting(self, tmp_servers_dat):
        logs = []

        with patch("client.guest_connect.APIClient") as MockClient:
            MockClient.return_value.get_host.return_value = {"hosting": False}
            with pytest.raises(SystemExit) as exc_info:
                cmd_join("http://fake", "token", servers_dat=tmp_servers_dat, log=logs.append)

        assert exc_info.value.code == 0
        assert any("Nobody is hosting" in m for m in logs)
        assert not tmp_servers_dat.exists()  # should not have written anything

    def test_writes_entry_when_hosting(self, tmp_servers_dat):
        logs = []

        with patch("client.guest_connect.APIClient") as MockClient:
            MockClient.return_value.get_host.return_value = {
                "hosting": True,
                "ip": "100.64.0.1",
                "port": 25565,
                "host_name": "player1",
            }
            cmd_join("http://fake", "token", servers_dat=tmp_servers_dat, log=logs.append)

        assert tmp_servers_dat.exists()
        nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
        assert str(nbt["servers"][0]["ip"]) == "100.64.0.1:25565"
        assert any("OurWorld" in m for m in logs)

    def test_falls_back_to_address_on_write_failure(self, tmp_path):
        logs = []
        # Point to a path inside a non-existent deeply nested dir
        bad_path = tmp_path / "nonexistent" / "deep" / "servers.dat"

        with patch("client.guest_connect.APIClient") as MockClient:
            MockClient.return_value.get_host.return_value = {
                "hosting": True,
                "ip": "100.64.0.1",
                "port": 25565,
                "host_name": "player1",
            }
            # Patch write_server_entry to raise
            with patch("client.guest_connect.write_server_entry", side_effect=PermissionError("denied")):
                cmd_join("http://fake", "token", servers_dat=bad_path, log=logs.append)

        # Should have printed the address instead
        assert any("100.64.0.1:25565" in m for m in logs)


# ------------------------------------------------------------------
# Integration: Phase 4 acceptance test
# "A second PC joins with no manual typing"
# ------------------------------------------------------------------


class TestPhase4Acceptance:

    def test_guest_joins_with_no_manual_typing(self, api_test_client, tmp_servers_dat):
        """
        Full scenario:
          1. Player 1 acquires lock, sets host address.
          2. Player 2 calls cmd_join.
          3. servers.dat is written automatically with the correct address.
          4. No manual input required — servers.dat ready to open in Minecraft.
        """
        from server.main import _PLAYERS
        _PLAYERS["player2-token"] = "player2"

        try:
            # ── Player 1 acquires lock and sets host address ───────────────
            r = api_test_client.post("/lock/acquire", headers=SERVER_HEADERS)
            token1 = r.json()["owner_token"]

            api_test_client.post(
                "/host/address",
                json={
                    "owner_token": token1,
                    "tailscale_ip": "100.64.0.1",
                    "port": 25565,
                },
            )

            # ── Player 2 runs guest connect ────────────────────────────────
            logs = []

            # We need cmd_join to talk to the TestClient, not a real server.
            # Patch APIClient.get_host to use the TestClient directly.
            with patch("client.guest_connect.APIClient") as MockClient:
                r_host = api_test_client.get("/host", headers={"x-api-token": "player2-token"})
                MockClient.return_value.get_host.return_value = r_host.json()
                cmd_join(
                    "http://fake",
                    "player2-token",
                    servers_dat=tmp_servers_dat,
                    log=logs.append,
                )

            # ── Verify servers.dat written correctly ──────────────────────
            assert tmp_servers_dat.exists(), "servers.dat must have been created"
            nbt = nbtlib.load(str(tmp_servers_dat), gzipped=False)
            servers = nbt["servers"]

            assert len(servers) >= 1
            top = servers[0]
            assert str(top["name"]) == SERVER_NAME
            assert str(top["ip"]) == "100.64.0.1:25565"

            # No manual typing needed: entry appears at top of Multiplayer list
            assert any("OurWorld" in m for m in logs)
            assert any("Multiplayer" in m for m in logs)

        finally:
            _PLAYERS.pop("player2-token", None)
