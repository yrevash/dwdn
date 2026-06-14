#!/usr/bin/env python3
"""Ingest an Apify Instagram Reel Scraper dataset (JSON) → R2.

  python apify_ingest.py <dataset.json> [gap_seconds]

Downloads each reel's videoUrl, transcodes to HEVC, uploads to R2, and records
it in manifest.jsonl — skipping any already in downloaded_urls.json. No
Instagram account is touched, so it's safe to run anywhere, any speed.
"""

import sys
import json
import time
from dotenv import load_dotenv
load_dotenv()

import r2
import apify


def main():
    if len(sys.argv) < 2:
        print("usage: apify_ingest.py <dataset.json> [gap_seconds]")
        return
    path = sys.argv[1]
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

    if not r2.is_configured():
        print("R2 not configured — check .env (R2_ACCOUNT_ID / KEY / SECRET / BUCKET)")
        return

    data = json.load(open(path))
    if not isinstance(data, list):
        data = [data]
    print(f"loaded {len(data)} items from {path}  →  bucket {r2.R2_BUCKET}/{r2.R2_PREFIX}")

    downloaded = apify.load_downloaded()
    counts = {"uploaded": 0, "skip": 0, "fail": 0}
    for i, item in enumerate(data, 1):
        res = apify.download_reel(item, downloaded)
        counts[res] += 1
        print(f"[{i}/{len(data)}] @{item.get('ownerUsername')} {item.get('shortCode')} -> {res}")
        if gap and res == "uploaded":
            time.sleep(gap)

    print(f"\nDONE: {counts['uploaded']} uploaded, {counts['skip']} skipped (dupes), {counts['fail']} failed")


if __name__ == "__main__":
    main()
