"""site-stats: a drop-in, admin-only traffic and security stats page for a FastAPI app.

    from sitestats import Stats

    stats = Stats("/data/stats.db")                      # its own SQLite file
    stats.install(app, is_admin=lambda request: ...)     # middleware + /api/stats routes
    stats.event("login_fail", request)                   # anything worth counting or listing
    stats.gauge("Accounts", lambda: db.count_accounts()) # numbers worked out when the page opens

The page itself is static/dashboard.js (no dependencies): `import('/api/stats/dashboard.js')` and call
`renderStats(element, { fetchJson })`, where fetchJson adds whatever auth your app uses.

What it keeps: requests per minute (last 2 hours, in memory), per day (last 90 days) by status class,
top paths per day, unique visitors per day (a salted hash of address + browser that changes daily, so
nobody can be followed across days), latency, and a log of events with the address cut to /24 (/48
for IPv6). No raw addresses are stored.
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

__all__ = ["Stats"]
__version__ = "0.1.0"

STATIC = Path(__file__).parent / "static"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, key));
CREATE TABLE IF NOT EXISTS visitors (day TEXT NOT NULL, h TEXT NOT NULL, PRIMARY KEY (day, h));
CREATE TABLE IF NOT EXISTS events (t REAL NOT NULL, kind TEXT NOT NULL, ip TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, t);
"""

# path segments that look like ids are folded so /api/photos/abc123.jpg counts as /api/photos/:id
_ID = re.compile(r"^(?:\d[\w.-]*|[\w-]{16,}(?:\.\w+)?|[\w-]*\d[\w-]*\.(?:png|jpg|json|geojson))$")


def _day(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%d")


def mask_ip(ip: str | None) -> str:
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return "?"
    net = ipaddress.ip_network(f"{a}/{24 if a.version == 4 else 48}", strict=False)
    return str(net)


def client_ip(request) -> str:
    """Cloudflare's header when behind its tunnel, else the socket. Only trust the header if the app
    can't be reached except through Cloudflare (bind to 127.0.0.1)."""
    h = request.headers
    return h.get("cf-connecting-ip") or (request.client.host if request.client else "")


def route_of(path: str, depth: int = 3) -> str:
    parts = [p for p in path.split("/") if p][:depth]
    return "/" + "/".join(":id" if _ID.match(p) else p for p in parts)


class Stats:
    def __init__(self, path: str | os.PathLike, *, keep_days: int = 90, flush_every: float = 10.0,
                 skip: Callable[[str], bool] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.keep_days = keep_days
        self.skip = skip or (lambda path: path.startswith("/api/stats"))
        self.started = time.time()
        self._pending: Counter = Counter()      # (day, key) -> n, flushed to SQLite
        self._visitors: set = set()             # (day, hash), flushed
        self._minutes: deque = deque(maxlen=120)  # [minute, requests, errors, ms total]
        self._latency: deque = deque(maxlen=2000)
        self._events: list = []
        self._salt_day, self._salt = None, b""
        self._gauges: list[tuple[str, Callable[[], object], str]] = []
        self._event_labels: dict[str, str] = {}
        threading.Thread(target=self._flusher, args=(flush_every,), daemon=True).start()

    # ---- what the host app plugs in

    def gauge(self, label: str, fn: Callable[[], object], group: str = "App") -> None:
        """A number (or short text) worked out each time the page loads, shown as a tile."""
        self._gauges.append((label, fn, group))

    def label(self, kind: str, text: str) -> None:
        """A readable name for an event kind on the page."""
        self._event_labels[kind] = text

    def count(self, key: str, n: int = 1) -> None:
        """A daily counter, charted on the page (e.g. 'signup', 'upload')."""
        with self.lock:
            self._pending[(_day(), key)] += n

    def event(self, kind: str, request=None, detail: str | dict | None = None, *, ip: str | None = None) -> None:
        """Counted per day and listed (newest first) with the masked address."""
        ip = ip if ip is not None else (client_ip(request) if request is not None else "")
        d = json.dumps(detail)[:500] if isinstance(detail, dict) else (str(detail)[:500] if detail else None)
        with self.lock:
            self._pending[(_day(), f"event:{kind}")] += 1
            self._events.append((time.time(), kind, mask_ip(ip) if ip else None, d))

    # ---- recording requests

    def _visitor(self, ip: str, ua: str) -> str:
        day = _day()
        if day != self._salt_day:  # a new random salt each day, never stored: hashes can't be linked across days
            self._salt_day, self._salt = day, secrets.token_bytes(16)
        return hashlib.sha256(self._salt + ip.encode() + b"|" + ua[:200].encode()).hexdigest()[:20]

    def record(self, path: str, status: int, ms: float, ip: str, ua: str) -> None:
        if self.skip(path):
            return
        day = _day()
        minute = int(time.time() // 60)
        cls = f"{status // 100}xx"
        with self.lock:
            p = self._pending
            p[(day, "requests")] += 1
            p[(day, f"status:{cls}")] += 1
            p[(day, f"path:{route_of(path)}")] += 1
            p[(day, "ms_total")] += int(ms)
            if status == 429:
                p[(day, "event:rate_limited")] += 1
            if ip:
                self._visitors.add((day, self._visitor(ip, ua)))
            if not self._minutes or self._minutes[-1][0] != minute:
                self._minutes.append([minute, 0, 0, 0])
            m = self._minutes[-1]
            m[1] += 1
            m[2] += status >= 400
            m[3] += ms
            self._latency.append(ms)

    def middleware(self):
        """Plain ASGI middleware class, for app.add_middleware(...)."""
        stats = self

        class StatsMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    return await self.app(scope, receive, send)
                t0 = time.perf_counter()
                status = {"code": 500}

                async def send2(msg):
                    if msg["type"] == "http.response.start":
                        status["code"] = msg["status"]
                    await send(msg)
                try:
                    await self.app(scope, receive, send2)
                finally:
                    hdr = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
                    ip = hdr.get("cf-connecting-ip") or (scope.get("client") or ("",))[0] or ""
                    stats.record(scope.get("path", ""), status["code"], (time.perf_counter() - t0) * 1000, ip, hdr.get("user-agent", ""))
        return StatsMiddleware

    # ---- storage

    def flush(self) -> None:
        with self.lock:
            pending, self._pending = self._pending, Counter()
            visitors, self._visitors = self._visitors, set()
            events, self._events = self._events, []
        if not (pending or visitors or events):
            return
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany("INSERT INTO daily (day, key, n) VALUES (?, ?, ?) ON CONFLICT (day, key) DO UPDATE SET n = n + excluded.n",
                          [(d, k, n) for (d, k), n in pending.items()])
            c.executemany("INSERT OR IGNORE INTO visitors (day, h) VALUES (?, ?)", list(visitors))
            c.executemany("INSERT INTO events (t, kind, ip, detail) VALUES (?, ?, ?, ?)", events)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _flusher(self, every: float) -> None:
        last_prune = 0.0
        while True:
            time.sleep(every)
            try:
                self.flush()
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    cut = _day(time.time() - self.keep_days * 86400)
                    self.conn.execute("DELETE FROM daily WHERE day < ?", (cut,))
                    self.conn.execute("DELETE FROM visitors WHERE day < ?", (cut,))
                    self.conn.execute("DELETE FROM events WHERE t < ?", (time.time() - self.keep_days * 86400,))
            except Exception:  # pragma: no cover - stats must never take the app down
                pass

    # ---- the page's data

    def snapshot(self, days: int = 30) -> dict:
        self.flush()
        days = max(1, min(days, self.keep_days))
        since = _day(time.time() - (days - 1) * 86400)
        rows = self.conn.execute("SELECT day, key, n FROM daily WHERE day >= ? ORDER BY day", (since,)).fetchall()
        by_day: dict[str, dict] = {}
        for d, k, n in rows:
            by_day.setdefault(d, {})[k] = n
        vis = dict(self.conn.execute("SELECT day, COUNT(*) FROM visitors WHERE day >= ? GROUP BY day", (since,)).fetchall())
        all_days = [_day(time.time() - i * 86400) for i in range(days - 1, -1, -1)]
        series = []
        for d in all_days:
            m = by_day.get(d, {})
            req = m.get("requests", 0)
            series.append({"day": d, "requests": req, "visitors": vis.get(d, 0),
                           "errors": m.get("status:4xx", 0) + m.get("status:5xx", 0), "server_errors": m.get("status:5xx", 0),
                           "ms": round(m.get("ms_total", 0) / req) if req else None,
                           "counts": {k: v for k, v in m.items() if not k.startswith(("path:", "status:", "requests", "ms_total"))}})
        today = by_day.get(_day(), {})
        paths = sorted(((k[5:], n) for k, n in today.items() if k.startswith("path:")), key=lambda x: -x[1])[:15]
        now_min = int(time.time() // 60)
        mins = {m[0]: m for m in self._minutes}
        last_hour = [{"t": (now_min - i) * 60, "requests": mins.get(now_min - i, [0, 0])[1], "errors": mins.get(now_min - i, [0, 0, 0])[2]}
                     for i in range(59, -1, -1)]
        lat = sorted(self._latency)
        pct = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else None
        events = [{"t": t, "kind": k, "label": self._event_labels.get(k, k), "ip": ip, "detail": d}
                  for t, k, ip, d in self.conn.execute("SELECT t, kind, ip, detail FROM events ORDER BY t DESC LIMIT 100")]
        kinds = sorted({k[6:] for m in by_day.values() for k in m if k.startswith("event:")})
        gauges = []
        for label, fn, group in self._gauges:
            try:
                v = fn()
            except Exception as e:  # one broken gauge shouldn't hide the page
                v = f"error: {e.__class__.__name__}"
            gauges.append({"label": label, "value": v, "group": group})
        return {"version": __version__, "now": time.time(), "up_since": self.started, "days": series, "last_hour": last_hour,
                "paths_today": paths, "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99)},
                "events": events, "event_kinds": [{"kind": k, "label": self._event_labels.get(k, k)} for k in kinds],
                "gauges": gauges, "db_bytes": self.path.stat().st_size if self.path.exists() else 0}

    # ---- FastAPI wiring

    def router(self, is_admin: Callable, prefix: str = "/api/stats"):
        """GET {prefix}/data (admins only) and {prefix}/dashboard.js (public: it's just code)."""
        from fastapi import APIRouter, Depends, HTTPException, Request
        from fastapi.responses import FileResponse

        r = APIRouter(prefix=prefix)

        def admin(request: Request):
            if not is_admin(request):
                raise HTTPException(404)  # don't even say it's there
            return True

        @r.get("/data")
        def data(days: int = 30, ok: bool = Depends(admin)):
            return self.snapshot(days)

        @r.get("/dashboard.js")
        def js():
            return FileResponse(STATIC / "dashboard.js", media_type="text/javascript",
                                headers={"Cache-Control": "no-cache"})
        return r

    def install(self, app, is_admin: Callable, prefix: str = "/api/stats") -> None:
        self.skip = lambda path, p=prefix: path.startswith(p)
        app.add_middleware(self.middleware())
        app.include_router(self.router(is_admin, prefix))
