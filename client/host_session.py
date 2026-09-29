"""
host_session.py – orchestrates the full host flow (Phase 3).

Step-by-step:
  1.  Acquire lock  →  get presigned download URL
  2.  Download & unpack world
  3.  Launch TLauncher
  4.  Wait for javaw.exe (Minecraft) to start
  5.  Wait for LAN announcement  →  parse port  (fallback: manual entry)
  6.  POST /host/address  with Tailscale IP + port
  7.  Start heartbeat thread
  8.  Block until Minecraft exits
  9.  Stop heartbeat
  10. Pack & upload world, commit, release lock

All external dependencies (launcher, sniffer, download/upload) are imported
at the module level so tests can patch them at 'client.host_session.*'.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

from client.api_client import APIClient, APIError
from client.game_launcher import launch_tlauncher, wait_for_exit, wait_for_minecraft
from client.heartbeat import HeartbeatThread
from client.lan_sniffer import get_tailscale_ip, sniff_port
from client.world_sync import _load_session, cmd_download, cmd_upload

LAN_SNIFF_TIMEOUT = 120   # seconds to wait for the LAN announcement


def run_host_session(
    api_url: str,
    token: str,
    world_dir: Path,
    *,
    tlauncher_path: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> None:
    """
    Run a complete host session.  Blocks until the world is uploaded and the
    lock is released.  Calls sys.exit(1) on unrecoverable errors.
    """
    api = APIClient(api_url, token)
    lock_lost = False

    def on_lock_lost() -> None:
        nonlocal lock_lost
        lock_lost = True
        log("⚠️  Lock lost! Another player may become host. Save your game NOW.")

    # ── 1 & 2.  Download world (also acquires lock internally) ─────────────
    log("[host] Acquiring lock and downloading world…")
    try:
        cmd_download(api_url, token, world_dir)
    except SystemExit:
        log("[host] Could not acquire lock or download world. Aborting.")
        raise

    owner_token = _load_session(world_dir)
    if owner_token is None:
        log("[host] No session token found after download. Aborting.")
        sys.exit(1)

    # ── 3.  Launch TLauncher ───────────────────────────────────────────────
    log("[host] Launching TLauncher…")
    launch_ts = time.time()
    try:
        launch_tlauncher(tlauncher_path)
    except FileNotFoundError as exc:
        log(f"[host] {exc}")
        sys.exit(1)
    log("[host] TLauncher started. Load your world and open it to LAN.")

    # ── 4.  Wait for Minecraft process ────────────────────────────────────
    log("[host] Waiting for Minecraft (javaw) to start…")
    try:
        mc_proc = wait_for_minecraft(launched_after=launch_ts)
        log(f"[host] Minecraft detected (PID {mc_proc.pid}).")
    except TimeoutError as exc:
        log(f"[host] {exc}")
        sys.exit(1)

    # ── 5.  Sniff LAN port ────────────────────────────────────────────────
    log("[host] Waiting for LAN announcement (open world to LAN in-game)…")
    port = sniff_port(timeout=LAN_SNIFF_TIMEOUT)
    if port is None:
        log("[host] Port sniffing timed out.")
        raw = input("[host] Enter port manually: ").strip()
        try:
            port = int(raw)
        except ValueError:
            log("[host] Invalid port. Aborting.")
            sys.exit(1)
    log(f"[host] LAN port: {port}")

    # ── 6.  Post host address ─────────────────────────────────────────────
    ts_ip = get_tailscale_ip()
    if ts_ip is None:
        log("[host] WARNING: Tailscale IP not found. Guests cannot connect.")
        ts_ip = "unknown"
    try:
        api.set_host_address(owner_token, ts_ip, port)
        log(f"[host] Host address posted: {ts_ip}:{port}")
    except APIError as exc:
        log(f"[host] Warning – could not post host address: {exc}")

    # ── 7.  Start heartbeat ───────────────────────────────────────────────
    hb = HeartbeatThread(
        heartbeat_fn=api.heartbeat,
        owner_token=owner_token,
        on_lock_lost=on_lock_lost,
    )
    hb.start()
    log("[host] Heartbeat running. Playing…")

    # ── 8.  Wait for Minecraft to exit ────────────────────────────────────
    wait_for_exit(mc_proc)
    log("[host] Minecraft closed.")

    # ── 9.  Stop heartbeat ────────────────────────────────────────────────
    hb.stop()
    hb.join(timeout=5)

    if lock_lost:
        log("[host] Lock was lost during the session. Attempting upload anyway…")

    # ── 10.  Upload world, commit, release ────────────────────────────────
    log("[host] Uploading world to cloud…")
    try:
        cmd_upload(api_url, token, world_dir)
    except SystemExit:
        log("[host] Upload failed. Your local copy is preserved.")
        log(
            f"[host] Retry: python -m client.world_sync upload "
            f"--api-url {api_url} --world-dir {world_dir}"
        )
        raise

    log("[host] Session complete! World saved to cloud.")
