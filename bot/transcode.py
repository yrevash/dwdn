#!/usr/bin/env python3
"""ffmpeg HEVC transcode — visually transparent compression.

Re-encodes a source mp4 to H.265/HEVC at a transparent CRF to shrink file size
without a visible quality loss. If ffmpeg is missing, errors, times out, or the
result comes out larger, it returns the ORIGINAL file untouched so a reel is
never lost.
"""

import os
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("dwdn-bot.transcode")

TRANSCODE        = os.getenv("TRANSCODE", "true").lower() == "true"
TRANSCODE_CRF    = os.getenv("TRANSCODE_CRF", "26")
TRANSCODE_PRESET = os.getenv("TRANSCODE_PRESET", "slow")
FFMPEG_TIMEOUT   = int(os.getenv("FFMPEG_TIMEOUT", "600"))


def transcode_hevc(src: Path) -> tuple[Path, str, int, int]:
    """Transcode src to HEVC. Returns (out_path, codec, src_bytes, out_bytes).

    - success      → (new *_hevc.mp4 path, "hevc", src_bytes, out_bytes)
    - disabled/fail → (original src, "source", src_bytes, src_bytes)
    """
    src_bytes = src.stat().st_size if src.exists() else 0

    if not TRANSCODE:
        return src, "source", src_bytes, src_bytes

    out = src.with_name(src.stem + "_hevc.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-c:v", "libx265", "-crf", str(TRANSCODE_CRF), "-preset", TRANSCODE_PRESET,
        "-tag:v", "hvc1",                 # makes HEVC play in native Apple/mobile players
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
            out_bytes = out.stat().st_size
            if src_bytes and out_bytes >= src_bytes:
                log.info(f"HEVC not smaller ({out_bytes} >= {src_bytes}); keeping original")
                out.unlink(missing_ok=True)
                return src, "source", src_bytes, src_bytes
            saved = 100 * (1 - out_bytes / src_bytes) if src_bytes else 0
            log.info(f"HEVC: {src_bytes // 1024}KB -> {out_bytes // 1024}KB ({saved:.0f}% smaller)")
            return out, "hevc", src_bytes, out_bytes
        log.warning(f"ffmpeg failed (rc={result.returncode}); using original — {result.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out; using original")
    except FileNotFoundError:
        log.warning("ffmpeg not installed; using original")
    except Exception as e:
        log.warning(f"ffmpeg error ({e}); using original")

    if out.exists():
        out.unlink(missing_ok=True)
    return src, "source", src_bytes, src_bytes
