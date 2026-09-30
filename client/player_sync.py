"""
player_sync.py – Seamless and foolproof inventory & playerdata synchronization
for rotating-host Minecraft P2P worlds.

Minecraft Java Edition Singleplayer Storage:
- When a world is played in Singleplayer (or opened to LAN by the host), the local
  host's player data (inventory, ender chest, XP, health, coordinates) is saved
  inside level.dat under the 'Data.Player' NBT compound tag.
- When remote guests join via LAN, the integrated server saves each remote player
  into 'playerdata/<UUID>.dat'.

The Challenge:
- When the host rotates, a player who was previously a guest now opens the world in
  Singleplayer. Without adjustment, Minecraft loads the previous host's items from
  level.dat, completely ignoring the new host's existing playerdata file.

The Solution:
1. prepare_world_for_host(world_dir, host_uuid):
   - Runs BEFORE Minecraft launches.
   - Detects whose data is in level.dat. If it belongs to a previous host, copies it
     into playerdata/<prev_uuid>.dat to preserve it.
   - Injects the current host's data from playerdata/<host_uuid>.dat into level.dat.
   - If the current host is brand new, clears Data.Player so Minecraft cleanly
     spawns them fresh with an empty inventory.
   - Uses atomic writes (.tmp -> os.replace) and read-back verification.

2. finalize_world_after_host(world_dir, host_uuid):
   - Runs AFTER Minecraft exits.
   - Reads the final level.dat (Data.Player) and mirrors it into
     playerdata/<host_uuid>.dat, ensuring the world zip contains complete and
     up-to-date playerdata for all players.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
import uuid
from pathlib import Path
from typing import Any, Optional

import nbtlib
from nbtlib.tag import Compound, Int, IntArray, Long, Short, String

logger = logging.getLogger("minecraft_p2p")


def offline_uuid(username: str) -> str:
    """
    Compute standard Minecraft Java Edition offline player UUID.
    Matches Java: UUID.nameUUIDFromBytes(("OfflinePlayer:" + username).getBytes(UTF_8))
    """
    clean_name = username.strip()
    m = bytearray(hashlib.md5(f"OfflinePlayer:{clean_name}".encode("utf-8")).digest())
    m[6] = (m[6] & 0x0F) | 0x30  # IETF Version 3 (MD5-based)
    m[8] = (m[8] & 0x3F) | 0x80  # IETF Variant
    return str(uuid.UUID(bytes=bytes(m)))


def uuid_to_ints(uuid_val: str | uuid.UUID) -> list[int]:
    """
    Convert a UUID string or UUID object into 4 signed 32-bit big-endian integers,
    matching Minecraft 1.16+ IntArray representation.
    """
    if isinstance(uuid_val, str):
        clean_hex = uuid_val.replace("-", "").strip()
        u = uuid.UUID(hex=clean_hex)
    else:
        u = uuid_val
    return list(struct.unpack(">4i", u.bytes))


def ints_to_uuid(ints: list[int] | tuple[int, ...]) -> str:
    """
    Convert 4 signed 32-bit big-endian integers back into a standard UUID string.
    """
    if len(ints) != 4:
        raise ValueError(f"Expected 4 integers for UUID array, got {len(ints)}")
    raw_bytes = struct.pack(">4i", *ints)
    return str(uuid.UUID(bytes=raw_bytes))


def extract_player_uuid_from_compound(player_tag: Any) -> Optional[str]:
    """
    Extract UUID string from an NBT Player compound tag.
    Supports Minecraft 1.16+ IntArray 'UUID', legacy 'UUIDMost'/'UUIDLeast',
    and string 'UUID'.
    """
    if not isinstance(player_tag, (dict, Compound)):
        return None

    # 1. Modern Minecraft 1.16+ (IntArray of 4 ints)
    if "UUID" in player_tag:
        raw_uuid = player_tag["UUID"]
        if isinstance(raw_uuid, (list, IntArray)) and len(raw_uuid) == 4:
            try:
                return ints_to_uuid([int(x) for x in raw_uuid])
            except Exception:
                pass
        elif isinstance(raw_uuid, str):
            try:
                return str(uuid.UUID(raw_uuid))
            except Exception:
                pass

    # 2. Legacy Minecraft (< 1.16: UUIDMost and UUIDLeast longs)
    if "UUIDMost" in player_tag and "UUIDLeast" in player_tag:
        try:
            most = int(player_tag["UUIDMost"])
            least = int(player_tag["UUIDLeast"])
            raw_bytes = struct.pack(">qq", most, least)
            return str(uuid.UUID(bytes=raw_bytes))
        except Exception:
            pass

    return None


def detect_local_player_identity(
    suggested_username: Optional[str] = None,
) -> tuple[str, str]:
    """
    Detect the local player's username and UUID using available launcher files
    and caches.

    Returns:
        (username, uuid_str)
    """
    appdata_str = os.environ.get("APPDATA")
    mc_dir = Path(appdata_str) / ".minecraft" if appdata_str else None

    # 1. Check TLauncher profiles (TlauncherProfiles.json)
    if mc_dir and (mc_dir / "TlauncherProfiles.json").exists():
        try:
            p_file = mc_dir / "TlauncherProfiles.json"
            data = json.loads(p_file.read_text(encoding="utf-8", errors="ignore"))
            accounts = data.get("accounts", {})
            if isinstance(accounts, dict) and accounts:
                # Prefer selectedAccountUUID if valid, otherwise first account
                sel = data.get("selectedAccountUUID")
                account = accounts.get(sel) if sel else None
                if not account:
                    account = next(iter(accounts.values()), None)

                if account and isinstance(account, dict):
                    name = account.get("username") or account.get("displayName")
                    raw_id = account.get("uuid")
                    if name and raw_id:
                        try:
                            clean_uuid = str(uuid.UUID(hex=raw_id.replace("-", "")))
                            return str(name), clean_uuid
                        except Exception:
                            pass
        except Exception as exc:
            logger.debug(f"[player_sync] Could not read TlauncherProfiles.json: {exc}")

    # 2. Check official launcher accounts (launcher_accounts.json)
    if mc_dir and (mc_dir / "launcher_accounts.json").exists():
        try:
            p_file = mc_dir / "launcher_accounts.json"
            data = json.loads(p_file.read_text(encoding="utf-8", errors="ignore"))
            accounts = data.get("accounts", {})
            sel = data.get("activeAccountLocalId")
            account = accounts.get(sel) if sel else None
            if not account and accounts:
                account = next(iter(accounts.values()), None)

            if account and isinstance(account, dict):
                prof = account.get("minecraftProfile", {})
                name = prof.get("name")
                raw_id = prof.get("id")
                if name and raw_id:
                    try:
                        clean_uuid = str(uuid.UUID(hex=raw_id.replace("-", "")))
                        return str(name), clean_uuid
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug(f"[player_sync] Could not read launcher_accounts.json: {exc}")

    # 3. If a suggested username was provided, check usercache.json
    target_name = (suggested_username or "").strip()
    if target_name and mc_dir and (mc_dir / "usercache.json").exists():
        try:
            uc_file = mc_dir / "usercache.json"
            entries = json.loads(uc_file.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(entries, list):
                for entry in entries:
                    if str(entry.get("name", "")).lower() == target_name.lower():
                        raw_id = entry.get("uuid")
                        if raw_id:
                            return target_name, str(uuid.UUID(raw_id))
        except Exception as exc:
            logger.debug(f"[player_sync] usercache lookup failed: {exc}")

    # 4. Fallback: Offline UUID derived from name (or default 'Player')
    resolved_name = target_name or "Player"
    return resolved_name, offline_uuid(resolved_name)


def prepare_world_for_host(
    world_dir: Path,
    host_uuid: str,
    log=print,
) -> bool:
    """
    Prepare the world before Minecraft launches:
    1. Read level.dat -> Data.Player.
    2. If Data.Player belongs to another player (previous host), save it
       into playerdata/<prev_uuid>.dat.
    3. If playerdata/<host_uuid>.dat exists, inject it into level.dat (Data.Player).
    4. If no playerdata exists for host_uuid, remove Data.Player so Minecraft
       cleanly performs a fresh spawn without inheriting the previous host's items.
    5. Save level.dat atomically with verification and rollback.

    Returns True on success or if no change needed; raises or returns False on failure.
    """
    world_dir = Path(world_dir).resolve()
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        log(f"[player_sync] Notice: {level_dat} does not exist yet (brand new world).")
        return True

    clean_host_uuid = str(uuid.UUID(host_uuid.replace("-", ""))).lower()
    playerdata_dir = world_dir / "playerdata"
    playerdata_dir.mkdir(parents=True, exist_ok=True)

    backup_file = world_dir / "level.dat_p2p_backup"
    try:
        shutil.copy2(level_dat, backup_file)
    except Exception as exc:
        log(f"[player_sync] Warning: Could not create safety backup of level.dat: {exc}")

    try:
        nbt = nbtlib.load(str(level_dat))
        data_tag = nbt.get("Data")
        if not isinstance(data_tag, (dict, Compound)):
            log("[player_sync] Warning: 'Data' tag missing in level.dat. Skipping sync.")
            return True

        current_player = data_tag.get("Player")
        current_uuid = extract_player_uuid_from_compound(current_player)

        # Check if the level.dat already belongs to this host
        if current_uuid and current_uuid.lower() == clean_host_uuid:
            log(f"[player_sync] Host UUID matches level.dat ({clean_host_uuid}). Ready.")
            return True

        # If level.dat has data from a previous host, preserve it into their .dat file
        if current_player and current_uuid:
            prev_file = playerdata_dir / f"{current_uuid.lower()}.dat"
            log(f"[player_sync] Preserving previous host items -> playerdata/{prev_file.name}")
            try:
                prev_nbt = nbtlib.File({k: v for k, v in current_player.items()})
                tmp_prev = prev_file.with_suffix(".dat.tmp")
                prev_nbt.save(str(tmp_prev), gzipped=True)
                os.replace(tmp_prev, prev_file)
            except Exception as exc:
                log(f"[player_sync] Warning: Could not backup previous host playerdata: {exc}")

        # Now load current host's data from playerdata/<clean_host_uuid>.dat
        target_pd = playerdata_dir / f"{clean_host_uuid}.dat"
        if target_pd.exists():
            log(f"[player_sync] Injecting playerdata/{target_pd.name} into level.dat for host.")
            pd_nbt = nbtlib.load(str(target_pd))
            new_player_compound = Compound({k: v for k, v in pd_nbt.items()})
            # Ensure UUID int array is correctly stamped
            ints = uuid_to_ints(clean_host_uuid)
            new_player_compound["UUID"] = IntArray([Int(x) for x in ints])
            data_tag["Player"] = new_player_compound
        else:
            log(f"[player_sync] New host ({clean_host_uuid}) has no prior inventory; resetting level.dat Player for fresh spawn.")
            if "Player" in data_tag:
                del data_tag["Player"]

        # Atomic save
        tmp_level = level_dat.with_suffix(".dat.tmp")
        nbt.save(str(tmp_level), gzipped=True)
        os.replace(tmp_level, level_dat)

        # Verification check: ensure file can be loaded back cleanly
        verified = nbtlib.load(str(level_dat))
        if "Data" not in verified:
            raise RuntimeError("Verification failed: 'Data' tag missing after write.")

        # Clean up backup upon verified success
        if backup_file.exists():
            try:
                backup_file.unlink()
            except Exception:
                pass

        log(f"[player_sync] World prepared successfully for host {clean_host_uuid}.")
        return True

    except Exception as exc:
        log(f"[player_sync] ERROR: Failed preparing world for host: {exc}")
        if backup_file.exists():
            log("[player_sync] Restoring level.dat from safety backup...")
            try:
                shutil.copy2(backup_file, level_dat)
                backup_file.unlink()
            except Exception as restore_err:
                log(f"[player_sync] Critical: Could not restore backup: {restore_err}")
        raise


def finalize_world_after_host(
    world_dir: Path,
    host_uuid: str,
    log=print,
) -> bool:
    """
    Run after Minecraft exits and session.lock is released.
    Reads level.dat (Data.Player) and mirrors it into playerdata/<host_uuid>.dat.
    This guarantees that the world zip uploaded to R2 contains an up-to-date
    .dat file for the host as well as all guests.
    """
    world_dir = Path(world_dir).resolve()
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        return True

    clean_host_uuid = str(uuid.UUID(host_uuid.replace("-", ""))).lower()
    playerdata_dir = world_dir / "playerdata"
    playerdata_dir.mkdir(parents=True, exist_ok=True)

    try:
        nbt = nbtlib.load(str(level_dat))
        data_tag = nbt.get("Data")
        if not isinstance(data_tag, (dict, Compound)):
            return False

        player_compound = data_tag.get("Player")
        if not player_compound:
            return True

        target_pd = playerdata_dir / f"{clean_host_uuid}.dat"
        tmp_pd = target_pd.with_suffix(".dat.tmp")

        pd_nbt = nbtlib.File({k: v for k, v in player_compound.items()})
        # Ensure UUID in file matches
        ints = uuid_to_ints(clean_host_uuid)
        pd_nbt["UUID"] = IntArray([Int(x) for x in ints])

        pd_nbt.save(str(tmp_pd), gzipped=True)
        os.replace(tmp_pd, target_pd)

        log(f"[player_sync] Mirrored host state into playerdata/{target_pd.name}")
        return True
    except Exception as exc:
        log(f"[player_sync] Warning: Could not mirror level.dat to playerdata: {exc}")
        return False
