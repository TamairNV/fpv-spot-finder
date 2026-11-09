"""3D buildings for the map: every footprint with its height from the EA LiDAR.

Each Overture footprint gets the 90th percentile of the LiDAR nDSM inside it (shrunk a little so the
ground at the walls doesn't pull it down, and with see-through tree crowns left out). Where there's no
LiDAR, or too little of the roof was seen, it falls back to the same estimate the scorer uses (mapped
height, floors, class, size). The app extrudes them from zoom 14.5 up, as static vector tiles in
web/public/data/<region>/buildings/14/x/y.pbf, one "b" layer with h (metres) and e=1 on estimates.

    python -m spotfinder.buildings3d newcastle
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import geopandas as gpd
import mapbox_vector_tile
import mercantile
import numpy as np
import rasterio
import shapely
from rasterio.features import rasterize
from rasterio.windows import from_bounds

from .config import Region
from .export import web_dir
from .heights import lidar_paths
from .rasterize import building_heights
from .sources import overture

log = logging.getLogger(__name__)

ZOOM = 14          # one zoom of tiles; MapLibre overzooms them to street level
EXTENT = 4096      # ~0.6 m per tile unit at z14
BUFFER = 16
CHUNK_M = 2000     # LiDAR is read in 2 km squares
PAD_M = 100        # plus this much, so a building whose centre is in the square fits whole
SHRINK_M = 0.75    # keep off the wall line, where half a pixel is ground
QUANTILE = 90      # the main roof, not a chimney or aerial
MIN_SEEN = 0.4     # share of the footprint the LiDAR must see (not nodata, not tree) to trust it
MIN_H, MAX_H = 2.0, 200.0
INFO = "buildings3d.json"


def lidar_heights(region: Region, b: gpd.GeoDataFrame) -> np.ndarray:
    """LiDAR height per footprint, NaN where it can't be trusted (or there's no LiDAR)."""
    out = np.full(len(b), np.nan, dtype="float32")
    lp = lidar_paths(region)
    if not lp:
        return out
    shrunk = b.geometry.buffer(-SHRINK_M)
    geoms = np.where(shrunk.is_empty | (shrunk.area < 4), b.geometry.values, shrunk.values)
    cx, cy = b.geometry.centroid.x.to_numpy(), b.geometry.centroid.y.to_numpy()
    xmin, ymin, xmax, ymax = region.bbox
    with rasterio.open(lp["ndsm"]) as nd:
        por = rasterio.open(lp["porosity"]) if "porosity" in lp else None
        for x0 in range(int(xmin), int(xmax), CHUNK_M):
            for y0 in range(int(ymin), int(ymax), CHUNK_M):
                idx = np.flatnonzero((cx >= x0) & (cx < x0 + CHUNK_M) & (cy >= y0) & (cy < y0 + CHUNK_M))
                if not len(idx):
                    continue
                win = from_bounds(x0 - PAD_M, y0 - PAD_M, x0 + CHUNK_M + PAD_M, y0 + CHUNK_M + PAD_M,
                                  nd.transform).round_offsets().round_lengths()
                h = nd.read(1, window=win, boundless=True, fill_value=255)
                tr = nd.window_transform(win)
                lab = rasterize(((geoms[i], k + 1) for k, i in enumerate(idx)), out_shape=h.shape,
                                transform=tr, fill=0, dtype="int32")
                hm = h.astype("float32") / 4.0
                seen = h != 255
                if por is not None:
                    p = por.read(1, window=win, boundless=True, fill_value=255)
                    seen &= ~((p != 255) & (p / 4.0 > 0.5 * hm) & (hm > 3))  # see-through: a tree
                n_all = np.bincount(lab.ravel(), minlength=len(idx) + 1)
                keep = (lab > 0) & seen
                lv, hv = lab[keep], hm[keep]
                n_seen = np.bincount(lv, minlength=len(idx) + 1)
                if not len(lv):
                    continue
                order = np.lexsort((hv, lv))
                lv, hv = lv[order], hv[order]
                start = np.searchsorted(lv, np.arange(len(idx) + 1))
                for k in np.flatnonzero((n_seen >= np.maximum(3, MIN_SEEN * n_all)) & (np.arange(len(n_all)) > 0)):
                    n = n_seen[k]
                    out[idx[k - 1]] = hv[start[k] + min(n - 1, round((n - 1) * QUANTILE / 100))]
        if por is not None:
            por.close()
    return out


def heights(region: Region) -> gpd.GeoDataFrame:
    b = overture.load_layer(region, "buildings")
    b = b[b.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    xmin, ymin, xmax, ymax = region.bbox
    b = b[b.geometry.intersects(shapely.box(xmin, ymin, xmax, ymax))].reset_index(drop=True)
    lid = lidar_heights(region, b)
    est = building_heights(b)
    mapped = b["height"].to_numpy(dtype="float64") if "height" in b else np.full(len(b), np.nan)
    # LiDAR we don't believe: ~0 m (built after the survey), or a house reading as a tower (the ground
    # model under it is off, e.g. a former tip). Those fall back to the estimate.
    odd = (lid < 1.5) | ((lid > np.maximum(3 * est, est + 25)) & ~(mapped > 0.5 * lid))
    lid = np.where(odd, np.nan, lid)
    # the nDSM tops out at 63.5 m: keep a taller mapped height
    lid = np.where((lid >= 63) & (mapped > lid), mapped, lid)
    b["e"] = np.isnan(lid).astype("uint8")
    b["h"] = np.clip(np.where(np.isnan(lid), est, lid), MIN_H, MAX_H)
    b["h"] = np.round(b["h"] * 2) / 2
    return b[["h", "e", "geometry"]]


def write_tiles(b: gpd.GeoDataFrame, out: Path) -> dict:
    shutil.rmtree(out, ignore_errors=True)
    merc = b.to_crs("EPSG:3857")
    merc = merc.set_geometry(merc.geometry.simplify(0.5, preserve_topology=True))
    merc = merc[~merc.geometry.is_empty]
    lon0, lat0, lon1, lat1 = b.to_crs("EPSG:4326").total_bounds
    tree = shapely.STRtree(merc.geometry.values)
    count = size = 0
    for t in mercantile.tiles(lon0, lat0, lon1, lat1, ZOOM):
        tb = mercantile.xy_bounds(t)
        pad = (tb.right - tb.left) * BUFFER / EXTENT
        box = (tb.left - pad, tb.bottom - pad, tb.right + pad, tb.top + pad)
        idx = tree.query(shapely.box(*box))
        if not len(idx):
            continue
        parts = merc.iloc[np.sort(idx)].copy()
        parts["geometry"] = shapely.clip_by_rect(parts.geometry.values, *box)
        parts = parts[~parts.geometry.is_empty & parts.geom_type.isin(["Polygon", "MultiPolygon"])]
        feats = []
        # one multipolygon per height: far fewer features, same picture
        for (h, e), sub in parts.groupby(["h", "e"], sort=False):
            polys = [p for g in sub.geometry.values for p in shapely.get_parts(g)]
            props = {"h": float(h)} if not e else {"h": float(h), "e": 1}
            feats.append({"geometry": shapely.MultiPolygon(polys) if len(polys) > 1 else polys[0], "properties": props})
        if not feats:
            continue
        data = mapbox_vector_tile.encode([{"name": "b", "features": feats}],
                                         default_options={"quantize_bounds": (tb.left, tb.bottom, tb.right, tb.top),
                                                          "extents": EXTENT})
        path = out / str(ZOOM) / str(t.x) / f"{t.y}.pbf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        count += 1
        size += len(data)
    return {"tiles": count, "bytes": size, "zoom": ZOOM,
            "bounds": [round(float(v), 5) for v in (lon0, lat0, lon1, lat1)]}


def write(region: Region) -> dict:
    out = web_dir(region)
    b = heights(region)
    info = write_tiles(b, out / "buildings")
    info.update({"count": len(b), "lidar_share": round(float(1 - b["e"].mean()), 3) if len(b) else 0})
    (region.out_dir / INFO).parent.mkdir(parents=True, exist_ok=True)
    (region.out_dir / INFO).write_text(json.dumps(info))
    meta_path = out / "meta.json"
    if meta_path.exists():  # so the app picks it up without a full re-export
        meta = json.loads(meta_path.read_text())
        meta.setdefault("sources", {})["buildings3d"] = info
        meta_path.write_text(json.dumps(meta, indent=2))
    log.info("3D buildings: %d footprints (%.0f%% from LiDAR), %d tiles, %.1f MB",
             info["count"], 100 * info["lidar_share"], info["tiles"], info["bytes"] / 1e6)
    return info


def info(region: Region, tiles_dir: Path | None = None) -> dict | None:
    """What write() recorded about a region's 3D buildings. When that record is gone (data/out moved
    or rebuilt) but the tiles are still in the web folder, a short one made from the tiles, so a
    re-export doesn't drop the app's 3D toggle while the buildings are still there."""
    p = region.out_dir / INFO
    if p.exists():
        return json.loads(p.read_text())
    if tiles_dir is None or not tiles_dir.is_dir():
        return None
    files = list(tiles_dir.rglob("*.pbf"))
    if not files:
        return None
    return {"tiles": len(files), "bytes": sum(f.stat().st_size for f in files), "zoom": ZOOM}


if __name__ == "__main__":
    import sys

    from .config import load_region

    logging.basicConfig(level=logging.INFO)
    print(write(load_region(sys.argv[1] if len(sys.argv) > 1 else "newcastle")))
