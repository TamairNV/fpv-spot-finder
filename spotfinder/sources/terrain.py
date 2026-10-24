"""Bare-earth terrain from the public AWS Terrain Tiles.

In England those tiles are built from the Environment Agency's 2 m LiDAR DTM, so
we get LiDAR-grade ground shape (cliffs, quarry faces, valleys) from a few
hundred small GeoTIFFs instead of the national download.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mercantile
import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import from_bounds

from ..config import BNG, DATA_DIR, Region

log = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/geotiff/{z}/{x}/{y}.tif"
CACHE = DATA_DIR / "cache" / "terrain_tiles"


def _fetch(tile: mercantile.Tile) -> Path:
    path = CACHE / str(tile.z) / str(tile.x) / f"{tile.y}.tif"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(TILE_URL.format(z=tile.z, x=tile.x, y=tile.y), timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def build_dtm(region: Region, res: float = 5.0, zoom: int = 13, refresh: bool = False) -> Path:
    """Mosaic terrain onto a BNG grid at `res` metres (float32 metres above sea level)."""
    out = region.cache_dir / f"dtm_{int(res)}m.tif"
    if out.exists() and not refresh:
        return out
    xmin, ymin, xmax, ymax = region.bbox
    width, height = int((xmax - xmin) / res), int((ymax - ymin) / res)
    transform = from_origin(xmin, ymax, res, res)
    dtm = np.full((height, width), np.nan, dtype="float32")

    west, south, east, north = region.lonlat_bbox(pad_m=200)
    tiles = list(mercantile.tiles(west, south, east, north, zooms=zoom))
    log.info("terrain: %d tiles at z%d", len(tiles), zoom)
    with ThreadPoolExecutor(max_workers=16) as pool:
        paths = list(pool.map(_fetch, tiles))

    for path in paths:
        with rasterio.open(path) as src:
            arr = src.read(1).astype("float32")
            bounds = transform_bounds(src.crs, BNG, *src.bounds)
            win = from_bounds(*bounds, transform=transform).round_offsets().round_lengths()
            r0, c0 = max(int(win.row_off) - 2, 0), max(int(win.col_off) - 2, 0)
            r1 = min(int(win.row_off + win.height) + 2, height)
            c1 = min(int(win.col_off + win.width) + 2, width)
            if r1 <= r0 or c1 <= c0:
                continue
            sub = np.full((r1 - r0, c1 - c0), np.nan, dtype="float32")
            reproject(
                source=arr, destination=sub,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=rasterio.transform.from_origin(xmin + c0 * res, ymax - r0 * res, res, res),
                dst_crs=BNG, resampling=Resampling.bilinear, src_nodata=None, dst_nodata=np.nan,
            )
            view = dtm[r0:r1, c0:c1]
            fill = np.isnan(view) & ~np.isnan(sub)
            view[fill] = sub[fill]

    missing = np.isnan(dtm).mean()
    if missing:
        log.warning("terrain: %.2f%% of cells missing, filling with nearest", 100 * missing)
        from scipy import ndimage

        idx = ndimage.distance_transform_edt(np.isnan(dtm), return_distances=False, return_indices=True)
        dtm = dtm[tuple(idx)]

    profile = dict(driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=BNG,
                   transform=transform, tiled=True, blockxsize=256, blockysize=256, compress="deflate",
                   predictor=3)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(dtm, 1)
    log.info("terrain mosaic written to %s", out)
    return out
