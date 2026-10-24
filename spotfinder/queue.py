"""Work through many regions unattended, resumably.

    python -m spotfinder queue --region all --lidar built --save-to /mnt/somewhere

Per region: prepare -> LiDAR -> features -> score, then a done.json marker in
data/out/<region>/ so a rerun skips it. Fetching (network, the bottleneck: LiDAR
is ~0.9 s per request) runs `fetch_ahead` regions at a time alongside the features
(CPU, all `workers`) of the current one. With --save-to, each
finished region's LiDAR rasters and caches are copied there and the local
1 km LiDAR squares deleted, so disk use stays at about one region.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import pipeline
from .config import ROOT, Region
from .sources import lidar_wcs

log = logging.getLogger(__name__)


def done_path(region: Region) -> Path:
    return region.out_dir / "done.json"


def fetch(region: Region, lidar: str, lidar_workers: int = 6) -> None:
    """The network-bound part: map data, then LiDAR (all / built-up squares only / none)."""
    pipeline.prepare(region)
    if lidar != "none":
        lidar_wcs.fetch_region(region, workers=lidar_workers, built=lidar == "built")


def save(region: Region, dest: Path) -> None:
    d = dest / region.key
    d.mkdir(parents=True, exist_ok=True)
    for name in ("lidar_ndsm_1m.tif", "lidar_porosity_1m.tif"):
        if (region.cache_dir / name).exists():
            shutil.copy2(region.cache_dir / name, d / name)
    tmp = d / "pipeline-state.tar.part"
    skip = {"lidar_tiles", "lidar_ndsm_1m.tif", "lidar_porosity_1m.tif", "lidar_dtm_2m.tif"}
    with tarfile.open(tmp, "w") as tar:
        for base in (region.cache_dir, region.out_dir):
            tar.add(base, arcname=str(base.relative_to(ROOT)), filter=lambda ti: None if Path(ti.name).name in skip else ti)
    tmp.rename(d / "pipeline-state.tar")
    shutil.rmtree(lidar_wcs.tiles_dir(region), ignore_errors=True)
    log.info("%s: saved to %s", region.key, d)


def run(regions: list[Region], lidar: str = "all", save_to: Path | None = None, redo: bool = False,
        workers: int = 4, fetch_ahead: int = 1, lidar_workers: int = 6, retry: bool = True) -> None:
    todo = [r for r in regions if redo or not done_path(r).exists()]
    log.info("queue: %d regions to do, %d already done", len(todo), len(regions) - len(todo))
    if not todo:
        return
    failed = []
    with ThreadPoolExecutor(max(fetch_ahead, 1)) as net:
        # Keep `fetch_ahead` regions downloading; submit the next one as each is taken for processing,
        # so downloads never get more than that far ahead (disk).
        fetched = {i: net.submit(fetch, todo[i], lidar, lidar_workers) for i in range(min(fetch_ahead, len(todo)))}
        for i, region in enumerate(todo):
            t0 = time.time()
            fut = fetched.pop(i)
            if i + fetch_ahead < len(todo):
                fetched[i + fetch_ahead] = net.submit(fetch, todo[i + fetch_ahead], lidar, lidar_workers)
            try:
                fut.result()
                pipeline.compute_cells(region, workers=workers)
                pipeline.score_and_export(region)
            except Exception:
                log.exception("%s: failed, skipping it", region.key)
                failed.append(region)
                continue
            done_path(region).write_text(json.dumps({
                "done_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "lidar": lidar, "seconds": round(time.time() - t0)}))
            if save_to:
                save(region, save_to)
            log.info("queue: %s done (%d/%d)", region.key, i + 1, len(todo))
    if failed and retry:
        # Failures so far have all been dropped downloads, so one more go usually clears them.
        log.info("queue: retrying %d failed regions: %s", len(failed), [r.key for r in failed])
        run(failed, lidar, save_to, redo, workers, fetch_ahead, lidar_workers, retry=False)


def run_jobs(argv: list[str], jobs: int) -> None:
    """Split the queue into `jobs` child processes (every jobs-th region each), sharing the cores and
    the LiDAR server between them, and wait for all of them. Each child logs to logs/queue-<i>.log."""
    cores = os.cpu_count() or 4
    argv = _without(argv, "--jobs")
    if not any(a.startswith("--workers") for a in argv):
        argv += ["--workers", str(max(cores // jobs, 2))]
    (ROOT / "logs").mkdir(exist_ok=True)
    procs = []
    for i in range(jobs):
        log_path = ROOT / "logs" / f"queue-{i}.log"
        log.info("queue: job %d/%d logging to %s", i + 1, jobs, log_path)
        procs.append(subprocess.Popen([sys.executable, "-m", "spotfinder", *argv, "--shard", f"{i}/{jobs}"],
                                      cwd=ROOT, stdout=open(log_path, "a"), stderr=subprocess.STDOUT))
    for p in procs:
        p.wait()


def _without(argv: list[str], flag: str) -> list[str]:
    """argv minus `flag` and its value (as `--flag v` or `--flag=v`)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == flag:
            skip = True
        elif not a.startswith(flag + "="):
            out.append(a)
    return out
