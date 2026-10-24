"""Command line: `python -m spotfinder run --region newcastle` (or `--region durham,leeds`, or `--region all`)."""

from __future__ import annotations

import argparse
import logging
import time

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
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--region", default="newcastle", help="region key, comma-separated keys, or 'all'")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--no-lidar", action="store_true", help="ignore LiDAR even if it's there")
        p.add_argument("--spots", type=int, default=250, help="spots per profile")
        p.add_argument("--previews", type=int, default=120, help="3D previews per profile")
        p.add_argument("--strict", action="store_true",
                       help="only list spots each drone may fly under its Open subcategory (default: label them)")
        if name == "lidar":
            p.add_argument("--top", type=int, default=0,
                           help="only fetch the 1 km squares under the best N spots per profile (0 = whole region)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    keys = list(load_regions()) if args.region == "all" else [k.strip() for k in args.region.split(",") if k.strip()]
    regions = [load_region(k) for k in keys]  # fail on a typo before doing any work

    from . import export

    if args.cmd == "regions":
        logging.info("regions.json: %s", [r["key"] for r in export.write_regions_index()])
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
