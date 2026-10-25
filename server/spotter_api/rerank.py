"""Re-rank a whole region with your 👍 / 👎: the pipeline's scorer, run on the server.

The server keeps each region's computed features (data/pipeline/out/<region>, from a full
`spotfinder run`). Re-ranking writes your ratings where the scorer looks for them and runs
`spotfinder score` into your own copy of the region's web data (data/mine/<id>/<region>), which
nginx serves at /mine/<id>/<region>/. The app loads that instead of the standard ranking until
you switch back. One job at a time; a re-rank takes a minute or two.
"""
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("spotter.rerank")

REPO = Path(os.environ.get("SPOTFINDER_REPO", "/app/repo"))
TIMEOUT_S = 30 * 60


class Busy(Exception):
    pass


def _link(src, dst):
    try:
        os.link(src, dst)
    except OSError:  # another disk or mount: copy instead
        shutil.copy2(src, dst)


class Reranker:
    def __init__(self, db, data_dir: Path):
        self.db, self.data = db, data_dir
        self.pipeline = Path(os.environ.get("SPOTFINDER_DATA") or data_dir / "pipeline")
        self.mine = data_dir / "mine"
        self.jobs = data_dir / "jobs"
        self.lock = threading.Lock()  # one scorer at a time: it's memory-hungry
        db.conn.execute("CREATE TABLE IF NOT EXISTS mine (profile_id INTEGER PRIMARY KEY, data_id TEXT UNIQUE NOT NULL)")
        # a job that was running when the server restarted won't finish
        db.write(lambda c: c.execute("UPDATE jobs SET state = 'failed', log = 'interrupted by a restart' WHERE state IN ('queued', 'running')"))

    def ready(self, region: str) -> bool:
        out = self.pipeline / "out" / region
        return (out / "cells.npz").exists() and (out / "cells_meta.json").exists()

    def data_id(self, pid: int) -> str:
        row = self.db.one("SELECT data_id FROM mine WHERE profile_id = ?", pid)
        if row:
            return row["data_id"]
        did = secrets.token_urlsafe(12)
        self.db.write(lambda c: c.execute("INSERT INTO mine (profile_id, data_id) VALUES (?, ?)", (pid, did)))
        return did

    def status(self, pid: int, region: str) -> dict:
        job = self.db.one("SELECT id, state, created, finished, log FROM jobs WHERE profile_id = ? AND region = ? AND kind = 'rerank' "
                          "ORDER BY created DESC LIMIT 1", pid, region)
        did = self.db.one("SELECT data_id FROM mine WHERE profile_id = ?", pid)
        out = {"ready": self.ready(region), "job": None, "copy": None}
        if job:
            out["job"] = {"state": job["state"], "created": job["created"], "finished": job["finished"],
                          "error": (job["log"] or "")[-600:] if job["state"] == "failed" else None}
        if did and (self.mine / did["data_id"] / region / "meta.json").exists():
            meta = json.loads((self.mine / did["data_id"] / region / "meta.json").read_text())
            out["copy"] = {"path": f"mine/{did['data_id']}/{region}", "made": meta.get("generated_at"), "ratings": meta.get("ratings")}
        return out

    def start(self, pid: int, region: str, ratings: dict) -> dict:
        busy = self.db.one("SELECT id FROM jobs WHERE profile_id = ? AND region = ? AND state IN ('queued', 'running')", pid, region)
        if busy:
            return self.status(pid, region)
        if self.db.one("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued', 'running')")["n"] >= 4:
            raise Busy("the server's busy re-ranking for others: try again in a few minutes")
        jid = secrets.token_hex(8)
        now = time.time()
        self.db.write(lambda c: c.execute("INSERT INTO jobs (id, profile_id, region, kind, state, created) VALUES (?, ?, ?, 'rerank', 'queued', ?)",
                                          (jid, pid, region, now)))
        threading.Thread(target=self._run, args=(jid, pid, region, ratings), daemon=True).start()
        return self.status(pid, region)

    def drop(self, pid: int, region: str) -> None:
        row = self.db.one("SELECT data_id FROM mine WHERE profile_id = ?", pid)
        if row:
            shutil.rmtree(self.mine / row["data_id"] / region, ignore_errors=True)

    def _run(self, jid: str, pid: int, region: str, ratings: dict) -> None:
        with self.lock:
            self._set(jid, "running")
            work = self.jobs / jid
            did = self.data_id(pid)
            dest = self.mine / did
            tmp = self.mine / f".{did}-{jid}"
            try:
                cfg = work / "config"
                (cfg / "ratings").mkdir(parents=True)
                for f in ("regions.toml", "seeds.toml"):
                    shutil.copy(REPO / "config" / f, cfg / f)
                (cfg / "ratings" / f"{region}.json").write_text(json.dumps({"kind": "fpv-spot-finder-ratings", "version": 1, "ratings": ratings}))
                # the basemap tiles, place names and legal lookup come from the full run, not the scorer:
                # link them in (same disk; the scorer never rewrites them)
                base = self.pipeline / "web" / region
                (tmp / region).mkdir(parents=True)
                for name in ("tiles", "places.json", "legal_lookup.png", "zones.geojson"):
                    src = base / name
                    if src.is_dir():
                        shutil.copytree(src, tmp / region / name, copy_function=_link)
                    elif src.exists():
                        _link(src, tmp / region / name)
                env = {**os.environ, "SPOTFINDER_CONFIG": str(cfg), "SPOTFINDER_DATA": str(self.pipeline), "SPOTFINDER_WEB_DATA": str(tmp)}
                p = subprocess.run(["python", "-m", "spotfinder", "score", "--region", region], cwd=REPO, env=env,
                                   capture_output=True, text=True, timeout=TIMEOUT_S)
                logtext = (p.stdout + p.stderr)[-20000:]
                if p.returncode != 0 or not (tmp / region / "meta.json").exists():
                    raise RuntimeError(logtext or f"exit {p.returncode}")
                dest.mkdir(parents=True, exist_ok=True)
                old = dest / f".old-{jid}"
                if (dest / region).exists():
                    (dest / region).rename(old)
                (tmp / region).rename(dest / region)
                shutil.rmtree(old, ignore_errors=True)
                self._set(jid, "done", logtext[-4000:])
            except Exception as e:
                log.warning("rerank %s failed: %s", jid, str(e)[-500:])
                self._set(jid, "failed", str(e)[-4000:])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.rmtree(work, ignore_errors=True)

    def _set(self, jid: str, state: str, logtext: str | None = None) -> None:
        done = time.time() if state in ("done", "failed") else None
        self.db.write(lambda c: c.execute("UPDATE jobs SET state = ?, finished = ?, log = COALESCE(?, log) WHERE id = ?",
                                          (state, done, logtext, jid)))
