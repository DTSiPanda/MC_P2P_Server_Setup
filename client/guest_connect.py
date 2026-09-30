"""
guest_connect.py – Phase 4 guest flow.

Flow:
  1. GET /host  →  ip:port (or abort if nobody is hosting)
  2. Find Minecraft's servers.dat (TLauncher or standard launcher)
  3. Write / update the "OurWorld (P2P)" entry at the top of the server list
  4. Tell the user to open Multiplayer — the server will already be listed

Fallback: if servers.dat cannot be written, print the address clearly so the
user can add it manually with one copy-paste.

Pass criterion (plan.md Phase 4):
  "A second PC joins with no manual typing (or one copy-paste)."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import nbtlib

from client.api_client import APIClient, APIError

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

SERVER_NAME = "OurWorld (P2P)"

_APPDATA = Path(os.environ.get("APPDATA", Path.home()))

# Ordered list of common Minecraft data directories on Windows
MINECRAFT_DIR_CANDIDATES: list[Path] = [
    _APPDATA / ".tlauncher" / "minecraft",
    _APPDATA / "TLauncher" / "minecraft",
    _APPDATA / ".minecraft",
]


# ------------------------------------------------------------------
# servers.dat helpers
# ------------------------------------------------------------------


def find_servers_dat() -> Optional[Path]:
    """
    Return the path to Minecraft's servers.dat if found, else None.
    Searches TLauncher locations first, then the standard launcher.
    """
    for directory in MINECRAFT_DIR_CANDIDATES:
        candidate = directory / "servers.dat"
        if candidate.exists():
            return candidate
    # If none exists yet, return the preferred write location
    # (TLauncher) so we can create the file there.
    for directory in MINECRAFT_DIR_CANDIDATES:
        if directory.exists():
            return directory / "servers.dat"
    return None


def write_server_entry(servers_dat: Path, ip: str, port: int) -> None:
    """
    Insert or update the "OurWorld (P2P)" entry in servers.dat.

    - If the file exists, existing entries are preserved; our entry is
      placed first so it appears at the top of the Multiplayer list.
    - If the file does not exist, it is created.
    - If an entry with the same name already exists, it is updated
      in-place (no duplicates).

    Minecraft Java Edition stores servers.dat as uncompressed NBT.
    """
    address = f"{ip}:{port}"

    if servers_dat.exists():
        try:
            nbt_file = nbtlib.load(str(servers_dat), gzipped=False)
        except Exception:
            # Fallback: try gzipped (some launcher versions gzip it)
            nbt_file = nbtlib.load(str(servers_dat), gzipped=True)
        existing = list(nbt_file.get("servers", []))
    else:
        servers_dat.parent.mkdir(parents=True, exist_ok=True)
        nbt_file = nbtlib.File()
        existing = []

    # Build the new entry
    entry = nbtlib.Compound({
        "name": nbtlib.String(SERVER_NAME),
        "ip": nbtlib.String(address),
        "acceptTextures": nbtlib.Byte(1),
    })

    # Deduplicate: keep everything except a prior entry with our name
    kept = [s for s in existing if str(s.get("name", "")) != SERVER_NAME]

    # Prepend our entry so it appears first in the Multiplayer list
    new_list = nbtlib.List[nbtlib.Compound]([entry] + kept)

    nbt_file["servers"] = new_list
    nbt_file.save(str(servers_dat), gzipped=False)


# ------------------------------------------------------------------
# CLI command
# ------------------------------------------------------------------


from client.modpack_manager import get_modpack_servers_dat_candidates


def cmd_join(
    api_url: str,
    token: str,
    servers_dat: Optional[Path] = None,
    log=print,
) -> None:
    """
    Full guest join flow.

    1. Fetch /host from the API.
    2. Write the server entry into all detected servers.dat files.
    3. Copy the address to the clipboard for instant Direct Connect if game is open.
    4. Print clear instructions.

    Accepts an optional `servers_dat` path override (used in tests).
    """
    api = APIClient(api_url, token)

    log("[guest] Checking who is hosting…")
    try:
        host_info = api.get_host()
    except Exception as exc:
        log(f"[guest] Could not reach API: {exc}")
        sys.exit(1)

    if not host_info.get("hosting"):
        log("[guest] Nobody is hosting right now. Ask someone to start a host session first.")
        sys.exit(0)

    ip: str = host_info["ip"]
    port: int = host_info["port"]
    host_name: str = host_info.get("host_name", "someone")
    address = f"{ip}:{port}"

    log(f"[guest] {host_name} is hosting at {address}")

    # Auto-copy to clipboard so user can instantly Direct Connect -> Ctrl+V if Minecraft is already open
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(address)
        r.update()
        r.destroy()
        log(f"[guest] 📋 Address {address} copied to clipboard!")
    except Exception:
        pass

    # Try to auto-write all detected servers.dat files
    targets = [servers_dat] if servers_dat else get_modpack_servers_dat_candidates()
    written_count = 0
    for dat_path in targets:
        try:
            write_server_entry(dat_path, ip, port)
            written_count += 1
        except Exception:
            pass

    if written_count > 0:
        log(f"[guest] Updated 'OurWorld (P2P)' in {written_count} server list(s).")
        log("[guest] In Minecraft Multiplayer:")
        log("[guest] ➜ Click 'OurWorld (P2P)' at the top of your list to join!")
        log("[guest] ➜ Or click 'Direct Connection' and press Ctrl+V (already copied!).")
    else:
        log(f"[guest] Address copied to clipboard: {address}")
        log("[guest] In Minecraft: Click Multiplayer ➔ Direct Connection ➔ Ctrl+V to join!")


# ------------------------------------------------------------------
# Entry point (python -m client.guest_connect)
# ------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Join the P2P Minecraft server")
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--token",
        default=os.getenv("PLAYER_TOKEN"),
        help="Player API token (or set PLAYER_TOKEN env var)",
    )
    args = parser.parse_args()

    if not args.token:
        print("ERROR: No API token. Pass --token or set PLAYER_TOKEN.", file=sys.stderr)
        sys.exit(1)

    cmd_join(args.api_url, args.token)


if __name__ == "__main__":
    main()
