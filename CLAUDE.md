# dwdn

Video downloader suite — web app + Instagram DM watcher bot.

## Architecture

### Web App (`app/`)
- **Next.js 15.2.3** with Turbopack, React 19, Tailwind CSS 3
- Runs on **port 8004**
- API routes use `yt-dlp` (system binary) + `ffmpeg` for highest quality downloads
- Supports YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, Vimeo, 1000+ sites

#### API Routes
- `POST /api/info` — runs `yt-dlp --dump-json` to get video metadata (title, thumbnail, duration, platform)
- `GET /api/download?url=` — downloads to temp file at best quality (`bestvideo+bestaudio` merged to mp4), streams to client, auto-cleans up

#### Key Files
- `app/page.tsx` — client-side UI: paste URL, auto-fetch on paste, show info card, download with progress
- `app/api/info/route.ts` — video info endpoint
- `app/api/download/route.ts` — download + stream endpoint
- `lib/downloader.ts` — yt-dlp wrapper with platform detection, format selection, cached binary path

### Instagram DM Watcher Bot (`bot/`)
- **Python** script that monitors `@gv_reeldb` dummy Instagram account DMs
- Any reel/video link sent to the account via DM is auto-downloaded and uploaded to Google Drive
- Uses `instagrapi` for auth + media info, raw Instagram API for DM fetching (bypasses pydantic model issues)

#### Key Design Decisions
- **Raw API for DMs**: `cl.private_request("direct_v2/inbox/")` instead of `cl.direct_threads()` — avoids pydantic `MediaXma` crash when `video_url=None`
- **Direct CDN download for Instagram**: uses `instagrapi.media_info_v1()` to get video URL, then `requests.get()` from CDN — no yt-dlp rate limits for Instagram content
- **yt-dlp only for non-Instagram**: YouTube, TikTok, etc. still go through yt-dlp with cookies
- **Fire-and-forget downloads**: `executor.submit(_safe_download, ...)` — one failure never blocks the batch
- **Thread-safe state**: `_state_lock` protects `seen_ids` and `downloaded_urls` sets
- **Username in filenames**: resolves `user_id` → username via `user_info_v1()`, cached in `username_cache.json`
- **Filename format**: `{timestamp}_{sender_username}_{caption}_{media_pk}.mp4`
- **rclone --ignore-checksum**: prevents false md5 mismatch failures on parallel uploads
- **Video URL fallback chain**: `info.video_url` → `info.video_versions` → `info.resources` → `clip_download()` → yt-dlp

#### Persistent State Files (in `bot/`)
- `session.json` — instagrapi login session (survives restarts)
- `ig_cookies.txt` — Netscape cookies for yt-dlp (refreshed every 10 poll cycles)
- `seen_ids.json` — message IDs already processed
- `downloaded_urls.json` — URL fingerprints for dedup
- `username_cache.json` — user_id → username mapping

#### Error Handling
- Instagram rate limit → exponential backoff up to 5 min
- Session expired → auto re-login
- Challenge required → log + wait 5 min
- yt-dlp rate limit → 60s wait + retry with fallback formats
- rclone upload fail → 3 retries with backoff
- Malformed DM messages → skipped, never crash pipeline
- Any download crash → caught by `_safe_download`, logged, continues

## Deployment

### Web App (RunPod)
```bash
git clone https://github.com/yrevash/dwdn.git && cd dwdn
chmod +x setup.sh && ./setup.sh
bun run start  # port 8004
```

### Bot (RunPod)
```bash
cd bot
pip install -r requirements.txt
set -a && source ../.env && set +a && python3 watcher.py
```

### Required .env (bot/)
```
IG_USERNAME=...
IG_PASSWORD=...
DOWNLOAD_DIR=./downloads
POLL_INTERVAL=30
GDRIVE_REMOTE=gdrive:Reels
DELETE_AFTER_UPLOAD=true
```

### Google Drive Setup
```bash
rclone config  # create remote named "gdrive", type "drive", scope "drive"
```

## System Dependencies
- `yt-dlp` — video downloader
- `ffmpeg` — video merging
- `rclone` — Google Drive upload
- `bun` — JS runtime + package manager
- `python3` + `pip` — bot runtime
