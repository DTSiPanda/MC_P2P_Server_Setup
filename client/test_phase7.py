"""
Phase 7 tests: UI module loading, CLI dispatching, and packaging verification.
"""

from __future__ import annotations

import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_ui_importable():
    """Verify that client.ui imports cleanly without syntax or missing dependency errors."""
    import client.ui as ui
    assert hasattr(ui, "AppUI")
    assert hasattr(ui, "main")


def test_client_main_cli_dispatch_host():
    """Test client.main CLI dispatches to host session with --host flag."""
    with patch("sys.argv", ["main.py", "--host"]):
        with patch("client.auth.get_api_token", return_value="test-token"):
            with patch("client.host_session.run_host_session") as mock_host:
                from client.main import main as client_main
                client_main()
                mock_host.assert_called_once()


def test_client_main_cli_dispatch_join():
    """Test client.main CLI dispatches to guest connect with --join flag."""
    with patch("sys.argv", ["main.py", "--join"]):
        with patch("client.auth.get_api_token", return_value="test-token"):
            with patch("client.guest_connect.cmd_join") as mock_join:
                from client.main import main as client_main
                client_main()
                mock_join.assert_called_once()


def test_spec_file_exists_and_configured():
    from pathlib import Path
    spec_path = Path("client") / "minecraft_p2p.spec"
    assert spec_path.exists(), "minecraft_p2p.spec must exist"
    content = spec_path.read_text(encoding="utf-8")
    assert "name='MinecraftP2P'" in content
    assert "console=False" in content
