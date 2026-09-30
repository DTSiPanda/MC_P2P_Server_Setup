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
from client.lan_sniffer import (
    get_tailscale_ip,
    ensure_on_tailnet,
    restore_tailnet,
    sniff_port,
)
from client.player_sync import (
    detect_local_player_identity,
    finalize_world_after_host,
    prepare_world_for_host,
)
from client.world_sync import (
    _load_session,
    cmd_download,
    cmd_upload,
    upload_and_commit_world,
)

LAN_SNIFF_TIMEOUT = 180   # seconds to wait for the LAN announcement


def run_host_session(
    api_url: str,
    token: str,
    world_dir: Path,
    *,
    client_version: str = "1.2.0",
    local_mc_version: Optional[str] = None,
    autosave_interval: float = 600,
    tlauncher_path: Optional[Path] = None,
    log: Callable[[str], None] = print,
    ask_port_fn: Optional[Callable[[], Optional[int]]] = None,
    confirm_tailnet_switch_fn: Optional[Callable[[str, str], bool]] = None,
) -> None:
    """
    Run a complete host session. Blocks until the world is uploaded and the
    lock is released. Calls sys.exit(1) on unrecoverable errors.

    confirm_tailnet_switch_fn(current, expected) -> bool
        Called when the user's active Tailscale tailnet is not the app's
        shared tailnet. Return True to allow the switch, False to abort.
        If None, the switch happens silently without asking.
    """
    api = APIClient(api_url, token)
    lock_lost = False
    _original_tailnet: Optional[str] = None  # restored when session ends

    # ── 0a.  Tailnet pre-flight ───────────────────────────────────────────
    # The app operates on a single shared tailnet (the admin's).
    # If the player is on a different tailnet, we must switch them over
    # so the Tailscale IP they advertise is reachable by all guests.
    expected_tailnet = ""
    try:
        tailnet_info = api.get_tailnet_info()
        if isinstance(tailnet_info, dict):
            raw_val = tailnet_info.get("tailnet_name", "")
            if isinstance(raw_val, str):
                expected_tailnet = raw_val.strip()
    except Exception:
        expected_tailnet = ""

    if expected_tailnet:
        ok, _original_tailnet = ensure_on_tailnet(
            expected_tailnet,
            confirm_fn=confirm_tailnet_switch_fn,
            log=log,
        )
        if not ok:
            raise RuntimeError(
                "Tailnet switch required but was declined or failed. "
                f"Please switch Tailscale to '{expected_tailnet}' before hosting."
            )
    else:
        log("[host] /tailnet-info not configured on server — skipping tailnet check.")

    # ── 0b.  Compatibility Check ──────────────────────────────────────────
    try:
        check_compatibility(api, current_client_version=client_version, local_mc_version=local_mc_version)
    except Exception as exc:
        restore_tailnet(_original_tailnet, log=log)
        log(f"[host] Version error: {exc}")
        raise RuntimeError(f"Version error: {exc}")

    def on_lock_lost() -> None:
        nonlocal lock_lost
        lock_lost = True
        log("⚠️  Lock lost! Another player may become host. Save your game NOW.")

    # ── 1 & 2.  Download world (also acquires lock internally) ─────────────
    log("[host] Acquiring lock and downloading world…")
    try:
        cmd_download(api_url, token, world_dir, log=log)
    except Exception as exc:
        log(f"[host] Failed to acquire lock or download world: {exc}")
        raise

    owner_token = _load_session(world_dir)
    if owner_token is None:
        log("[host] No session token found after download. Aborting.")
        raise RuntimeError("No session token found after download. Aborting.")

    # ── 2b. Prepare world playerdata for host ────────────────────────────
    # In Singleplayer, Minecraft loads the host from level.dat (Data.Player).
    # We detect the local host's identity, preserve any previous host's items,
    # and inject this host's personal playerdata into level.dat.
    host_name, host_uuid = detect_local_player_identity()
    log(f"[host] Preparing player data for host '{host_name}' ({host_uuid})…")
    try:
        prepare_world_for_host(world_dir, host_uuid, log=log)
    except Exception as exc:
        log(f"[host] Warning – could not prepare player data: {exc}")

    # ── 3.  Launch TLauncher / Minecraft ──────────────────────────────────
    log("[host] Launching TLauncher / Minecraft…")
    launch_ts = time.time()
    try:
        launch_tlauncher(tlauncher_path)
        log("[host] Launcher started. Load your world and open it to LAN.")
    except FileNotFoundError as exc:
        log(f"[host] Note: {exc}")
        log("[host] If launcher didn't start, please start Minecraft manually and open your world to LAN...")

    # ── 4.  Wait for Minecraft process ────────────────────────────────────
    log("[host] Waiting for Minecraft to start…")
    try:
        mc_proc = wait_for_minecraft(launched_after=launch_ts)
        log(f"[host] Minecraft detected (PID {mc_proc.pid}).")
    except TimeoutError as exc:
        log(f"[host] {exc}")
        raise RuntimeError(str(exc))

    # ── 5.  Sniff LAN port ────────────────────────────────────────────────
    log("[host] Waiting for LAN announcement (open world to LAN in-game)…")
    port = sniff_port(timeout=LAN_SNIFF_TIMEOUT)
    if port is None:
        log("[host] LAN auto-detection timed out.")
        if ask_port_fn is not None:
            log("[host] Prompting for manual port entry...")
            port = ask_port_fn()
        else:
            try:
                raw = input("[host] Enter port manually: ").strip()
                port = int(raw)
            except Exception:
                port = None

    if port is None or not (1 <= port <= 65535):
        log("[host] No valid LAN port available. Aborting.")
        raise RuntimeError("LAN port sniffing timed out and no valid port was provided.")
    log(f"[host] LAN port: {port}")

    # ── 6.  Post host address ─────────────────────────────────────────────
    # Fetch the expected tailnet name from the server so we pick the IP
    # that belongs to the shared app tailnet (not the host's personal tailnet).
    # This fixes the case where a guest-turned-host has multiple Tailscale IPs
    # (one from their own tailnet, one from the shared tailnet) and psutil/CLI
    # would non-deterministically return the wrong one.
    expected_tailnet: Optional[str] = None
    try:
        tailnet_info = api.get_tailnet_info()
        expected_tailnet = tailnet_info.get("tailnet_name") or None
        if expected_tailnet:
            log(f"[host] Tailnet filter: {expected_tailnet}")
    except Exception as exc:
        log(f"[host] Could not fetch tailnet info (ignored): {exc}")

    ts_ip = get_tailscale_ip(expected_tailnet=expected_tailnet)
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
                finalize_world_after_host(world_dir, host_uuid, log=log)
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

    # ── 9b. Finalize host playerdata mirror ──────────────────────────────
    try:
        finalize_world_after_host(world_dir, host_uuid, log=log)
    except Exception as exc:
        log(f"[host] Warning – could not mirror host player data: {exc}")

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
        restore_tailnet(_original_tailnet, log=log)
        raise

    log("[host] Session complete! World saved to cloud.")

    # ── 11.  Restore original Tailscale tailnet ───────────────────────────
    restore_tailnet(_original_tailnet, log=log)
