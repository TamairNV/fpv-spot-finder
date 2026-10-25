"""SQLite storage: profiles, synced items (ratings, notes, flights), photos, push subscriptions.

One file under DATA_DIR. Writes go through one lock; reads are cheap enough not to bother.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("SPOTTER_DATA", "/data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY,
  created REAL NOT NULL
);
-- one key per device; linking a device adds a key to the same profile
CREATE TABLE IF NOT EXISTS keys (
  key_hash TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  created REAL NOT NULL,
  seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
  code TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  expires REAL NOT NULL
);
-- kind: rating (k = "<drone>/<spot id>"), note (k = spot id), flight (k = flight id).
-- value is the client's JSON (null = deleted); t is the client's edit time in ms (last write wins);
-- rev is this profile's change counter, so a device asks for "everything since rev N".
CREATE TABLE IF NOT EXISTS items (
  profile_id INTEGER NOT NULL,
  region TEXT NOT NULL,
  kind TEXT NOT NULL,
  k TEXT NOT NULL,
  value TEXT NOT NULL,
  t REAL NOT NULL,
  rev INTEGER NOT NULL,
  PRIMARY KEY (profile_id, region, kind, k)
);
CREATE INDEX IF NOT EXISTS items_rev ON items (profile_id, region, rev);
CREATE TABLE IF NOT EXISTS photos (
  id TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  region TEXT NOT NULL,
  flight_id TEXT NOT NULL,
  w INTEGER, h INTEGER,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS photos_flight ON photos (profile_id, region, flight_id);
CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  sub TEXT NOT NULL,
  region TEXT NOT NULL,
  drone TEXT NOT NULL,
  created REAL NOT NULL,
  last_sent TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  profile_id INTEGER,
  region TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  created REAL NOT NULL,
  finished REAL,
  log TEXT
);
"""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class DB:
    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "spotter.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def write(self, fn):
        """Run fn(conn) in one transaction."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self.conn)
                self.conn.execute("COMMIT")
                return out
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    # ---- profiles

    def _add_key(self, c, pid: int) -> str:
        key = secrets.token_urlsafe(24)
        now = time.time()
        c.execute("INSERT INTO keys (key_hash, profile_id, created, seen) VALUES (?, ?, ?, ?)", (hash_key(key), pid, now, now))
        return key

    def new_profile(self) -> tuple[int, str]:
        def go(c):
            pid = c.execute("INSERT INTO profiles (created) VALUES (?)", (time.time(),)).lastrowid
            return pid, self._add_key(c, pid)
        return self.write(go)

    def profile_for(self, key: str) -> int | None:
        row = self.one("SELECT profile_id, seen FROM keys WHERE key_hash = ?", hash_key(key))
        if not row:
            return None
        if time.time() - row["seen"] > 3600:
            self.write(lambda c: c.execute("UPDATE keys SET seen = ? WHERE key_hash = ?", (time.time(), hash_key(key))))
        return row["profile_id"]

    def devices(self, pid: int) -> int:
        return self.one("SELECT COUNT(*) AS n FROM keys WHERE profile_id = ?", pid)["n"]

    def new_link(self, pid: int) -> str:
        # 8 characters from an alphabet without look-alikes, valid for 10 minutes
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        now = time.time()

        def go(c):
            c.execute("DELETE FROM links WHERE expires < ? OR profile_id = ?", (now, pid))
            c.execute("INSERT INTO links (code, profile_id, expires) VALUES (?, ?, ?)", (code, pid, now + 600))
        self.write(go)
        return code

    def claim_link(self, code: str) -> str | None:
        """Swap a link code for a new device key on the same profile."""
        code = code.strip().upper().replace("-", "").replace(" ", "")

        def go(c):
            row = c.execute("SELECT profile_id FROM links WHERE code = ? AND expires > ?", (code, time.time())).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM links WHERE code = ?", (code,))
            return self._add_key(c, row["profile_id"])
        return self.write(go)

    # ---- synced items

    def merge(self, pid: int, region: str, changes: list[dict]) -> None:
        """Last write wins per item, by the client's edit time."""
        def go(c):
            rev = c.execute("SELECT COALESCE(MAX(rev), 0) FROM items WHERE profile_id = ? AND region = ?",
                            (pid, region)).fetchone()[0]
            for ch in changes:
                old = c.execute("SELECT t FROM items WHERE profile_id = ? AND region = ? AND kind = ? AND k = ?",
                                (pid, region, ch["kind"], ch["k"])).fetchone()
                if old and old["t"] >= ch["t"]:
                    continue
                rev += 1
                c.execute("INSERT OR REPLACE INTO items (profile_id, region, kind, k, value, t, rev) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (pid, region, ch["kind"], ch["k"], json.dumps(ch["value"]), ch["t"], rev))
        self.write(go)

    def since(self, pid: int, region: str, rev: int) -> tuple[list[dict], int]:
        rows = self.q("SELECT kind, k, value, t, rev FROM items WHERE profile_id = ? AND region = ? AND rev > ? ORDER BY rev",
                      pid, region, rev)
        top = self.one("SELECT COALESCE(MAX(rev), 0) AS r FROM items WHERE profile_id = ? AND region = ?", pid, region)["r"]
        return [{"kind": r["kind"], "k": r["k"], "value": json.loads(r["value"]), "t": r["t"]} for r in rows], top

    def items(self, pid: int, region: str, kind: str) -> list[dict]:
        """Live (not deleted) items of one kind, newest edit first."""
        rows = self.q("SELECT k, value, t FROM items WHERE profile_id = ? AND region = ? AND kind = ? AND value != 'null' ORDER BY t DESC",
                      pid, region, kind)
        return [{"k": r["k"], "t": r["t"], **json.loads(r["value"])} for r in rows]

    def counts(self, pid: int) -> dict:
        rows = self.q("SELECT region, kind, COUNT(*) AS n FROM items WHERE profile_id = ? AND value != 'null' GROUP BY region, kind", pid)
        out: dict = {}
        for r in rows:
            out.setdefault(r["region"], {})[r["kind"]] = r["n"]
        return out
