"""Burn Overture vectors onto the region's 1 m grid (buildings, water, roads, paths)."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import box
from tqdm import tqdm

from .config import BNG, Region
from .sources import overture

log = logging.getLogger(__name__)

# Rough heights (m) for buildings without a height or floor count.
CLASS_HEIGHT = {
    "house": 8, "detached": 8, "semidetached_house": 8, "terrace": 9, "residential": 8, "bungalow": 5,
    "apartments": 14, "dormitory": 14, "hotel": 15,
    "garage": 3, "garages": 3, "shed": 3, "hut": 3, "carport": 3, "roof": 4, "static_caravan": 3,
    "kiosk": 3, "toilets": 3, "shelter": 3, "greenhouse": 3, "service": 3, "transformer_tower": 6,
    "commercial": 10, "retail": 8, "office": 14, "supermarket": 8,
    "industrial": 10, "warehouse": 10, "factory": 12, "manufacture": 12,
    "barn": 7, "farm": 8, "farm_auxiliary": 6, "stable": 5, "cowshed": 6,
    "school": 10, "university": 18, "college": 14, "hospital": 18,
    "church": 15, "cathedral": 30, "chapel": 8, "mosque": 12,
    "stadium": 25, "grandstand": 12, "sports_centre": 10, "storage_tank": 8, "silo": 18,
}

ROAD_WIDTH = {  # metres of carriageway we treat as "road" (not flyable take-off ground)
    "motorway": 16, "trunk": 12, "primary": 10, "secondary": 8, "tertiary": 7, "unclassified": 6,
    "residential": 6, "living_street": 5, "service": 4, "unknown": 4,
}
PATH_CLASSES = {"footway", "path", "cycleway", "bridleway", "steps", "pedestrian", "track"}

ROAD_CODE = 1
PATH_CODE = 2
RAIL_CODE = 3
# land_1m.tif: ground a suggested line keeps off, and viaducts whose arches you can fly through
HOMES_CODE = 1    # houses and their gardens (residential land that isn't a park or a big green)
PARKING_CODE = 2  # car parks: cars and people getting in and out
VIADUCT_CODE = 3  # the deck of a railway or footpath viaduct or aqueduct (not a road one)
GREEN_LEISURE = {"park", "common", "recreation_ground", "nature_reserve", "village_green", "golf_course", "pitch"}
GREEN_LANDUSE = {"recreation_ground", "village_green", "forest", "meadow", "grass"}
GREEN_NATURAL = {"wood", "scrub", "grassland", "heath", "wetland"}
GREEN_MIN_M2 = 5000  # a verge or a patch of grass between houses is still the estate
ROAD_HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
                 "primary_link", "secondary_link", "unclassified", "residential", "service"}


def building_heights(b: gpd.GeoDataFrame) -> np.ndarray:
    h = pd.to_numeric(b["height"], errors="coerce")
    floors = pd.to_numeric(b["num_floors"], errors="coerce")
    from_floors = floors * 3.0 + 1.5
    by_class = b["class"].map(CLASS_HEIGHT)
    area = b.geometry.area
    default = np.where(area < 30, 3.0, np.where(area > 3000, 11.0, 7.0))
    # Overture's ML heights for UK houses sit around eaves height; add a little for the roof.
    est = h.where(h.notna(), from_floors).where(lambda s: s.notna(), by_class).fillna(pd.Series(default, index=b.index))
    return np.clip(est.to_numpy(dtype="float64"), 2.0, 200.0)


def _profile(region: Region, dtype: str, res: float = 1.0, nodata=None) -> dict:
    xmin, ymin, xmax, ymax = region.bbox
    return dict(
        driver="GTiff", width=int((xmax - xmin) / res), height=int((ymax - ymin) / res), count=1,
        dtype=dtype, crs=BNG, transform=from_origin(xmin, ymax, res, res), tiled=True,
        blockxsize=512, blockysize=512, compress="deflate", nodata=nodata, BIGTIFF="IF_SAFER",
    )


def build_vector_rasters(region: Region, tile_m: int = 2000, refresh: bool = False) -> dict[str, Path]:
    """bld_dm_1m.tif (uint16 building height in decimetres), water_1m.tif, ways_1m.tif (road/path/rail codes)."""
    out = {
        "bld": region.cache_dir / "bld_dm_1m.tif",
        "water": region.cache_dir / "water_1m.tif",
        "ways": region.cache_dir / "ways_1m.tif",
    }
    if all(p.exists() for p in out.values()) and not refresh:
        return out

    b = overture.load_layer(region, "buildings")
    b = b[b.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    b["h_dm"] = np.round(building_heights(b) * 10).astype("uint16")
    b = b.sort_values("h_dm")  # taller buildings burn last, so they win overlaps

    w = overture.load_layer(region, "water")
    w = w[~w["class"].isin(["swimming_pool", "fish_pass", "spring", "cape"])].copy()
    lines = w.geom_type.isin(["LineString", "MultiLineString"])
    widths = np.where(w["class"].isin(["river", "canal"]), 6.0, np.where(w["class"] == "stream", 2.0, 1.5))
    w.loc[lines, "geometry"] = w.loc[lines].geometry.buffer(1).buffer(widths[lines.to_numpy()] / 2 - 1)
    w = w[w.geom_type.isin(["Polygon", "MultiPolygon"])]

    s = overture.load_layer(region, "segments")
    roads = s[(s["subtype"] == "road") & s["class"].isin(ROAD_WIDTH.keys())].copy()
    roads["geometry"] = roads.geometry.buffer(roads["class"].map(ROAD_WIDTH).to_numpy() / 2, cap_style="flat")
    paths = s[(s["subtype"] == "road") & s["class"].isin(PATH_CLASSES)].copy()
    paths["geometry"] = paths.geometry.buffer(1.0, cap_style="flat")
    rails = s[s["subtype"] == "rail"].copy()
    rails["geometry"] = rails.geometry.buffer(2.5, cap_style="flat")

    xmin, ymin, xmax, ymax = region.bbox
    profiles = {
        "bld": _profile(region, "uint16"),
        "water": _profile(region, "uint8"),
        "ways": _profile(region, "uint8"),
    }
    tmp = {k: p.with_suffix(".part.tif") for k, p in out.items()}
    dsts = {k: rasterio.open(tmp[k], "w", **profiles[k]) for k in out}
    def burn(gdf, values, dtype, tb, tr, shape, fill=0):
        idx = gdf.sindex.query(tb, predicate="intersects")
        if len(idx) == 0:
            return np.full(shape, fill, dtype=dtype)
        sub = gdf.iloc[idx]
        vals = values.iloc[idx] if isinstance(values, pd.Series) else [values] * len(sub)
        return rasterize(zip(sub.geometry, vals), out_shape=shape, transform=tr, fill=fill, dtype=dtype)

    def tile(x0, y0):
        tb = box(x0, y0, x0 + tile_m, y0 + tile_m)
        tr = from_origin(x0, y0 + tile_m, 1.0, 1.0)
        shape = (tile_m, tile_m)
        win = rasterio.windows.Window(x0 - xmin, ymax - (y0 + tile_m), tile_m, tile_m)
        bld = burn(b, b["h_dm"], "uint16", tb, tr, shape)
        water = burn(w, 1, "uint8", tb, tr, shape)
        ways = burn(paths, PATH_CODE, "uint8", tb, tr, shape)
        r = burn(rails, RAIL_CODE, "uint8", tb, tr, shape)
        ways[r > 0] = RAIL_CODE
        rd = burn(roads, ROAD_CODE, "uint8", tb, tr, shape)
        ways[rd > 0] = ROAD_CODE
        return win, {"bld": bld, "water": water, "ways": ways}

    # GDAL's burn releases the GIL, so tiles rasterise on a few threads; writes stay on this one.
    for k in (b, w, paths, rails, roads):
        k.sindex  # build the spatial indexes before the threads share them
    jobs = [(x0, y0) for y0 in range(ymin, ymax, tile_m) for x0 in range(xmin, xmax, tile_m)]
    try:
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for win, arrs in tqdm(pool.map(lambda j: tile(*j), jobs), total=len(jobs), desc="rasterize tiles"):
                for k, a in arrs.items():
                    dsts[k].write(a, 1, window=win)
    finally:
        for d in dsts.values():
            d.close()
    for k in out:
        tmp[k].rename(out[k])
    return out


def build_land_raster(region: Region, tile_m: int = 2000, refresh: bool = False) -> Path:
    """land_1m.tif: homes and gardens, car parks and viaduct decks (codes above), from OpenStreetMap."""
    from .sources import osm

    out = region.cache_dir / "land_1m.tif"
    if out.exists() and not refresh:
        return out
    o = osm.extract(region)
    a = o["areas"]
    polys = a[a.geom_type.isin(["Polygon", "MultiPolygon"])]
    tag = lambda k: polys["tags"].map(lambda t: t.get(k))
    homes = polys[tag("landuse").isin(["residential", "garages"])
                  | ((tag("leisure") == "garden") & (tag("access") == "private"))]
    green = polys[(tag("leisure").isin(GREEN_LEISURE) | tag("landuse").isin(GREEN_LANDUSE)
                   | tag("natural").isin(GREEN_NATURAL)) & (polys.geometry.area >= GREEN_MIN_M2)]
    parking = polys[(tag("amenity") == "parking") | tag("parking").isin(["surface", "multi-storey"])]
    # Viaducts: the mapped deck outline where there is one, else the line, 8 m wide. Road viaducts and
    # the Metro (traffic, overhead wires) don't count.
    l = o["lines"]
    vt = l["tags"]
    via = l[vt.map(lambda t: osm.is_viaduct(t) and t.get("highway") not in ROAD_HIGHWAYS
                   and t.get("railway") not in {"light_rail", "subway", "tram"})]
    decks = polys[tag("bridge").isin(["viaduct", "aqueduct"]) & (tag("man_made") == "bridge")]
    viaducts = pd.concat([decks.geometry, via[via.geom_type != "Polygon"].geometry.buffer(4, cap_style="flat")])
    layers = [(gpd.GeoDataFrame(geometry=list(g), crs=BNG), code) for g, code in
              ((homes.geometry, HOMES_CODE), (parking.geometry, PARKING_CODE), (viaducts, VIADUCT_CODE))]
    green = gpd.GeoDataFrame(geometry=list(green.geometry), crs=BNG)
    for g, _ in layers:
        g.sindex
    green.sindex

    xmin, ymin, xmax, ymax = region.bbox
    tmp = out.with_suffix(".part.tif")
    dst = rasterio.open(tmp, "w", **_profile(region, "uint8"))

    def burn(gdf, tb, tr, shape):
        idx = gdf.sindex.query(tb, predicate="intersects")
        if len(idx) == 0:
            return np.zeros(shape, dtype="uint8")
        return rasterize(((g, 1) for g in gdf.geometry.iloc[idx]), out_shape=shape, transform=tr, fill=0,
                         dtype="uint8")

    def tile(x0, y0):
        tb = box(x0, y0, x0 + tile_m, y0 + tile_m)
        tr = from_origin(x0, y0 + tile_m, 1.0, 1.0)
        shape = (tile_m, tile_m)
        win = rasterio.windows.Window(x0 - xmin, ymax - (y0 + tile_m), tile_m, tile_m)
        land = np.zeros(shape, dtype="uint8")
        for gdf, code in layers:
            land[burn(gdf, tb, tr, shape) > 0] = code
        land[(land == HOMES_CODE) & (burn(green, tb, tr, shape) > 0)] = 0  # a park inside an estate is a park
        return win, land

    jobs = [(x0, y0) for y0 in range(ymin, ymax, tile_m) for x0 in range(xmin, xmax, tile_m)]
    try:
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for win, land in tqdm(pool.map(lambda j: tile(*j), jobs), total=len(jobs), desc="rasterize land"):
                dst.write(land, 1, window=win)
    finally:
        dst.close()
    tmp.rename(out)
    return out
