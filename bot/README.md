# ig-reel-r2 — Instagram reel → Cloudflare R2 bot

Watches the `@gv_reeldb` Instagram account's DMs. Any reel sent to it is
downloaded from the Instagram CDN, transcoded to visually-transparent H.265/HEVC,
and uploaded to the **`granthvani-reels-inbox`** R2 bucket. One job, nothing else.

## Security model

The bot only ever holds an R2 token **scoped to `granthvani-reels-inbox`**. That
token cannot read, write, or even see your main `granthvani-cdn` bucket. If this
server is ever compromised, the blast radius is one disposable staging bucket.
Moving reels from the inbox to your main bucket happens elsewhere, on trusted
infra that holds your real R2 key — never here.

## What lands in R2

- `reels/<timestamp>_<sender>_<caption>_<shortcode>.mp4` — one object per reel
- `manifest.jsonl` — one JSON line per reel:
  `{key, sender, caption, shortcode, media_pk, codec, src_bytes, out_bytes, ts}`

## Files

| File | Purpose |
| --- | --- |
| `watcher.py` | DM poller + download pipeline |
| `r2.py` | the only code that talks to R2 (upload + manifest) |
| `transcode.py` | ffmpeg HEVC transcode (falls back to original on failure) |
| `Dockerfile`, `docker-compose.yml` | deploy |
| `.env` | your secrets — never committed |

## Config (`.env`)

Copy `.env.example` → `.env` and fill in. Plain `KEY=value`, **no quotes**:

| Key | Notes |
| --- | --- |
| `IG_USERNAME`, `IG_PASSWORD` | the @gv_reeldb dummy account |
| `R2_ACCOUNT_ID` | Cloudflare account id |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | the **scoped** inbox token only |
| `R2_BUCKET` | `granthvani-reels-inbox` |
| `TRANSCODE`, `TRANSCODE_CRF`, `TRANSCODE_PRESET` | HEVC settings |
| `IG_PROXY` | optional residential proxy |

## Two modes (run concurrently)

1. **DM-triggered** — any reel sent to `@gv_reeldb` is grabbed.
2. **Scheduled scrape** — accounts listed in `targets.txt` are crawled:
   their **entire reel history** (resumable backfill) plus new reels going
   forward. One `@username` per line; `@handle`, bare name, or a `/reels/` URL
   all work. In Docker, put `targets.txt` in the mounted `state/` folder so you
   can edit the list without rebuilding.

### Anti-flag pacing — do NOT crank this up

A historical crawl of a big account is the riskiest thing here. Safety knobs:

| Env | Default | Effect |
| --- | --- | --- |
| `DOWNLOAD_MIN_GAP` | `8` | min seconds between download *starts*, globally — ~450 reels/hr cap. Raise to go slower/safer. |
| `SCRAPE_INTERVAL` | `7200` | seconds between ongoing re-checks (2h) once backfilled |
| `SCRAPE_PAGE_SIZE` | `30` | reels fetched per backfill page |
| `SCRAPE_PAGE_DELAY` | `60` | base seconds between backfill pages |
| `SCRAPE_MAX_PER_ACCOUNT` | `0` | `0` = unlimited; set e.g. `500` to cap |
| `SCRAPE_BACKFILL` | `true` | `false` = only grab new reels, skip history |

Backfill is **resumable** — progress per account is saved in `scrape_state.json`,
so a restart continues where it left off. Use a pre-seeded session and ideally a
residential `IG_PROXY` for large crawls.

## One-time: seed a warm session (avoids datacenter-IP login blocks)

Instagram distrusts logins from cloud IPs. Generate `session.json` on a trusted
(home) machine first, then copy it to the server so the bot **resumes** an
established session instead of cold-logging from Azure:

```bash
# on your local machine, inside bot/
python3 -m pip install -r requirements.txt
brew install ffmpeg                 # macOS  (Linux: apt-get install ffmpeg)
python3 watcher.py                  # logs in, writes session.json, starts watching
# send a test reel to @gv_reeldb → confirm it appears in granthvani-reels-inbox
# then Ctrl-C.  session.json now exists in bot/ — you'll copy it to the VM.
```

## Deploy on an Azure Ubuntu VM

```bash
# 1. Provision an Ubuntu 22.04+ VM (B2s or larger is plenty — CPU-only), SSH in.

# 2. Install Docker + the compose plugin.
curl -fsSL https://get.docker.com | sh

# 3. Copy this bot/ folder to the VM, including .env, then drop the warm
#    session.json into the state volume:
#      scp -r bot/ azureuser@<vm-ip>:~/ig-reel-r2/
mkdir -p ~/ig-reel-r2/state
mv ~/ig-reel-r2/session.json ~/ig-reel-r2/state/session.json   # the one you generated

# 4. Build + run (detached, auto-restart).
cd ~/ig-reel-r2
sudo docker compose up -d --build

# 5. Watch it work.
sudo docker compose logs -f
```

The `state/` volume holds `session.json` and all dedup/manifest files, so the bot
survives restarts and redeploys. To update code: re-copy the files and
`sudo docker compose up -d --build`.

## Local run (without Docker)

```bash
cd bot
python3 -m pip install -r requirements.txt   # needs ffmpeg on PATH
python3 watcher.py                            # auto-loads .env
```
