#!/usr/bin/env python3
"""Cloudflare R2 uploader + manifest — the ONLY place that talks to R2.

Holds a SCOPED token for the `granthvani-reels-inbox` bucket only. By design it
physically cannot reach the main content bucket (`granthvani-cdn`).
"""

import os
import json
import logging
import threading
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("dwdn-bot.r2")

R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET            = os.getenv("R2_BUCKET", "granthvani-reels-inbox")
R2_PREFIX            = os.getenv("R2_PREFIX", "reels/")
R2_ENDPOINT          = os.getenv("R2_ENDPOINT", "") or (
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)

MANIFEST_FILE = Path(os.getenv("MANIFEST_FILE", "manifest.jsonl"))
MANIFEST_KEY  = os.getenv("MANIFEST_KEY", "manifest.jsonl")

_manifest_lock = threading.Lock()
_client = None


def _prefix() -> str:
    p = R2_PREFIX
    return p if (p == "" or p.endswith("/")) else p + "/"


def is_configured() -> bool:
    return bool(R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def get_client():
    global _client
    if _client is None:
        if not is_configured():
            raise RuntimeError(
                "R2 not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY, R2_BUCKET"
            )
        _client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
    return _client


def upload(local_path: Path, filename: str, content_type: str = "video/mp4") -> str | None:
    """Upload a file to R2 under the configured prefix. Returns the object key or None."""
    key = _prefix() + filename
    try:
        get_client().upload_file(
            str(local_path), R2_BUCKET, key,
            ExtraArgs={"ContentType": content_type},
        )
        log.info(f"R2 uploaded: {R2_BUCKET}/{key}")
        return key
    except (BotoCoreError, ClientError) as e:
        log.error(f"R2 upload failed for {filename}: {e}")
        return None


def append_manifest(entry: dict) -> None:
    """Append one JSON line to the local manifest, then mirror the whole file to R2.

    The local manifest.jsonl is the source of truth (crash-safe). The R2 copy is
    a mirror your trusted app can read without listing the bucket.
    """
    with _manifest_lock:
        try:
            with open(MANIFEST_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"manifest append failed: {e}")
            return
        try:
            get_client().upload_file(
                str(MANIFEST_FILE), R2_BUCKET, MANIFEST_KEY,
                ExtraArgs={"ContentType": "application/x-ndjson"},
            )
        except (BotoCoreError, ClientError) as e:
            log.warning(f"manifest mirror to R2 failed (kept locally): {e}")
