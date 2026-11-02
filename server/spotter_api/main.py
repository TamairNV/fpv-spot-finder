"""The spot finder's server side: sync between devices, a flight log with photos, the flying
forecast and evening notifications. The app works without it (GitHub Pages); it switches these
on when /api/health answers.

A device gets a random key the first time it has something to save, and another device joins the
same profile with a short code from the first one. An account (username + password) is optional on
top: it lets you sign in on any device, and your star reviews show to others under your username.
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import forecast, photos
from sitestats import Stats

from . import mail, turnstile
from .db import DATA_DIR, DB, NotVerified, Taken, check_pw
from .models import Models, NotHere
from .rerank import Busy, Reranker

log = logging.getLogger("spotter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

db = DB()
reranker = Reranker(db, DATA_DIR)
models = Models(DATA_DIR)
PHOTO_DIR = DATA_DIR / "photos"
REGION_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
KINDS = {"rating", "note", "flight", "saved", "place", "review"}
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
DRONE_RE = re.compile(r"^[a-z0-9]{1,40}$")
MAX_PHOTOS = 3000
MAX_UPLOAD = 30 * 1024 * 1024
NOTIFY_AT = (18, 30)  # local time the evening-before check runs

push = None
try:
    from .push import Push
    push = Push(DATA_DIR)
except Exception as e:  # pragma: no cover - the rest works without push
    log.warning("push disabled: %s", e)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(notify_loop()) if push else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="FPV Spot Finder API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# The admin stats page (site-stats): only for verified accounts whose email is in SPOTTER_ADMIN_EMAILS.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("SPOTTER_ADMIN_EMAILS", "").split(",") if e.strip()}
stats = Stats(DATA_DIR / "stats.db")


def is_admin_pid(pid: int | None) -> bool:
    acct = db.account(pid) if pid else None
    return bool(acct and acct["verified"] and acct["email"].lower() in ADMIN_EMAILS)


def is_admin(request: Request) -> bool:
    key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return is_admin_pid(db.profile_for(key) if key else None)


stats.install(app, is_admin)
for kind, text in {"signup": "Account made (unconfirmed)", "verified": "Email confirmed", "login": "Signed in",
                   "login_fail": "Wrong password", "unverified_login": "Sign-in before confirming", "captcha_fail": "Failed are-you-human check",
                   "rate_limited": "Rate limited (429)", "limit_hit": "Hit a sign-in/sign-up/email limit", "reset_request": "Password reset asked for", "reset_done": "Password reset",
                   "signup_taken_email": "Sign-up with an email in use", "account_deleted": "Account deleted", "new_profile": "New device profile"}.items():
    stats.label(kind, text)
_one = lambda sql: (db.one(sql) or [0])[0]
stats.gauge("Device profiles", lambda: _one("SELECT COUNT(*) FROM profiles"), "Users")
stats.gauge("Accounts", lambda: _one("SELECT COUNT(*) FROM accounts WHERE verified = 1"), "Users")
stats.gauge("Unconfirmed sign-ups", lambda: _one("SELECT COUNT(*) FROM accounts WHERE verified = 0"), "Users")
stats.gauge("Devices seen, 7 days", lambda: db.one("SELECT COUNT(*) FROM keys WHERE seen > ?", time.time() - 7 * 86400)[0], "Users")
stats.gauge("Star reviews", lambda: _one("SELECT COUNT(*) FROM items WHERE kind = 'review' AND value != 'null'"), "Content")
stats.gauge("Saved spots", lambda: _one("SELECT COUNT(*) FROM items WHERE kind = 'saved' AND value != 'null'"), "Content")
stats.gauge("Own places", lambda: _one("SELECT COUNT(*) FROM items WHERE kind = 'place' AND value != 'null'"), "Content")
stats.gauge("Flights logged", lambda: _one("SELECT COUNT(*) FROM items WHERE kind = 'flight' AND value != 'null'"), "Content")
stats.gauge("Photos", lambda: _one("SELECT COUNT(*) FROM photos"), "Content")
stats.gauge("Push subscriptions", lambda: _one("SELECT COUNT(*) FROM push_subs"), "Content")


# ---------------------------------------------------------------- auth

def profile(authorization: str = Header(default="")) -> int:
    key = authorization.removeprefix("Bearer ").strip()
    pid = db.profile_for(key) if key else None
    if not pid:
        raise HTTPException(401, "unknown key")
    return pid


def region_ok(region: str) -> str:
    if not REGION_RE.match(region):
        raise HTTPException(400, "bad region")
    return region


_made: dict[str, list[float]] = defaultdict(list)


def client_ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")


# ---------------------------------------------------------------- profile + linking

@app.get("/api/health")
def health():
    return {"ok": True, "features": ["sync", "flights", "photos", "forecast", "rerank", "reviews"]
            + (["accounts"] if mail.configured() else []) + (["push"] if push else []),
            "turnstile": turnstile.SITE_KEY or None}


@app.post("/api/profile")
def new_profile(req: Request):
    ip = client_ip(req)
    now = time.time()
    _made[ip] = [t for t in _made[ip] if now - t < 86400]
    if len(_made[ip]) >= 30:
        raise HTTPException(429, "too many new profiles from here today")
    _made[ip].append(now)
    _, key = db.new_profile()
    stats.event("new_profile", req)
    return {"key": key}


@app.get("/api/me")
def me(pid: int = Depends(profile)):
    acct = db.account(pid)
    return {"devices": db.devices(pid), "counts": db.counts(pid),
            "username": acct["username"] if acct and acct["verified"] else None, "admin": is_admin_pid(pid)}


# ---------------------------------------------------------------- accounts
#
# Made with a username, an email and a password; it can't sign in, and its reviews don't show, until
# the email link is opened. Bots are kept out by Cloudflare Turnstile and per-address limits; nothing
# says whether an email has an account (sign-up and "forgot password" answer the same either way).

EMAIL_RE = re.compile(r"^[^@\s<>,;\"']{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class Signup(BaseModel):
    username: str = Field(max_length=40)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    captcha: str | None = Field(default=None, max_length=2048)


class Login(BaseModel):
    username: str = Field(max_length=254)  # or the email
    password: str = Field(max_length=200)


def tries(bucket: dict, who: str, limit: int, window: float = 600) -> None:
    now = time.time()
    bucket[who] = [t for t in bucket[who] if now - t < window]
    if len(bucket[who]) >= limit:
        stats.event("limit_hit", ip=who if not who.startswith(("user:", "to:")) else "", detail=who.split(":")[0] if ":" in who else None)
        raise HTTPException(429, "too many tries, wait a while and try again")
    bucket[who].append(now)


_logins: dict[str, list[float]] = defaultdict(list)
_signups: dict[str, list[float]] = defaultdict(list)
_mails: dict[str, list[float]] = defaultdict(list)


def good_password(pw: str, username: str = "", email: str = "") -> None:
    low = pw.lower()
    if len(pw) < 10:
        raise HTTPException(400, "pick a password of at least 10 characters")
    if len(set(pw)) < 5 or (username and username.lower() in low) or (email and email.split("@")[0].lower() in low) \
            or low in {"password123", "1234567890", "qwertyuiop", "password12", "letmein123", "iloveyou12"}:
        raise HTTPException(400, "that password's too easy to guess")


def captcha_ok(token: str | None, req: Request) -> None:
    if not turnstile.check(token, client_ip(req)):
        stats.event("captcha_fail", req)
        raise HTTPException(403, "the are-you-human check didn't pass: try again")


def accounts_on() -> None:
    if not mail.configured():
        raise HTTPException(503, "accounts aren't switched on here yet")


@app.post("/api/account/signup")
def signup(body: Signup, req: Request, authorization: str = Header(default="")):
    accounts_on()
    ip = client_ip(req)
    tries(_signups, ip, 5, 86400)  # five accounts a day from one address is plenty for people
    captcha_ok(body.captcha, req)
    name, email = body.username.strip(), body.email.strip().lower()
    if not USER_RE.match(name):
        raise HTTPException(400, "usernames are 3 to 24 letters, numbers, dots, dashes or underscores")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "that email doesn't look right")
    good_password(body.password, name, email)
    key = authorization.removeprefix("Bearer ").strip()
    pid = db.profile_for(key) if key else None
    try:
        new_pid, new_key = db.signup(pid, name, email, body.password)
    except Taken as e:
        if e.args[0] == "username":
            raise HTTPException(409, "that username's taken")
        # the email has an account: tell its owner, and answer as if it worked so this can't be used
        # to find out who's signed up
        acct = db.by_email(email)
        stats.event("signup_taken_email", req)
        tries(_mails, "to:" + email, 3, 3600)
        mail.taken(email, acct["username"])
        return {"key": key or None, "check_email": True}
    stats.event("signup", req, name)
    tries(_mails, "to:" + email, 3, 3600)
    mail.verify(email, name, db.new_token(new_pid, "verify"))
    # this device keeps its key (and everything on it); a device with none gets the new profile's
    return {"key": new_key or key, "check_email": True}


class Token(BaseModel):
    token: str = Field(max_length=100)


@app.post("/api/account/verify")
def verify(body: Token, req: Request):
    tries(_logins, client_ip(req), 20)
    key = db.verify(body.token)
    if not key:
        raise HTTPException(404, "that link has expired or was already used: sign in, or sign up again")
    name = db.account(db.profile_for(key))["username"]
    stats.event("verified", req, name)
    return {"key": key, "username": name}


@app.post("/api/account/login")
def login(body: Login, req: Request):
    who = body.username.strip()
    tries(_logins, client_ip(req), 20)
    tries(_logins, "user:" + who.lower(), 10, 3600)  # slows guessing at one account from many addresses
    try:
        key = db.login(who, body.password)
    except NotVerified:
        acct = db.by_email(who.lower()) or db.one("SELECT profile_id, username, email FROM accounts WHERE username = ?", who)
        if acct:
            try:
                tries(_mails, "to:" + acct["email"], 3, 3600)
                mail.verify(acct["email"], acct["username"], db.new_token(acct["profile_id"], "verify"))
            except HTTPException:
                pass
        stats.event("unverified_login", req)
        raise HTTPException(403, "confirm your email first: we've sent the link again")
    if not key:
        stats.event("login_fail", req)
        raise HTTPException(401, "wrong username or password")
    name = db.account(db.profile_for(key))["username"]
    stats.event("login", req, name)
    return {"key": key, "username": name}


class Forgot(BaseModel):
    email: str = Field(max_length=254)
    captcha: str | None = Field(default=None, max_length=2048)


@app.post("/api/account/forgot")
def forgot(body: Forgot, req: Request):
    accounts_on()
    tries(_mails, client_ip(req), 5, 3600)
    captcha_ok(body.captcha, req)
    email = body.email.strip().lower()
    acct = db.by_email(email) if EMAIL_RE.match(email) else None
    stats.event("reset_request", req)
    if acct:
        try:
            tries(_mails, "to:" + email, 3, 3600)
            mail.reset(email, acct["username"], db.new_token(acct["profile_id"], "reset"))
        except HTTPException:
            pass  # quietly: the answer mustn't differ
    return {"ok": True}  # the same whether or not there's an account


class Reset(BaseModel):
    token: str = Field(max_length=100)
    password: str = Field(max_length=200)


@app.post("/api/account/reset")
def reset(body: Reset, req: Request):
    tries(_logins, client_ip(req), 20)
    good_password(body.password)
    key = db.reset_password(body.token, body.password)
    if not key:
        raise HTTPException(404, "that link has expired or was already used: ask for a new one")
    name = db.account(db.profile_for(key))["username"]
    stats.event("reset_done", req, name)
    return {"key": key, "username": name}


@app.post("/api/account/logout")
def logout(authorization: str = Header(default=""), pid: int = Depends(profile)):
    db.drop_key(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


class NewPassword(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/account/password")
def password(body: NewPassword, req: Request, authorization: str = Header(default=""), pid: int = Depends(profile)):
    tries(_logins, client_ip(req), 20)
    acct = db.account(pid) or {}
    good_password(body.new, acct.get("username", ""), acct.get("email", ""))
    if not db.set_password(pid, body.old, body.new, authorization.removeprefix("Bearer ").strip()):
        raise HTTPException(403, "that's not your current password")
    return {"ok": True}


class Delete(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/account/delete")
def delete_account(body: Delete, req: Request, pid: int = Depends(profile)):
    tries(_logins, client_ip(req), 20)
    row = db.one("SELECT pw FROM accounts WHERE profile_id = ?", pid)
    if not row or not check_pw(body.password, row["pw"]):
        raise HTTPException(403, "that's not your password")
    ids = [r["id"] for r in db.q("SELECT id FROM photos WHERE profile_id = ?", pid)]
    db.delete_account(pid)
    stats.event("account_deleted", req)
    for i in ids:
        photos.delete(i, PHOTO_DIR)
    return {"ok": True}


# ---------------------------------------------------------------- reviews (public: stars and words from accounts)

@app.get("/api/reviews")
def reviews(region: str, drone: str, spot: str):
    region_ok(region)
    if not DRONE_RE.match(drone) or len(spot) > 100:
        raise HTTPException(400, "bad spot")
    out = db.reviews(region, f"{drone}/{spot}")
    n = len(out)
    return {"avg": round(sum(r["stars"] for r in out) / n, 1) if n else None, "n": n, "reviews": out[:50]}


class Hide(BaseModel):
    region: str
    drone: str
    spot: str = Field(max_length=100)
    user: str = Field(max_length=40)


@app.post("/api/reviews/hide")
def hide_review(body: Hide, pid: int = Depends(profile)):
    if not is_admin_pid(pid):
        raise HTTPException(403, "not allowed")
    region_ok(body.region)
    if not DRONE_RE.match(body.drone) or not db.hide_review(body.region, f"{body.drone}/{body.spot}", body.user):
        raise HTTPException(404, "no such review")
    return {"ok": True}


@app.get("/api/reviews/summary")
def review_summary(region: str, drone: str):
    region_ok(region)
    if not DRONE_RE.match(drone):
        raise HTTPException(400, "bad drone")
    return db.review_summary(region, drone)


@app.post("/api/link")
def link(pid: int = Depends(profile)):
    return {"code": db.new_link(pid), "expires_in": 600}


class Claim(BaseModel):
    code: str = Field(max_length=20)


_claims: dict[str, list[float]] = defaultdict(list)


@app.post("/api/link/claim")
def claim(body: Claim, req: Request):
    ip = client_ip(req)
    now = time.time()
    _claims[ip] = [t for t in _claims[ip] if now - t < 600]
    if len(_claims[ip]) >= 10:  # 32^8 codes, 10 tries per 10 minutes: not guessable
        raise HTTPException(429, "too many tries, wait a few minutes")
    _claims[ip].append(now)
    key = db.claim_link(body.code)
    if not key:
        raise HTTPException(404, "that code isn't valid (they last 10 minutes)")
    return {"key": key}


# ---------------------------------------------------------------- sync

class Change(BaseModel):
    kind: str
    k: str = Field(max_length=200)
    value: dict | None
    t: float


class SyncBody(BaseModel):
    region: str
    rev: int = 0
    changes: list[Change] = Field(default_factory=list, max_length=5000)


@app.post("/api/sync")
def sync(body: SyncBody, pid: int = Depends(profile)):
    region = region_ok(body.region)
    changes = []
    for c in body.changes:
        if c.kind not in KINDS:
            raise HTTPException(400, f"bad kind {c.kind}")
        if c.value is not None and len(json.dumps(c.value)) > 20000:
            raise HTTPException(413, "item too big")
        changes.append(c.model_dump())
    if changes:
        db.merge(pid, region, changes)
    items, rev = db.since(pid, region, body.rev)
    return {"items": items, "rev": rev, "photos": photo_index(pid, region)}


def photo_index(pid: int, region: str) -> dict:
    out: dict = defaultdict(list)
    for r in db.q("SELECT id, flight_id, w, h FROM photos WHERE profile_id = ? AND region = ? ORDER BY created", pid, region):
        out[r["flight_id"]].append({"id": r["id"], "w": r["w"], "h": r["h"]})
    return out


# ---------------------------------------------------------------- photos

@app.post("/api/photos")
async def upload(region: str, flight: str, file: UploadFile = File(...), pid: int = Depends(profile)):
    region_ok(region)
    if len(flight) > 100:
        raise HTTPException(400, "bad flight id")
    if db.one("SELECT COUNT(*) AS n FROM photos WHERE profile_id = ?", pid)["n"] >= MAX_PHOTOS:
        raise HTTPException(413, "photo limit reached")
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "photo too big (30 MB max)")
    try:
        info = await asyncio.to_thread(photos.save, raw, PHOTO_DIR)
    except Exception as e:
        raise HTTPException(415, f"can't read that image ({e.__class__.__name__})")
    db.write(lambda c: c.execute("INSERT INTO photos (id, profile_id, region, flight_id, w, h, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (info["id"], pid, region, flight, info["w"], info["h"], time.time())))
    return info


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: str, pid: int = Depends(profile)):
    row = db.one("SELECT id FROM photos WHERE id = ? AND profile_id = ?", photo_id, pid)
    if not row:
        raise HTTPException(404, "no such photo")
    db.write(lambda c: c.execute("DELETE FROM photos WHERE id = ?", (photo_id,)))
    photos.delete(photo_id, PHOTO_DIR)
    return {"ok": True}


@app.get("/api/photos/{name}")
def get_photo(name: str):
    # ids are 128-bit random: the link is the permission, so <img> tags work without headers
    if not re.fullmatch(r"[\w-]{20,30}(_t)?\.jpg", name):
        raise HTTPException(404)
    path = PHOTO_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------- forecast

def watched_spots(pid: int, region: str, drone: str) -> list[dict]:
    """Spots worth a forecast: the ones you 👍 or saved for this drone, then the ones you've flown."""
    out: dict[str, dict] = {}
    for r in db.items(pid, region, "rating"):
        d, _, sid = r["k"].partition("/")
        ll = r.get("ll")
        if r.get("v", 0) > 0 and d == drone and ll:
            out.setdefault(sid, {"id": sid, "title": r.get("title") or sid, "lon": ll[0], "lat": ll[1], "drone": d})
    for r in db.items(pid, region, "saved"):
        d, _, sid = r["k"].partition("/")
        ll = r.get("ll")
        if d == drone and ll:
            out.setdefault(sid, {"id": sid, "title": r.get("title") or sid, "lon": ll[0], "lat": ll[1], "drone": d})
    for f in db.items(pid, region, "flight"):
        ll = f.get("ll")
        if ll and f.get("spot"):
            out.setdefault(f["spot"], {"id": f["spot"], "title": f.get("title") or f["spot"], "lon": ll[0], "lat": ll[1],
                                       "drone": f.get("drone")})
    return list(out.values())


@app.get("/api/forecast")
def get_forecast(region: str, drone: str = "freestyle5", lon: float | None = None, lat: float | None = None,
                 authorization: str = Header(default="")):
    region_ok(region)
    key = authorization.removeprefix("Bearer ").strip()
    pid = db.profile_for(key) if key else None
    spots = watched_spots(pid, region, drone) if pid else []
    mine = bool(spots)
    if not spots:
        if lon is None or lat is None:
            raise HTTPException(400, "no spots to forecast: pass lon and lat for the region")
        spots = [{"id": None, "title": "the region", "lon": lon, "lat": lat}]
    try:
        out = forecast.week(spots, drone)
    except Exception as e:
        raise HTTPException(502, f"forecast unavailable ({e.__class__.__name__})")
    return {**out, "mine": mine}


# ---------------------------------------------------------------- re-rank with your ratings

class Rerank(BaseModel):
    region: str


@app.get("/api/rerank")
def rerank_status(region: str, pid: int = Depends(profile)):
    return reranker.status(pid, region_ok(region))


@app.post("/api/rerank")
def rerank(body: Rerank, pid: int = Depends(profile)):
    region = region_ok(body.region)
    if not reranker.ready(region):
        raise HTTPException(409, "the server hasn't worked this region out yet")
    ratings = {r["k"]: {"v": r["v"]} for r in db.items(pid, region, "rating") if r.get("v")}
    if not ratings:
        raise HTTPException(400, "rate a few spots first")
    try:
        return reranker.start(pid, region, ratings)
    except Busy as e:
        raise HTTPException(429, str(e))


@app.delete("/api/rerank")
def rerank_drop(region: str, pid: int = Depends(profile)):
    reranker.drop(pid, region_ok(region))
    return reranker.status(pid, region)


# ---------------------------------------------------------------- 3D models on request

SPOT_RE = re.compile(r"^(\d{1,4})-(\d{1,4})$")
_models_made: dict[str, list[float]] = defaultdict(list)


@app.get("/api/models/{region}/{profile_key}/{spot}")
def get_model(region: str, profile_key: str, spot: str, req: Request):
    m = SPOT_RE.match(spot)
    if not m:
        raise HTTPException(400, "bad spot")
    region_ok(region)
    if not models.png(region, spot).exists():  # only new ones cost anything, so only they're limited
        ip, now = client_ip(req), time.time()
        recent = _models_made[ip] = [t for t in _models_made[ip] if now - t < 3600]
        if len(recent) >= 300:
            raise HTTPException(429, "that's a lot of 3D models: try again in a bit")
        recent.append(now)
    try:
        return models.get(region, profile_key, int(m[1]), int(m[2]))
    except NotHere:
        raise HTTPException(404, "the server doesn't have this region's height data yet")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/models/{region}/{name}")
def get_model_png(region: str, name: str):
    m = re.fullmatch(r"(\d{1,4}-\d{1,4})\.png", name)
    path = models.png(region_ok(region), m[1]) if m else None
    if not path or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------------------------------------------- push

class Subscribe(BaseModel):
    region: str
    drone: str = Field(max_length=40)
    sub: dict
    lon: float
    lat: float


@app.get("/api/push/key")
def push_key():
    if not push:
        raise HTTPException(503, "push not set up")
    return {"key": push.public_key}


@app.post("/api/push/subscribe")
def subscribe(body: Subscribe, pid: int = Depends(profile)):
    if not push:
        raise HTTPException(503, "push not set up")
    region_ok(body.region)
    endpoint = body.sub.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise HTTPException(400, "bad subscription")
    sub = {**body.sub, "center": [body.lon, body.lat]}
    db.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO push_subs (endpoint, profile_id, sub, region, drone, created, last_sent) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (endpoint, pid, json.dumps(sub), body.region, body.drone, time.time())))
    push.send(body.sub, {"title": "Flying forecast on", "body": "You'll get a heads-up the evening before a good day to fly.",
                         "url": f"./?region={body.region}"})
    return {"ok": True}


class Unsubscribe(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def unsubscribe(body: Unsubscribe, pid: int = Depends(profile)):
    db.write(lambda c: c.execute("DELETE FROM push_subs WHERE endpoint = ? AND profile_id = ?", (body.endpoint, pid)))
    return {"ok": True}


@app.get("/api/push/status")
def push_status(endpoint: str, pid: int = Depends(profile)):
    row = db.one("SELECT region, drone FROM push_subs WHERE endpoint = ? AND profile_id = ?", endpoint, pid)
    return {"on": bool(row), **(dict(row) if row else {})}


def tomorrow_message(sub_row) -> dict | None:
    sub = json.loads(sub_row["sub"])
    drone = sub_row["drone"]
    spots = watched_spots(sub_row["profile_id"], sub_row["region"], drone)
    if not spots:
        lon, lat = sub["center"]
        spots = [{"id": None, "title": "your area", "lon": lon, "lat": lat}]
    wk = forecast.week(spots, drone)
    tomorrow = (datetime.now(forecast.TZ) + timedelta(days=1)).date().isoformat()
    day = next((d for d in wk["days"] if d["date"] == tomorrow), None)
    if not day or not day["best"]:
        return None
    b = day["best"]
    hm = lambda s: s[11:16]
    names = [s["title"] for s in day["spots"] if s["id"]]
    where = f" at {names[0]}" + (f" and {len(names) - 1} more of your spots" if len(names) > 1 else "") if names else ""
    first = day["spots"][0] if day["spots"] and day["spots"][0]["id"] else None
    url = f"./?region={sub_row['region']}" + (f"#{drone}.{first['id']}" if first else "")
    return {"title": f"Good flying tomorrow {hm(b['start'])}–{hm(b['end'])}",
            "body": f"Gusts up to {b['gust']} mph, {b['rain']}% rain at most{where}.", "url": url}


async def notify_loop():
    while True:
        try:
            now = datetime.now(forecast.TZ)
            if (now.hour, now.minute) >= NOTIFY_AT and now.hour < 22:
                today = now.date().isoformat()
                for row in db.q("SELECT * FROM push_subs WHERE last_sent IS NULL OR last_sent != ?", today):
                    msg = await asyncio.to_thread(tomorrow_message, row)
                    alive = True
                    if msg:
                        alive = await asyncio.to_thread(push.send, json.loads(row["sub"]), msg)
                    if alive:
                        db.write(lambda c: c.execute("UPDATE push_subs SET last_sent = ? WHERE endpoint = ?", (today, row["endpoint"])))
                    else:
                        db.write(lambda c: c.execute("DELETE FROM push_subs WHERE endpoint = ?", (row["endpoint"],)))
        except Exception:
            log.exception("notify loop")
        await asyncio.sleep(600)
