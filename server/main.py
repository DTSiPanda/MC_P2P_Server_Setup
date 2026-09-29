"""
Phase 1 + Phase 2 API.
Phase 1 – lock, heartbeat, release, host address, /host.
Phase 2 – /config, /world/upload-url, /world/commit,
           lock/acquire now returns a presigned download URL.
State is kept in-memory (restart clears it; swap for a DB in production).
"""

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

app = FastAPI(title="Minecraft P2P API", version="0.2.0")


@app.get("/")
def root():
    """Health check / info."""
    return {"status": "ok", "service": "Minecraft P2P API", "version": "0.2.0"}


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

LOCK_TTL_SECONDS = 90  # heartbeat must arrive before this to keep the lock

# Version config returned by /config
MIN_CLIENT_VERSION = "0.1.0"
REQUIRED_MC_VERSION = "1.20.1"


class LockState:
    holder_name: Optional[str] = None
    owner_token_hash: Optional[str] = None  # sha256 hex of the raw token
    expires_at: float = 0.0  # epoch seconds


class HostState:
    ip: Optional[str] = None
    port: Optional[int] = None
    host_name: Optional[str] = None
    updated_at: Optional[float] = None


@dataclass
class WorldVersion:
    key: str
    sha256: str
    size: int
    created_by: str
    created_at: float
    is_current: bool = False


@dataclass
class PendingUpload:
    """Tracks an upload-url that was issued but not yet committed."""
    key: str
    sha256: str       # sha256 the client declared before uploading
    size: int
    created_by: str
    issued_at: float


_lock = LockState()
_host = HostState()
_world_versions: list[WorldVersion] = []
_pending_uploads: dict[str, PendingUpload] = {}  # key → PendingUpload

# ---------------------------------------------------------------------------
# Fake player registry (Phase 1/2 stub; replaced in Phase 5)
# ---------------------------------------------------------------------------
_PHASE1_TOKEN = os.getenv("PLAYER_TOKEN", "phase1-dev-token")

_PLAYERS: dict[str, str] = {
    _PHASE1_TOKEN: "player1",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_player(x_api_token: str = Header(...)) -> str:
    """Dependency: resolve API token → player name, or raise 401."""
    name = _PLAYERS.get(x_api_token)
    if name is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")
    return name


def _lock_is_expired() -> bool:
    return time.time() >= _lock.expires_at


def _verify_owner_token(owner_token: str) -> None:
    """Raise 403 / 410 if the token doesn't match the current lock owner."""
    if _lock.owner_token_hash is None or _lock_is_expired():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Lock is not held")
    if not secrets.compare_digest(_hash(owner_token), _lock.owner_token_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid owner token")


def _current_version() -> Optional[WorldVersion]:
    for v in reversed(_world_versions):
        if v.is_current:
            return v
    return None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LockAcquireResponse(BaseModel):
    owner_token: str
    expires_at: float
    holder_name: str
    download_url: Optional[str] = None   # presigned GET URL; None if no world yet
    world_version_key: Optional[str] = None


class LockConflictResponse(BaseModel):
    holder_name: str
    expires_at: float


class HeartbeatRequest(BaseModel):
    owner_token: str


class HeartbeatResponse(BaseModel):
    expires_at: float


class HostAddressRequest(BaseModel):
    owner_token: str
    tailscale_ip: str
    port: int


class HostResponse(BaseModel):
    hosting: bool
    host_name: Optional[str] = None
    ip: Optional[str] = None
    port: Optional[int] = None
    since: Optional[float] = None


class ReleaseRequest(BaseModel):
    owner_token: str


class ConfigResponse(BaseModel):
    min_client_version: str
    required_mc_version: str
    current_world_version: Optional[str] = None   # R2 key


class UploadUrlRequest(BaseModel):
    owner_token: str
    size: int
    sha256: str   # hex sha256 of the zip the client will upload


class UploadUrlResponse(BaseModel):
    upload_url: str   # presigned PUT URL
    version_key: str  # the R2 key the client must PUT to


class CommitRequest(BaseModel):
    owner_token: str
    version_key: str
    sha256: str   # must match what was declared in upload-url


class CommitResponse(BaseModel):
    committed: bool
    version_key: str


# ---------------------------------------------------------------------------
# Endpoints – Phase 1 (unchanged behaviour, lock/acquire extended)
# ---------------------------------------------------------------------------


@app.post("/lock/acquire", status_code=200, response_model=LockAcquireResponse)
def lock_acquire(player: str = Depends(_get_player)):
    """
    Acquire the world lock.
    Returns 200 + owner_token + presigned download URL (if a world exists).
    Returns 409 if another player holds a live lock.
    """
    import server.r2 as r2

    now = time.time()

    if _lock.holder_name is not None and not _lock_is_expired():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "holder_name": _lock.holder_name,
                "expires_at": _lock.expires_at,
            },
        )

    raw_token = secrets.token_urlsafe(32)
    _lock.holder_name = player
    _lock.owner_token_hash = _hash(raw_token)
    _lock.expires_at = now + LOCK_TTL_SECONDS

    # Presigned download URL for the current world version (if any)
    current = _current_version()
    download_url: Optional[str] = None
    world_version_key: Optional[str] = None
    if current is not None:
        try:
            download_url = r2.presign_download(current.key)
            world_version_key = current.key
        except Exception:
            pass  # R2 not configured locally; the client handles None gracefully

    return LockAcquireResponse(
        owner_token=raw_token,
        expires_at=_lock.expires_at,
        holder_name=player,
        download_url=download_url,
        world_version_key=world_version_key,
    )


@app.post("/lock/heartbeat")
def lock_heartbeat(body: HeartbeatRequest):
    """Extend the lock TTL. Returns 410 if the lock was lost or expired."""
    _verify_owner_token(body.owner_token)
    _lock.expires_at = time.time() + LOCK_TTL_SECONDS
    return HeartbeatResponse(expires_at=_lock.expires_at)


@app.post("/lock/release", status_code=200)
def lock_release(body: ReleaseRequest):
    """Release the lock and clear the host address."""
    _verify_owner_token(body.owner_token)

    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0

    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None

    return {"released": True}


@app.post("/host/address", status_code=200)
def set_host_address(body: HostAddressRequest):
    """Store the current host's Tailscale IP and port."""
    _verify_owner_token(body.owner_token)

    _host.ip = body.tailscale_ip
    _host.port = body.port
    _host.host_name = _lock.holder_name
    _host.updated_at = time.time()

    return {"stored": True}


@app.get("/host", response_model=HostResponse)
def get_host(player: str = Depends(_get_player)):
    """Return the current host's address, or {hosting: false}."""
    if _host.ip is None or _lock_is_expired():
        return HostResponse(hosting=False)

    return HostResponse(
        hosting=True,
        host_name=_host.host_name,
        ip=_host.ip,
        port=_host.port,
        since=_host.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints – Phase 2
# ---------------------------------------------------------------------------


@app.get("/config", response_model=ConfigResponse)
def get_config(player: str = Depends(_get_player)):
    """Return versioning config so clients can enforce compatibility."""
    current = _current_version()
    return ConfigResponse(
        min_client_version=MIN_CLIENT_VERSION,
        required_mc_version=REQUIRED_MC_VERSION,
        current_world_version=current.key if current else None,
    )


@app.post("/world/upload-url", response_model=UploadUrlResponse)
def world_upload_url(body: UploadUrlRequest):
    """
    Issue a presigned PUT URL for a new world version.
    The caller must be the lock holder.
    The sha256 declared here is later verified at /world/commit.
    """
    import server.r2 as r2

    _verify_owner_token(body.owner_token)

    version_key = r2.new_version_key()
    upload_url = r2.presign_upload(version_key)

    _pending_uploads[version_key] = PendingUpload(
        key=version_key,
        sha256=body.sha256,
        size=body.size,
        created_by=_lock.holder_name,  # type: ignore[arg-type]
        issued_at=time.time(),
    )

    return UploadUrlResponse(upload_url=upload_url, version_key=version_key)


@app.post("/world/commit", response_model=CommitResponse)
def world_commit(body: CommitRequest):
    """
    Verify the upload and move the 'current' pointer.

    Rejects if:
    - wrong owner token
    - version_key was never registered via /world/upload-url
    - sha256 doesn't match what was declared at upload-url time
    - the object doesn't exist in R2
    """
    import server.r2 as r2

    _verify_owner_token(body.owner_token)

    pending = _pending_uploads.get(body.version_key)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown version key; call /world/upload-url first",
        )

    if not secrets.compare_digest(pending.sha256, body.sha256):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sha256 mismatch – upload may be corrupted",
        )

    if not r2.object_exists(body.version_key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Object not found in R2; upload did not complete",
        )

    # Mark all previous versions non-current, add new current version
    for v in _world_versions:
        v.is_current = False

    new_version = WorldVersion(
        key=body.version_key,
        sha256=body.sha256,
        size=pending.size,
        created_by=pending.created_by,
        created_at=time.time(),
        is_current=True,
    )
    _world_versions.append(new_version)

    # Clean up pending record
    del _pending_uploads[body.version_key]

    # Prune old versions in R2 (best-effort)
    current_keys = [v.key for v in _world_versions]
    r2.prune_old_versions(current_keys)

    return CommitResponse(committed=True, version_key=body.version_key)
