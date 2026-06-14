# IG Reel → R2 Bot — Complete Context / Session Doc

_Last updated: 2026-06-14_

A single-purpose server that collects Instagram Reels into Cloudflare R2, transcoded to
HEVC. Two sources: (1) reels DM'd to a dummy IG account, (2) reels scraped from a list of
target accounts via **Apify** (off our account). Built to feed a future recommendation
engine for app users.

---

## 1. TL;DR — current running state

- **Mode:** Apify-only (`DM_ENABLED=false`). The Instagram account is **not used at all** right now.
- **What's running:** systemd service `reelbot` on an Azure VM. Every 6h it asks Apify for the
  target accounts' reels, downloads each from the CDN, transcodes to HEVC, uploads to R2,
  records it in `manifest.jsonl`.
- **Live as of this session:** a first ~557-reel backfill (limit 100/account × 5) is processing.
- **Account safety:** scraping is 100% off-account (Apify does it). The IG account only matters
  for the (currently disabled) DM feature.

---

## 2. Architecture

```
                 ┌─────────────────────────── Azure VM "reel" (systemd: reelbot) ──────────────────────────┐
                 │                                                                                          │
 Apify Reel      │   apify_scraper.py ── runs actor (off-account) ── saves dataset → apify_pending.json     │
 Scraper actor ──┼─▶ for each reel item:                                                                    │
 (their infra)   │      apify.py.download_reel():  videoUrl ─(CDN)─▶ temp.mp4                                │
                 │                                  transcode.py: HEVC x265 CRF28 ─▶ smaller.mp4             │──▶ Cloudflare R2
                 │                                  r2.py: put_object ─────────────────────────────────────▶│    granthvani-reels-inbox/reels/
                 │                                  r2.py: append manifest.jsonl ───────────────────────────│    + manifest.jsonl
 IG DMs ────────▶│   watcher.py (DM mode, DISABLED now) ── instagrapi inbox poll ── same pipeline           │
 (@gv_reeldb)    │                                                                                          │
                 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

- **DM mode** = the only part that uses the IG account (currently off).
- **Apify mode** = no IG account; Apify scrapes, we download public CDN URLs.

---

## 3. Repo & files

- **Repo:** `github.com/yrevash/dwdn` (private), branch `main`. Bot lives in `bot/`.
- **Local:** `/Users/yrevash/gv_frontend/content/dwdn/bot` (Mac), `~/dwdn/bot` (VM).

| File | Purpose |
| --- | --- |
| `watcher.py` | Entry point / main loop. Starts the Apify thread always; runs the DM watcher only if `DM_ENABLED=true`. |
| `apify_scraper.py` | Calls the Apify actor, saves the dataset to `apify_pending.json`, processes it (resumable, progress logged). Incremental via `onlyPostsNewerThan`. |
| `apify.py` | `download_reel(item)` — download an Apify reel's `videoUrl` → HEVC → R2 → manifest. |
| `apify_ingest.py` | Manual: ingest a downloaded Apify dataset JSON file (`python apify_ingest.py file.json`). |
| `transcode.py` | ffmpeg HEVC (libx265) transcode; falls back to original on failure. |
| `r2.py` | The only code that talks to R2 (boto3): `upload()` + `append_manifest()`. |
| `scraper.py` | **Legacy** instagrapi on-account scraper — replaced by Apify, no longer started. |
| `login_by_session.py` | Mint `session.json` from a browser `sessionid` cookie (for DM mode). Auto-tests DM read. |
| `login_setup.py` | Interactive user/pass login with device persistence (DM mode, alternative). |
| `diag_env.py` | `.env` integrity check — prints NO secret values. |
| `verify_all.py` | Session health check — DM read, media_info, scrape, R2 (used for the datacenter-IP test). |
| `Dockerfile`, `docker-compose.yml` | Container deploy (alternative to systemd; ffmpeg baked in). |
| `requirements.txt` | instagrapi, boto3, requests, python-dotenv, apify-client. |
| `.env.example`, `targets.txt.example` | Templates. |

**State files (gitignored, live on the VM):** `session.json`, `downloaded_urls.json`,
`manifest.jsonl`, `apify_state.json`, `apify_pending.json`, `seen_ids.json`,
`username_cache.json`, `scrape_state.json`.

---

## 4. Deployment (Azure)

- **VM:** `reel` — Standard **B4as_v2** (4 vcpu / 16 GB), **Ubuntu 24.04 LTS x64**, **Central India**.
  Public IP `40.81.226.90`. User `azureuser`. Resource group `instances`. ~$72/mo (covered by credits).
- **Run:** systemd unit `/etc/systemd/system/reelbot.service` →
  `WorkingDirectory=/home/azureuser/dwdn/bot`, `ExecStart=…/venv/bin/python …/watcher.py`,
  `Restart=always`. venv at `~/dwdn/bot/venv` (Python 3.12). Auto-starts on boot.
- **Code on VM:** `git clone` of the private repo; secrets (`.env`, `session.json`, `targets.txt`)
  scp'd separately (gitignored).

### Deploy an update
```bash
cd ~/dwdn && git pull
cd bot && ./venv/bin/pip install -r requirements.txt   # only if deps changed
sudo systemctl restart reelbot
journalctl -u reelbot -f
```

---

## 5. Security hardening (done)

- **No public inbound ports.** Azure NSG `reel-nsg` has only the 3 defaults (`DenyAllInBound`).
  SSH port 22 deleted from public.
- **Access only via Tailscale** (`tailscale up --ssh`). Reach the VM at `azureuser@reel` (tailnet).
- SSH hardened (key-only, no root, no passwords) + **fail2ban** + **ufw** (deny inbound, allow tailscale0)
  + **unattended-upgrades**.
- Secret files `chmod 600`. **R2 token is scoped to `granthvani-reels-inbox` only** — cannot touch the
  main `granthvani-cdn` bucket even if the VM is compromised.

---

## 6. Config (`.env`) — current VM values

```
# Instagram (DM mode — currently OFF)
IG_USERNAME=gv_reeldb
IG_PASSWORD=...
IG_TOTP_SEED=                 # 2FA is OFF on the account
DM_ENABLED=false             # ← Apify-only; IG account never used

# Cloudflare R2 (scoped inbox token)
R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
R2_BUCKET=granthvani-reels-inbox
R2_PREFIX=reels/

# Transcode
TRANSCODE=true
TRANSCODE_CRF=28             # quality/size; higher=smaller. 28 ≈ great on phones
TRANSCODE_PRESET=slow        # ⚠ slow ≈ realtime encode (~2-3 min/reel). medium ≈ 3x faster, +5% size

# Apify scraping (the active path)
APIFY_ENABLED=true
APPIFY_TOKEN=...             # NOTE: stored as APPIFY_TOKEN (double-P typo); code accepts APIFY_TOKEN too
APIFY_ACTOR=xMc5Ga1oCONPmWJIa   # Instagram Reel Scraper
APIFY_BACKFILL_LIMIT=100     # reels/account on FIRST run (raise for full history)
APIFY_ONGOING_LIMIT=50       # reels/account on later runs (only NEW reels billed)
APIFY_INTERVAL=21600         # 6h

SCRAPE_ENABLED=false         # legacy on-account scraper off
DOWNLOAD_DIR=./downloads
```

**Target accounts** (`targets.txt`, one per line): `bhajanmarg_official`, `rajendradasjimaharaj`,
`bhaktipath`, `sripundrik`, `vinodbabajimaharaj`.

---

## 7. How auth works (the saga + the working solution)

- **instagrapi cold login (user/pass) is blocked** by Instagram's newer **"Bloks" checkpoint**, which
  instagrapi cannot auto-resolve and which did NOT surface an approvable in-app prompt.
- **Working method for DM mode:** mint `session.json` from a **fresh browser `sessionid` cookie** via
  `login_by_session.py` (`cl.login_by_sessionid`). A **stale** sessionid returns `login_required` on
  private endpoints — always grab a fresh one (log out/in the browser first).
- The browser session is **"web-grade"**: it can do some reads but the **private API rejects it**
  (`media_info_v1`, `get_timeline_feed`, DM inbox all returned `login_required` at times). On a fresh
  session everything worked; it degrades under load / IP changes.
- **Datacenter IP risk:** heavy instagrapi scraping from the Azure IP got the account a **checkpoint
  warning**. → We pivoted scraping to **Apify** (off-account), which removed the risk entirely.
- `watcher.py` resume trusts the loaded session (checks `cl.user_id`, never cold-logs — that would
  re-trigger the checkpoint), and auto-persists the session every ~10 min.

---

## 8. Apify scraping — how it works & cost

- Actor **`xMc5Ga1oCONPmWJIa`** (Instagram Reel Scraper). Input: `username: [accounts]`,
  `resultsLimit`, `onlyPostsNewerThan` (string; **omitted** on backfill — null is rejected),
  `skipPinnedPosts`, `includeDownloadedVideo`. Output items carry `videoUrl`, `shortCode`,
  `ownerUsername`, `ownerId`, `inputUrl`, `caption`, `timestamp`, `videoDuration`, `type`, `productType`.
- **Flow:** first run = **BACKFILL** (up to `APIFY_BACKFILL_LIMIT`/account, no date filter), then every
  run is **incremental** (`onlyPostsNewerThan = last_seen`) so Apify **only bills for new reels**.
- **Resumable:** after the actor runs, the full dataset is saved to `apify_pending.json`. Processing
  reads from that file. **On restart, if the pending file exists it's resumed for FREE — the actor is
  NOT re-run.** Re-reading a dataset costs nothing; only *running* the actor costs. So restarts/crashes
  never re-charge. State (`backfilled`, `last_seen`) advances and the pending file is cleared only when
  the whole dataset is processed.
- **Cost:** ~$2.96 / 1000 reels (free-plan rate). Validation backfill (500) ≈ $1.50. Full history of all
  5 accounts (~10-15k reels) ≈ $30-45 one-time, then pennies/day incremental.
- `apify-client` 2.x returns a **pydantic `Run` object** (not a dict) — use the `_dataset_id()` helper.

---

## 9. Data model

- **R2 object key:** `reels/<timestamp>_<creator>_<caption>_<shortcode>.mp4`
  (filename uses the reel's **true creator** `ownerUsername`, which can differ from the scraped channel
  for reposts/collabs).
- **`manifest.jsonl`** (one JSON line per reel, mirrored to R2) — the index the rec engine reads:
  - Apify: `{key, origin:"scrape", sender(creator), source_channel, creator_id, caption, shortcode,
    media_pk, video_duration, codec, src_bytes, out_bytes, ts}`
  - DM: `{key, origin:"dm", sender, caption, shortcode, media_pk, codec, src_bytes, out_bytes, ts}`
- **Dedup:** `downloaded_urls.json` = set of `md5(reel_url)` fingerprints. Checked before every
  download; added only after a successful upload. Works across DM + Apify + restarts.
- Temp downloads in `downloads/` are deleted right after upload — disk stays near-empty.

---

## 10. Operational commands

```bash
# status / logs
sudo systemctl status reelbot
journalctl -u reelbot -f

# progress (how many reels in R2)
wc -l ~/dwdn/bot/manifest.jsonl
# (new code logs "Apify progress: X/total … left" every 10 reels)

# stop / start / restart
sudo systemctl stop|start|restart reelbot

# change a setting safely (no dup lines)
setenv() { grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
setenv TRANSCODE_PRESET medium
sudo systemctl restart reelbot
```

---

## 11. Open items / next steps

1. **Let the current ~557 backfill finish** (on old code; ~20h at `slow`).
2. **Then deploy the resumable fix** (`git pull` + restart → cheap incremental, future restarts free)
   and consider switching `TRANSCODE_PRESET=slow → medium` (~3× faster, +~5% size).
3. **Full history backfill** when ready:
   ```bash
   cd ~/dwdn/bot
   sed -i 's/^APIFY_BACKFILL_LIMIT=.*/APIFY_BACKFILL_LIMIT=10000/' .env
   rm -f apify_state.json apify_pending.json
   sudo systemctl restart reelbot
   ```
4. **Recommendation engine** for app users (the VM's second job) — to be designed.
5. **DM mode** is paused (`DM_ENABLED=false`). To re-enable: mint a fresh session via
   `login_by_session.py`, set `DM_ENABLED=true`, restart.

---

## 12. Gotchas & lessons learned

- `.env` values: **no quotes** (compose `env_file` keeps quotes literally). dotenv does `$`-expansion —
  avoid `$` in unquoted values.
- The Apify token was saved as **`APPIFY_TOKEN`** (double-P typo). Code reads both.
- `onlyPostsNewerThan` must be a **string**; omit it (don't pass `null`) on the backfill.
- `preset slow` ≈ **real-time** encode — brutal for big backfills. `medium` is the sane default.
- The **backfill "done" flag** is saved only after the whole dataset is processed → with the OLD code, a
  restart mid-batch re-ran (re-charged) the actor. **Fixed** by the resumable/pending-file design
  (commit `48c1960`) — deploy it once the current batch completes.
- A restart of `journalctl -f` (Ctrl+C) does NOT stop the bot — systemd keeps it running 24/7.

---

## 13. Git history (this session, branch `main`)

| Commit | What |
| --- | --- |
| `fc72a23` | Initial bot: DM watcher + on-account scraper + HEVC + R2 + Docker |
| `1799239` | Apify-based scraping (off-account), replaces instagrapi scraper |
| `aca8eb9` | `DM_ENABLED` switch (Apify-only) + fix Apify `onlyPostsNewerThan` |
| `48c1960` | Resumable backfill — save dataset to `apify_pending.json`, never re-charge on restart |
| `6bff6fe` | Live `X/total` progress log every 10 reels |

(Original design spec also exists at `~/ig-reel-r2/docs/superpowers/specs/2026-06-14-ig-reel-to-r2-design.md`.)
