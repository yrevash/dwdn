#!/usr/bin/env python3
"""
import_songs.py — granthvani-reels-inbox/youtube/ → granthvani-cdn + public.media.

Publishes the Mridul Ras (@Mridulras) live bhajan recordings into Sacred Music with a
YouTube-Music-style Song / Video toggle. Licensed content — permission confirmed by the
owner 2026-08-02.

For each source video it:
  1. downloads it from the inbox,
  2. extracts a high-bitrate AAC audio rendition   → songs/<id>.m4a
  3. transcodes video to H.264 720p                → song-videos/<id>.mp4
  4. grabs a poster frame                          → songs_thumbnail/<id>.jpg
  5. recovers the real title via YouTube oEmbed,
  6. inserts a public.media row (category=bhajans, type=audio, video_url set).

── Why these codec choices ──────────────────────────────────────────────────────
Source is AV1 1080p30 ~334kbps + Opus ~110kbps 48kHz stereo (probed 2026-08-02).

AUDIO. The owner asked for no quality loss. Strictly, that means `-c:a copy` — but the
source is Opus, and Opus does not play through AVFoundation/expo-av on iOS, so copying
it would ship a catalogue that is silent on every iPhone. The source is ALSO already
lossy (110kbps), so "lossless" is not on the table regardless. AAC 256k VBR gives well
over 2x the bitrate headroom of the source, which puts the single transcode generation
below audibility, and plays everywhere. Re-encoding to a *higher* number than that buys
nothing: you cannot restore detail Opus already discarded, you only pay for storage.

VIDEO. AV1 has the same problem HEVC had for reels — unreliable in the RN player — so
it must become H.264. Note this makes files BIGGER (AV1 is far more efficient); that is
the price of playing everywhere, not a mistake in the settings. 720p because these are
performance recordings watched on a phone.

── Notes ──
Resumable: a local ledger plus a `source_youtube_id` check against the DB, so a re-run
skips finished tracks. Reads the inbox, only ever writes NEW objects to the CDN bucket —
originals are left untouched as the backup.

Dedupe: the inbox has 93 files but 92 distinct YouTube ids (one video was downloaded
twice). The id — the only identifier that survived the mangled Devanagari filenames — is
the dedupe key, so the duplicate is imported once.

Deps (already in the bot's .venv): boto3, requests, python-dotenv. Needs ffmpeg+ffprobe.

Run (on the server, under tmux — this is hours of AV1 decoding, not a laptop job):
    cd dwdn/bot/reel-importer
    PLAN_ONLY=1 ../.venv/bin/python import_songs.py   # dry run, no writes
    MAX_COUNT=2 ../.venv/bin/python import_songs.py   # validate on 2 tracks first
    ../.venv/bin/python import_songs.py               # full run
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import requests
from botocore.config import Config
from dotenv import load_dotenv

for _envname in (".env.import", ".env"):
    _envpath = Path(__file__).with_name(_envname)
    if _envpath.exists():
        load_dotenv(_envpath, override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("song-importer")

# ─── config ───
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
SRC_BUCKET           = os.getenv("SRC_BUCKET", "granthvani-reels-inbox")
DST_BUCKET           = os.getenv("DST_BUCKET", "granthvani-cdn")

# ⚠️ SONGS_-prefixed on purpose. `.env.import` is SHARED with import_reels.py and sets
# SRC_PREFIX=reels/ — reading that name here made a dry run enumerate 1510 Instagram
# reels instead of the 92 YouTube videos, which would have published the entire reels
# library into Sacred Music as bhajans. Any setting these two importers must not share
# gets its own name.
SRC_PREFIX           = os.getenv("SONGS_SRC_PREFIX", "youtube/")

SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY     = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

FFMPEG               = os.getenv("FFMPEG", "ffmpeg")
FFPROBE              = os.getenv("FFPROBE", "ffprobe")

AUDIO_BITRATE        = os.getenv("AUDIO_BITRATE", "256k")

# Encode time is explicitly not a constraint here (owner, 2026-08-02), so we spend it.
#
# `veryslow` is not vanity: x264's slower presets find better encoding decisions, so at a
# fixed CRF the file comes out SMALLER at the same visual quality. That budget is then
# spent on CRF 20 instead of 23 — visibly better picture at roughly the size a medium/CRF23
# encode would have produced. The listener's mobile data is the real constraint on video,
# and slow encoding is the one lever that improves quality without costing them anything.
#
# 720p, not 1080p: these are performance recordings watched on a phone, and 1080p would
# roughly double the download for detail a handset cannot resolve. Set VIDEO_HEIGHT=1080
# to change that — nothing else needs to move.
VIDEO_CRF            = os.getenv("VIDEO_CRF", "20")
VIDEO_HEIGHT         = os.getenv("VIDEO_HEIGHT", "720")
VIDEO_PRESET         = os.getenv("VIDEO_PRESET", "veryslow")
FFMPEG_TIMEOUT       = int(os.getenv("FFMPEG_TIMEOUT", "21600"))  # 6h: veryslow + AV1 decode

PLAN_ONLY   = os.getenv("PLAN_ONLY") == "1"
MAX_COUNT   = int(os.getenv("MAX_COUNT", "0")) or None
# Deliberately low, and SONGS_-prefixed so the reel importer's CONCURRENCY cannot leak
# in: each task holds a multi-hundred-MB temp file and pins a CPU core for the AV1
# decode. Raising this on a small server causes disk-full, not speed.
CONCURRENCY = int(os.getenv("SONGS_CONCURRENCY", "2"))

CDN_BASE    = os.getenv("CDN_BASE", "https://cdn.granthvani.com")
LEDGER      = Path(__file__).with_name("import_songs_done.json")

CATEGORY    = os.getenv("CATEGORY", "bhajans")
AUTHOR      = os.getenv("AUTHOR", "Mridul Ras")

# The 11-char YouTube id immediately before the extension.
YTID_RE = re.compile(r"([A-Za-z0-9_-]{11})\.mp4$")

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
    config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
)

PG_HEADERS = {
    "apikey": SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}

_ledger_lock = threading.Lock()
_ledger: set[str] = set()


def load_ledger() -> None:
    global _ledger
    if LEDGER.exists():
        try:
            _ledger = set(json.loads(LEDGER.read_text()))
        except Exception:
            _ledger = set()


def mark_done(ytid: str) -> None:
    with _ledger_lock:
        _ledger.add(ytid)
        try:
            LEDGER.write_text(json.dumps(sorted(_ledger)))
        except Exception as e:
            log.warning(f"ledger write failed: {e}")


def list_inbox() -> dict[str, dict]:
    """Inbox videos keyed by YouTube id — the id dedupes re-downloads of one video."""
    by_id: dict[str, dict] = {}
    token = None
    while True:
        kw = {"Bucket": SRC_BUCKET, "Prefix": SRC_PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = r2.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            if not o["Key"].lower().endswith(".mp4"):
                continue
            m = YTID_RE.search(o["Key"])
            if not m:
                log.warning(f"no YouTube id in filename, skipping: {o['Key']}")
                continue
            ytid = m.group(1)
            # Keep the largest copy when the same video was downloaded more than once.
            if ytid not in by_id or o["Size"] > by_id[ytid]["Size"]:
                by_id[ytid] = {"Key": o["Key"], "Size": o["Size"]}
        if r.get("IsTruncated"):
            token = r.get("NextContinuationToken")
        else:
            break
    return by_id


def existing_ids() -> set[str]:
    """source_youtube_id values already in public.media — survives a lost ledger."""
    out: set[str] = set()
    off = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/media",
            headers=PG_HEADERS,
            params={"select": "source_youtube_id", "source_youtube_id": "not.is.null",
                    "limit": 1000, "offset": off},
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        out.update(x["source_youtube_id"] for x in rows if x.get("source_youtube_id"))
        if len(rows) < 1000:
            return out
        off += 1000


def fetch_meta(ytid: str) -> dict:
    """
    Real title + channel via YouTube oEmbed.

    oEmbed rather than yt-dlp on purpose: yt-dlp is currently blocked from this network
    with "Sign in to confirm you're not a bot", while oEmbed is a public endpoint that
    needs no key and no auth. It is also the only way back to the real titles — the
    download filenames replaced every Devanagari character with '_'.
    """
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={ytid}", "format": "json"},
            timeout=20,
        )
        if r.ok:
            j = r.json()
            return {"title": (j.get("title") or "").strip(),
                    "author": (j.get("author_name") or "").strip()}
    except Exception as e:
        log.warning(f"{ytid}: oEmbed failed ({e})")
    return {"title": "", "author": ""}


def probe_duration(path: Path) -> int | None:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return int(float(out.stdout.strip()))
    except Exception:
        return None


def run_ffmpeg(args: list[str], label: str) -> bool:
    try:
        p = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args],
                           capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        if p.returncode != 0:
            log.error(f"{label}: ffmpeg failed: {p.stderr[:400]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error(f"{label}: ffmpeg timed out after {FFMPEG_TIMEOUT}s")
        return False


def upload(path: Path, key: str, content_type: str) -> str:
    r2.upload_file(
        str(path), DST_BUCKET, key,
        ExtraArgs={
            "ContentType": content_type,
            # Set explicitly rather than relying on the zone Cache Rule, so the object
            # is still correct if it is ever served from somewhere other than the CDN.
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )
    return f"{CDN_BASE}/{key}"


def process(ytid: str, obj: dict) -> bool:
    key = obj["Key"]
    log.info(f"{ytid}: start ({round(obj['Size']/1e6)} MB)")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = tmp / "src.mp4"
        r2.download_file(SRC_BUCKET, key, str(src))

        duration = probe_duration(src)

        audio = tmp / "audio.m4a"
        if not run_ffmpeg(
            ["-i", str(src), "-vn",
             "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", str(audio)],
            f"{ytid} audio",
        ):
            return False

        video = tmp / "video.mp4"
        if not run_ffmpeg(
            ["-i", str(src),
             "-vf", f"scale=-2:{VIDEO_HEIGHT}",
             "-c:v", "libx264", "-crf", VIDEO_CRF, "-preset", VIDEO_PRESET,
             # yuv420p + High@4.0 is the combination every Android/iOS decoder handles.
             # AV1 sources can be 10-bit; passing that through would produce a file that
             # plays perfectly on a desktop and shows a black screen on a phone.
             "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
             "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(video)],
            f"{ytid} video",
        ):
            return False

        thumb = tmp / "thumb.jpg"
        # A frame from ~15s in — the opening second is usually a title card or black.
        if not run_ffmpeg(
            ["-ss", "15", "-i", str(src), "-frames:v", "1",
             "-vf", "scale=-2:720", "-q:v", "3", str(thumb)],
            f"{ytid} thumb",
        ):
            # Non-fatal: fall back to the very first frame.
            run_ffmpeg(["-i", str(src), "-frames:v", "1", "-vf", "scale=-2:720",
                        "-q:v", "3", str(thumb)], f"{ytid} thumb-fallback")

        stream_url = upload(audio, f"songs/{ytid}.m4a", "audio/mp4")
        video_url = upload(video, f"song-videos/{ytid}.mp4", "video/mp4")
        thumb_url = upload(thumb, f"songs_thumbnail/{ytid}.jpg", "image/jpeg") if thumb.exists() else None

    meta = fetch_meta(ytid)
    # media_title_length caps title at 300 chars. These YouTube titles run long
    # ("…।Live समाज गायन Use Headphones"), so truncate rather than lose the whole row
    # after the transcode has already been paid for.
    title = (meta["title"] or f"Bhajan {ytid}")[:300]

    row = {
        "title": title,
        "title_hindi": title,          # source titles are already Devanagari
        "type": "audio",
        "category": CATEGORY,
        # 'hi', not 'hindi' — valid_media_language allows only en|hi|sa.
        "language": "hi",
        "author": meta["author"] or AUTHOR,
        "duration": duration,
        "stream_url": stream_url,
        "media_url": stream_url,       # legacy column the app still reads
        "video_url": video_url,
        "thumbnail_url": thumb_url,
        "image_url": thumb_url,
        "source_youtube_id": ytid,
    }

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/media",
        headers={**PG_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
        json=[row], timeout=60,
    )
    if not r.ok:
        log.error(f"{ytid}: insert failed {r.status_code} {r.text[:300]}")
        return False

    mark_done(ytid)
    log.info(f"{ytid}: done — {title[:60]}")
    return True


def main() -> None:
    # Belt-and-braces after the shared-.env incident above: refuse to run against any
    # prefix but the YouTube inbox unless someone opts in loudly and explicitly. A
    # mis-scoped run here publishes to the live catalogue, so failing closed is right.
    if SRC_PREFIX != "youtube/" and os.getenv("I_KNOW_WHAT_IM_DOING") != "1":
        raise SystemExit(
            f"refusing to run: SONGS_SRC_PREFIX is {SRC_PREFIX!r}, expected 'youtube/'. "
            "This importer publishes into Sacred Music; pointing it at another prefix "
            "(e.g. reels/) would import the wrong library. Set I_KNOW_WHAT_IM_DOING=1 to override."
        )

    load_ledger()
    inbox = list_inbox()
    done = _ledger | existing_ids()
    todo = {k: v for k, v in inbox.items() if k not in done}

    dupes = sum(1 for _ in inbox) # distinct ids
    log.info(f"inbox: {dupes} distinct videos · already done: {len(done)} · to import: {len(todo)}")

    if PLAN_ONLY:
        for ytid, o in list(todo.items())[:10]:
            log.info(f"  would import {ytid}  ({round(o['Size']/1e6)} MB)  {o['Key'][:70]}")
        log.info("PLAN_ONLY=1 — nothing written.")
        return

    items = list(todo.items())[:MAX_COUNT] if MAX_COUNT else list(todo.items())
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for success in ex.map(lambda kv: process(kv[0], kv[1]), items):
            if success:
                ok += 1
            else:
                fail += 1

    log.info(f"finished — imported {ok}, failed {fail}, remaining {len(todo) - ok}")
    if fail:
        log.warning("failures are safe to retry: re-running skips everything already done.")


if __name__ == "__main__":
    main()
