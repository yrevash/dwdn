#!/usr/bin/env python3
"""Scheduled reel scraper — runs alongside the DM watcher.

For every account in targets.txt it does two things:

  1. BACKFILL — walk the account's ENTIRE reel history, page by page, resumable
     via a saved cursor in scrape_state.json. A restart continues where it left
     off instead of starting over.
  2. ONGOING  — once an account is fully backfilled, periodically re-check its
     newest page for new reels.

Every reel found is handed to the same download → HEVC → R2 → manifest pipeline;
shared dedup means nothing is downloaded twice.

A historical crawl of a large account is the single riskiest thing this bot
does. It is deliberately SLOW — one page per account per round, jittered delays,
resumable cursors — to keep the Instagram account from getting flagged. Use a
pre-seeded session and ideally a residential IG_PROXY. Do not crank the pace up.
"""

import os
import json
import random
import logging
import threading
from pathlib import Path

log = logging.getLogger("dwdn-bot.scraper")

SCRAPE_ENABLED   = os.getenv("SCRAPE_ENABLED", "true").lower() == "true"
SCRAPE_BACKFILL  = os.getenv("SCRAPE_BACKFILL", "true").lower() == "true"
SCRAPE_INTERVAL  = int(os.getenv("SCRAPE_INTERVAL", "7200"))      # idle re-check (s), 2h
SCRAPE_PAGE_SIZE = int(os.getenv("SCRAPE_PAGE_SIZE", "30"))       # reels fetched per API page
SCRAPE_PAGE_DELAY = int(os.getenv("SCRAPE_PAGE_DELAY", "60"))     # base seconds between backfill pages
SCRAPE_MAX_PER_ACCOUNT = int(os.getenv("SCRAPE_MAX_PER_ACCOUNT", "0"))  # 0 = unlimited
TARGETS_FILE     = Path(os.getenv("TARGETS_FILE", "targets.txt"))
STATE_FILE       = Path(os.getenv("SCRAPE_STATE_FILE", "scrape_state.json"))

# Jittered spacing between per-account API calls so traffic isn't bursty.
ACCOUNT_DELAY_MIN = float(os.getenv("SCRAPE_ACCOUNT_DELAY_MIN", "20"))
ACCOUNT_DELAY_MAX = float(os.getenv("SCRAPE_ACCOUNT_DELAY_MAX", "60"))

_state_lock = threading.Lock()


def load_targets() -> list[str]:
    """Read targets.txt — one username per line. '#' comments and blanks ignored.

    Accepts bare usernames, '@username', or a full profile URL; normalizes them.
    """
    if not TARGETS_FILE.exists():
        return []
    targets = []
    for raw in TARGETS_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A full URL like .../<username>/reels/ → take the first path segment
        # after the domain. Otherwise treat the line as a bare handle.
        if "instagram.com/" in line:
            handle = line.split("instagram.com/")[-1].split("/")[0]
        else:
            handle = line
        handle = handle.lstrip("@").split("?")[0].strip("/")
        if handle:
            targets.append(handle)
    return targets


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with _state_lock:
            STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning(f"Could not save scrape state: {e}")


def _is_reel(media) -> bool:
    # Reels (clips) have product_type == "clips".
    return getattr(media, "product_type", "") == "clips"


def _queue_reels(medias, username, submit) -> int:
    queued = 0
    for media in medias:
        if not _is_reel(media):
            continue
        if not getattr(media, "code", None):
            continue
        submit(media, username)   # pass the media object — it carries video_url
        queued += 1
    return queued


def _scrape_account(cl, username, state, submit) -> bool:
    """Advance one account by one page. Returns True if more backfill remains."""
    acct = state.setdefault(username, {"cursor": "", "done": False, "count": 0})

    try:
        user_id = cl.user_id_from_username(username)
    except Exception as e:
        log.warning(f"  @{username}: cannot resolve user id: {e}")
        return False

    # ONGOING (already backfilled) — re-check the newest page only.
    if acct.get("done"):
        try:
            medias, _ = cl.user_medias_paginated(user_id, amount=SCRAPE_PAGE_SIZE, end_cursor="")
            q = _queue_reels(medias, username, submit)
            log.info(f"  @{username}: ongoing check → {q} reel(s) queued")
        except Exception as e:
            log.warning(f"  @{username}: ongoing check failed: {e}")
        return False

    # BACKFILL — fetch the next older page using the saved cursor.
    try:
        medias, next_cursor = cl.user_medias_paginated(
            user_id, amount=SCRAPE_PAGE_SIZE, end_cursor=acct.get("cursor", "")
        )
    except Exception as e:
        log.warning(f"  @{username}: backfill page failed: {e}")
        return True  # leave 'done' false; retry this page next round

    q = _queue_reels(medias, username, submit)
    acct["count"] = acct.get("count", 0) + q
    acct["cursor"] = next_cursor or ""

    capped = SCRAPE_MAX_PER_ACCOUNT and acct["count"] >= SCRAPE_MAX_PER_ACCOUNT
    if not next_cursor or capped:
        acct["done"] = True
        log.info(f"  @{username}: backfill COMPLETE — {acct['count']} reels queued total")
    else:
        log.info(f"  @{username}: backfill page → {q} reel(s) (running total {acct['count']})")

    _save_state(state)
    return not acct["done"]


def run_scraper(cl, submit, shutdown) -> None:
    """Background loop. `submit(url, source_username)` queues a reel for download."""
    if not SCRAPE_ENABLED:
        log.info("Scheduled scraping disabled (SCRAPE_ENABLED=false)")
        return

    log.info(f"Scraper started — backfill={SCRAPE_BACKFILL}, page={SCRAPE_PAGE_SIZE}, "
             f"idle re-check every {SCRAPE_INTERVAL}s, targets file: {TARGETS_FILE}")
    shutdown.wait(30)  # let login + DM seed settle first

    state = _load_state()

    while not shutdown.is_set():
        targets = load_targets()
        if not targets:
            log.info(f"No scrape targets in {TARGETS_FILE} — re-checking in {SCRAPE_INTERVAL}s")
            shutdown.wait(SCRAPE_INTERVAL)
            continue

        backfilling = False
        for username in targets:
            if shutdown.is_set():
                return
            if not SCRAPE_BACKFILL:
                # backfill disabled → treat every account as ongoing-only
                state.setdefault(username, {"cursor": "", "count": 0})["done"] = True
            more = _scrape_account(cl, username, state, submit)
            backfilling = backfilling or more
            shutdown.wait(random.uniform(ACCOUNT_DELAY_MIN, ACCOUNT_DELAY_MAX))

        if backfilling:
            # still draining history — short jittered gap, then the next pages
            gap = SCRAPE_PAGE_DELAY * random.uniform(0.8, 1.2)
            log.info(f"Backfill in progress — next pages in ~{int(gap)}s")
            shutdown.wait(max(30, gap))
        else:
            # everything backfilled — idle until the next ongoing re-check
            jitter = SCRAPE_INTERVAL * random.uniform(-0.1, 0.1)
            shutdown.wait(max(60, SCRAPE_INTERVAL + jitter))
