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


def get_playerdata_dirs(world_dir: Path) -> list[Path]:
    """
    Return all directories used for playerdata storage in this world:
    - world_dir / 'players' / 'data' (common in modpacks / server ports)
    - world_dir / 'playerdata' (vanilla Java edition)
    """
    world_dir = Path(world_dir).resolve()
    found: list[Path] = []

    # Check modpack path: players/data
    mp_dir = world_dir / "players" / "data"
    if mp_dir.exists():
        found.append(mp_dir)

    # Check vanilla path: playerdata
    vanilla_dir = world_dir / "playerdata"
    if vanilla_dir.exists():
        found.append(vanilla_dir)

    # If neither exists, default to vanilla and create it
    if not found:
        vanilla_dir.mkdir(parents=True, exist_ok=True)
        found.append(vanilla_dir)

    return found


def find_playerdata_file(world_dir: Path, uuid_str: str) -> Optional[Path]:
    """Search all playerdata directories for a player's .dat file."""
    clean_uuid = str(uuid.UUID(uuid_str.replace("-", ""))).lower()
    for d in get_playerdata_dirs(world_dir):
        f = d / f"{clean_uuid}.dat"
        if f.exists():
            return f
    return None


KNOWN_PLAYER_UUID_PAIRS: dict[str, str] = {
    # DTSiPanda (Tejas): Host UUID <-> Guest UUID
    "34208dac-aa74-42d4-aa9e-87bd7718d849": "d1ae6bd2-27f0-387f-998f-1bb2b35f9dfa",
    # bellisarius (Atharva): Host UUID <-> Guest UUID
    "ad385985-99b9-4e6d-9046-a4f8e09319a5": "5897a92d-648c-368b-94ec-f619ce17eb2c",
}


def _sync_dat_pair(file_a: Path, uuid_a: str, file_b: Path, uuid_b: str, log=print) -> None:
    if not file_a.exists() and not file_b.exists():
        return
    if file_a.exists() and not file_b.exists():
        src, src_id, dst, dst_id = file_a, uuid_a, file_b, uuid_b
    elif file_b.exists() and not file_a.exists():
        src, src_id, dst, dst_id = file_b, uuid_b, file_a, uuid_a
    else:
        if file_a.stat().st_mtime >= file_b.stat().st_mtime:
            src, src_id, dst, dst_id = file_a, uuid_a, file_b, uuid_b
        else:
            src, src_id, dst, dst_id = file_b, uuid_b, file_a, uuid_a

    try:
        nbt = nbtlib.load(str(src))
        ints = uuid_to_ints(dst_id)
        nbt["UUID"] = IntArray([Int(x) for x in ints])
        tmp_dst = dst.with_suffix(".dat.tmp")
        dst.parent.mkdir(parents=True, exist_ok=True)
        nbt.save(str(tmp_dst), gzipped=True)
        os.replace(tmp_dst, dst)
        log(f"[player_sync] Synced .dat: {src.name} -> {dst.name}")
    except Exception as exc:
        log(f"[player_sync] Warning: Could not sync .dat pair ({src} -> {dst}): {exc}")


def _sync_json_pair(file_a: Path, file_b: Path, log=print) -> None:
    if not file_a.exists() and not file_b.exists():
        return
    if file_a.exists() and not file_b.exists():
        src, dst = file_a, file_b
    elif file_b.exists() and not file_a.exists():
        src, dst = file_b, file_a
    else:
        if file_a.stat().st_mtime >= file_b.stat().st_mtime:
            src, dst = file_a, file_b
        else:
            src, dst = file_b, file_a

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"[player_sync] Synced json: {src.name} -> {dst.name}")
    except Exception as exc:
        log(f"[player_sync] Warning: Could not sync json pair: {exc}")


def sync_player_pair(world_dir: Path, uuid_a: str, uuid_b: str, log=print) -> None:
    """
    Bidirectionally sync player data, advancements, and stats between two UUIDs
    representing the same human player (e.g. TLauncher host UUID vs offline LAN guest UUID).
    """
    clean_a = str(uuid.UUID(uuid_a.replace("-", ""))).lower()
    clean_b = str(uuid.UUID(uuid_b.replace("-", ""))).lower()
    if clean_a == clean_b:
        return

    world_dir = Path(world_dir).resolve()

    # 1. Sync .dat files in all playerdata directories
    for p_dir in get_playerdata_dirs(world_dir):
        file_a = p_dir / f"{clean_a}.dat"
        file_b = p_dir / f"{clean_b}.dat"
        _sync_dat_pair(file_a, clean_a, file_b, clean_b, log)

    # 2. Sync advancements in players/advancements and advancements
    for adv_parent in [world_dir / "players" / "advancements", world_dir / "advancements"]:
        if adv_parent.exists():
            file_a = adv_parent / f"{clean_a}.json"
            file_b = adv_parent / f"{clean_b}.json"
            _sync_json_pair(file_a, file_b, log)

    # 3. Sync stats in players/stats and stats
    for stat_parent in [world_dir / "players" / "stats", world_dir / "stats"]:
        if stat_parent.exists():
            file_a = stat_parent / f"{clean_a}.json"
            file_b = stat_parent / f"{clean_b}.json"
            _sync_json_pair(file_a, file_b, log)


IDENTITY_REGISTRY_FILE = "p2p_player_identities.json"


def load_identity_registry(world_dir: Path) -> dict[str, dict[str, str]]:
    """
    Load the shared identity registry from world_dir / p2p_player_identities.json.
    Returns: { "username": { "host_uuid": "...", "guest_uuid": "..." } }
    """
    reg_path = Path(world_dir) / IDENTITY_REGISTRY_FILE
    if not reg_path.exists():
        return {}
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("players", {})
    except Exception as exc:
        logger.debug(f"[player_sync] Could not read identity registry: {exc}")
    return {}


def register_player_identity(
    world_dir: Path,
    username: str,
    host_uuid: str,
    guest_uuid: str,
    log=print,
) -> None:
    """
    Record or update a player's (host_uuid, guest_uuid) pairing in the world's
    p2p_player_identities.json registry file.
    """
    world_dir = Path(world_dir).resolve()
    reg_path = world_dir / IDENTITY_REGISTRY_FILE

    clean_host = str(uuid.UUID(host_uuid.replace("-", ""))).lower()
    clean_guest = str(uuid.UUID(guest_uuid.replace("-", ""))).lower()

    registry = load_identity_registry(world_dir)
    existing = registry.get(username, {})
    if existing.get("host_uuid") == clean_host and existing.get("guest_uuid") == clean_guest:
        return

    registry[username] = {
        "host_uuid": clean_host,
        "guest_uuid": clean_guest,
    }

    try:
        tmp_path = reg_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps({"players": registry}, indent=2), encoding="utf-8")
        os.replace(tmp_path, reg_path)
        log(f"[player_sync] Registered identity for '{username}': Host={clean_host} ⟷ Guest={clean_guest}")
    except Exception as exc:
        log(f"[player_sync] Warning: Could not save identity registry: {exc}")


def discover_and_sync_all_player_pairs(world_dir: Path, log=print) -> None:
    """
    Discover all player pairs (from registry, local launcher profiles, and usercache)
    and sync their data.
    """
    pairs: dict[str, str] = dict(KNOWN_PLAYER_UUID_PAIRS)

    # 1. Register and include the local host identity
    try:
        name, local_uuid = detect_local_player_identity()
        if name and local_uuid:
            off_id = offline_uuid(name).lower()
            c_local = str(uuid.UUID(local_uuid.replace("-", ""))).lower()
            register_player_identity(world_dir, name, c_local, off_id, log=log)
            pairs[c_local] = off_id
    except Exception:
        pass

    # 2. Load from the world's shared identity registry
    registry = load_identity_registry(world_dir)
    for uname, pinfo in registry.items():
        h_id = pinfo.get("host_uuid")
        g_id = pinfo.get("guest_uuid")
        if h_id and g_id:
            pairs[h_id.lower()] = g_id.lower()

    # 3. Inspect usercache.json for any other players
    appdata_str = os.environ.get("APPDATA")
    mc_dir = Path(appdata_str) / ".minecraft" if appdata_str else None
    if mc_dir and (mc_dir / "usercache.json").exists():
        try:
            uc_entries = json.loads((mc_dir / "usercache.json").read_text(encoding="utf-8", errors="ignore"))
            if isinstance(uc_entries, list):
                for entry in uc_entries:
                    uname = entry.get("name")
                    raw_id = entry.get("uuid")
                    if uname and raw_id:
                        off_id = offline_uuid(uname).lower()
                        c_id = str(uuid.UUID(raw_id)).lower()
                        if off_id != c_id:
                            pairs[c_id] = off_id
        except Exception:
            pass

    for u_a, u_b in pairs.items():
        sync_player_pair(world_dir, u_a, u_b, log=log)


def prepare_world_for_host(
    world_dir: Path,
    host_uuid: str,
    log=print,
) -> bool:
    """
    Prepare the world before Minecraft launches:
    1. For Minecraft 26.1.2+:
       - Update singleplayer_uuid in level.dat to host_uuid.
       - Sync all player pairs (Host UUID <-> Guest UUID) in players/data, advancements, stats.
    2. For pre-26.1.2:
       - Preserve previous host items from level.dat (Data.Player) to their .dat file.
       - Inject current host items into level.dat (Data.Player).
    3. Save level.dat atomically with verification and rollback.
    """
    world_dir = Path(world_dir).resolve()
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        log(f"[player_sync] Notice: {level_dat} does not exist yet (brand new world).")
        return True

    clean_host_uuid = str(uuid.UUID(host_uuid.replace("-", ""))).lower()

    # Pre-sync all player pairs
    discover_and_sync_all_player_pairs(world_dir, log=log)

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

        # ── 1. Minecraft 26.1.2+ Support (singleplayer_uuid) ───────────────
        if "singleplayer_uuid" in data_tag:
            current_raw = data_tag.get("singleplayer_uuid")
            current_sp_uuid = None
            if isinstance(current_raw, (list, IntArray)) and len(current_raw) == 4:
                try:
                    current_sp_uuid = ints_to_uuid([int(x) for x in current_raw]).lower()
                except Exception:
                    pass

            if current_sp_uuid != clean_host_uuid:
                log(f"[player_sync] Setting 26.1.2 singleplayer_uuid: {current_sp_uuid} -> {clean_host_uuid}")
                ints = uuid_to_ints(clean_host_uuid)
                data_tag["singleplayer_uuid"] = IntArray([Int(x) for x in ints])
            else:
                log(f"[player_sync] 26.1.2 singleplayer_uuid matches host ({clean_host_uuid}).")

        # ── 2. Pre-26.1.2 Support (level.dat Data.Player) ────────────────────
        current_player = data_tag.get("Player")
        current_uuid = extract_player_uuid_from_compound(current_player)

        if current_player is not None:
            if current_uuid and current_uuid.lower() == clean_host_uuid:
                log(f"[player_sync] Host UUID matches level.dat Player ({clean_host_uuid}).")
            else:
                # Preserve previous host
                if current_uuid:
                    prev_uuid_str = current_uuid.lower()
                    prev_nbt = nbtlib.File({k: v for k, v in current_player.items()})
                    for p_dir in get_playerdata_dirs(world_dir):
                        prev_file = p_dir / f"{prev_uuid_str}.dat"
                        log(f"[player_sync] Preserving previous host items -> {p_dir.name}/{prev_file.name}")
                        try:
                            tmp_prev = prev_file.with_suffix(".dat.tmp")
                            prev_nbt.save(str(tmp_prev), gzipped=True)
                            os.replace(tmp_prev, prev_file)
                        except Exception as exc:
                            log(f"[player_sync] Warning: Could not backup previous host playerdata in {p_dir}: {exc}")

                # Inject current host
                target_pd = find_playerdata_file(world_dir, clean_host_uuid)
                if target_pd and target_pd.exists():
                    log(f"[player_sync] Injecting {target_pd.parent.name}/{target_pd.name} into level.dat for host.")
                    pd_nbt = nbtlib.load(str(target_pd))
                    new_player_compound = Compound({k: v for k, v in pd_nbt.items()})
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

        # Verification check
        verified = nbtlib.load(str(level_dat))
        if "Data" not in verified:
            raise RuntimeError("Verification failed: 'Data' tag missing after write.")

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
    1. Syncs all dual-UUID player pairs (Host UUID <-> Guest UUID) in players/data, advancements, stats.
    2. If level.dat has Data.Player, mirrors it to playerdata/<host_uuid>.dat and players/data/<host_uuid>.dat.
    """
    world_dir = Path(world_dir).resolve()
    clean_host_uuid = str(uuid.UUID(host_uuid.replace("-", ""))).lower()

    # Sync all player pairs (data, advancements, stats)
    discover_and_sync_all_player_pairs(world_dir, log=log)

    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        return True

    try:
        nbt = nbtlib.load(str(level_dat))
        data_tag = nbt.get("Data")
        if not isinstance(data_tag, (dict, Compound)):
            return False

        player_compound = data_tag.get("Player")
        if not player_compound:
            return True

        pd_nbt = nbtlib.File({k: v for k, v in player_compound.items()})
        ints = uuid_to_ints(clean_host_uuid)
        pd_nbt["UUID"] = IntArray([Int(x) for x in ints])

        p_dirs = get_playerdata_dirs(world_dir)
        if (world_dir / "players" / "data").exists():
            v_dir = world_dir / "playerdata"
            v_dir.mkdir(parents=True, exist_ok=True)
            if v_dir not in p_dirs:
                p_dirs.append(v_dir)
        elif (world_dir / "playerdata").exists():
            mp_dir = world_dir / "players" / "data"
            mp_dir.mkdir(parents=True, exist_ok=True)
            if mp_dir not in p_dirs:
                p_dirs.append(mp_dir)

        for p_dir in p_dirs:
            target_pd = p_dir / f"{clean_host_uuid}.dat"
            tmp_pd = target_pd.with_suffix(".dat.tmp")
            pd_nbt.save(str(tmp_pd), gzipped=True)
            os.replace(tmp_pd, target_pd)
            log(f"[player_sync] Mirrored host state into {p_dir.parent.name}/{p_dir.name}/{target_pd.name}")

        return True
    except Exception as exc:
        log(f"[player_sync] Warning: Could not mirror level.dat to playerdata: {exc}")
        return False

