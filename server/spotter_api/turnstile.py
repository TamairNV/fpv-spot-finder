"""Cloudflare Turnstile: a free, usually invisible check that a person, not a script, is making an
account or asking for a reset email. Set TURNSTILE_SITE_KEY and TURNSTILE_SECRET (Cloudflare
dashboard > Turnstile). Without them the check is skipped, so local testing still works.
"""
import os

import httpx

SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
SECRET = os.environ.get("TURNSTILE_SECRET", "")


def check(token: str | None, ip: str) -> bool:
    if not SECRET:
        return True
    if not token or len(token) > 2048:
        return False
    try:
        r = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                       data={"secret": SECRET, "response": token, "remoteip": ip}, timeout=10)
        return bool(r.json().get("success"))
    except Exception:
        return False
