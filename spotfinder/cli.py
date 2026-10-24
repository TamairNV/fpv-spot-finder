"""Command line: `python -m spotfinder run --region newcastle`."""

from __future__ import annotations

import argparse
import logging
import time

from .config import load_region


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
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--region", default="newcastle")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--no-lidar", action="store_true", help="ignore LiDAR even if it's there")
        p.add_argument("--spots", type=int, default=250, help="spots per profile")
        p.add_argument("--previews", type=int, default=120, help="3D previews per profile")
        if name == "lidar":
            p.add_argument("--top", type=int, default=0,
                           help="only fetch the 1 km squares under the best N spots per profile (0 = whole region)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    region = load_region(args.region)

    from . import pipeline

    t0 = time.time()
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
        pipeline.score_and_export(region, n_spots=args.spots, previews=args.previews)
    logging.info("done in %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()
