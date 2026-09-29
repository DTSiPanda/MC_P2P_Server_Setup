"""
host_session.py – orchestrates the full host flow with autosaving, version checks, and handoff warnings.

Step-by-step:
  0.  Check client compatibility (/config)
  1.  Acquire lock  →  get presigned download URL
  2.  Download & unpack world (with local conflict resolution)
  3.  Launch TLauncher
  4.  Wait for javaw.exe (Minecraft) to start
  5.  Wait for LAN announcement  →  parse port  (fallback: manual entry)
  6.  POST /host/address  with Tailscale IP + port
  7.  Start heartbeat & autosave threads
  8.  Block until Minecraft exits
  9.  Stop heartbeat & autosave
  10. Pack & upload world, commit, release lock
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

from client.api_client import APIClient, APIError, check_compatibility
from client.autosave import AutosaveThread
from client.game_launcher import launch_tlauncher, wait_for_exit, wait_for_minecraft
from client.heartbeat import HeartbeatThread
from client.lan_sniffer import get_tailscale_ip, sniff_port
from client.world_sync import (
    _load_session,
    cmd_download,
    cmd_upload,
    upload_and_commit_world,
)

LAN_SNIFF_TIMEOUT = 120   # seconds to wait for the LAN announcement


def run_host_session(
    api_url: str,
    token: str,
    world_dir: Path,
    *,
    client_version: str = "0.1.0",
    local_mc_version: Optional[str] = None,
    autosave_interval: float = 600,
    tlauncher_path: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> None:
    """
    Run a complete host session. Blocks until the world is uploaded and the
    lock is released. Calls sys.exit(1) on unrecoverable errors.
    """
    api = APIClient(api_url, token)
    lock_lost = False

    # ── 0.  Compatibility Check ──────────────────────────────────────────
    try:
        check_compatibility(api, current_client_version=client_version, local_mc_version=local_mc_version)
    except Exception as exc:
        log(f"[host] Version error: {exc}")
        sys.exit(1)

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

    # ── 7.  Start heartbeat & autosave ────────────────────────────────────
    hb = HeartbeatThread(
        heartbeat_fn=api.heartbeat,
        owner_token=owner_token,
        on_lock_lost=on_lock_lost,
    )
    hb.start()

    def do_autosave() -> None:
        if not lock_lost:
            try:
                upload_and_commit_world(api_url, owner_token, world_dir, release=False, log=log)
            except Exception as e:
                log(f"[host] Autosave warning: {e}")

    autosave = AutosaveThread(
        autosave_fn=do_autosave,
        interval=autosave_interval,
        log=log,
    )
    autosave.start()

    log("[host] Heartbeat & autosave running. Playing…")

    # ── 8.  Wait for Minecraft to exit ────────────────────────────────────
    wait_for_exit(mc_proc)
    log("[host] Minecraft closed.")

    # ── 9.  Stop heartbeat & autosave ─────────────────────────────────────
    autosave.stop()
    autosave.join(timeout=5)

    hb.stop()
    hb.join(timeout=5)

    if lock_lost:
        log("[host] Lock was lost during the session. Attempting upload anyway…")

    # ── 10.  Upload world, commit, release ────────────────────────────────
    log("[host] Uploading final world to cloud…")
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
