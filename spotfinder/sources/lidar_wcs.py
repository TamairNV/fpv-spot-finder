"""EA LiDAR straight from the Environment Agency's Web Coverage Service.

No 100s-of-GB national download: we ask the WCS for exactly the 1 km squares
we want, three layers each (first-return DSM, last-return DSM, DTM), and keep
only what the scorer needs:

- lidar_ndsm_1m.tif      uint8, height above ground = value / 4 m, 255 = no data
- lidar_porosity_1m.tif  uint8, first minus last return = value / 4 m (big over
                         canopy you can see through, ~0 over roofs), 255 = no data
- lidar_dtm_2m.tif       float32 bare-earth height, 2 m, NaN = no data

That is about 1.5 MB per km² on disk instead of 12 MB of raw floats.

Two ways to use it:
- whole region (`--top 0`): every 1 km square, ~1600 for Newcastle, ~15 min;
- spots only (`--top N`): just the squares under the best N spots of each
  profile from a previous run, so a big region costs a few hundred requests.
Squares we have no LiDAR for fall back to the tree-height model.

Downloaded squares are cached in data/cache/<region>/lidar_tiles/, so an
interrupted run picks up where it stopped.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.transform import from_origin
from rasterio.windows import Window
from tqdm import tqdm

from ..config import BNG, Region

log = logging.getLogger(__name__)

BASE = "https://environment.data.gov.uk/spatialdata/{service}/wcs"
COVERAGES = {
    "first": ("lidar-composite-digital-surface-model-first-return-dsm-1m",
              "df4e3ec3-315e-48aa-aaaf-b5ae74d7b2bb__Lidar_Composite_Elevation_FZ_DSM_1m"),
    "last": ("lidar-composite-digital-surface-model-last-return-dsm-1m",
             "9ba4d5ac-d596-445a-9056-dae3ddec0178__Lidar_Composite_Elevation_LZ_DSM_1m"),
    "dtm": ("lidar-composite-digital-terrain-model-dtm-1m",
            "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m"),
}
TILE = 1000
NODATA_U8 = 255


def tile_url(layer: str, x0: int, y0: int) -> str:
    service, cov = COVERAGES[layer]
    return (BASE.format(service=service) + "?service=WCS&version=2.0.1&request=GetCoverage"
            f"&CoverageId={cov}&format=image/tiff&subset=E({x0},{x0 + TILE})&subset=N({y0},{y0 + TILE})")


def _get(session: requests.Session, url: str, tries: int = 4) -> np.ndarray | None:
    """One coverage as a 1000x1000 float32 array (NaN = no data); None if the square is empty.

    The EA server answers HTTP 500 for squares with no data at all (offshore, for one), so a 500
    that persists through the retries counts as empty rather than as a failure."""
    status = None
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 200 and r.content[:2] in (b"II", b"MM"):
                with rasterio.MemoryFile(io.BytesIO(r.content)) as mf, mf.open() as src:
                    a = src.read(1).astype("float32")
                    nd = src.nodata
                if a.shape != (TILE, TILE):
                    a = _fit(a)
                a[(a < -1000) | (a > 5000)] = np.nan
                if nd is not None and not math.isnan(nd):
                    a[a == nd] = np.nan
                return a if np.isfinite(a).any() else None
            status = r.status_code
            if r.status_code in (400, 404):
                return None  # outside coverage
        except (requests.RequestException, rasterio.errors.RasterioIOError) as e:
            status = None
            log.debug("retry %s: %s", url, e)
        time.sleep(2 ** attempt)
    if status == 500:
        log.debug("treating persistent HTTP 500 as an empty square: %s", url)
        return None
    raise RuntimeError(f"WCS kept failing: {url}")


def _fit(a: np.ndarray) -> np.ndarray:
    out = np.full((TILE, TILE), np.nan, dtype="float32")
    h, w = min(a.shape[0], TILE), min(a.shape[1], TILE)
    out[:h, :w] = a[:h, :w]
    return out


def encode(first: np.ndarray | None, last: np.ndarray | None, dtm: np.ndarray | None) -> dict[str, np.ndarray]:
    """Same encodings as the shared Newcastle pack, so either source works."""
    nan = np.full((TILE, TILE), np.nan, dtype="float32")
    first = nan if first is None else first
    last = nan if last is None else last
    dtm = nan if dtm is None else dtm
    nd = first - dtm
    ndsm = np.where(np.isfinite(nd), np.round(np.clip(nd, 0, 63.5) * 4), NODATA_U8).astype("uint8")
    por = first - last
    porosity = np.where(np.isfinite(por), np.round(np.clip(por, 0, 63.5) * 4), NODATA_U8).astype("uint8")
    with np.errstate(invalid="ignore"):
        dtm2 = np.nanmean(dtm.reshape(TILE // 2, 2, TILE // 2, 2), axis=(1, 3)).astype("float32")
    return {"ndsm": ndsm, "porosity": porosity, "dtm2": dtm2}


def tiles_dir(region: Region) -> Path:
    d = region.cache_dir / "lidar_tiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_tile(session: requests.Session, region: Region, x0: int, y0: int) -> str:
    path = tiles_dir(region) / f"{x0}_{y0}.npz"
    if path.exists():
        return "cached"
    layers = {k: _get(session, tile_url(k, x0, y0)) for k in COVERAGES}
    if all(v is None for v in layers.values()):
        np.savez_compressed(path, empty=np.array(1))
        return "empty"
    np.savez_compressed(path, **encode(layers["first"], layers["last"], layers["dtm"]))
    return "ok"


def region_tiles(region: Region) -> list[tuple[int, int]]:
    xmin, ymin, xmax, ymax = region.bbox
    return [(x, y) for y in range(ymin, ymax, TILE) for x in range(xmin, xmax, TILE)]


def spot_tiles(region: Region, top: int, pad_m: float = 250) -> list[tuple[int, int]]:
    """1 km squares under the best `top` spots of each profile (from the last `score` run) and the seeds."""
    from ..export import web_dir

    pts = []
    d = web_dir(region)
    for f in sorted(d.glob("spots_*.geojson")):
        feats = json.loads(f.read_text())["features"][:top]
        pts += [ft["properties"]["preview"]["bng"] if ft["properties"].get("preview") else
                _bng(ft["geometry"]["coordinates"]) for ft in feats]
    meta = d / "meta.json"
    if meta.exists():
        pts += [_bng((s["lon"], s["lat"])) for s in json.loads(meta.read_text()).get("seeds", [])]
    if not pts:
        raise FileNotFoundError("No spots yet: run `spotfinder run` first, or use --top 0 for the whole region")
    xmin, ymin, xmax, ymax = region.bbox
    out = set()
    for x, y in pts:
        for tx in range(int((x - pad_m - xmin) // TILE), int((x + pad_m - xmin) // TILE) + 1):
            for ty in range(int((y - pad_m - ymin) // TILE), int((y + pad_m - ymin) // TILE) + 1):
                x0, y0 = xmin + tx * TILE, ymin + ty * TILE
                if xmin <= x0 < xmax and ymin <= y0 < ymax:
                    out.add((x0, y0))
    return sorted(out, key=lambda t: (-t[1], t[0]))


def built_tiles(region: Region, min_buildings: int = 200) -> list[tuple[int, int]]:
    """1 km squares with some town in them (Overture buildings, from `prepare`), plus their neighbours.

    That's where LiDAR matters most (gaps between buildings, roof heights); open country falls
    back to the tree-height model, which is fine for woods and lone trees."""
    from . import overture

    xmin, ymin, xmax, ymax = region.bbox
    nx, ny = (xmax - xmin) // TILE, (ymax - ymin) // TILE
    pts = overture.load_layer(region, "buildings").geometry.centroid
    ix = ((pts.x.to_numpy() - xmin) // TILE).astype(int)
    iy = ((pts.y.to_numpy() - ymin) // TILE).astype(int)
    ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    counts = np.zeros((ny, nx), int)
    np.add.at(counts, (iy[ok], ix[ok]), 1)
    from scipy import ndimage

    want = ndimage.binary_dilation(counts >= min_buildings, structure=np.ones((3, 3)))
    return [(xmin + i * TILE, ymin + j * TILE) for j, i in zip(*np.nonzero(want))]


def _bng(lonlat) -> tuple[float, float]:
    from ..config import lonlat_to_bng

    return lonlat_to_bng(*lonlat)


def assemble(region: Region) -> dict[str, Path]:
    """Stitch the cached squares into the three region rasters (missing squares stay no-data)."""
    xmin, ymin, xmax, ymax = region.bbox
    w, h = xmax - xmin, ymax - ymin
    out = {
        "ndsm": region.cache_dir / "lidar_ndsm_1m.tif",
        "porosity": region.cache_dir / "lidar_porosity_1m.tif",
        "dtm2": region.cache_dir / "lidar_dtm_2m.tif",
    }
    common = dict(driver="GTiff", count=1, crs=BNG, tiled=True, blockxsize=512, blockysize=512,
                  compress="deflate", sparse_ok=True, bigtiff="if_safer")
    files = sorted(tiles_dir(region).glob("*.npz"), key=lambda p: (-int(p.stem.split("_")[1]), int(p.stem.split("_")[0])))
    os.environ.setdefault("GDAL_CACHEMAX", "1024")
    with rasterio.open(out["ndsm"], "w", width=w, height=h, dtype="uint8", nodata=NODATA_U8,
                       transform=from_origin(xmin, ymax, 1, 1), **common) as dn, \
            rasterio.open(out["porosity"], "w", width=w, height=h, dtype="uint8", nodata=NODATA_U8,
                          transform=from_origin(xmin, ymax, 1, 1), **common) as dp, \
            rasterio.open(out["dtm2"], "w", width=w // 2, height=h // 2, dtype="float32", nodata=float("nan"),
                          transform=from_origin(xmin, ymax, 2, 2), predictor=3, **common) as dd:
        n = 0
        for f in files:
            x0, y0 = (int(v) for v in f.stem.split("_"))
            if not (xmin <= x0 < xmax and ymin <= y0 < ymax):
                continue
            with np.load(f) as z:
                if "empty" in z.files:
                    continue
                col, row = x0 - xmin, ymax - (y0 + TILE)
                dn.write(z["ndsm"], 1, window=Window(col, row, TILE, TILE))
                dp.write(z["porosity"], 1, window=Window(col, row, TILE, TILE))
                dd.write(z["dtm2"], 1, window=Window(col // 2, row // 2, TILE // 2, TILE // 2))
                n += 1
    log.info("LiDAR: stitched %d squares into %s", n, region.cache_dir)
    return out


def fetch_region(region: Region, workers: int = 6, top: int = 0, built: bool = False) -> dict[str, Path]:
    """Fetch LiDAR for the whole region, just under its best spots (top > 0) or just its built-up
    squares (built), then stitch."""
    if built:
        tiles, what = built_tiles(region), "built-up squares"
    elif top > 0:
        tiles, what = spot_tiles(region, top), f"top {top} spots"
    else:
        tiles, what = region_tiles(region), "whole region"
    log.info("LiDAR: %d squares of 1 km to check (%s)", len(tiles), what)
    session = requests.Session()
    session.headers["User-Agent"] = "fpv-spot-finder (personal hobby project)"
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fetch_tile, session, region, x, y) for x, y in tiles]
        for f in tqdm(as_completed(futs), total=len(futs), desc="LiDAR squares"):
            k = f.result()
            counts[k] = counts.get(k, 0) + 1
    log.info("LiDAR squares: %s", counts)
    return assemble(region)
