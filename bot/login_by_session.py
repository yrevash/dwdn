#!/usr/bin/env python3
"""Mint session.json from your BROWSER's Instagram session — bypasses the login
checkpoint entirely by reusing the already-trusted browser login.

Get the cookie:
  1. Log into instagram.com in your browser (you already can).
  2. Open DevTools:  Chrome/Edge/Brave  → F12 / Cmd+Opt+I → Application tab
                     Firefox            → Storage tab
                     Safari (enable Develop menu first) → Web Inspector → Storage
  3. Cookies → https://www.instagram.com → click the 'sessionid' row
  4. Copy its VALUE (looks like  1234567890%3AAbCd...%3A12%3A...).
  5. Run this and paste it.

    cd bot && .venv/bin/python login_by_session.py
"""

import getpass
from pathlib import Path

from instagrapi import Client

SESSION_FILE = Path("session.json")


def main():
    print("Paste the 'sessionid' cookie VALUE from instagram.com (it's hidden as you paste).")
    sessionid = getpass.getpass("sessionid: ").strip().strip('"').strip("'")
    if not sessionid:
        print("No sessionid given — aborting.")
        return

    cl = Client()
    cl.delay_range = [3, 7]

    proxy = input("Proxy URL (optional, blank = none): ").strip()
    if proxy:
        cl.set_proxy(proxy)
        print(f"Using proxy {proxy}")

    try:
        cl.login_by_sessionid(sessionid)
    except Exception as e:
        print(f"\n❌ Could not use that sessionid: {e}")
        print("   Re-copy the FULL value of the 'sessionid' cookie from the instagram.com")
        print("   Cookies panel (it's HttpOnly, so `document.cookie` won't show it — you must")
        print("   read it from DevTools → Application/Storage → Cookies).")
        return

    # confirm who we are
    try:
        who = cl.username or cl.account_info().username
    except Exception:
        who = "(unknown)"

    cl.dump_settings(SESSION_FILE)
    print(f"\n✅ Authenticated as @{who} via your browser session — wrote {SESSION_FILE.resolve()}")

    # Immediately test the two private-API capabilities the bot needs.
    print("\nTesting what this session can actually do:")
    try:
        resp = cl.private_request("direct_v2/inbox/", params={"limit": "1", "thread_message_limit": "1"})
        n = len(resp.get("inbox", {}).get("threads", []))
        print(f"   ✅ DMs READABLE — inbox returned {n} thread(s). DM mode will work!")
    except Exception as e:
        print(f"   ❌ DMs blocked ({type(e).__name__}: {e}).")
        print("      This session is restricted for DMs. Try again with a FRESH sessionid")
        print("      after the account cools off, or use a residential proxy.")

    print("\n   Scraping uses public reads and already works regardless.")
    print("   Start the bot:  .venv/bin/python watcher.py")
    print("   For Azure: copy session.json into the bot's state/ folder.")


if __name__ == "__main__":
    main()
