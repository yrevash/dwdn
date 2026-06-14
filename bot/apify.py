#!/usr/bin/env python3
"""Download reels from an Apify Instagram Reel Scraper dataset.

Apify does the actual Instagram scraping (off our account, via Apify's own
infrastructure) and returns reel items that include a direct `videoUrl`. We just
download that URL → HEVC → R2 → manifest. No Instagram account is involved here,
so this path carries ZERO ban risk. Dedup is shared with the rest of the bot via
downloaded_urls.json.
"""

import os
import re
import json
import hashlib
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime

import r2
import transcode

log = logging.getLogger("dwdn-bot.apify")

DOWNLOAD_DIR    = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOADED_FILE = Path(os.getenv("DOWNLOADED_FILE", "downloaded_urls.json"))
_state_lock = threading.Lock()


def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-]", "_", name or "")
    return name[:40].strip("_") or "unknown"


def url_fingerprint(url: str) -> str:
    return hashlib.md5(url.strip().rstrip("/").encode()).hexdigest()


def make_filename(sender: str, label: str, uid: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{sender}_{sanitize(label)[:50]}_{uid}.mp4"


def _source_channel(item: dict) -> str:
    """The account we asked Apify to scrape (from inputUrl) — may differ from the
    creator for reposts/collabs. Kept in the manifest for the rec engine."""
    iu = item.get("inputUrl") or ""
    if "instagram.com/" in iu:
        iu = iu.split("instagram.com/")[-1].split("/")[0]
    return sanitize(iu.lstrip("@").strip("/")) if iu else ""


def load_downloaded() -> set:
    try:
        return set(json.loads(DOWNLOADED_FILE.read_text()))
    except Exception:
        return set()


def save_downloaded(s: set) -> None:
    try:
        DOWNLOADED_FILE.write_text(json.dumps(list(s)))
    except Exception as e:
        log.warning(f"could not save {DOWNLOADED_FILE}: {e}")


def download_reel(item: dict, downloaded: set) -> str:
    """Process one Apify reel item → R2. Returns 'uploaded' | 'skip' | 'fail'."""
    code = item.get("shortCode") or ""
    video_url = item.get("videoUrl")
    owner = sanitize(item.get("ownerUsername") or "unknown")
    caption = (item.get("caption") or "").split("\n")[0][:50]

    if item.get("type") and item.get("type") != "Video":
        return "skip"                       # not a video reel
    if not video_url:
        log.warning(f"no videoUrl for {code}")
        return "fail"

    url = f"https://www.instagram.com/reel/{code}/" if code else video_url
    fp = url_fingerprint(url)
    with _state_lock:
        if fp in downloaded:
            return "skip"                   # already have it

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DOWNLOAD_DIR / f"apify_{code or fp[:8]}.mp4"
    try:
        with requests.get(video_url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        log.warning(f"download failed {code}: {e}")
        tmp.unlink(missing_ok=True)
        return "fail"

    final, codec, src_b, out_b = transcode.transcode_hevc(tmp)
    filename = make_filename(owner, caption or "reel", code or fp[:8])
    key = r2.upload(final, filename)
    for p in {tmp, final}:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    if not key:
        return "fail"

    r2.append_manifest({
        "key": key, "origin": "scrape",
        "sender": owner,                          # the reel's true creator (in the filename)
        "source_channel": _source_channel(item),  # the account we scraped it from
        "creator_id": str(item.get("ownerId", "")),
        "caption": caption, "shortcode": code, "media_pk": str(item.get("id", "")),
        "video_duration": item.get("videoDuration"),
        "codec": codec, "src_bytes": src_b, "out_bytes": out_b,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    with _state_lock:
        downloaded.add(fp)
        save_downloaded(downloaded)
    return "uploaded"
