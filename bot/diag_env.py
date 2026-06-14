#!/usr/bin/env python3
"""Safe .env diagnostic — prints NO credential VALUES, only integrity metadata,
so we can tell a misread password apart from an Instagram-side block.

Run from the bot/ folder:   .venv/bin/python diag_env.py
"""
from dotenv import dotenv_values

interp = dotenv_values(".env", interpolate=True)    # exactly how watcher.py loads it
raw    = dotenv_values(".env", interpolate=False)   # the literal file contents


def charflags(s):
    if s is None:
        return "MISSING"
    hits = [name for name, bad in {
        "$": "$" in s, "#": "#" in s,
        "quote": ('"' in s or "'" in s),
        "lead/trail-space": s != s.strip(),
        "backtick": "`" in s,
    }.items() if bad]
    return "clean" if not hits else "has " + ", ".join(hits)


pw_i, pw_r = interp.get("IG_PASSWORD"), raw.get("IG_PASSWORD")
seed = raw.get("IG_TOTP_SEED")

print("=== .env integrity (NO secret values shown) ===")
print(f"IG_USERNAME        : {interp.get('IG_USERNAME')!r}")   # already visible in your logs
print(f"IG_PASSWORD length : literal={len(pw_r) if pw_r else 0}  as-loaded={len(pw_i) if pw_i else 0}")
if pw_r and pw_i and len(pw_r) != len(pw_i):
    print("  >> MISMATCH: python-dotenv is ALTERING your password (a '$' is being treated")
    print("     as a variable). Instagram received the WRONG password. ROOT CAUSE = H1.")
elif pw_r:
    print("  >> password length unchanged by dotenv (H1 from $-expansion unlikely)")
print(f"IG_PASSWORD chars  : {charflags(pw_r)}")
print(f"IG_TOTP_SEED       : {('set, len=' + str(len(seed))) if seed else 'NOT SET (required — 2FA is on)'}")
print(f"SCRAPE_ENABLED     : {interp.get('SCRAPE_ENABLED')!r}")
for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
    v = raw.get(k)
    print(f"{k:<19}: {('set, len=' + str(len(v))) if v else 'NOT SET'}")
