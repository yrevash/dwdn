#!/usr/bin/env python3
"""
Instagram DM Watcher — Battle-hardened edition
Handles: rate limits, session expiry, cookie refresh, yt-dlp retries,
         duplicate detection, exponential backoff, parallel downloads,
         persistent state across restarts.
"""

import os
import re
import time
import json
import signal
import logging
import hashlib
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, ChallengeRequired,
    RateLimitError, ClientError, ClientConnectionError,
    ClientThrottledError,
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

IG_USERNAME         = os.getenv("IG_USERNAME", "")
IG_PASSWORD         = os.getenv("IG_PASSWORD", "")
DOWNLOAD_DIR        = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL", "30"))
GDRIVE_REMOTE       = os.getenv("GDRIVE_REMOTE", "")
DELETE_AFTER_UPLOAD = os.getenv("DELETE_AFTER_UPLOAD", "true").lower() == "true"
MAX_WORKERS         = int(os.getenv("MAX_WORKERS", "3"))   # parallel downloads

SESSION_FILE    = Path("session.json")
COOKIES_FILE    = Path("ig_cookies.txt")
SEEN_FILE       = Path("seen_ids.json")       # persists across restarts
DOWNLOADED_FILE = Path("downloaded_urls.json") # dedup by URL

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dwdn-bot")

# ─── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

_shutdown = threading.Event()

def _handle_signal(sig, frame):
    log.info("Shutting down gracefully...")
    _shutdown.set()

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ─── PERSISTENT STATE ──────────────────────────────────────────────────────────

def load_json_set(path: Path) -> set:
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()

def save_json_set(path: Path, data: set) -> None:
    try:
        path.write_text(json.dumps(list(data)))
    except Exception as e:
        log.warning(f"Could not save {path}: {e}")

# ─── URL EXTRACTION ────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s\"']+")
REEL_CODE_RE = re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)")

def extract_urls(text: str) -> list:
    return URL_RE.findall(text or "")

def reel_code_from_str(s: str) -> str | None:
    codes = REEL_CODE_RE.findall(s)
    return codes[0] if codes else None

def url_fingerprint(url: str) -> str:
    return hashlib.md5(url.strip().rstrip("/").encode()).hexdigest()

# ─── COOKIES ───────────────────────────────────────────────────────────────────

def write_cookies(cl: Client) -> None:
    try:
        cookies = cl.cookie_dict
        if not cookies:
            return
        lines = ["# Netscape HTTP Cookie File\n"]
        for name, value in cookies.items():
            lines.append(f".instagram.com\tTRUE\t/\tTRUE\t9999999999\t{name}\t{value}\n")
        COOKIES_FILE.write_text("".join(lines))
        log.info(f"Cookies updated ({len(cookies)} entries)")
    except Exception as e:
        log.warning(f"Cookie write failed: {e}")

# ─── INSTAGRAM LOGIN ───────────────────────────────────────────────────────────

def make_client() -> Client:
    cl = Client()
    cl.delay_range = [3, 7]  # human-like delays between requests
    return cl

def login(cl: Client) -> None:
    if SESSION_FILE.exists():
        log.info("Loading saved session...")
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(IG_USERNAME, IG_PASSWORD)
            write_cookies(cl)
            log.info("Session resumed")
            return
        except Exception as e:
            log.warning(f"Saved session failed ({e}), fresh login...")
            SESSION_FILE.unlink(missing_ok=True)

    log.info(f"Logging in as @{IG_USERNAME}...")
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    write_cookies(cl)
    log.info("Logged in — session saved")

# ─── YT-DLP DOWNLOAD ───────────────────────────────────────────────────────────

# Format preference order: best mp4 merge → any best merge → single best
FORMATS = [
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",
    "best[ext=mp4]/best",
]

def _run_ytdlp(url: str, fmt: str, output_template: str) -> subprocess.CompletedProcess:
    cmd = ["yt-dlp"]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd += [
        "--no-playlist",
        "--no-warnings",
        "--retries", "5",
        "--fragment-retries", "5",
        "--retry-sleep", "5",
        "--sleep-requests", "2",     # 2s between API requests
        "--sleep-interval", "3",     # 3s between downloads
        "--max-sleep-interval", "8", # random up to 8s
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--no-part",
        "--print", "after_move:filepath",
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

def download_video(url: str, downloaded_urls: set) -> bool:
    fp = url_fingerprint(url)
    if fp in downloaded_urls:
        log.info(f"Already downloaded, skipping: {url}")
        return True

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_template = str(DOWNLOAD_DIR / f"{timestamp}_%(title).80s.%(ext)s")

    log.info(f"Downloading: {url}")

    for attempt, fmt in enumerate(FORMATS, 1):
        try:
            result = _run_ytdlp(url, fmt, output_template)

            if result.returncode == 0:
                output_path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None
                log.info(f"Downloaded: {output_path or 'ok'}")

                # Mark as downloaded before upload (so crash during upload doesn't re-download)
                downloaded_urls.add(fp)
                save_json_set(DOWNLOADED_FILE, downloaded_urls)

                # Upload to Drive
                if GDRIVE_REMOTE and output_path:
                    upload_to_drive(Path(output_path))
                elif GDRIVE_REMOTE:
                    for f in DOWNLOAD_DIR.glob(f"{timestamp}*.mp4"):
                        upload_to_drive(f)

                return True

            stderr = result.stderr[-500:]

            # Specific error handling
            if "rate-limit" in stderr.lower() or "429" in stderr:
                log.warning(f"yt-dlp rate limited, waiting 60s...")
                time.sleep(60)
            elif "login required" in stderr.lower() or "cookies" in stderr.lower():
                log.warning(f"yt-dlp needs auth (attempt {attempt}/{len(FORMATS)}), trying next format...")
            else:
                log.error(f"yt-dlp failed (attempt {attempt}): {stderr}")

        except subprocess.TimeoutExpired:
            log.error(f"yt-dlp timed out (attempt {attempt})")
        except Exception as e:
            log.error(f"yt-dlp error (attempt {attempt}): {e}")

    log.error(f"All download attempts failed for: {url}")
    return False

# ─── GOOGLE DRIVE UPLOAD ───────────────────────────────────────────────────────

def upload_to_drive(file_path: Path, retries: int = 3) -> bool:
    if not GDRIVE_REMOTE or not file_path.exists():
        return False

    log.info(f"Uploading to Drive: {file_path.name}")

    for attempt in range(1, retries + 1):
        result = subprocess.run(
            ["rclone", "copy", str(file_path), GDRIVE_REMOTE, "--progress"],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0:
            log.info(f"Uploaded: {file_path.name}")
            if DELETE_AFTER_UPLOAD:
                file_path.unlink(missing_ok=True)
                log.info(f"Deleted local: {file_path.name}")
            return True

        log.warning(f"rclone attempt {attempt}/{retries} failed: {result.stderr[-200:]}")
        if attempt < retries:
            time.sleep(10 * attempt)  # backoff

    log.error(f"Upload failed after {retries} attempts: {file_path.name}")
    return False

# ─── MESSAGE PARSING ───────────────────────────────────────────────────────────

def extract_url_from_msg(msg) -> str | None:
    """Extract a downloadable URL from any message type."""
    t = msg.item_type

    # Text with pasted URL
    if t == "text":
        urls = extract_urls(msg.text or "")
        return urls[0] if urls else None

    # Modern reel share (xma_clip, xma_media_share)
    if t in ("xma_clip", "xma_media_share", "xma_link"):
        # Try xma_share attribute
        xma = getattr(msg, "xma_share", None)
        if isinstance(xma, dict):
            code = xma.get("shortcode") or xma.get("code")
            if not code:
                target = xma.get("target_url", "") or xma.get("url", "")
                code = reel_code_from_str(target)
            if code:
                return f"https://www.instagram.com/reel/{code}/"

        # Scan full string representation for shortcode
        code = reel_code_from_str(str(msg))
        if code:
            return f"https://www.instagram.com/reel/{code}/"

        # Try extracting any instagram URL from repr
        urls = [u for u in extract_urls(str(msg)) if "instagram.com" in u]
        return urls[0] if urls else None

    # Old clip/media share format
    if t in ("clip", "felix_share"):
        clip = getattr(msg, "clip", None)
        if isinstance(clip, dict):
            code = clip.get("code") or reel_code_from_str(str(clip))
            if code:
                return f"https://www.instagram.com/reel/{code}/"

    if t == "media_share":
        ms = getattr(msg, "media_share", None)
        if isinstance(ms, dict):
            code = ms.get("code")
            if code:
                return f"https://www.instagram.com/p/{code}/"

    # Link share
    if t == "link":
        try:
            link = getattr(msg, "link", {}) or {}
            url = link.get("link_context", {}).get("link_url", "")
            return url or None
        except Exception:
            pass

    return None

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("Set IG_USERNAME and IG_PASSWORD env vars")

    cl = make_client()
    login(cl)

    seen_ids       = load_json_set(SEEN_FILE)
    downloaded_urls = load_json_set(DOWNLOADED_FILE)

    log.info(f"Loaded {len(seen_ids)} seen IDs, {len(downloaded_urls)} downloaded URLs")
    log.info(f"Polling every {POLL_INTERVAL}s — saving to {DOWNLOAD_DIR.resolve()}")

    # Seed existing messages so we skip them on first run
    log.info("Seeding existing messages...")
    try:
        threads = cl.direct_threads(amount=20)
        for thread in threads:
            msgs = cl.direct_messages(thread.id, amount=20)
            for m in msgs:
                seen_ids.add(str(m.id))
        save_json_set(SEEN_FILE, seen_ids)
        log.info(f"Seeded {len(seen_ids)} messages — watching for new ones")
    except Exception as e:
        log.warning(f"Seed failed: {e}")

    consecutive_errors = 0
    cookies_refresh_counter = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while not _shutdown.is_set():
            try:
                threads = cl.direct_threads(amount=20)
                futures = []

                for thread in threads:
                    msgs = cl.direct_messages(thread.id, amount=10)
                    for msg in msgs:
                        mid = str(msg.id)
                        if mid in seen_ids:
                            continue

                        seen_ids.add(mid)
                        sender = getattr(msg, "user_id", "?")
                        log.info(f"NEW MSG from {sender} | type={msg.item_type}")

                        url = extract_url_from_msg(msg)
                        if url:
                            log.info(f"Queuing download: {url}")
                            fut = executor.submit(download_video, url, downloaded_urls)
                            futures.append(fut)
                        else:
                            log.info(f"No downloadable URL in {msg.item_type} message")

                # Wait for all downloads in this batch
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        log.error(f"Download task error: {e}")

                save_json_set(SEEN_FILE, seen_ids)

                consecutive_errors = 0

                # Refresh cookies every 10 cycles (~5 min) to keep them fresh
                cookies_refresh_counter += 1
                if cookies_refresh_counter >= 10:
                    write_cookies(cl)
                    cookies_refresh_counter = 0

            except (RateLimitError, ClientThrottledError):
                wait = min(300, 60 * (consecutive_errors + 1))
                log.warning(f"Rate limited — waiting {wait}s")
                _shutdown.wait(wait)
                consecutive_errors += 1

            except LoginRequired:
                log.warning("Session expired — re-logging in...")
                try:
                    login(cl)
                    consecutive_errors = 0
                except Exception as e:
                    log.error(f"Re-login failed: {e}")
                    _shutdown.wait(30)

            except ChallengeRequired:
                log.error("Instagram challenge required — check the account manually, waiting 5 min")
                _shutdown.wait(300)

            except (ClientConnectionError, ClientError) as e:
                consecutive_errors += 1
                wait = min(120, 10 * consecutive_errors)
                log.warning(f"Instagram API error ({e}) — retrying in {wait}s")
                _shutdown.wait(wait)

            except Exception as e:
                consecutive_errors += 1
                wait = min(120, 15 * consecutive_errors)
                log.error(f"Unexpected error ({e}) — retrying in {wait}s")
                _shutdown.wait(wait)

            else:
                _shutdown.wait(POLL_INTERVAL)

    log.info("Bot stopped.")


if __name__ == "__main__":
    run()
