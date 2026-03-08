#!/usr/bin/env python3
"""
Instagram DM Watcher — Battle-hardened v3
Fixes: sender username in filename, thread-safe state, unique filenames,
       rclone checksum errors, username cache, locked dedup set.
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
import requests
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
MAX_WORKERS         = int(os.getenv("MAX_WORKERS", "3"))

SESSION_FILE    = Path("session.json")
COOKIES_FILE    = Path("ig_cookies.txt")
SEEN_FILE       = Path("seen_ids.json")
DOWNLOADED_FILE = Path("downloaded_urls.json")
USERNAME_CACHE_FILE = Path("username_cache.json")

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

# ─── THREAD-SAFE STATE ─────────────────────────────────────────────────────────

_state_lock = threading.Lock()   # protects seen_ids + downloaded_urls
_username_lock = threading.Lock() # protects username cache

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

def load_json_dict(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def save_json_dict(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data))
    except Exception as e:
        log.warning(f"Could not save {path}: {e}")

# ─── USERNAME CACHE ────────────────────────────────────────────────────────────

_ig_client: Client | None = None
_username_cache: dict = {}   # user_id (str) -> username (str)

def sanitize(name: str) -> str:
    """Make a string safe for filenames."""
    name = re.sub(r"[^\w\-]", "_", name)
    return name[:40].strip("_") or "unknown"

def get_username(user_id: str) -> str:
    """Resolve user_id to Instagram username. Cached + thread-safe."""
    global _ig_client, _username_cache

    with _username_lock:
        if user_id in _username_cache:
            return _username_cache[user_id]

    try:
        if _ig_client:
            info = _ig_client.user_info_v1(int(user_id))
            username = sanitize(info.username or user_id)
        else:
            username = sanitize(user_id)
    except Exception:
        username = sanitize(user_id)

    with _username_lock:
        _username_cache[user_id] = username
        save_json_dict(USERNAME_CACHE_FILE, _username_cache)

    return username

# ─── URL HELPERS ───────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s\"']+")
REEL_CODE_RE = re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)")
IS_INSTAGRAM_URL = re.compile(r"instagram\.com/(?:reel|reels|p)/")

def extract_urls(text: str) -> list:
    return URL_RE.findall(text or "")

def reel_code_from_str(s: str) -> str | None:
    codes = REEL_CODE_RE.findall(s)
    return codes[0] if codes else None

def url_fingerprint(url: str) -> str:
    return hashlib.md5(url.strip().rstrip("/").encode()).hexdigest()

def make_filename(sender_username: str, label: str, unique_id: str) -> str:
    """Build a guaranteed-unique filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = sanitize(label)[:50]
    return f"{ts}_{sender_username}_{label}_{unique_id}.mp4"

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
    cl.delay_range = [3, 7]
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

# ─── GOOGLE DRIVE UPLOAD ───────────────────────────────────────────────────────

def upload_to_drive(file_path: Path, retries: int = 3) -> bool:
    if not GDRIVE_REMOTE or not file_path.exists():
        return False

    log.info(f"Uploading to Drive: {file_path.name}")

    for attempt in range(1, retries + 1):
        result = subprocess.run(
            [
                "rclone", "copy",
                str(file_path), GDRIVE_REMOTE,
                "--ignore-checksum",   # avoids md5 mismatch false failures
                "--transfers", "1",
            ],
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
            time.sleep(10 * attempt)

    log.error(f"Upload failed after {retries} attempts: {file_path.name}")
    return False

# ─── INSTAGRAPI DIRECT DOWNLOAD ────────────────────────────────────────────────

def download_via_instagrapi(url: str, sender_username: str, downloaded_urls: set) -> bool:
    global _ig_client
    if not _ig_client:
        return False

    fp = url_fingerprint(url)

    try:
        media_pk = _ig_client.media_pk_from_url(url)
        info = _ig_client.media_info_v1(media_pk)
        video_url = str(info.video_url) if info.video_url else ""
        if not video_url:
            log.warning(f"No video_url for {url}")
            return False

        # Get reel title/caption for filename
        caption = ""
        try:
            caption = (info.caption_text or "").split("\n")[0][:50]
        except Exception:
            pass

        filename = make_filename(sender_username, caption or "reel", str(media_pk)[-8:])
        out_path = DOWNLOAD_DIR / filename
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        log.info(f"CDN download → {filename}")
        headers = {"User-Agent": "Instagram 269.0.0.18.75 Android"}

        with requests.get(video_url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)

        log.info(f"Downloaded: {filename}")

        with _state_lock:
            downloaded_urls.add(fp)
            save_json_set(DOWNLOADED_FILE, downloaded_urls)

        if GDRIVE_REMOTE:
            upload_to_drive(out_path)

        return True

    except Exception as e:
        log.warning(f"instagrapi download failed: {e}")
        return False

# ─── YT-DLP DOWNLOAD (non-Instagram) ──────────────────────────────────────────

FORMATS = [
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",
    "best[ext=mp4]/best",
]

def download_via_ytdlp(url: str, sender_username: str, downloaded_urls: set) -> bool:
    fp = url_fingerprint(url)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Unique output template per sender + time
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = hashlib.md5(url.encode()).hexdigest()[:8]
    output_template = str(DOWNLOAD_DIR / f"{ts}_{sender_username}_%(title).50s_{uid}.%(ext)s")

    for attempt, fmt in enumerate(FORMATS, 1):
        cmd = ["yt-dlp"]
        if COOKIES_FILE.exists():
            cmd += ["--cookies", str(COOKIES_FILE)]
        cmd += [
            "--no-playlist", "--no-warnings",
            "--retries", "5",
            "--fragment-retries", "5",
            "--retry-sleep", "5",
            "--sleep-requests", "2",
            "--sleep-interval", "3",
            "--max-sleep-interval", "8",
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--no-part",
            "--print", "after_move:filepath",
            url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                output_path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None
                log.info(f"yt-dlp downloaded: {Path(output_path).name if output_path else 'ok'}")

                with _state_lock:
                    downloaded_urls.add(fp)
                    save_json_set(DOWNLOADED_FILE, downloaded_urls)

                if GDRIVE_REMOTE and output_path and Path(output_path).exists():
                    upload_to_drive(Path(output_path))

                return True

            stderr = result.stderr[-500:]
            if "rate-limit" in stderr.lower() or "429" in stderr:
                log.warning(f"yt-dlp rate limited — waiting 60s (attempt {attempt})")
                time.sleep(60)
            elif "login required" in stderr.lower() or "cookies" in stderr.lower():
                log.warning(f"yt-dlp auth error (attempt {attempt})")
            else:
                log.error(f"yt-dlp failed (attempt {attempt}): {stderr[-200:]}")

        except subprocess.TimeoutExpired:
            log.error(f"yt-dlp timed out (attempt {attempt})")
        except Exception as e:
            log.error(f"yt-dlp error (attempt {attempt}): {e}")

    log.error(f"All download attempts failed: {url}")
    return False

# ─── UNIFIED DOWNLOAD ENTRY ────────────────────────────────────────────────────

def download_video(url: str, sender_id: str, downloaded_urls: set) -> bool:
    fp = url_fingerprint(url)

    with _state_lock:
        if fp in downloaded_urls:
            log.info(f"Already downloaded, skipping: {url}")
            return True

    sender_username = get_username(sender_id)
    log.info(f"Downloading [{sender_username}]: {url}")

    if IS_INSTAGRAM_URL.search(url):
        ok = download_via_instagrapi(url, sender_username, downloaded_urls)
        if ok:
            return True
        log.info("Falling back to yt-dlp...")

    return download_via_ytdlp(url, sender_username, downloaded_urls)

# ─── MESSAGE PARSING ───────────────────────────────────────────────────────────

def extract_url_from_msg(msg) -> str | None:
    t = msg.item_type

    if t == "text":
        urls = extract_urls(msg.text or "")
        return urls[0] if urls else None

    if t in ("xma_clip", "xma_media_share", "xma_link"):
        xma = getattr(msg, "xma_share", None)
        if isinstance(xma, dict):
            code = xma.get("shortcode") or xma.get("code")
            if not code:
                target = xma.get("target_url", "") or xma.get("url", "")
                code = reel_code_from_str(target)
            if code:
                return f"https://www.instagram.com/reel/{code}/"
        code = reel_code_from_str(str(msg))
        if code:
            return f"https://www.instagram.com/reel/{code}/"
        urls = [u for u in extract_urls(str(msg)) if "instagram.com" in u]
        return urls[0] if urls else None

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

    global _ig_client, _username_cache

    cl = make_client()
    login(cl)
    _ig_client = cl

    seen_ids        = load_json_set(SEEN_FILE)
    downloaded_urls = load_json_set(DOWNLOADED_FILE)
    _username_cache = load_json_dict(USERNAME_CACHE_FILE)

    log.info(f"Loaded {len(seen_ids)} seen IDs, {len(downloaded_urls)} downloaded, {len(_username_cache)} cached usernames")
    log.info(f"Polling every {POLL_INTERVAL}s — saving to {DOWNLOAD_DIR.resolve()}")

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
    cookies_counter = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while not _shutdown.is_set():
            try:
                threads = cl.direct_threads(amount=20)
                futures = []

                for thread in threads:
                    msgs = cl.direct_messages(thread.id, amount=10)
                    for msg in msgs:
                        mid = str(msg.id)

                        with _state_lock:
                            if mid in seen_ids:
                                continue
                            seen_ids.add(mid)

                        sender_id = str(getattr(msg, "user_id", "0"))
                        log.info(f"NEW MSG from {sender_id} | type={msg.item_type}")

                        url = extract_url_from_msg(msg)
                        if url:
                            log.info(f"Queuing: {url}")
                            fut = executor.submit(download_video, url, sender_id, downloaded_urls)
                            futures.append(fut)
                        else:
                            log.info(f"No URL in {msg.item_type} message")

                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        log.error(f"Download error: {e}")

                with _state_lock:
                    save_json_set(SEEN_FILE, seen_ids)

                consecutive_errors = 0
                cookies_counter += 1
                if cookies_counter >= 10:
                    write_cookies(cl)
                    cookies_counter = 0

            except (RateLimitError, ClientThrottledError):
                wait = min(300, 60 * (consecutive_errors + 1))
                log.warning(f"Instagram rate limited — waiting {wait}s")
                _shutdown.wait(wait)
                consecutive_errors += 1

            except LoginRequired:
                log.warning("Session expired — re-logging in...")
                try:
                    login(cl)
                    _ig_client = cl
                    consecutive_errors = 0
                except Exception as e:
                    log.error(f"Re-login failed: {e}")
                    _shutdown.wait(30)

            except ChallengeRequired:
                log.error("Instagram challenge — check account manually, waiting 5 min")
                _shutdown.wait(300)

            except (ClientConnectionError, ClientError) as e:
                consecutive_errors += 1
                wait = min(120, 10 * consecutive_errors)
                log.warning(f"API error ({e}) — retrying in {wait}s")
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
