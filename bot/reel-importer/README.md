# reel-importer

One-shot job that moves the scraped reels from **`granthvani-reels-inbox`** into your
**live `granthvani-cdn` bucket + `reels` DB table**, re-encoding them so the app can
play them. Run it once (e.g. overnight); it transcodes everything, uploads, inserts the
DB rows, then stops.

## What it does per reel

```
inbox/reels/…HEVC.mp4  →  download  →  ffmpeg → H.264 720p CRF26  →  upload to
                                                                     granthvani-cdn/reels/gv-reel-NNN.mp4
                                                                   + reel-thumbs/gv-reel-NNN.jpg
                                                                   →  INSERT public.reels row
```

- **Why re-encode?** The inbox files are HEVC (H.265). Your 272 existing reels are all
  H.264, and HEVC is unreliable in the app's video player. This normalizes them to H.264
  and shrinks the batch from ~5.8 GB to ~1–1.5 GB.
- **Appends** as `gv-reel-273`, `274`, … — never touches your existing reels.
- **Attribution:** `creator` = the original Instagram handle from the manifest; the Hindi
  caption becomes the reel's description.
- **Non-destructive:** only ever *reads* the inbox; the inbox originals stay as a backup.
- **Resumable:** safe to re-run. It skips anything already done (local `import_done.json`
  ledger + ids already in the DB). If it crashes at reel 300, just run it again.

## Prerequisites

- `ffmpeg` + `ffprobe` on PATH  (`ffmpeg -version` should work)
- The bot's existing Python venv (has `boto3`, `requests`, `python-dotenv` — no new installs)

## Setup (one time)

```bash
cd /Users/yrevash/gv_frontend/content/dwdn/bot/reel-importer

# 1. create your env file and fill it in (all values are in backend/.env)
cp .env.import.example .env.import
nano .env.import        # paste R2 + Supabase + Cloudflare values from backend/.env
```

`.env.import` needs a token that can **read the inbox AND write the CDN bucket** — that's
the backend token you just granted inbox-read to. Copy these straight from
`/Users/yrevash/gv_frontend/backend/.env`:
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `CLOUDFLARE_CACHE_PURGE_TOKEN`.

## Run

Use the bot's venv python (adjust the path if your venv lives elsewhere):

```bash
# 1. DRY RUN — prints exact counts, id range, skipped outliers. Writes NOTHING.
PLAN_ONLY=1 ../.venv/bin/python import_reels.py

# 2. VALIDATE — import just 3 reels, then check they play in the app / open the URLs.
MAX_COUNT=3 ../.venv/bin/python import_reels.py

# 3. FULL RUN — do the rest. Runs for a while, then stops on its own.
../.venv/bin/python import_reels.py

# Run it detached overnight and log to a file:
nohup ../.venv/bin/python import_reels.py > import.log 2>&1 &
tail -f import.log
```

> No venv? Any Python 3.10+ works: `pip install boto3 requests python-dotenv` then
> `python import_reels.py`.

## Knobs (env vars)

| var | default | meaning |
|-----|---------|---------|
| `PLAN_ONLY=1` | — | dry run, no writes |
| `MAX_COUNT=N` | all | only import the first N (for validation) |
| `CONCURRENCY=N` | 3 | parallel transcodes — raise toward server core count |
| `MAX_SRC_MB=N` | 60 | skip source files bigger than this (non-reel outliers) |
| `TRANSCODE_CRF=N` | 26 | H.264 quality/size — lower = bigger/better |

## After it finishes

- It prints `imported=… failed=… remaining=…`.
- Any failures are written to `import_failures.json`; just re-run to retry them.
- Don't delete `import_done.json` between runs — it's the resume ledger.
- The CDN cache for the new URLs is purged automatically at the end.
- Tell me the final count and I'll verify the new reels are live and sane in the DB.
