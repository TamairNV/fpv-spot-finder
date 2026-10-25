"""Flying forecast: the week's calm, dry daylight windows at the spots you like.

Open-Meteo (free, no key) for up to 7 days, one request for many points, cached for an hour.
The limits match the app's wind strip (web/src/conditions.js): a judgement call, not a rule.
"""
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Europe/London")
LIMITS = {
    "freestyle5": {"gust": 28, "wind": 20},
    "cine5": {"gust": 28, "wind": 20},
    "cine85": {"gust": 12, "wind": 8},
}
RAIN_OK = 30  # % chance
MIN_HOURS = 2  # a window shorter than this isn't worth the drive
CACHE_S = 3600
MAX_POINTS = 30

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _key(lon: float, lat: float) -> tuple:
    # points within ~1-2 km share a forecast: the model grid is coarser than that anyway
    return (round(lon / 0.02) * 0.02, round(lat / 0.02) * 0.02)


def fetch(points: list[tuple[float, float]]) -> dict[tuple, dict]:
    """Hourly + daily forecast for each point, keyed by the rounded point."""
    keys = list(dict.fromkeys(_key(lon, lat) for lon, lat in points))
    now = time.time()
    with _lock:
        missing = [k for k in keys if k not in _cache or now - _cache[k][0] > CACHE_S]
    for i in range(0, len(missing), 50):
        batch = missing[i:i + 50]
        r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": ",".join(f"{k[1]:.3f}" for k in batch),
            "longitude": ",".join(f"{k[0]:.3f}" for k in batch),
            "hourly": "wind_speed_10m,wind_gusts_10m,precipitation_probability,precipitation",
            "daily": "sunrise,sunset",
            "wind_speed_unit": "mph", "timezone": "Europe/London", "forecast_days": 7,
        })
        r.raise_for_status()
        js = r.json()
        if isinstance(js, dict):
            js = [js]
        with _lock:
            for k, j in zip(batch, js):
                _cache[k] = (now, j)
    with _lock:
        return {k: _cache[k][1] for k in keys}


def hours(fc: dict, drone: str) -> list[dict]:
    """Each daylight hour with a go / meh / no verdict."""
    lim = LIMITS.get(drone, LIMITS["freestyle5"])
    h, d = fc["hourly"], fc["daily"]
    light = {s[:10]: (datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in zip(d["sunrise"], d["sunset"])}
    out = []
    for i, t in enumerate(h["time"]):
        start = datetime.fromisoformat(t)
        day = light.get(t[:10])
        # an hour counts as daylight if most of it is between sunrise and sunset
        if not day or start + timedelta(minutes=30) < day[0] or start + timedelta(minutes=30) > day[1]:
            continue
        gust = h["wind_gusts_10m"][i] or 0
        wind = h["wind_speed_10m"][i] or 0
        rain = h["precipitation_probability"][i] or 0
        cls = ("no" if gust > lim["gust"] * 1.3 or rain > 60
               else "meh" if gust > lim["gust"] or wind > lim["wind"] or rain > RAIN_OK else "go")
        out.append({"t": start, "gust": gust, "wind": wind, "rain": rain, "cls": cls})
    return out


def windows(hrs: list[dict]) -> list[dict]:
    """Runs of consecutive good hours, at least MIN_HOURS long."""
    out, run = [], []
    for h in hrs + [None]:
        if h and h["cls"] == "go" and (not run or h["t"] - run[-1]["t"] == timedelta(hours=1)):
            run.append(h)
            continue
        if len(run) >= MIN_HOURS:
            out.append({
                "start": run[0]["t"], "end": run[-1]["t"] + timedelta(hours=1), "hours": len(run),
                "gust": round(max(x["gust"] for x in run)), "wind": round(max(x["wind"] for x in run)),
                "rain": max(x["rain"] for x in run),
            })
        run = [h] if h and h["cls"] == "go" else []
    return out


def week(spots: list[dict], drone: str, now: datetime | None = None) -> dict:
    """Best window per day across the spots, and the next one coming up.

    spots: [{id, title, lon, lat}]. Returns days (today + 6) with the best window and which
    spots are good in it, the next window, and an hour-by-hour strip per day for the best spot.
    """
    now = now or datetime.now(TZ).replace(tzinfo=None)
    spots = spots[:MAX_POINTS]
    fcs = fetch([(s["lon"], s["lat"]) for s in spots])
    per_spot = []
    for s in spots:
        hrs = hours(fcs[_key(s["lon"], s["lat"])], drone)
        hrs = [h for h in hrs if h["t"] + timedelta(hours=1) > now]
        per_spot.append((s, hrs, windows(hrs)))

    days = []
    for n in range(7):
        date = (now + timedelta(days=n)).date()
        best, good_spots = None, []
        for s, _, wins in per_spot:
            ws = [w for w in wins if w["start"].date() == date]
            if not ws:
                continue
            w = max(ws, key=lambda w: (w["hours"], -w["gust"]))
            good_spots.append({"id": s["id"], "title": s["title"], "drone": s.get("drone"), "hours": w["hours"], "gust": w["gust"]})
            if not best or (w["hours"], -w["gust"]) > (best["hours"], -best["gust"]):
                best = w
        good_spots.sort(key=lambda g: (-g["hours"], g["gust"]))
        # hourly strip from the first spot (they share weather closely within a region)
        strip = [{"h": h["t"].hour, "cls": h["cls"], "gust": round(h["gust"]), "rain": h["rain"]}
                 for h in (per_spot[0][1] if per_spot else []) if h["t"].date() == date]
        days.append({
            "date": date.isoformat(),
            "best": _fmt(best) if best else None,
            "spots": good_spots[:8],
            "n_spots": len(good_spots),
            "strip": strip,
        })
    upcoming = [(w, s) for s, _, wins in per_spot for w in wins]
    upcoming.sort(key=lambda ws: (ws[0]["start"], -ws[0]["hours"]))
    nxt = None
    if upcoming:
        w0 = upcoming[0][0]
        same = [s for w, s in upcoming if w["start"] == w0["start"]]
        nxt = {**_fmt(w0), "spots": [{"id": s["id"], "title": s["title"], "drone": s.get("drone")} for s in same[:5]], "n_spots": len(same)}
    return {"drone": drone, "limits": LIMITS.get(drone, LIMITS["freestyle5"]), "days": days, "next": nxt,
            "spots": len(spots), "updated": now.isoformat(timespec="minutes")}


def _fmt(w: dict) -> dict:
    return {**w, "start": w["start"].isoformat(timespec="minutes"), "end": w["end"].isoformat(timespec="minutes")}
