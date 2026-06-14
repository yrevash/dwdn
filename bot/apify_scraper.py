#!/usr/bin/env python3
"""Apify-powered reel scraper — runs alongside the DM watcher.

Calls the Apify Instagram Reel Scraper actor for the accounts in targets.txt,
then runs each returned reel's videoUrl through the HEVC -> R2 -> manifest
pipeline (apify.download_reel). Instagram is scraped by Apify's infrastructure,
NOT our account, so this path carries zero ban risk.

Cost control: one-time BACKFILL (up to APIFY_BACKFILL_LIMIT reels/account), then
incremental runs that fetch only reels newer than the last one seen — so Apify
only bills for genuinely-new reels.
"""

import os
import json
import logging
from pathlib import Path

from apify_client import ApifyClient

import apify

log = logging.getLogger("dwdn-bot.apify-scraper")

APIFY_TOKEN          = os.getenv("APIFY_TOKEN", "") or os.getenv("APPIFY_TOKEN", "")
APIFY_ACTOR          = os.getenv("APIFY_ACTOR", "xMc5Ga1oCONPmWJIa")  # Instagram Reel Scraper
APIFY_ENABLED        = os.getenv("APIFY_ENABLED", "true").lower() == "true"
APIFY_BACKFILL_LIMIT = int(os.getenv("APIFY_BACKFILL_LIMIT", "1000"))  # per account, first run
APIFY_ONGOING_LIMIT  = int(os.getenv("APIFY_ONGOING_LIMIT", "50"))     # per account, later runs
APIFY_INTERVAL       = int(os.getenv("APIFY_INTERVAL", "21600"))       # seconds between runs (6h)
TARGETS_FILE         = Path(os.getenv("TARGETS_FILE", "targets.txt"))
STATE_FILE           = Path(os.getenv("APIFY_STATE_FILE", "apify_state.json"))
PENDING_FILE         = Path(os.getenv("APIFY_PENDING_FILE", "apify_pending.json"))


def load_targets() -> list[str]:
    if not TARGETS_FILE.exists():
        return []
    out = []
    for raw in TARGETS_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "instagram.com/" in line:
            line = line.split("instagram.com/")[-1].split("/")[0]
        h = line.lstrip("@").split("?")[0].strip("/")
        if h:
            out.append(h)
    return out


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(s, indent=2))
    except Exception as e:
        log.warning(f"could not save {STATE_FILE}: {e}")


def _load_pending():
    """A dataset already scraped from Apify but not yet fully processed."""
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return None


def _save_pending(items) -> None:
    try:
        PENDING_FILE.write_text(json.dumps(items))
    except Exception as e:
        log.warning(f"could not save {PENDING_FILE}: {e}")


def _clear_pending() -> None:
    try:
        PENDING_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _dataset_id(run):
    """apify-client returns a dict (older) or a pydantic Run object (2.x)."""
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    return getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)


def run_apify_scraper(shutdown) -> None:
    if not APIFY_ENABLED:
        log.info("Apify scraping disabled (APIFY_ENABLED=false)")
        return
    if not APIFY_TOKEN:
        log.error("APIFY_TOKEN not set — Apify scraping is OFF")
        return

    client = ApifyClient(APIFY_TOKEN)
    downloaded = apify.load_downloaded()
    log.info(f"Apify scraper started — actor={APIFY_ACTOR}, every {APIFY_INTERVAL}s")
    shutdown.wait(20)  # let login + DM seed settle first

    while not shutdown.is_set():
        # 1) Prefer a dataset already scraped but not finished — costs NOTHING
        #    (we only pay to RUN the actor, never to re-read a saved dataset).
        pending = _load_pending()

        if pending is None:
            targets = load_targets()
            if not targets:
                log.info(f"No targets in {TARGETS_FILE} — re-checking in {APIFY_INTERVAL}s")
                shutdown.wait(APIFY_INTERVAL)
                continue

            state = _load_state()
            backfilled = state.get("backfilled", False)
            last_seen = state.get("last_seen")

            run_input = {
                "username": targets,
                "resultsLimit": APIFY_ONGOING_LIMIT if backfilled else APIFY_BACKFILL_LIMIT,
                "skipPinnedPosts": False,
                "includeDownloadedVideo": False,
            }
            # The actor rejects null — only include the date filter when we have one.
            if backfilled and last_seen:
                run_input["onlyPostsNewerThan"] = str(last_seen)
            mode = "incremental" if backfilled else "BACKFILL"
            log.info(f"Apify {mode} run: {len(targets)} account(s), "
                     f"limit={run_input['resultsLimit']}/acct, newer_than={last_seen}")

            try:
                run = client.actor(APIFY_ACTOR).call(run_input=run_input)
                pending = list(client.dataset(_dataset_id(run)).iterate_items())
            except Exception as e:
                log.error(f"Apify run failed: {e} — retrying next cycle")
                shutdown.wait(min(APIFY_INTERVAL, 1800))
                continue

            _save_pending(pending)  # persist NOW so any restart resumes without re-charging
            log.info(f"Apify returned {len(pending)} reels — saved to {PENDING_FILE}; "
                     f"processing (restarts will NOT re-scrape)")
        else:
            log.info(f"Resuming {len(pending)} reels from {PENDING_FILE} — no Apify charge")

        # 2) Process the saved dataset; dedup skips anything already uploaded, so a
        #    resumed run just picks up where it left off.
        downloaded = apify.load_downloaded()
        newest = _load_state().get("last_seen")
        for it in pending:
            ts = it.get("timestamp")
            if ts and (not newest or str(ts) > str(newest)):
                newest = ts

        up = sk = fa = 0
        interrupted = False
        for item in pending:
            if shutdown.is_set():
                interrupted = True
                break
            try:
                res = apify.download_reel(item, downloaded)
            except Exception as e:
                log.warning(f"reel failed: {e}")
                res = "fail"
            up += res == "uploaded"
            sk += res == "skip"
            fa += res == "fail"

        if interrupted:
            log.info(f"Stopped mid-batch — {PENDING_FILE} kept; will resume on restart (no re-charge)")
            return

        # 3) Whole dataset done → advance state and delete the local dataset.
        log.info(f"Apify cycle done: {up} uploaded, {sk} skipped (dupes), {fa} failed")
        state = _load_state()
        state["backfilled"] = True
        if newest:
            state["last_seen"] = newest
        _save_state(state)
        _clear_pending()

        shutdown.wait(APIFY_INTERVAL)
