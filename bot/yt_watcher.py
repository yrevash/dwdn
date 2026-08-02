#!/usr/bin/env python3
"""Mirror a (permitted) YouTube channel of bhajans into Cloudflare R2.

Enumerates the channel with yt-dlp, keeps only videos with >= YT_VIEW_MIN views,
downloads each at best quality, optionally HEVC-compresses (reusing transcode.py),
and uploads via the shared r2.py — same bucket/creds as the reels bot, but under
YT_PREFIX ("youtube/") so the two never collide. Dedup by YouTube video id
(youtube_done.json) guarantees a video is never downloaded or uploaded twice, so
restarts are free and safe.

Runs as its own always-on service: it backfills the whole channel once, then
wakes every YT_INTERVAL seconds to pick up newly-published videos. Low-view
videos are simply left for a later sweep (NOT marked done), so a video that
crosses the view threshold later still gets mirrored.

Only mirror channels you have permission to redistribute.
"""

import os
import re
import sys
import json
import signal
import logging
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import r2
import transcode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("yt-mirror")

YT_CHANNEL   = os.getenv("YT_CHANNEL", "").strip()
YT_VIEW_MIN  = int(os.getenv("YT_VIEW_MIN", "500"))
YT_PREFIX    = os.getenv("YT_PREFIX", "youtube/")
YT_TRANSCODE = os.getenv("YT_TRANSCODE", "true").lower() == "true"
YT_INTERVAL  = int(os.getenv("YT_INTERVAL", "43200"))   # 12h between channel sweeps
YT_FORMAT    = os.getenv(
    "YT_FORMAT",
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
)
YT_DONE_FILE = Path(os.getenv("YT_DONE_FILE", "youtube_done.json"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
# On datacenter IPs (cloud VMs) YouTube throws "Sign in to confirm you're not a
# bot" — point YT_COOKIES at a Netscape cookies.txt exported from a logged-in
# (ideally throwaway) YouTube account to authenticate every yt-dlp call.
YT_COOKIES   = os.getenv("YT_COOKIES", "").strip()
# Recent yt-dlp needs a downloadable "ejs" challenge-solver component (run via
# deno) to solve YouTube's JS challenges; it's skipped unless enabled. Default
# on; set YT_REMOTE_COMPONENTS="" to disable for an older yt-dlp that lacks it.
YT_REMOTE_COMPONENTS = os.getenv("YT_REMOTE_COMPONENTS", "ejs:github").strip()
# Pacing to avoid YouTube's volume-based bot flag (seconds). yt-dlp sleeps a
# random YT_SLEEP_MIN..YT_SLEEP_MAX before each download and YT_SLEEP_REQUESTS
# between data requests.
YT_SLEEP_REQUESTS = os.getenv("YT_SLEEP_REQUESTS", "1")
YT_SLEEP_MIN      = os.getenv("YT_SLEEP_MIN", "2")
YT_SLEEP_MAX      = os.getenv("YT_SLEEP_MAX", "10")

# Prefer the yt-dlp installed in THIS venv, fall back to PATH.
_venv_bin = Path(sys.executable).parent
YTDLP = os.getenv("YTDLP_BIN") or (
    str(_venv_bin / "yt-dlp") if (_venv_bin / "yt-dlp").exists() else "yt-dlp"
)

_shutdown = threading.Event()
_done_lock = threading.Lock()


def _yt(*args) -> list:
    """Build a yt-dlp command, injecting --cookies when configured."""
    base = [YTDLP]
    if YT_REMOTE_COMPONENTS:
        base += ["--remote-components", YT_REMOTE_COMPONENTS]
    if YT_COOKIES:
        base += ["--cookies", YT_COOKIES]
    return base + list(args)


def _sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-]", "_", name or "")
    return name[:60].strip("_") or "video"


def load_done() -> set:
    try:
        return set(json.loads(YT_DONE_FILE.read_text()))
    except Exception:
        return set()


def save_done(done: set) -> None:
    with _done_lock:
        try:
            YT_DONE_FILE.write_text(json.dumps(sorted(done)))
        except Exception as e:
            log.warning(f"could not save {YT_DONE_FILE}: {e}")


def _run_json(args: list) -> list:
    """Run a yt-dlp command that emits JSON lines; return parsed objects."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except Exception as e:
        log.warning(f"yt-dlp invocation failed: {e}")
        return []
    if out.returncode != 0 and not out.stdout.strip():
        log.warning(f"yt-dlp error: {out.stderr.strip()[:300]}")
        return []
    objs = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objs.append(json.loads(line))
        except Exception:
            pass
    return objs


def list_channel(channel: str) -> list:
    """Return [{id, title, view_count, channel}] for every video on the channel.

    A flat-playlist dump is one fast request; view_count is present for most
    channels. When it's missing we look it up per-video before deciding.
    """
    entries = _run_json(_yt(
        "--flat-playlist", "--dump-json", "--ignore-errors", channel,
    ))
    vids = []
    for e in entries:
        vid = e.get("id")
        if not vid or e.get("_type") == "playlist":
            continue
        vids.append({
            "id": vid,
            "title": e.get("title") or "",
            "view_count": e.get("view_count"),
            "channel": e.get("channel") or e.get("uploader") or "",
        })
    return vids


def video_views(video_id: str):
    objs = _run_json(_yt(
        "-J", "--skip-download",
        f"https://www.youtube.com/watch?v={video_id}",
    ))
    return objs[0].get("view_count") if objs else None


def download(video_id: str):
    """Download one video; return the local Path or None."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # clear any stale partials for this id
    for p in DOWNLOAD_DIR.glob(f"yt_{video_id}.*"):
        p.unlink(missing_ok=True)
    cmd = _yt(
        "-f", YT_FORMAT, "--merge-output-format", "mp4",
        "--no-playlist", "--no-progress",
        "--sleep-requests", YT_SLEEP_REQUESTS,
        "--sleep-interval", YT_SLEEP_MIN, "--max-sleep-interval", YT_SLEEP_MAX,
        "-o", str(DOWNLOAD_DIR / f"yt_{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        log.warning(f"download timed out/failed {video_id}: {e}")
        return None
    if r.returncode != 0:
        log.warning(f"download failed {video_id}: {r.stderr.strip()[:300]}")
        return None
    # prefer the merged mp4
    for ext in (".mp4", ".mkv", ".webm"):
        p = DOWNLOAD_DIR / f"yt_{video_id}{ext}"
        if p.exists():
            return p
    hits = list(DOWNLOAD_DIR.glob(f"yt_{video_id}.*"))
    return hits[0] if hits else None


def process_video(v: dict, done: set) -> str:
    """Returns 'uploaded' | 'skip' | 'below' | 'fail'."""
    vid = v["id"]
    if vid in done:
        return "skip"

    views = v.get("view_count")
    if views is None:
        views = video_views(vid)
    # Below threshold: leave it for a future sweep (do NOT mark done) so it can
    # be picked up once it crosses YT_VIEW_MIN.
    if views is not None and views < YT_VIEW_MIN:
        return "below"

    src = download(vid)
    if not src:
        return "fail"

    final, codec = src, "source"
    src_b = out_b = src.stat().st_size
    if YT_TRANSCODE:
        try:
            final, codec, src_b, out_b = transcode.transcode_hevc(src)
        except Exception as e:
            log.warning(f"transcode failed {vid}, uploading source: {e}")
            final, codec = src, "source"

    channel = _sanitize(v.get("channel") or "youtube")
    title = _sanitize(v.get("title") or "bhajan")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{channel}_{title}_{vid}.mp4"

    key = r2.upload(final, filename, prefix=YT_PREFIX)

    for p in {src, final}:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass

    if not key:
        return "fail"

    r2.append_manifest({
        "key": key, "origin": "youtube",
        "sender": channel, "video_id": vid,
        "title": (v.get("title") or "")[:120], "view_count": views,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "codec": codec, "src_bytes": src_b, "out_bytes": out_b,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    done.add(vid)
    save_done(done)
    log.info(f"mirrored {vid} ({views} views) -> {key}")
    return "uploaded"


def sweep() -> None:
    done = load_done()
    vids = list_channel(YT_CHANNEL)
    if not vids:
        log.warning("channel returned 0 videos — check YT_CHANNEL / yt-dlp")
        return
    # Most-popular first: mirror the highest-view videos before the rest.
    # (Videos missing a flat view_count sort last but are still processed —
    # their real view count is looked up per-video in process_video.)
    vids.sort(key=lambda v: (v.get("view_count") or 0), reverse=True)
    log.info(f"channel has {len(vids)} videos; {len(done)} already mirrored; "
             f"processing most-viewed first")

    up = below = fail = skip = 0
    total = len(vids)
    for i, v in enumerate(vids, 1):
        if _shutdown.is_set():
            log.info("shutdown requested — stopping sweep (progress saved)")
            break
        try:
            res = process_video(v, done)
        except Exception as e:
            log.warning(f"video {v.get('id')} failed: {e}")
            res = "fail"
        up += res == "uploaded"
        below += res == "below"
        fail += res == "fail"
        skip += res == "skip"
        if i % 10 == 0 or i == total:
            log.info(f"progress {i}/{total}: {up} uploaded, {skip} already-had, "
                     f"{below} low-view, {fail} failed")
    log.info(f"sweep done: {up} uploaded, {skip} already-had, "
             f"{below} below {YT_VIEW_MIN} views, {fail} failed")


def main() -> None:
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: _shutdown.set())

    if not YT_CHANNEL:
        raise SystemExit("Set YT_CHANNEL in .env (the channel URL you may mirror)")
    if not r2.is_configured():
        raise SystemExit("R2 not configured — check R2_* vars in .env")

    if not YT_COOKIES:
        log.warning("YT_COOKIES not set — YouTube may reject datacenter IPs with "
                    "'Sign in to confirm you're not a bot'. Set YT_COOKIES to a "
                    "cookies.txt path if downloads fail.")
    log.info(f"YouTube->R2 mirror starting: channel={YT_CHANNEL}, "
             f"view_min={YT_VIEW_MIN}, transcode={'HEVC' if YT_TRANSCODE else 'off'}, "
             f"prefix={YT_PREFIX}, cookies={'yes' if YT_COOKIES else 'no'}, "
             f"every {YT_INTERVAL}s, yt-dlp={YTDLP}")

    while not _shutdown.is_set():
        try:
            sweep()
        except Exception as e:
            log.error(f"sweep error: {e}")
        if _shutdown.is_set():
            break
        log.info(f"sleeping {YT_INTERVAL}s until next sweep")
        _shutdown.wait(YT_INTERVAL)
    log.info("YouTube->R2 mirror stopped")


if __name__ == "__main__":
    main()
