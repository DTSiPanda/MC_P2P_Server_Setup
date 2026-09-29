"""
world_sync.py – World download, pack, upload, commit, and conflict management.

Handles:
- Section 6A Local world conflict rules (marker tracking, pending upload retries, backup rotation).
- Section 7 Mid-upload kill and recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

import requests

from client.conflict_manager import (
    check_and_resolve_conflict,
    hash_world_folder,
    load_marker,
    save_marker,
    set_upload_pending,
)

# Directories to exclude when packing the world
PACK_EXCLUDES = {"cache", "logs", "crash-reports", "crash_reports"}

# Retry / timeout settings (Render free tier can cold-start for ~30 s)
REQUEST_TIMEOUT = 15
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0


def _api(method: str, url: str, **kwargs) -> requests.Response:
    """Call the API with retries and backoff. Prints a waiting message on cold start."""
    delay = RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            return r
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == MAX_RETRIES:
                raise
            print(f"  [world_sync] Server unreachable/slow, retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})…")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("Unreachable")


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack_world(world_dir: Path, out_zip: Path) -> str:
    """
    Zip world_dir into out_zip, excluding PACK_EXCLUDES.
    Returns the hex sha256 of the resulting zip.
    """
    world_dir = world_dir.resolve()
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(world_dir.rglob("*")):
            try:
                rel = item.relative_to(world_dir)
            except ValueError:
                continue
            if rel.parts and rel.parts[0].lower() in PACK_EXCLUDES:
                continue
            if item.is_file():
                zf.write(item, rel)
    return sha256_of_file(out_zip)


def unpack_world(zip_path: Path, target_dir: Path) -> None:
    """
    Unpack zip_path into a temp directory, then atomically swap it into target_dir.
    """
    parent = target_dir.parent
    tmp = Path(tempfile.mkdtemp(dir=parent, prefix="_world_tmp_"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp)

        if target_dir.exists():
            old_backup = parent / f"{target_dir.name}_prev"
            if old_backup.exists():
                shutil.rmtree(old_backup)
            target_dir.rename(old_backup)

        tmp.rename(target_dir)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def upload_and_commit_world(
    api_url: str,
    owner_token: str,
    world_dir: Path,
    release: bool = False,
    log=print,
) -> str:
    """
    Pack, upload to R2, and commit the world.
    If release=True, also calls POST /lock/release.
    Updates the local conflict marker.
    Returns the committed version key.
    """
    world_name = world_dir.name
    # Set pending upload marker
    set_upload_pending(world_name, True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = Path(tmp_dir) / "world.zip"

        log(f"[world_sync] Packing {world_dir}…")
        sha = pack_world(world_dir, zip_path)
        size = zip_path.stat().st_size
        log(f"[world_sync] Packed {size:,} bytes  sha256={sha[:12]}…")

        log("[world_sync] Requesting upload URL…")
        r_url = _api(
            "POST",
            f"{api_url}/world/upload-url",
            json={"owner_token": owner_token, "size": size, "sha256": sha},
        )
        if r_url.status_code != 200:
            _die(f"world/upload-url failed: {r_url.status_code} {r_url.text}")

        url_data = r_url.json()
        upload_url: str = url_data["upload_url"]
        version_key: str = url_data["version_key"]

        log(f"[world_sync] Uploading to R2 (key={version_key})…")
        with open(zip_path, "rb") as f:
            put_r = requests.put(upload_url, data=f, timeout=300)
        if put_r.status_code not in (200, 204):
            _die(f"PUT to R2 failed: {put_r.status_code}")

        log("[world_sync] Committing version…")
        r_commit = _api(
            "POST",
            f"{api_url}/world/commit",
            json={"owner_token": owner_token, "version_key": version_key, "sha256": sha},
        )
        if r_commit.status_code != 200:
            _die(f"world/commit failed: {r_commit.status_code} {r_commit.text}")

        log(f"[world_sync] Committed: {version_key}")

        # Update marker: successfully committed!
        save_marker(
            world_name=world_name,
            version_id=version_key,
            folder_hash=hash_world_folder(world_dir),
            upload_pending=False,
        )

    if release:
        log("[world_sync] Releasing lock…")
        _api(
            "POST",
            f"{api_url}/lock/release",
            json={"owner_token": owner_token},
        )
        _clear_session(world_dir)
        log("[world_sync] Lock released.")

    return version_key


def cmd_download(api_url: str, token: str, world_dir: Path) -> None:
    """Acquire lock, check conflicts, download world, unpack."""
    world_name = world_dir.name

    # Check local world conflict rules (Section 6A)
    conflict = check_and_resolve_conflict(world_dir, world_name)
    if conflict == "UPLOAD_PENDING":
        print("[world_sync] Uncommitted session detected (upload_pending=True). Retrying upload first...")
        cmd_upload(api_url, token, world_dir)
        print("[world_sync] Previous session successfully saved. Now proceeding with download.")

    print("[world_sync] Acquiring lock…")
    r = _api(
        "POST",
        f"{api_url}/lock/acquire",
        headers={"x-api-token": token},
    )
    if r.status_code == 409:
        holder = r.json().get("detail", {}).get("holder_name", "someone else")
        _die(f"Lock is held by {holder}. Wait for them to finish.")
    if r.status_code != 200:
        _die(f"lock/acquire failed: {r.status_code} {r.text}")

    data = r.json()
    owner_token: str = data["owner_token"]
    download_url: Optional[str] = data.get("download_url")
    version_key = data.get("world_version_key")

    if download_url is None:
        print("[world_sync] No world in cloud yet. Starting fresh.")
        save_marker(world_name, None, hash_world_folder(world_dir) if world_dir.exists() else None, False)
        _save_session(world_dir, owner_token)
        return

    print(f"[world_sync] Downloading world ({version_key})…")
    dl = requests.get(download_url, timeout=120, stream=True)
    dl.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        for chunk in dl.iter_content(65536):
            tmp.write(chunk)
        tmp_path = Path(tmp.name)

    try:
        print(f"[world_sync] Unpacking into {world_dir}…")
        unpack_world(tmp_path, world_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Save marker after successful download
    save_marker(
        world_name=world_name,
        version_id=version_key,
        folder_hash=hash_world_folder(world_dir),
        upload_pending=False,
    )

    print("[world_sync] Download complete.")
    _save_session(world_dir, owner_token)


def cmd_upload(api_url: str, token: str, world_dir: Path) -> None:
    """Pack world, upload to R2, commit, release lock."""
    owner_token = _load_session(world_dir)
    if owner_token is None:
        print("[world_sync] No active session – acquiring lock…")
        r = _api("POST", f"{api_url}/lock/acquire", headers={"x-api-token": token})
        if r.status_code != 200:
            _die(f"lock/acquire failed: {r.status_code} {r.text}")
        owner_token = r.json()["owner_token"]

    upload_and_commit_world(
        api_url=api_url,
        owner_token=owner_token,
        world_dir=world_dir,
        release=True,
    )
    print("[world_sync] Done.")


def _session_file(world_dir: Path) -> Path:
    return world_dir.parent / f".{world_dir.name}_session"


def _save_session(world_dir: Path, owner_token: str) -> None:
    _session_file(world_dir).write_text(owner_token, encoding="utf-8")


def _load_session(world_dir: Path) -> Optional[str]:
    f = _session_file(world_dir)
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return None


def _clear_session(world_dir: Path) -> None:
    _session_file(world_dir).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Minecraft world sync CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-url", required=True, help="Base URL of the P2P API")
    common.add_argument(
        "--token",
        default=os.getenv("PLAYER_TOKEN"),
        help="Player API token (or set PLAYER_TOKEN env var)",
    )
    common.add_argument("--world-dir", required=True, type=Path, help="Path to the world folder")

    sub.add_parser("download", parents=[common], help="Acquire lock and download world")
    sub.add_parser("upload", parents=[common], help="Pack, upload, commit, and release lock")

    args = parser.parse_args()

    if not args.token:
        _die("No API token. Pass --token or set PLAYER_TOKEN.")

    if args.cmd == "download":
        cmd_download(args.api_url, args.token, args.world_dir)
    elif args.cmd == "upload":
        cmd_upload(args.api_url, args.token, args.world_dir)


if __name__ == "__main__":
    main()
