"""Account emails (confirm your address, reset your password) over SMTP, set by environment:

  SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = TLS), SMTP_USER, SMTP_PASS, MAIL_FROM
  PUBLIC_URL  the app's address, for the links (https://spotter.tamairnv.co.uk)

Without SMTP_HOST nothing is sent and the link goes to the log instead (for local testing), and
/api/health says accounts can't be made.
"""
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

log = logging.getLogger("spotter.mail")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://spotter.tamairnv.co.uk").rstrip("/")
sent: list[EmailMessage] = []  # the tests read these


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST")) or os.environ.get("MAIL_LOG_ONLY") == "1"


def _send(msg: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.warning("no SMTP_HOST: not sending %r to %s; body:\n%s", msg["Subject"], msg["To"], msg.get_content())
        sent.append(msg)
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls(context=ctx)
        with s:
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        log.info("sent %r to %s", msg["Subject"], msg["To"])
    except Exception:
        log.exception("couldn't send %r", msg["Subject"])


def send(to: str, subject: str, text: str) -> None:
    """In the background, so a slow mail server can't hold a request up or say whether an address exists."""
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "FPV Spot Finder <no-reply@localhost>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    threading.Thread(target=_send, args=(msg,), daemon=True).start()


def verify(to: str, username: str, token: str) -> None:
    send(to, "Confirm your FPV Spot Finder account", f"""Hi {username},

Confirm your email to finish making your account:

{PUBLIC_URL}/#verify={token}

The link works once, for 48 hours. If you didn't sign up, ignore this: the account won't work without it.
""")


def reset(to: str, username: str, token: str) -> None:
    send(to, "Reset your FPV Spot Finder password", f"""Hi {username},

Someone (hopefully you) asked to reset your password. Pick a new one here:

{PUBLIC_URL}/#reset={token}

The link works once, for 1 hour, and signs out every device. If it wasn't you, ignore this: nothing changes.
""")


def taken(to: str, username: str) -> None:
    """Someone tried to sign up with an address that already has an account: tell its owner, not them."""
    send(to, "Someone tried to sign up with your email", f"""Hi {username},

Someone tried to make a new FPV Spot Finder account with this email, which already has yours.
If that was you, sign in instead, or use "Forgot password" at {PUBLIC_URL}/. Otherwise ignore this.
""")
