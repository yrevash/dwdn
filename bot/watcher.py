#!/usr/bin/env python3
"""
Instagram DM Watcher
Monitors a dummy Instagram account for reel/video links sent via DM
and auto-downloads them using yt-dlp.
"""

import os
import re
import time
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired

# ─── CONFIG ────────────────────────────────────────────────────────────────────

INSTAGRAM_USERNAME = os.getenv("IG_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("IG_PASSWORD", "")

# Where downloaded videos are saved
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))

# How often to check DMs (seconds)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

# Session file to avoid re-login every restart
SESSION_FILE = Path("session.json")

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dwdn-bot")

# ─── URL DETECTION ─────────────────────────────────────────────────────────────

URL_PATTERN = re.compile(
    r"https?://(?:"
    r"(?:www\.)?instagram\.com/(?:reel|reels|p)/[^\s]+"
    r"|(?:www\.)?youtube\.com/(?:shorts|watch)[^\s]+"
    r"|youtu\.be/[^\s]+"
    r"|(?:www\.)?tiktok\.com/[^\s]+"
    r"|(?:vm\.)?tiktok\.com/[^\s]+"
    r"|(?:www\.)?twitter\.com/[^\s]+"
    r"|x\.com/[^\s]+"
    r"|[^\s]+)"
)

def extract_urls(text: str) -> list[str]:
    return URL_PATTERN.findall(text or "")

# ─── DOWNLOADER ────────────────────────────────────────────────────────────────

def download_video(url: str) -> bool:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_template = str(DOWNLOAD_DIR / f"{timestamp}_%(title).80s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--no-part",
        url,
    ]

    log.info(f"Downloading: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode == 0:
        log.info(f"Downloaded successfully")
        return True
    else:
        log.error(f"yt-dlp failed: {result.stderr[-300:]}")
        return False

# ─── INSTAGRAM CLIENT ──────────────────────────────────────────────────────────

def login(cl: Client) -> None:
    if SESSION_FILE.exists():
        log.info("Loading saved session...")
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            log.info("Resumed session")
            return
        except Exception:
            log.warning("Saved session invalid, logging in fresh...")
            SESSION_FILE.unlink(missing_ok=True)

    log.info(f"Logging in as @{INSTAGRAM_USERNAME}...")
    cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    log.info("Logged in and session saved")

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
        raise RuntimeError("Set IG_USERNAME and IG_PASSWORD env vars")

    cl = Client()
    cl.delay_range = [2, 5]  # human-like delays

    login(cl)

    seen_message_ids: set[str] = set()
    log.info(f"Watching DMs every {POLL_INTERVAL}s... saving to {DOWNLOAD_DIR.resolve()}")

    # Seed seen messages on first run so we don't re-download old stuff
    log.info("Seeding existing messages (skipping old ones)...")
    try:
        threads = cl.direct_threads(amount=20)
        for thread in threads:
            messages = cl.direct_messages(thread.id, amount=20)
            for msg in messages:
                seen_message_ids.add(str(msg.id))
        log.info(f"Seeded {len(seen_message_ids)} existing messages — watching for new ones")
    except Exception as e:
        log.warning(f"Could not seed messages: {e}")

    while True:
        try:
            threads = cl.direct_threads(amount=20)

            for thread in threads:
                messages = cl.direct_messages(thread.id, amount=10)

                for msg in messages:
                    msg_id = str(msg.id)
                    if msg_id in seen_message_ids:
                        continue

                    seen_message_ids.add(msg_id)
                    sender = getattr(msg, "user_id", "unknown")

                    # Check text messages for URLs
                    if msg.item_type == "text":
                        urls = extract_urls(msg.text or "")
                        for url in urls:
                            log.info(f"New URL from {sender}: {url}")
                            download_video(url)

                    # Check if someone shared a reel/clip directly
                    elif msg.item_type in ("clip", "media", "felix_share"):
                        # Try to get the media URL from the share
                        try:
                            if hasattr(msg, "clip") and msg.clip:
                                code = msg.clip.get("code") or msg.clip.get("pk")
                                if code:
                                    url = f"https://www.instagram.com/reel/{code}/"
                                    log.info(f"Shared reel from {sender}: {url}")
                                    download_video(url)
                            elif hasattr(msg, "media_share") and msg.media_share:
                                pk = msg.media_share.get("pk") or msg.media_share.get("id")
                                code = msg.media_share.get("code")
                                if code:
                                    url = f"https://www.instagram.com/p/{code}/"
                                    log.info(f"Shared post from {sender}: {url}")
                                    download_video(url)
                        except Exception as e:
                            log.warning(f"Could not parse shared media: {e}")

                    # Link shares
                    elif msg.item_type == "link":
                        try:
                            link_url = msg.link.get("link_context", {}).get("link_url", "")
                            if link_url:
                                log.info(f"Link from {sender}: {link_url}")
                                download_video(link_url)
                        except Exception as e:
                            log.warning(f"Could not parse link: {e}")

        except LoginRequired:
            log.warning("Session expired, re-logging in...")
            login(cl)
        except ChallengeRequired:
            log.error("Instagram challenge required (check the account manually)")
            time.sleep(60)
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
