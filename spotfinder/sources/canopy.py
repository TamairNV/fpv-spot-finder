"""1 m tree canopy heights from Meta / WRI's global canopy height map.

Fallback for when EA LiDAR isn't reachable. Heights are model predictions from
satellite imagery (Tolan et al. 2024), so treat them as "roughly how tall the
trees are", good to a few metres. The data is public on S3:
s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely.geometry import box
from tqdm import tqdm

from ..config import BNG, DATA_DIR, Region

log = logging.getLogger(__name__)

BASE_URL = "https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float"
TILE_INDEX_CACHE = DATA_DIR / "cache" / "meta_chm" / "tiles.geojson"


def _download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(tmp, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as bar:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                bar.update(len(chunk))
    tmp.rename(dest)
    return dest


def source_tiles(region: Region) -> list[Path]:
    index = gpd.read_file(_download(f"{BASE_URL}/tiles.geojson", TILE_INDEX_CACHE))
    hits = index[index.intersects(box(*region.lonlat_bbox(pad_m=500)))]
    return [
        _download(f"{BASE_URL}/chm/{q}.tif", DATA_DIR / "cache" / "meta_chm" / f"{q}.tif")
        for q in hits["tile"]
    ]


def build_chm(region: Region, tile_m: int = 2000, refresh: bool = False) -> Path:
    """Mosaic the canopy height for the region onto a 1 m BNG grid (uint8 metres)."""
    out = region.cache_dir / "chm_1m.tif"
    if out.exists() and not refresh:
        return out
    xmin, ymin, xmax, ymax = region.bbox
    width, height = xmax - xmin, ymax - ymin
    transform = from_origin(xmin, ymax, 1.0, 1.0)
    profile = dict(
        driver="GTiff", width=width, height=height, count=1, dtype="uint8", crs=BNG, transform=transform,
        tiled=True, blockxsize=512, blockysize=512, compress="deflate", predictor=2, nodata=None,
        BIGTIFF="IF_SAFER",
    )
    sources = source_tiles(region)
    tmp = out.with_suffix(".part.tif")
    # GDAL block cache keeps the full-width source strips around while we sweep a row of tiles.
    with rasterio.Env(GDAL_CACHEMAX=3072), rasterio.open(tmp, "w", **profile) as dst:
        vrts = []
        for path in sources:
            src = rasterio.open(path)
            vrts.append(
                # No nodata: 0 means open ground here, and must take part in resampling.
                WarpedVRT(src, crs=BNG, transform=transform, width=width, height=height,
                          resampling=Resampling.bilinear)
            )
        rows = range(0, height, tile_m)
        for row in tqdm(rows, desc="canopy rows"):
            for col in range(0, width, tile_m):
                win = Window(col, row, min(tile_m, width - col), min(tile_m, height - row))
                acc = np.zeros((int(win.height), int(win.width)), dtype="uint8")
                for vrt in vrts:
                    a = vrt.read(1, window=win)
                    np.maximum(acc, a, out=acc)
                dst.write(acc, 1, window=win)
        for vrt in vrts:
            vrt.src_dataset.close()
            vrt.close()
    tmp.rename(out)
    log.info("canopy mosaic written to %s", out)
    return out
