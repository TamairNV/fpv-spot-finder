"""Command line: `python -m spotfinder run --region newcastle` (or `--region durham,leeds`, or `--region all`)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .config import load_region, load_regions


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="spotfinder", description="Find FPV spots from height and map data.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in [
        ("prepare", "download and grid the source data"),
        ("features", "compute per-cell features (slow part)"),
        ("context", "recompute only the map-context features, e.g. after editing bando rules (fast)"),
        ("legal", "rebuild only the legal grids, e.g. after a new NATS AIRAC file (a few minutes)"),
        ("score", "score cells, pick spots and write the web data (fast)"),
        ("run", "prepare + features + score"),
        ("lidar", "fetch EA LiDAR for the region straight from the WCS (needs environment.data.gov.uk)"),
        ("regions", "rewrite web/public/data/regions.json, the app's list of regions"),
        ("grid", "tile a whole country into 40 km regions (config/regions_grid.toml)"),
        ("queue", "work through many regions unattended; skips ones already done, so safe to rerun"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--region", default="newcastle", help="region key, comma-separated keys, or 'all'")
        p.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="CPU processes (default: all cores)")
        p.add_argument("--no-lidar", action="store_true", help="ignore LiDAR even if it's there")
        p.add_argument("--spots", type=int, default=250, help="spots per profile")
        p.add_argument("--previews", type=int, default=120, help="3D previews per profile")
        p.add_argument("--strict", action="store_true",
                       help="only list spots each drone may fly under its Open subcategory (default: label them)")
        if name == "grid":
            p.add_argument("--country", default="england", choices=["england", "wales", "scotland"])
            p.add_argument("--index", help="a saved copy of Geofabrik's index-v1.json (default: download it)")
        if name == "queue":
            p.add_argument("--lidar", default="all", choices=["all", "built", "none"],
                           help="LiDAR for every 1 km square, only built-up ones (much faster), or none")
            p.add_argument("--save-to", type=Path, help="copy each finished region's data here and free local disk")
            p.add_argument("--redo", action="store_true", help="redo regions that are already done")
            p.add_argument("--skip", default="", help="comma-separated regions to leave out (e.g. done elsewhere)")
            p.add_argument("--fetch-ahead", type=int, default=2, help="regions downloading at once")
            p.add_argument("--lidar-workers", type=int, default=6, help="parallel LiDAR requests per region")
            p.add_argument("--jobs", type=int, default=1,
                           help="regions processed side by side, each in its own process; the map-data and "
                                "scoring steps are mostly single-threaded, so this is what fills all the cores")
            p.add_argument("--shard", help=argparse.SUPPRESS)  # i/n: set by --jobs for each child
        if name == "lidar":
            p.add_argument("--top", type=int, default=0,
                           help="only fetch the 1 km squares under the best N spots per profile (0 = whole region)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "grid":
        from . import grid

        regions = grid.plan(grid.load_index(args.index), args.country)
        grid.write(regions)
        return
    keys = list(load_regions()) if args.region == "all" else [k.strip() for k in args.region.split(",") if k.strip()]
    regions = [load_region(k) for k in keys]  # fail on a typo before doing any work

    from . import export

    if args.cmd == "regions":
        logging.info("regions.json: %s", [r["key"] for r in export.write_regions_index()])
        return
    if args.cmd == "queue":
        from . import queue

        skip = set(args.skip.split(","))
        regions = [r for r in regions if r.key not in skip]
        if args.jobs > 1 and args.shard is None:
            queue.run_jobs(argv if argv is not None else sys.argv[1:], args.jobs)
            return
        if args.shard is not None:
            i, n = (int(v) for v in args.shard.split("/"))
            regions = regions[i::n]
        queue.run(regions, lidar=args.lidar, save_to=args.save_to, redo=args.redo,
                  workers=args.workers, fetch_ahead=args.fetch_ahead, lidar_workers=args.lidar_workers)
        return
    for region in regions:
        t0 = time.time()
        logging.info("=== %s (%s)", region.key, region.name)
        _run(args, region)
        logging.info("%s done in %.0fs", region.key, time.time() - t0)


def _run(args, region) -> None:
    from . import pipeline

    if args.cmd == "lidar":
        from .sources import lidar_wcs

        lidar_wcs.fetch_region(region, workers=min(args.workers, 6), top=args.top)
    if args.cmd in ("prepare", "run"):
        pipeline.prepare(region)
    if args.cmd in ("features", "run"):
        pipeline.compute_cells(region, use_lidar=not args.no_lidar, workers=args.workers)
    if args.cmd == "context":
        pipeline.refresh_context(region)
    if args.cmd == "legal":
        pipeline.refresh_legal(region)
    if args.cmd in ("score", "run"):
        pipeline.score_and_export(region, n_spots=args.spots, previews=args.previews, strict=args.strict)


if __name__ == "__main__":
    main()
