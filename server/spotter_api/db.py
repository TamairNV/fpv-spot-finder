"""SQLite storage: profiles and accounts, synced items (ratings, notes, flights, saved spots, your own
places, star reviews), photos, push subscriptions.

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
-- an account is a username, email and password on a profile: signing in on a device gives it a new
-- key. It can't sign in (and its reviews don't show) until the email is confirmed.
CREATE TABLE IF NOT EXISTS accounts (
  profile_id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
-- one-use links sent by email (kind: verify, reset); only the hash is kept
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
  code TEXT PRIMARY KEY,
  profile_id INTEGER NOT NULL,
  expires REAL NOT NULL
);
-- kind: rating (k = "<drone>/<spot id>"), note (k = spot id), flight (k = flight id),
-- saved (k = "<drone>/<spot id>"), place (k = place id), review (k = "<drone>/<spot id>": stars + text).
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


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return secrets.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


class Taken(Exception):
    """.args[0] is which: 'username' or 'email'."""


class NotVerified(Exception):
    pass


TOKEN_LIFE = {"verify": 48 * 3600, "reset": 3600}


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

    def drop_key(self, key: str) -> None:
        self.write(lambda c: c.execute("DELETE FROM keys WHERE key_hash = ?", (hash_key(key),)))

    # ---- accounts

    def account(self, pid: int) -> dict | None:
        row = self.one("SELECT username, email, verified FROM accounts WHERE profile_id = ?", pid)
        return dict(row) if row else None

    def signup(self, pid: int | None, username: str, email: str, pw: str) -> tuple[int, str | None]:
        """An unconfirmed account on this device's profile (keeping what's on it), or on a new one.
        Returns the profile and, for a new profile, its first key."""
        pw_hash = hash_pw(pw)  # outside the lock: it's slow on purpose

        def go(c):
            if c.execute("SELECT 1 FROM accounts WHERE username = ?", (username,)).fetchone():
                raise Taken("username")
            if c.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
                raise Taken("email")
            p, key = pid, None
            if p is not None and c.execute("SELECT 1 FROM accounts WHERE profile_id = ?", (p,)).fetchone():
                p = None  # this device already belongs to an account: start a fresh profile
            if p is None:
                p = c.execute("INSERT INTO profiles (created) VALUES (?)", (time.time(),)).lastrowid
                key = self._add_key(c, p)
            c.execute("INSERT INTO accounts (profile_id, username, email, pw, verified, created) VALUES (?, ?, ?, ?, 0, ?)",
                      (p, username, email, pw_hash, time.time()))
            return p, key
        return self.write(go)

    def login(self, who: str, pw: str) -> str | None:
        """who is the username or the email. Raises NotVerified for a right password on an unconfirmed account."""
        row = self.one("SELECT profile_id, pw, verified FROM accounts WHERE username = ? OR email = ?", who, who)
        if not row:
            hash_pw(pw)  # same time either way, so it doesn't say which accounts exist
            return None
        if not check_pw(pw, row["pw"]):
            return None
        if not row["verified"]:
            raise NotVerified()
        return self.write(lambda c: self._add_key(c, row["profile_id"]))

    def by_email(self, email: str) -> dict | None:
        row = self.one("SELECT profile_id, username, email, verified FROM accounts WHERE email = ?", email)
        return dict(row) if row else None

    def new_token(self, pid: int, kind: str) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()

        def go(c):
            c.execute("DELETE FROM tokens WHERE expires < ? OR (profile_id = ? AND kind = ?)", (now, pid, kind))
            c.execute("INSERT INTO tokens (token_hash, profile_id, kind, expires) VALUES (?, ?, ?, ?)",
                      (hash_key(tok), pid, kind, now + TOKEN_LIFE[kind]))
        self.write(go)
        return tok

    def use_token(self, tok: str, kind: str) -> int | None:
        def go(c):
            row = c.execute("SELECT profile_id FROM tokens WHERE token_hash = ? AND kind = ? AND expires > ?",
                            (hash_key(tok), kind, time.time())).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM tokens WHERE token_hash = ?", (hash_key(tok),))
            return row["profile_id"]
        return self.write(go)

    def verify(self, tok: str) -> str | None:
        """Confirms the email; returns a new key for the device that opened the link."""
        pid = self.use_token(tok, "verify")
        if pid is None:
            return None

        def go(c):
            c.execute("UPDATE accounts SET verified = 1 WHERE profile_id = ?", (pid,))
            return self._add_key(c, pid)
        return self.write(go)

    def reset_password(self, tok: str, pw: str) -> str | None:
        """A new password from an emailed link: every device is signed out, this one gets a new key.
        Opening the link proves the email, so it confirms the account too."""
        pw_hash = hash_pw(pw)
        pid = self.use_token(tok, "reset")
        if pid is None:
            return None

        def go(c):
            c.execute("UPDATE accounts SET pw = ?, verified = 1 WHERE profile_id = ?", (pw_hash, pid))
            c.execute("DELETE FROM keys WHERE profile_id = ?", (pid,))
            c.execute("DELETE FROM tokens WHERE profile_id = ? AND kind = 'reset'", (pid,))
            return self._add_key(c, pid)
        return self.write(go)

    def set_password(self, pid: int, old: str, new: str, keep_key: str) -> bool:
        row = self.one("SELECT pw FROM accounts WHERE profile_id = ?", pid)
        if not row or not check_pw(old, row["pw"]):
            return False
        pw_hash = hash_pw(new)

        def go(c):
            c.execute("UPDATE accounts SET pw = ? WHERE profile_id = ?", (pw_hash, pid))
            # every other device has to sign in again with the new one
            c.execute("DELETE FROM keys WHERE profile_id = ? AND key_hash != ?", (pid, hash_key(keep_key)))
        self.write(go)
        return True

    def delete_account(self, pid: int) -> None:
        """The account and everything on its profile (photos' files are the caller's job)."""
        def go(c):
            for t in ("accounts", "keys", "links", "items", "photos", "push_subs", "tokens"):
                c.execute(f"DELETE FROM {t} WHERE profile_id = ?", (pid,))
            c.execute("DELETE FROM profiles WHERE id = ?", (pid,))
        self.write(go)

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

    # ---- reviews other people can see: stars and words from accounts, never notes

    def reviews(self, region: str, k: str) -> list[dict]:
        rows = self.q("""SELECT a.username, i.value, i.t FROM items i JOIN accounts a ON a.profile_id = i.profile_id
                         WHERE a.verified = 1 AND i.region = ? AND i.kind = 'review' AND i.k = ? AND i.value != 'null' ORDER BY i.t DESC""", region, k)
        out = []
        for r in rows:
            v = json.loads(r["value"])
            if isinstance(v.get("stars"), int) and 1 <= v["stars"] <= 5:
                out.append({"user": r["username"], "stars": v["stars"], "text": str(v.get("text") or "")[:2000], "t": r["t"]})
        return out

    def review_summary(self, region: str, drone: str) -> dict:
        """{spot id: [average stars, count]} for one drone's spots."""
        rows = self.q("""SELECT i.k, i.value FROM items i JOIN accounts a ON a.profile_id = i.profile_id
                         WHERE a.verified = 1 AND i.region = ? AND i.kind = 'review' AND i.k LIKE ? AND i.value != 'null'""", region, f"{drone}/%")
        acc: dict[str, list[int]] = {}
        for r in rows:
            stars = json.loads(r["value"]).get("stars")
            if isinstance(stars, int) and 1 <= stars <= 5:
                acc.setdefault(r["k"].split("/", 1)[1], []).append(stars)
        return {k: [round(sum(v) / len(v), 1), len(v)] for k, v in acc.items()}

    def hide_review(self, region: str, k: str, username: str) -> bool:
        """Moderation: blank one account's review of a spot (it syncs back to them as deleted)."""
        row = self.one("SELECT profile_id FROM accounts WHERE username = ?", username)
        if not row:
            return False
        self.merge(row["profile_id"], region, [{"kind": "review", "k": k, "value": None, "t": time.time() * 1000}])
        return True
