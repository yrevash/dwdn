#!/usr/bin/env python3
"""
Instagram DM Watcher → Cloudflare R2

Watches the @gv_reeldb account's DMs, downloads any reel sent to it, transcodes
it to visually-transparent HEVC, and uploads the mp4 to a SCOPED R2 inbox
bucket. Does one thing only — no web app, no API, no database.

Forked from the battle-hardened Google-Drive watcher; storage swapped to R2,
yt-dlp / rclone / cookies removed, HEVC transcode added.
"""

import os

# Load .env (if present) BEFORE anything reads os.environ — including the r2 /
# transcode modules, which read their config at import time. Harmless in Docker,
# where compose injects the environment and no .env file is present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import re
import time
import json
import signal
import logging
import hashlib
import threading
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, ChallengeRequired,
    RateLimitError, ClientError, ClientConnectionError,
    ClientThrottledError,
)

import r2
import transcode
import scraper


# ─── CONFIG ────────────────────────────────────────────────────────────────────

IG_USERNAME   = os.getenv("IG_USERNAME", "")
IG_PASSWORD   = os.getenv("IG_PASSWORD", "")
IG_PROXY      = os.getenv("IG_PROXY", "")
IG_TOTP_SEED  = os.getenv("IG_TOTP_SEED", "")   # authenticator-app secret → auto 2FA codes
IG_2FA_CODE   = os.getenv("IG_2FA_CODE", "")    # one-time 6-digit code (first-login fallback)
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
MAX_WORKERS   = int(os.getenv("MAX_WORKERS", "3"))
DOWNLOAD_MIN_GAP = float(os.getenv("DOWNLOAD_MIN_GAP", "8"))  # min secs between download starts (anti-flag)

SESSION_FILE        = Path("session.json")
SEEN_FILE           = Path("seen_ids.json")
DOWNLOADED_FILE     = Path("downloaded_urls.json")
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

# ─── RAW DM FETCHING (bypasses pydantic models entirely) ──────────────────────

def fetch_threads_raw(cl: Client) -> list[dict]:
    """Fetch DM threads via raw API — no pydantic, no crashes."""
    try:
        resp = cl.private_request("direct_v2/inbox/", params={
            "visual_message_return_type": "unseen",
            "thread_message_limit": "10",
            "persistentBadging": "true",
            "limit": "20",
            "is_prefetching": "false",
        })
        return resp.get("inbox", {}).get("threads", [])
    except Exception as e:
        log.warning(f"Failed to fetch inbox: {e}")
        return []

def fetch_messages_raw(cl: Client, thread_id: str) -> list[dict]:
    """Fetch messages for a thread via raw API — no pydantic, no crashes."""
    try:
        resp = cl.private_request(f"direct_v2/threads/{thread_id}/", params={
            "visual_message_return_type": "unseen",
            "direction": "older",
            "limit": "20",
        })
        return resp.get("thread", {}).get("items", [])
    except Exception as e:
        log.warning(f"Failed to fetch thread {thread_id}: {e}")
        return []

def extract_url_from_raw_item(item: dict) -> str | None:
    """Extract downloadable URL from a raw DM message dict."""
    try:
        item_type = item.get("item_type", "")

        # Text message with URL
        if item_type == "text":
            return (extract_urls(item.get("text", "")) or [None])[0]

        # Reel/clip share (xma_clip, xma_media_share)
        if item_type in ("xma_clip", "xma_media_share", "xma_link"):
            # Try xma_media_share list
            for xma in item.get("xma_media_share", []):
                code = reel_code_from_str(xma.get("target_url", "") or xma.get("preview_url", ""))
                if code:
                    return f"https://www.instagram.com/reel/{code}/"
            # Scan the whole item string for shortcodes
            code = reel_code_from_str(json.dumps(item))
            if code:
                return f"https://www.instagram.com/reel/{code}/"
            return None

        # Direct clip share
        if item_type in ("clip", "felix_share"):
            clip = item.get("clip", {}).get("clip", {})
            code = clip.get("code")
            if code:
                return f"https://www.instagram.com/reel/{code}/"
            code = reel_code_from_str(json.dumps(item))
            if code:
                return f"https://www.instagram.com/reel/{code}/"

        # Media share (photo/video post)
        if item_type == "media_share":
            code = item.get("media_share", {}).get("code")
            if code:
                return f"https://www.instagram.com/p/{code}/"

        # Link
        if item_type == "link":
            link_url = item.get("link", {}).get("link_context", {}).get("link_url", "")
            return link_url or None

    except Exception as e:
        log.warning(f"URL extract error: {e}")

    return None

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

# ─── INSTAGRAM LOGIN ───────────────────────────────────────────────────────────

def make_client() -> Client:
    cl = Client()
    cl.delay_range = [3, 7]
    if IG_PROXY:
        try:
            cl.set_proxy(IG_PROXY)
            log.info("Using IG_PROXY for Instagram traffic")
        except Exception as e:
            log.warning(f"set_proxy failed: {e}")
    return cl

def _login_with_2fa(cl: Client) -> None:
    """cl.login, supplying a 2FA code when the account needs one.

    Prefers a TOTP seed (authenticator-app secret) so headless re-logins work
    forever; falls back to a one-time IG_2FA_CODE for a first manual login.
    """
    code = ""
    if IG_TOTP_SEED:
        try:
            code = cl.totp_generate_code(IG_TOTP_SEED)
        except Exception as e:
            log.warning(f"TOTP code generation failed: {e}")
    elif IG_2FA_CODE:
        code = IG_2FA_CODE

    if code:
        cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
    else:
        cl.login(IG_USERNAME, IG_PASSWORD)

def login(cl: Client) -> None:
    if SESSION_FILE.exists():
        log.info("Loading saved session...")
        try:
            cl.load_settings(SESSION_FILE)
            if cl.user_id:
                # Trust the loaded session — do NOT probe with a private-API call
                # here. A browser-minted session can read media (scraping) while
                # still failing strict endpoints like timeline_feed, and a cold
                # login fallback would just re-trigger the device checkpoint.
                # Individual operations handle their own failures.
                log.info(f"Session resumed (user_id={cl.user_id})")
                return
            log.warning("Session has no user_id; falling back to a full login...")
        except Exception as e:
            log.warning(f"Could not load session ({e}); falling back to a full login...")

    log.info(f"Logging in as @{IG_USERNAME}...")
    _login_with_2fa(cl)
    cl.dump_settings(SESSION_FILE)
    log.info("Logged in — session saved")

# ─── TRANSCODE + R2 UPLOAD + MANIFEST ──────────────────────────────────────────

def _finalize(local_path: Path, sender_username: str, caption: str,
              shortcode: str, media_pk, origin: str = "dm") -> bool:
    """Transcode → upload to R2 → append manifest → clean up local files.

    Returns True only if the object actually landed in R2.
    """
    final_path, codec, src_bytes, out_bytes = transcode.transcode_hevc(local_path)

    label = caption or "reel"
    uid = shortcode or str(media_pk)[-8:]
    filename = make_filename(sender_username, label, uid)

    key = r2.upload(final_path, filename)

    # Clean up local files (original + transcoded copy if they differ)
    for p in {local_path, final_path}:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass

    if not key:
        return False

    r2.append_manifest({
        "key": key,
        "origin": origin,            # "dm" (you sent it) or "scrape" (auto-pulled)
        "sender": sender_username,   # DM sender, or the source account for scrapes
        "caption": caption,
        "shortcode": shortcode,
        "media_pk": str(media_pk),
        "codec": codec,
        "src_bytes": src_bytes,
        "out_bytes": out_bytes,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return True

# ─── DOWNLOAD THROTTLE (anti-flag) ─────────────────────────────────────────────

_dl_gate_lock = threading.Lock()
_last_dl_start = 0.0

def _throttle_download() -> None:
    """Start at most one new download every DOWNLOAD_MIN_GAP seconds, no matter
    how many the scraper queues — the real 'not all at once' guarantee that keeps
    Instagram from flagging the account during big backfills. Already-downloaded
    reels are skipped before reaching here, so they don't consume gate time."""
    global _last_dl_start
    with _dl_gate_lock:
        now = time.monotonic()
        wait = _last_dl_start + DOWNLOAD_MIN_GAP - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_dl_start = now

# ─── INSTAGRAPI DIRECT DOWNLOAD ────────────────────────────────────────────────

def download_via_instagrapi(url: str, sender_username: str, downloaded_urls: set,
                            origin: str = "dm") -> bool:
    global _ig_client
    if not _ig_client:
        return False

    _throttle_download()

    fp = url_fingerprint(url)
    shortcode = reel_code_from_str(url) or ""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        media_pk = _ig_client.media_pk_from_url(url)
        info = _ig_client.media_info_v1(media_pk)

        # Get reel caption for the filename/manifest
        caption = ""
        try:
            caption = (info.caption_text or "").split("\n")[0][:50]
        except Exception:
            pass

        # Try multiple sources for the video URL
        video_url = ""
        if info.video_url:
            video_url = str(info.video_url)
        elif hasattr(info, "video_versions") and info.video_versions:
            # Pick highest quality version
            best = sorted(info.video_versions, key=lambda v: getattr(v, "width", 0) * getattr(v, "height", 0), reverse=True)
            video_url = str(best[0].url) if best else ""
        elif hasattr(info, "resources") and info.resources:
            # Carousel — grab first video resource
            for res in info.resources:
                if getattr(res, "video_url", None):
                    video_url = str(res.video_url)
                    break

        # Fallback: instagrapi clip_download (still no yt-dlp)
        if not video_url:
            log.warning(f"No video_url found for {url}, trying clip_download...")
            try:
                out = _ig_client.clip_download(media_pk, folder=DOWNLOAD_DIR)
                out_path = Path(out) if out else None
            except Exception as e2:
                log.warning(f"clip_download also failed: {e2}")
                return False
            if not (out_path and out_path.exists()):
                log.warning(f"clip_download produced nothing for {url}")
                return False
            ok = _finalize(out_path, sender_username, caption, shortcode, media_pk, origin)
            if ok:
                with _state_lock:
                    downloaded_urls.add(fp)
                    save_json_set(DOWNLOADED_FILE, downloaded_urls)
            return ok

        # Stream from the Instagram CDN to a temp file
        tmp_path = DOWNLOAD_DIR / f"dl_{media_pk}.mp4"
        log.info(f"CDN download → {tmp_path.name}")
        headers = {"User-Agent": "Instagram 269.0.0.18.75 Android"}

        with requests.get(video_url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)

        log.info(f"Downloaded: {tmp_path.name}")

        ok = _finalize(tmp_path, sender_username, caption, shortcode, media_pk, origin)
        if ok:
            with _state_lock:
                downloaded_urls.add(fp)
                save_json_set(DOWNLOADED_FILE, downloaded_urls)
        return ok

    except Exception as e:
        log.warning(f"instagrapi download failed: {e}")
        return False

# ─── UNIFIED DOWNLOAD ENTRY ────────────────────────────────────────────────────

def _safe_download(url: str, sender_id: str, downloaded_urls: set) -> None:
    """Wrapper so any exception in a download task is logged and swallowed — never blocks the pool."""
    try:
        download_video(url, sender_id, downloaded_urls)
    except Exception as e:
        log.error(f"Download task crashed ({url}): {e}")

def download_video(url: str, sender_id: str, downloaded_urls: set) -> bool:
    fp = url_fingerprint(url)

    with _state_lock:
        if fp in downloaded_urls:
            log.info(f"Already downloaded, skipping: {url}")
            return True

    # Instagram-only: ignore anything that isn't a reel/post link
    if not IS_INSTAGRAM_URL.search(url):
        log.info(f"Not an Instagram reel/post URL, skipping: {url}")
        return False

    sender_username = get_username(sender_id)
    log.info(f"Downloading [{sender_username}]: {url}")
    return download_via_instagrapi(url, sender_username, downloaded_urls)

def _safe_scrape_download(media, source_username: str, downloaded_urls: set) -> None:
    """Wrapper for scraper-queued downloads — one failure never blocks the pool."""
    try:
        download_from_media(media, source_username, downloaded_urls)
    except Exception as e:
        log.error(f"Scrape download crashed: {e}")

def download_from_media(media, source_username: str, downloaded_urls: set) -> bool:
    """Download a reel straight from a listing media object's video_url (origin =
    scrape). Avoids media_info_v1, which a browser-grade session can't call."""
    code = getattr(media, "code", "") or ""
    url = f"https://www.instagram.com/reel/{code}/" if code else ""
    fp = url_fingerprint(url) if url else None

    if fp is not None:
        with _state_lock:
            if fp in downloaded_urls:
                return True

    # Video URL straight from the listing — no extra API call needed.
    video_url = ""
    if getattr(media, "video_url", None):
        video_url = str(media.video_url)
    elif getattr(media, "video_versions", None):
        best = sorted(media.video_versions,
                      key=lambda v: getattr(v, "width", 0) * getattr(v, "height", 0),
                      reverse=True)
        video_url = str(best[0].url) if best else ""
    if not video_url:
        log.warning(f"[scrape] no video_url for {code or 'media'}")
        return False

    _throttle_download()

    caption = ""
    try:
        caption = (getattr(media, "caption_text", "") or "").split("\n")[0][:50]
    except Exception:
        pass

    pk = getattr(media, "pk", "") or code
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = DOWNLOAD_DIR / f"scrape_{pk}.mp4"
    log.info(f"[scrape] download → {tmp_path.name} (@{source_username})")

    headers = {"User-Agent": "Instagram 269.0.0.18.75 Android"}
    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        log.warning(f"[scrape] download failed for {code}: {e}")
        tmp_path.unlink(missing_ok=True)
        return False

    ok = _finalize(tmp_path, sanitize(source_username), caption, code, pk, origin="scrape")
    if ok and fp is not None:
        with _state_lock:
            downloaded_urls.add(fp)
            save_json_set(DOWNLOADED_FILE, downloaded_urls)
    return ok

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("Set IG_USERNAME and IG_PASSWORD env vars")
    if not r2.is_configured():
        raise RuntimeError(
            "R2 not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET"
        )

    global _ig_client, _username_cache

    cl = make_client()
    login(cl)
    _ig_client = cl

    seen_ids        = load_json_set(SEEN_FILE)
    downloaded_urls = load_json_set(DOWNLOADED_FILE)
    _username_cache = load_json_dict(USERNAME_CACHE_FILE)

    log.info(f"Loaded {len(seen_ids)} seen IDs, {len(downloaded_urls)} downloaded, {len(_username_cache)} cached usernames")
    log.info(f"Polling every {POLL_INTERVAL}s — uploading to R2 bucket "
             f"'{r2.R2_BUCKET}' under '{r2.R2_PREFIX}'")

    log.info("Seeding existing messages...")
    try:
        raw_threads = fetch_threads_raw(cl)
        for rt in raw_threads:
            tid = rt.get("thread_id", "")
            if not tid:
                continue
            raw_msgs = fetch_messages_raw(cl, tid)
            for item in raw_msgs:
                mid = item.get("item_id", "")
                if mid:
                    seen_ids.add(str(mid))
        save_json_set(SEEN_FILE, seen_ids)
        log.info(f"Seeded {len(seen_ids)} messages — watching for new ones")
    except Exception as e:
        log.warning(f"Seed failed: {e}")

    consecutive_errors = 0
    session_save_counter = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        # Scheduled scraper: Apify scrapes Instagram (off our account) and returns
        # video URLs; we download those → HEVC → R2. Zero ban risk on this path.
        import apify_scraper
        threading.Thread(
            target=apify_scraper.run_apify_scraper, args=(_shutdown,),
            daemon=True, name="apify-scraper",
        ).start()

        while not _shutdown.is_set():
            try:
                raw_threads = fetch_threads_raw(cl)

                for rt in raw_threads:
                    tid = rt.get("thread_id", "")
                    if not tid:
                        continue

                    raw_msgs = fetch_messages_raw(cl, tid)
                    for item in raw_msgs:
                        mid = str(item.get("item_id", ""))
                        if not mid:
                            continue

                        with _state_lock:
                            if mid in seen_ids:
                                continue
                            seen_ids.add(mid)

                        sender_id = str(item.get("user_id", "0"))
                        item_type = item.get("item_type", "?")
                        log.info(f"NEW MSG from {sender_id} | type={item_type}")

                        url = extract_url_from_raw_item(item)
                        if url:
                            log.info(f"Queuing: {url}")
                            executor.submit(_safe_download, url, sender_id, downloaded_urls)

                with _state_lock:
                    save_json_set(SEEN_FILE, seen_ids)

                consecutive_errors = 0
                # Persist the session periodically so Instagram's cookie/token
                # rotation is captured — THIS is what keeps the login alive long
                # term without ever re-pasting a sessionid.
                session_save_counter += 1
                if session_save_counter >= 20:
                    try:
                        cl.dump_settings(SESSION_FILE)
                        log.info("Session persisted")
                    except Exception as e:
                        log.warning(f"Could not persist session: {e}")
                    session_save_counter = 0

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
