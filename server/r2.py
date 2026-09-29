"""
Cloudflare R2 (S3-compatible) helpers.
All credentials come from environment variables; never hardcoded.

Tests inject a mock client via set_client().
"""

import os
import uuid
from typing import Optional

import boto3
from botocore.config import Config

BUCKET: str = os.getenv("R2_BUCKET_NAME", "minecraft-p2p")
PRESIGN_EXPIRY: int = 300   # 5 minutes
KEEP_VERSIONS: int = 10

# Module-level singleton – replaced in tests via set_client()
_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _client


def set_client(c) -> None:
    """Inject a boto3 client. Used by tests to supply a moto-backed client."""
    global _client
    _client = c


def new_version_key() -> str:
    """Generate a unique R2 key for a new world version."""
    return f"worlds/{uuid.uuid4()}.zip"


def presign_download(key: str) -> str:
    """Return a presigned GET URL for the given key."""
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=PRESIGN_EXPIRY,
    )


def presign_upload(key: str) -> str:
    """Return a presigned PUT URL for the given key."""
    return get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=PRESIGN_EXPIRY,
    )


def object_exists(key: str) -> bool:
    """Return True if the object exists in the bucket."""
    try:
        get_client().head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def prune_old_versions(current_keys: list[str]) -> None:
    """
    Delete world zips not in current_keys beyond KEEP_VERSIONS.
    Called after a successful commit.
    """
    try:
        paginator = get_client().get_paginator("list_objects_v2")
        all_keys: list[str] = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix="worlds/"):
            for obj in page.get("Contents", []):
                all_keys.append(obj["Key"])

        # Protect current_keys; delete the oldest excess versions
        deletable = [k for k in all_keys if k not in current_keys]
        for key in deletable[KEEP_VERSIONS:]:
            get_client().delete_object(Bucket=BUCKET, Key=key)
    except Exception:
        pass  # pruning is best-effort; never block a commit
