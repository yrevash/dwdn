#!/usr/bin/env python3
"""
import_reels.py — one-shot importer: granthvani-reels-inbox → live granthvani-cdn + reels DB.

For every source .mp4 in granthvani-reels-inbox/reels/ it:
  1. downloads it,
  2. transcodes HEVC → H.264 720p CRF26 (matches the app's existing 272 reels; HEVC
     is unreliable in the RN player, hence the re-encode),
  3. extracts a first-frame JPEG thumbnail,
  4. uploads both to granthvani-cdn (reels/ + reel-thumbs/),
  5. inserts a row in public.reels via PostgREST (share_token auto-filled by trigger).

APPENDS as gv-reel-273… — never touches the existing rows. Runs to completion, then
stops. Safe to re-run: it resumes (skips shortcodes already done via the local ledger
and ids already in the DB). Reads from the inbox, only ever *writes* new objects to the
CDN bucket — the inbox originals are left untouched as a backup.

Deps (already in the bot's .venv): boto3, requests, python-dotenv.
Needs ffmpeg + ffprobe on PATH.

Run:
    cd dwdn/bot/reel-importer
    ../.venv/bin/python import_reels.py            # full run
    PLAN_ONLY=1 ../.venv/bin/python import_reels.py  # dry run — counts only, no writes
    MAX_COUNT=3 ../.venv/bin/python import_reels.py   # import just 3 (validation)
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import requests
from botocore.config import Config
from dotenv import load_dotenv

# Load .env.import sitting next to this script (falls back to process env).
load_dotenv(Path(__file__).with_name(".env.import"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reel-importer")

# ─── config ───
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
SRC_BUCKET           = os.getenv("SRC_BUCKET", "granthvani-reels-inbox")
DST_BUCKET           = os.getenv("DST_BUCKET", "granthvani-cdn")
SRC_PREFIX           = os.getenv("SRC_PREFIX", "reels/")

SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY     = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

CF_TOKEN             = os.getenv("CLOUDFLARE_CACHE_PURGE_TOKEN", "")
CF_ZONE_ID           = os.getenv("CF_ZONE_ID", "8358d145dc4aac0f7ab0b1a6bce61c85")

FFMPEG               = os.getenv("FFMPEG", "ffmpeg")
FFPROBE              = os.getenv("FFPROBE", "ffprobe")
TRANSCODE_CRF        = os.getenv("TRANSCODE_CRF", "26")
FFMPEG_TIMEOUT       = int(os.getenv("FFMPEG_TIMEOUT", "900"))

PLAN_ONLY   = os.getenv("PLAN_ONLY") == "1"
MAX_COUNT   = int(os.getenv("MAX_COUNT", "0")) or None   # 0/unset = all
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))
MAX_SRC_MB  = int(os.getenv("MAX_SRC_MB", "60"))

CDN_BASE    = os.getenv("CDN_BASE", "https://cdn.granthvani.com")
LEDGER      = Path(__file__).with_name("import_done.json")

import re
SHORTCODE_RE = re.compile(r"_([A-Za-z0-9_-]{9,})\.mp4$")

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


def mark_done(shortcode: str) -> None:
    with _ledger_lock:
        _ledger.add(shortcode)
        try:
            LEDGER.write_text(json.dumps(sorted(_ledger)))
        except Exception as e:
            log.warning(f"ledger write failed: {e}")


def list_inbox_videos() -> list[dict]:
    out, token = [], None
    while True:
        kw = {"Bucket": SRC_BUCKET, "Prefix": SRC_PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = r2.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            if o["Key"].lower().endswith(".mp4"):
                out.append({"Key": o["Key"], "Size": o["Size"]})
        if r.get("IsTruncated"):
            token = r.get("NextContinuationToken")
        else:
            break
    return out


def load_manifest() -> tuple[dict, dict]:
    by_key, by_short = {}, {}
    try:
        body = r2.get_object(Bucket=SRC_BUCKET, Key="manifest.jsonl")["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("key"):
                by_key[e["key"]] = e
            if e.get("shortcode"):
                by_short[e["shortcode"]] = e
    except Exception as e:
        log.warning(f"manifest load failed: {e}")
    return by_key, by_short


def shortcode_from_key(key: str) -> str:
    m = SHORTCODE_RE.search(key)
    return m.group(1) if m else key


def fetch_existing_reels() -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/reels",
        headers=PG_HEADERS,
        params={"select": "id,sort_order", "limit": "100000"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def download(key: str, dest: Path) -> None:
    r2.download_file(SRC_BUCKET, key, str(dest))


def transcode(src: Path, out: Path) -> None:
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", "scale=720:-2",
        "-c:v", "libx264", "-crf", str(TRANSCODE_CRF), "-preset", "fast",
        "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if res.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"transcode failed: {res.stderr[-300:]}")


def extract_thumb(src: Path, out: Path) -> None:
    cmd = [FFMPEG, "-y", "-i", str(src), "-vframes", "1", "-q:v", "3", "-loglevel", "error", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"thumb failed: {res.stderr[-200:]}")


def upload(local: Path, key: str, content_type: str) -> None:
    r2.upload_file(
        str(local), DST_BUCKET, key,
        ExtraArgs={"ContentType": content_type, "CacheControl": "public, max-age=31536000"},
    )


def insert_reel(item: dict) -> str:
    """POST a reels row. Returns 'ok' | 'exists' (409) | raises."""
    body = {
        "id": item["id"],
        "title": f"Reel {item['id'].replace('gv-reel-', '')}",
        "description": item["description"][:500],
        "creator": item["creator"],
        "video_url": f"{CDN_BASE}/reels/{item['id']}.mp4",
        "image_url": f"{CDN_BASE}/reel-thumbs/{item['id']}.jpg",
        "sort_order": item["sort_order"],
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/reels",
        headers={**PG_HEADERS, "Prefer": "return=minimal"},
        data=json.dumps(body),
        timeout=60,
    )
    if r.status_code in (200, 201):
        return "ok"
    if r.status_code == 409:
        return "exists"
    raise RuntimeError(f"insert {r.status_code}: {r.text[:300]}")


def purge_cache(urls: list[str]) -> None:
    if not CF_TOKEN or not urls:
        return
    purged = 0
    for i in range(0, len(urls), 30):
        batch = urls[i:i + 30]
        try:
            resp = requests.post(
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache",
                headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
                data=json.dumps({"files": batch}),
                timeout=60,
            )
            if resp.json().get("success"):
                purged += len(batch)
        except Exception as e:
            log.warning(f"purge batch failed: {e}")
    log.info(f"purged {purged} CDN URLs")


def main() -> None:
    load_ledger()
    log.info(f"src={SRC_BUCKET}/{SRC_PREFIX}  dst={DST_BUCKET}  crf={TRANSCODE_CRF}  concurrency={CONCURRENCY}")

    existing = fetch_existing_reels()
    existing_ids = {r["id"] for r in existing}
    nums = [int(r["id"].replace("gv-reel-", "")) for r in existing if r["id"].replace("gv-reel-", "").isdigit()]
    max_num = max(nums) if nums else 0
    max_sort = max((r.get("sort_order") or 0) for r in existing) if existing else 0
    log.info(f"existing reels: {len(existing_ids)} (max gv-reel-{max_num:03d}, max sort_order {max_sort})")

    videos = list_inbox_videos()
    by_key, by_short = load_manifest()
    log.info(f"inbox videos: {len(videos)}, manifest entries: {len(by_key)}")

    videos.sort(key=lambda v: v["Key"])  # chronological (timestamp-prefixed names)
    seen, skipped_dupe, skipped_large, kept = set(), [], [], []
    for v in videos:
        short = shortcode_from_key(v["Key"])
        if short in seen:
            skipped_dupe.append(v["Key"]); continue
        seen.add(short)
        if v["Size"] > MAX_SRC_MB * 1024 * 1024:
            skipped_large.append((v["Key"], v["Size"] / 1024 / 1024)); continue
        kept.append({"Key": v["Key"], "Size": v["Size"], "short": short})

    n, sort = max_num, max_sort
    plan = []
    for k in kept:
        n += 1; sort += 1
        m = by_key.get(k["Key"]) or by_short.get(k["short"]) or {}
        sender = m.get("sender") or ""
        creator = sender if sender and sender != "unknown" else "GranthVani"
        description = (m.get("caption") or "").strip() or creator
        plan.append({
            "srcKey": k["Key"], "size": k["Size"], "short": k["short"],
            "id": f"gv-reel-{n:03d}", "sort_order": sort,
            "creator": creator, "description": description,
        })

    todo = [p for p in plan if p["id"] not in existing_ids and p["short"] not in _ledger]

    print("\n─── PLAN ───")
    print(f"  inbox videos:              {len(videos)}")
    print(f"  skipped (duplicate short): {len(skipped_dupe)}")
    print(f"  skipped (> {MAX_SRC_MB}MB source):  {len(skipped_large)}")
    print(f"  to import (total):         {len(plan)}")
    print(f"  already done (resume):     {len(plan) - len(todo)}")
    remaining = todo[:MAX_COUNT] if MAX_COUNT else todo
    print(f"  this run:                  {len(remaining)}" + (f" (MAX_COUNT={MAX_COUNT})" if MAX_COUNT else ""))
    if plan:
        print(f"  new id range:              {plan[0]['id']} … {plan[-1]['id']}  (total after: {len(existing_ids) + len(plan)})")
    if skipped_large:
        print("\n  oversized (skipped — review manually):")
        for key, mb in skipped_large:
            print(f"    {mb:6.1f}MB  {key}")
    by_creator: dict[str, int] = {}
    for p in plan:
        by_creator[p["creator"]] = by_creator.get(p["creator"], 0) + 1
    print("\n  creator breakdown:")
    for c, cnt in sorted(by_creator.items(), key=lambda x: -x[1]):
        print(f"    {cnt:5}  {c}")

    if PLAN_ONLY:
        print("\nPLAN_ONLY — no writes. Exiting.")
        return

    print(f"\n─── IMPORT ({len(remaining)} items) ───", flush=True)
    done = {"ok": 0, "fail": 0}
    done_urls: list[str] = []
    failures: list[dict] = []
    counter_lock = threading.Lock()
    processed = 0
    total = len(remaining)

    def work(item: dict) -> None:
        nonlocal processed
        with tempfile.TemporaryDirectory(prefix="reelimp-") as td:
            tdp = Path(td)
            src = tdp / f"{item['id']}.src.mp4"
            out = tdp / f"{item['id']}.mp4"
            jpg = tdp / f"{item['id']}.jpg"
            try:
                download(item["srcKey"], src)
                transcode(src, out)
                extract_thumb(out, jpg)
                upload(out, f"reels/{item['id']}.mp4", "video/mp4")
                upload(jpg, f"reel-thumbs/{item['id']}.jpg", "image/jpeg")
                status = insert_reel(item)
                mark_done(item["short"])
                with counter_lock:
                    done["ok"] += 1
                    done_urls.append(f"{CDN_BASE}/reels/{item['id']}.mp4")
                    done_urls.append(f"{CDN_BASE}/reel-thumbs/{item['id']}.jpg")
                    processed += 1
                    out_mb = out.stat().st_size / 1024 / 1024
                    log.info(f"✓ {item['id']} {item['size']/1024/1024:.1f}→{out_mb:.1f}MB {item['creator']} "
                             f"({status}) [{processed}/{total}]")
            except Exception as e:
                with counter_lock:
                    done["fail"] += 1; processed += 1
                    failures.append({"id": item["id"], "key": item["srcKey"], "err": str(e)})
                log.error(f"✗ {item['id']} FAILED: {e} [{processed}/{total}]")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, remaining))

    purge_cache(done_urls)

    print(f"\nDone. imported={done['ok']} failed={done['fail']} remaining={len(todo) - len(remaining)}")
    if failures:
        fp = Path(__file__).with_name("import_failures.json")
        fp.write_text(json.dumps(failures, indent=2, ensure_ascii=False))
        print(f"  failures → {fp}  (re-run the script to retry them)")


if __name__ == "__main__":
    main()
