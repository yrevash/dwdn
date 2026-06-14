#!/usr/bin/env python3
"""One-time interactive Instagram login → mints a trusted session.json.

Clears the 'challenge_required' checkpoint by PERSISTING one device across
attempts: run it → approve the login in the Instagram app → run it again with
the SAME device, SAME network. Type the password directly (never from .env).

    cd bot && .venv/bin/python login_setup.py
"""

import getpass
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired, ChallengeRequired, BadPassword

SESSION_FILE = Path("session.json")


def build_client():
    cl = Client()
    cl.delay_range = [3, 7]
    # Reuse the SAME device across attempts. Instagram trusts a *device*; a fresh
    # random one each run re-triggers the checkpoint forever.
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            print(f"↻ Reusing the device saved in {SESSION_FILE}")
        except Exception:
            pass
    return cl


def main():
    username = input("Instagram username [gv_reeldb]: ").strip() or "gv_reeldb"
    password = getpass.getpass("Password (typed, hidden, NOT from .env): ")

    cl = build_client()

    proxy = input("Proxy URL (optional, blank = none): ").strip()
    if proxy:
        cl.set_proxy(proxy)
        print(f"Using proxy {proxy}")

    seed = input("TOTP seed / authenticator setup key (blank = type a code): ").strip().replace(" ", "")
    code = ""
    if seed:
        try:
            code = cl.totp_generate_code(seed)
            print(f"2FA code from seed: {code}")
        except Exception as e:
            print(f"Couldn't use seed ({e}); will ask for a typed code if needed.")

    # Lock in this device now so a retry after app-approval reuses it.
    cl.dump_settings(SESSION_FILE)

    try:
        if code:
            cl.login(username, password, verification_code=code)
        else:
            try:
                cl.login(username, password)
            except TwoFactorRequired:
                otp = input("2FA code from your authenticator app: ").strip()
                cl.login(username, password, verification_code=otp)

    except ChallengeRequired:
        cl.dump_settings(SESSION_FILE)  # keep the SAME device for the retry
        print("\n" + "=" * 64)
        print("CHECKPOINT — Instagram wants to confirm this login.")
        print("=" * 64)
        print("1) Open the Instagram APP signed in as this account.")
        print("2) Approve the login:  'We noticed a new login / Was this you?' → YES,")
        print("   or Settings → Accounts Center → Password & security →")
        print("   'Where you're logged in' / Login activity → approve the new attempt.")
        print("3) Then RUN THIS SCRIPT AGAIN (same device is saved, stay on the SAME wifi).")
        print("   The now-trusted device should go straight through.")
        return
    except BadPassword:
        print("\n❌ Bad password — type the NEW password you just set on Instagram.")
        return

    cl.dump_settings(SESSION_FILE)
    print(f"\n✅ Logged in as @{cl.username} — wrote {SESSION_FILE.resolve()}")
    print("   Next: .venv/bin/python watcher.py   (it now resumes this session)")
    print("   For Azure: copy session.json into the bot's state/ folder.")


if __name__ == "__main__":
    main()
