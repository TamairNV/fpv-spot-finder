"""The spot finder's server side: sync between devices, a flight log with photos, the flying
forecast and evening notifications. The app works without it (GitHub Pages); it switches these
on when /api/health answers.

No accounts: a device gets a random key the first time it has something to save, and another
device joins the same profile with a short code from the first one.
"""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import forecast, photos
from .db import DATA_DIR, DB
from .rerank import Busy, Reranker

log = logging.getLogger("spotter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

db = DB()
reranker = Reranker(db, DATA_DIR)
PHOTO_DIR = DATA_DIR / "photos"
REGION_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
KINDS = {"rating", "note", "flight"}
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
    return {"ok": True, "features": ["sync", "flights", "photos", "forecast", "rerank"] + (["push"] if push else [])}


@app.post("/api/profile")
def new_profile(req: Request):
    ip = client_ip(req)
    now = time.time()
    _made[ip] = [t for t in _made[ip] if now - t < 86400]
    if len(_made[ip]) >= 30:
        raise HTTPException(429, "too many new profiles from here today")
    _made[ip].append(now)
    _, key = db.new_profile()
    return {"key": key}


@app.get("/api/me")
def me(pid: int = Depends(profile)):
    return {"devices": db.devices(pid), "counts": db.counts(pid)}


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
    """Spots worth a forecast: the ones you 👍 for this drone, then the ones you've flown."""
    out: dict[str, dict] = {}
    for r in db.items(pid, region, "rating"):
        d, _, sid = r["k"].partition("/")
        ll = r.get("ll")
        if r.get("v", 0) > 0 and d == drone and ll:
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
