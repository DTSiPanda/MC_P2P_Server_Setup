"""
conflict_manager.py – Local world conflict rules and backup management (plan.md Section 6A).

Rules:
1. Marker stores: version_id, folder_hash, upload_pending (bool).
2. Before host or join-prep:
   - If upload_pending is True (previous session crashed/interrupted):
     Must retry upload first before anything else! Never download over uncommitted changes.
   - If local world differs from marker and upload_pending is False:
     Someone played solo -> create backup in ~/.minecraft_p2p/backups/<world_name>/,
     keep last 3-5, then allow override with shared world.
   - If local world matches marker:
     Safe to override with cloud version.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

APP_DATA_DIR = Path.home() / ".minecraft_p2p"
MARKERS_DIR = APP_DATA_DIR / "markers"
BACKUPS_DIR = APP_DATA_DIR / "backups"
MAX_BACKUPS = 5

PACK_EXCLUDES = {"cache", "logs", "crash-reports", "crash_reports"}


def hash_world_folder(world_dir: Path) -> str:
    """Compute a deterministic sha256 hash of all relevant files in world_dir."""
    if not world_dir.exists():
        return ""

    h = hashlib.sha256()
    # Sort files deterministically
    all_files = sorted(world_dir.rglob("*"))
    for f in all_files:
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(world_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0].lower() in PACK_EXCLUDES:
            continue
        # Include relative path and file contents in hash
        h.update(str(rel).encode("utf-8"))
        with open(f, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
    return h.hexdigest()


def _marker_path(world_name: str) -> Path:
    MARKERS_DIR.mkdir(parents=True, exist_ok=True)
    return MARKERS_DIR / f"{world_name}.json"


def load_marker(world_name: str) -> dict[str, Any]:
    p = _marker_path(world_name)
    if not p.exists():
        return {"version_id": None, "folder_hash": None, "upload_pending": False}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version_id": None, "folder_hash": None, "upload_pending": False}


def save_marker(
    world_name: str,
    version_id: Optional[str] = None,
    folder_hash: Optional[str] = None,
    upload_pending: bool = False,
) -> None:
    p = _marker_path(world_name)
    data = {
        "version_id": version_id,
        "folder_hash": folder_hash,
        "upload_pending": upload_pending,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_upload_pending(world_name: str, pending: bool) -> None:
    current = load_marker(world_name)
    current["upload_pending"] = pending
    p = _marker_path(world_name)
    p.write_text(json.dumps(current, indent=2), encoding="utf-8")


def create_local_backup(world_dir: Path, world_name: str) -> Optional[Path]:
    """
    Back up world_dir to ~/.minecraft_p2p/backups/<world_name>/OurWorld_overridden_YYYY-MM-DD_HHMM.zip.
    Maintains last MAX_BACKUPS (prunes oldest).
    """
    if not world_dir.exists():
        return None

    target_backup_dir = BACKUPS_DIR / world_name
    target_backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    zip_name = f"{world_name}_overridden_{timestamp}.zip"
    backup_file = target_backup_dir / zip_name

    with zipfile.ZipFile(backup_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in world_dir.rglob("*"):
            if item.is_file():
                try:
                    rel = item.relative_to(world_dir)
                    zf.write(item, rel)
                except Exception:
                    pass

    # Prune old backups if count > MAX_BACKUPS
    all_backups = sorted(target_backup_dir.glob(f"{world_name}_overridden_*.zip"), key=lambda f: f.stat().st_mtime)
    while len(all_backups) > MAX_BACKUPS:
        oldest = all_backups.pop(0)
        try:
            oldest.unlink(missing_ok=True)
        except Exception:
            pass

    return backup_file


def check_and_resolve_conflict(
    world_dir: Path,
    world_name: str,
    on_upload_pending: Optional[Callable[[], bool]] = None,
    log: Callable[[str], None] = print,
) -> str:
    """
    Evaluate local conflict status according to plan.md Section 6A.

    Returns:
    - "UPLOAD_PENDING": Local changes must be uploaded first!
    - "BACKED_UP": Local solo changes found, backed up, and ready to overwrite with shared world.
    - "SAFE": Safe to overwrite with cloud version or continue.
    """
    marker = load_marker(world_name)

    # 1. upload_pending is set
    if marker.get("upload_pending"):
        log("[conflict_manager] WARNING: Last session was not committed to cloud (upload_pending=True).")
        return "UPLOAD_PENDING"

    # 2. Local world differs and upload_pending is not set (someone played solo)
    if world_dir.exists():
        curr_hash = hash_world_folder(world_dir)
        last_synced_hash = marker.get("folder_hash")

        if last_synced_hash and curr_hash != last_synced_hash:
            backup_path = create_local_backup(world_dir, world_name)
            log(f"[conflict_manager] Local solo changes detected! Saved backup to {backup_path}.")
            log("[conflict_manager] Local changes found and saved to backups. Loading the shared world.")
            return "BACKED_UP"

    return "SAFE"
