"""
Minecraft P2P API – Phase 1 through Phase 6 (Hardened).

Phase 1 – Lock, heartbeat, release, host address, /host
Phase 2 – /config, /world/upload-url, /world/commit, R2 storage
Phase 5 – App invite codes, /join, Tailscale user-invites, /join/status,
           admin endpoints (/admin/invite-code, /admin/players, /admin/revoke)
Phase 6 – Rate limiting, admin lockout, persistent file logging, error recovery.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel

# Configure logging to both console and minecraft_p2p.log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("minecraft_p2p.log", mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("minecraft_p2p")

app = FastAPI(title="Minecraft P2P API", version="0.3.0")


@app.get("/")
def root():
    """Health check / info."""
    return {"status": "ok", "service": "Minecraft P2P API", "version": "0.3.0"}


# ---------------------------------------------------------------------------
# In-memory state & Data Models
# ---------------------------------------------------------------------------

LOCK_TTL_SECONDS = 90  # heartbeat must arrive before this to keep the lock
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
    key: str
    sha256: str
    size: int
    created_by: str
    issued_at: float


@dataclass
class PlayerRecord:
    id: str
    name: str
    email: str
    token_hash: str
    created_at: float
    revoked: bool = False
    tailscale_invite_id: Optional[str] = None


@dataclass
class InviteRecord:
    code_hash: str
    uses_left: int
    expires_at: float
    created_at: float


_lock = LockState()
_host = HostState()
_world_versions: list[WorldVersion] = []
_pending_uploads: dict[str, PendingUpload] = {}

# Player & Invite registries
_players_registry: dict[str, PlayerRecord] = {}       # id -> PlayerRecord
_token_hash_to_player_id: dict[str, str] = {}         # token_hash -> id
_invites: dict[str, InviteRecord] = {}                 # code_hash -> InviteRecord

# Rate limiting and lockout state
_RATE_LIMIT_WINDOWS: dict[str, list[float]] = {}
_ADMIN_FAILURES: dict[str, list[float]] = {}
_ADMIN_LOCKOUT_MAX = 5
_ADMIN_LOCKOUT_WINDOW = 300.0  # 5 minutes

# Default dev/phase1 token support & backward compatibility
_PHASE1_TOKEN = os.getenv("PLAYER_TOKEN", "phase1-dev-token")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# Initialize default player for dev / initial token
_default_player = PlayerRecord(
    id="player-1",
    name="player1",
    email="player1@local",
    token_hash=_hash(_PHASE1_TOKEN),
    created_at=time.time(),
    revoked=False,
)
_players_registry[_default_player.id] = _default_player
_token_hash_to_player_id[_default_player.token_hash] = _default_player.id

# _PLAYERS dictionary kept for test compatibility
_PLAYERS: dict[str, str] = {
    _PHASE1_TOKEN: "player1",
}


# ---------------------------------------------------------------------------
# Rate limiting & Security helpers
# ---------------------------------------------------------------------------


def _check_rate_limit(request: Request, key: str, max_requests: int = 30, window_seconds: float = 60.0):
    client_ip = request.client.host if request.client else "unknown"
    bucket_key = f"{key}:{client_ip}"
    now = time.time()
    timestamps = _RATE_LIMIT_WINDOWS.setdefault(bucket_key, [])
    timestamps[:] = [t for t in timestamps if t > now - window_seconds]
    if len(timestamps) >= max_requests:
        logger.warning(f"Rate limit exceeded on {key} by {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests to {key}. Please wait a moment.",
        )
    timestamps.append(now)


def _check_admin_lockout(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    fails = _ADMIN_FAILURES.setdefault(client_ip, [])
    fails[:] = [t for t in fails if t > now - _ADMIN_LOCKOUT_WINDOW]
    if len(fails) >= _ADMIN_LOCKOUT_MAX:
        logger.warning(f"Locked out IP {client_ip} attempted admin access")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Admin access temporarily locked out due to multiple failed attempts. Try again in 5 minutes.",
        )


def _record_admin_failure(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    fails = _ADMIN_FAILURES.setdefault(client_ip, [])
    fails.append(now)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_player_record(x_api_token: str = Header(...)) -> PlayerRecord:
    """Dependency: resolve API token → PlayerRecord, or raise 401/403."""
    token_h = _hash(x_api_token)
    player_id = _token_hash_to_player_id.get(token_h)

    if player_id:
        p = _players_registry.get(player_id)
        if p is not None:
            if p.revoked:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Player access has been revoked",
                )
            return p

    # Check test backdoor _PLAYERS
    if x_api_token in _PLAYERS:
        name = _PLAYERS[x_api_token]
        for p in _players_registry.values():
            if p.name == name:
                if p.revoked:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Player access has been revoked",
                    )
                return p
        synthetic = PlayerRecord(
            id=f"syn-{name}",
            name=name,
            email=f"{name}@local",
            token_hash=token_h,
            created_at=time.time(),
            revoked=False,
        )
        return synthetic

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")


def _get_player(x_api_token: str = Header(...)) -> str:
    """Dependency: resolve API token → player name string, or raise 401/403."""
    return _get_player_record(x_api_token).name


def _verify_admin(
    request: Request,
    x_admin_secret: str = Header(...),
    player: PlayerRecord = Depends(_get_player_record),
) -> PlayerRecord:
    """
    Verify admin request.
    Requires valid player token + valid x-admin-secret header.
    Constant-time comparison via hmac.compare_digest and lockout on failures.
    """
    _check_admin_lockout(request)

    admin_secret = os.getenv("ADMIN_SECRET", "dev-admin-secret")
    if not hmac.compare_digest(x_admin_secret.strip().encode(), admin_secret.strip().encode()):
        _record_admin_failure(request)
        logger.warning(f"Unauthorized admin attempt by player {player.name} (id={player.id})")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin secret")
    return player


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
    download_url: Optional[str] = None
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
    current_world_version: Optional[str] = None


class UploadUrlRequest(BaseModel):
    owner_token: str
    size: int
    sha256: str


class UploadUrlResponse(BaseModel):
    upload_url: str
    version_key: str


class CommitRequest(BaseModel):
    owner_token: str
    version_key: str
    sha256: str


class CommitResponse(BaseModel):
    committed: bool
    version_key: str


# Phase 5 Models
class JoinRequest(BaseModel):
    invite_code: str
    email: str
    display_name: str


class JoinResponse(BaseModel):
    api_token: str
    player_id: str
    tailscale_invite_url: Optional[str] = None


class JoinStatusResponse(BaseModel):
    accepted: bool
    email: str
    status: str


class CreateInviteRequest(BaseModel):
    ttl_hours: int = 48
    uses: int = 1


class CreateInviteResponse(BaseModel):
    code: str
    expires_at: float
    uses_left: int


class PlayerSummary(BaseModel):
    id: str
    name: str
    email: str
    created_at: float
    revoked: bool


class AdminPlayersResponse(BaseModel):
    players: list[PlayerSummary]
    slots_used: int
    slots_max: int


class RevokePlayerRequest(BaseModel):
    player_id: str


class RevokePlayerResponse(BaseModel):
    revoked: bool
    player_id: str


# ---------------------------------------------------------------------------
# Endpoints – Phase 1 & Phase 2
# ---------------------------------------------------------------------------


@app.post("/lock/acquire", status_code=200, response_model=LockAcquireResponse)
def lock_acquire(request: Request, player: str = Depends(_get_player)):
    _check_rate_limit(request, "lock_acquire", max_requests=60, window_seconds=60.0)
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

    current = _current_version()
    download_url: Optional[str] = None
    world_version_key: Optional[str] = None
    if current is not None:
        try:
            download_url = r2.presign_download(current.key)
            world_version_key = current.key
        except Exception:
            pass

    return LockAcquireResponse(
        owner_token=raw_token,
        expires_at=_lock.expires_at,
        holder_name=player,
        download_url=download_url,
        world_version_key=world_version_key,
    )


@app.post("/lock/heartbeat")
def lock_heartbeat(body: HeartbeatRequest):
    _verify_owner_token(body.owner_token)
    _lock.expires_at = time.time() + LOCK_TTL_SECONDS
    return HeartbeatResponse(expires_at=_lock.expires_at)


@app.post("/lock/release", status_code=200)
def lock_release(body: ReleaseRequest):
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
    _verify_owner_token(body.owner_token)

    _host.ip = body.tailscale_ip
    _host.port = body.port
    _host.host_name = _lock.holder_name
    _host.updated_at = time.time()

    return {"stored": True}


@app.get("/host", response_model=HostResponse)
def get_host(player: str = Depends(_get_player)):
    if _host.ip is None or _lock_is_expired():
        return HostResponse(hosting=False)

    return HostResponse(
        hosting=True,
        host_name=_host.host_name,
        ip=_host.ip,
        port=_host.port,
        since=_host.updated_at,
    )


@app.get("/config", response_model=ConfigResponse)
def get_config(player: str = Depends(_get_player)):
    current = _current_version()
    return ConfigResponse(
        min_client_version=MIN_CLIENT_VERSION,
        required_mc_version=REQUIRED_MC_VERSION,
        current_world_version=current.key if current else None,
    )


@app.post("/world/upload-url", response_model=UploadUrlResponse)
def world_upload_url(body: UploadUrlRequest):
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
    del _pending_uploads[body.version_key]

    current_keys = [v.key for v in _world_versions]
    r2.prune_old_versions(current_keys)

    return CommitResponse(committed=True, version_key=body.version_key)


# ---------------------------------------------------------------------------
# Endpoints – Phase 5 (Onboarding, Invites, Admin)
# ---------------------------------------------------------------------------


@app.post("/join", response_model=JoinResponse)
def player_join(request: Request, body: JoinRequest):
    """
    Onboarding: verifies single-use app invite code, invites user to Tailscale tailnet,
    creates player, and issues a persistent API token.
    """
    _check_rate_limit(request, "join", max_requests=20, window_seconds=60.0)
    import server.tailscale as tailscale_mod

    code_clean = body.invite_code.strip()
    code_h = _hash(code_clean)
    now = time.time()

    invite = _invites.get(code_h)
    if invite is None or invite.uses_left <= 0 or now >= invite.expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired invite code",
        )

    active_count = sum(1 for p in _players_registry.values() if not p.revoked)
    if active_count >= 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tailnet capacity reached (maximum 6 players)",
        )

    invite.uses_left -= 1

    ts_client = tailscale_mod.get_tailscale_client()
    ts_invite = ts_client.create_user_invite(body.email.strip())
    invite_id = ts_invite.get("id")
    invite_url = ts_invite.get("inviteUrl")

    raw_token = secrets.token_urlsafe(32)
    token_h = _hash(raw_token)
    player_id = secrets.token_hex(6)

    player = PlayerRecord(
        id=player_id,
        name=body.display_name.strip(),
        email=body.email.strip().lower(),
        token_hash=token_h,
        created_at=now,
        revoked=False,
        tailscale_invite_id=invite_id,
    )
    _players_registry[player_id] = player
    _token_hash_to_player_id[token_h] = player_id
    _PLAYERS[raw_token] = player.name

    logger.info(f"New player joined: {player.name} ({player.email}) ID={player.id}")

    return JoinResponse(
        api_token=raw_token,
        player_id=player_id,
        tailscale_invite_url=invite_url,
    )


@app.get("/join/status", response_model=JoinStatusResponse)
def player_join_status(player: PlayerRecord = Depends(_get_player_record)):
    import server.tailscale as tailscale_mod

    ts_client = tailscale_mod.get_tailscale_client()
    in_tailnet = ts_client.is_user_in_tailnet(player.email)

    return JoinStatusResponse(
        accepted=in_tailnet,
        email=player.email,
        status="active" if in_tailnet else "invited",
    )


@app.post("/admin/invite-code", response_model=CreateInviteResponse)
def admin_create_invite_code(
    body: CreateInviteRequest,
    admin: PlayerRecord = Depends(_verify_admin),
):
    raw_code = f"MC-{secrets.token_hex(4).upper()}"
    code_h = _hash(raw_code)
    now = time.time()
    expires_at = now + (body.ttl_hours * 3600)

    invite = InviteRecord(
        code_hash=code_h,
        uses_left=body.uses,
        expires_at=expires_at,
        created_at=now,
    )
    _invites[code_h] = invite

    logger.info(f"Admin '{admin.name}' created invite code {raw_code} (uses={body.uses})")

    return CreateInviteResponse(
        code=raw_code,
        expires_at=expires_at,
        uses_left=body.uses,
    )


@app.get("/admin/players", response_model=AdminPlayersResponse)
def admin_list_players(admin: PlayerRecord = Depends(_verify_admin)):
    summaries = [
        PlayerSummary(
            id=p.id,
            name=p.name,
            email=p.email,
            created_at=p.created_at,
            revoked=p.revoked,
        )
        for p in _players_registry.values()
    ]
    slots_used = sum(1 for p in _players_registry.values() if not p.revoked)

    return AdminPlayersResponse(
        players=summaries,
        slots_used=slots_used,
        slots_max=6,
    )


@app.post("/admin/revoke", response_model=RevokePlayerResponse)
def admin_revoke_player(
    body: RevokePlayerRequest,
    admin: PlayerRecord = Depends(_verify_admin),
):
    import server.tailscale as tailscale_mod

    player = _players_registry.get(body.player_id)
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player '{body.player_id}' not found",
        )

    player.revoked = True

    if _lock.holder_name == player.name:
        _lock.holder_name = None
        _lock.owner_token_hash = None
        _lock.expires_at = 0.0
        _host.ip = None
        _host.port = None
        _host.host_name = None
        _host.updated_at = None

    if player.tailscale_invite_id:
        ts_client = tailscale_mod.get_tailscale_client()
        ts_client.delete_user_invite(player.tailscale_invite_id)

    logger.info(f"Admin '{admin.name}' revoked player '{player.name}' (id={player.id})")

    return RevokePlayerResponse(revoked=True, player_id=player.id)


@app.post("/admin/world/reset")
def admin_reset_world(admin: PlayerRecord = Depends(_verify_admin)):
    """Admin only: delete all cloud world archives, reset pointer to None, and clear any lock."""
    import server.r2 as r2

    r2.delete_all_worlds()
    _world_versions.clear()
    _pending_uploads.clear()

    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0

    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None

    logger.info(f"Admin '{admin.name}' wiped and reset all cloud worlds")

    return {"reset": True, "current_world_version": None}


@app.post("/admin/cloud/clear")
def admin_cloud_clear(admin: PlayerRecord = Depends(_verify_admin)):
    """
    Admin only: fully wipe all cloud world data from R2 and in-memory state.
    Also force-releases any held lock and clears host address.
    Use this to start fresh when the cloud world is in a bad state.
    """
    import server.r2 as r2

    r2.delete_all_worlds()
    _world_versions.clear()
    _pending_uploads.clear()

    prev_holder = _lock.holder_name
    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0

    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None

    logger.info(
        f"Admin '{admin.name}' cleared all cloud worlds. "
        f"Previous lock holder: {prev_holder or 'none'}"
    )

    return {
        "cleared": True,
        "previous_lock_holder": prev_holder,
        "current_world_version": None,
    }


@app.post("/admin/lock/force-release")
def admin_force_release_lock(admin: PlayerRecord = Depends(_verify_admin)):
    """
    Admin only: forcefully release the lock regardless of who holds it.
    Use this when a player's lock is stuck/stale and blocking others from hosting.
    Also clears the host address so guests see no active host.
    """
    prev_holder = _lock.holder_name
    prev_expires = _lock.expires_at

    _lock.holder_name = None
    _lock.owner_token_hash = None
    _lock.expires_at = 0.0

    _host.ip = None
    _host.port = None
    _host.host_name = None
    _host.updated_at = None

    logger.info(
        f"Admin '{admin.name}' force-released lock. "
        f"Previous holder: {prev_holder or 'none'} (expired={prev_expires})"
    )

    return {
        "force_released": True,
        "previous_holder": prev_holder,
        "previous_expires_at": prev_expires,
    }
