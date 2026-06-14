#!/usr/bin/env python3
"""Full capability check on the current session — every private-API op both
modes need."""
from dotenv import load_dotenv
load_dotenv()
import r2, watcher
from instagrapi import Client

cl = Client()
cl.delay_range = [3, 7]
cl.load_settings("session.json")
watcher._ig_client = cl
print("session user_id:", cl.user_id, "\n")
ok = {}

# 1. DM read (mode 1 — DM watcher discovery)
try:
    resp = cl.private_request("direct_v2/inbox/", params={"limit": "5", "thread_message_limit": "1"})
    threads = resp.get("inbox", {}).get("threads", [])
    print(f"[1 DM read]     OK  — inbox returned {len(threads)} thread(s)")
    ok["dm_read"] = True
except Exception as e:
    print(f"[1 DM read]     FAIL — {type(e).__name__}: {e}")
    ok["dm_read"] = False

# 2. media_info_v1 (mode 1 — DM-sent reel download path)
try:
    pk = cl.media_pk_from_code("DZjfDvWzQOM")
    info = cl.media_info_v1(pk)
    print(f"[2 DM download] OK  — media_info_v1 works (video_url={bool(info.video_url)})")
    ok["dm_dl"] = True
except Exception as e:
    print(f"[2 DM download] FAIL — {type(e).__name__}: {e}")
    ok["dm_dl"] = False

# 3. scrape discovery (mode 2)
try:
    uid = cl.user_id_from_username("bhajanmarg_official")
    medias, _ = cl.user_medias_paginated(uid, amount=3, end_cursor="")
    reels = [m for m in medias if getattr(m, "product_type", "") == "clips"]
    print(f"[3 Scrape]      OK  — listing works, {len(reels)} reels with video_url="
          f"{sum(1 for m in reels if getattr(m,'video_url',None))}")
    ok["scrape"] = True
except Exception as e:
    print(f"[3 Scrape]      FAIL — {type(e).__name__}: {e}")
    ok["scrape"] = False

# 4. R2 target
print(f"[4 R2]          {'OK  — configured -> ' + r2.R2_BUCKET if r2.is_configured() else 'FAIL — not configured'}")
ok["r2"] = r2.is_configured()

print("\nSUMMARY:", "✅ ALL GREEN — both modes fully functional" if all(ok.values())
      else "⚠️ issues: " + ", ".join(k for k, v in ok.items() if not v))
