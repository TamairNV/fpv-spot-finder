"""Work through many regions unattended, resumably.

    python -m spotfinder queue --region all --lidar built --save-to /mnt/somewhere

Per region: prepare -> LiDAR -> features -> score, then a done.json marker in
data/out/<region>/ so a rerun skips it. Fetching (network) for the next region
runs alongside the features (CPU) of the current one. With --save-to, each
finished region's LiDAR rasters and caches are copied there and the local
1 km LiDAR squares deleted, so disk use stays at about one region.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
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


def fetch(region: Region, lidar: str) -> None:
    """The network-bound part: map data, then LiDAR (all / built-up squares only / none)."""
    pipeline.prepare(region)
    if lidar != "none":
        lidar_wcs.fetch_region(region, built=lidar == "built")


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
        workers: int = 4) -> None:
    todo = [r for r in regions if redo or not done_path(r).exists()]
    log.info("queue: %d regions to do, %d already done", len(todo), len(regions) - len(todo))
    if not todo:
        return
    with ThreadPoolExecutor(1) as net:
        ahead = net.submit(fetch, todo[0], lidar)
        for i, region in enumerate(todo):
            t0 = time.time()
            try:
                ahead.result()
            except Exception:
                log.exception("%s: fetching failed, skipping it", region.key)
                ahead = net.submit(fetch, todo[i + 1], lidar) if i + 1 < len(todo) else None
                continue
            ahead = net.submit(fetch, todo[i + 1], lidar) if i + 1 < len(todo) else None
            try:
                pipeline.compute_cells(region, workers=workers)
                pipeline.score_and_export(region)
            except Exception:
                log.exception("%s: processing failed, skipping it", region.key)
                continue
            done_path(region).write_text(json.dumps({
                "done_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "lidar": lidar, "seconds": round(time.time() - t0)}))
            if save_to:
                save(region, save_to)
            log.info("queue: %s done (%d/%d)", region.key, i + 1, len(todo))
