"""A light basemap the app can draw with no tile server (static vector tiles next to the app).

Water, woods, built-up land, green space, main roads, railways and place names, simplified to a
few metres. The app falls back to it when the online basemap can't load: offline in the field,
or inside hosts that block map tiles.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import geopandas as gpd
import mapbox_vector_tile
import mercantile
import numpy as np
import pandas as pd
import shapely

from .config import Region
from .export import web_dir
from .sources import osm, overture

log = logging.getLogger(__name__)

WATER_POLY = {"water", "river", "canal", "reservoir", "lake", "ocean", "sea", "bay", "lagoon", "basin", "dock", "pond"}
WATER_LINE = {"river", "canal"}
WOOD = {"wood", "forest", "scrub"}
BUILT = {"residential", "industrial", "commercial", "retail", "school", "hospital", "university", "college"}
GREEN = {"park", "recreation_ground", "golf_course", "cemetery", "village_green", "common", "allotments",
         "nature_reserve"}
# Main roads only: the streets would quadruple the size, and the spot cards link out to real maps.
ROADS = {"motorway": 1, "trunk": 1, "primary": 2, "secondary": 2, "tertiary": 3}
RAIL = {"standard_gauge", "light_rail", "narrow_gauge", "tram"}
PLACES = {"city": 1, "town": 1, "suburb": 2, "village": 2, "quarter": 3, "neighbourhood": 3, "hamlet": 3}


TILE_ZOOMS = range(8, 13)  # z12 tiles hold ~1.4 m detail, which MapLibre overzooms to street level
EXTENT = 4096
BUFFER = 64  # tile units of overlap, so lines and outlines don't show seams at tile edges
WORLD = 2 * math.pi * 6378137


def _layer(g: gpd.GeoDataFrame, kind: str, tol: float, min_area: float = 0.0, rank: int | None = None) -> gpd.GeoDataFrame:
    if min_area:
        g = g[g.geometry.area >= min_area]
    geom = g.geometry.simplify(tol, preserve_topology=False)
    geom = geom[~geom.is_empty & geom.notna()]
    out = gpd.GeoDataFrame(geometry=geom.values, crs=g.crs)
    out["k"], out["r"] = kind, rank or 0
    return out


POLY_KIND_NAMES = {"water", "wood", "built", "green"}
POLY_KINDS, LINE_KINDS = (shapely.Polygon, shapely.MultiPolygon), (shapely.LineString, shapely.MultiLineString)


def _only(geom, types):
    """Clipping and merging can leave a GeometryCollection (a polygon plus slivers of edge line),
    which MVT can't hold; keep just the parts of the type this kind is drawn as."""
    parts = [p for p in shapely.get_parts(geom) if isinstance(p, types) and not p.is_empty]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else (shapely.union_all(parts) if types is POLY_KINDS else shapely.MultiLineString(
        [ls for p in parts for ls in shapely.get_parts(p)]))


def _encode_tile(parts: gpd.GeoDataFrame, bounds: tuple[float, float, float, float]) -> bytes:
    """One MVT tile: a single "base" layer, one multi-geometry per kind (and road rank)."""
    feats = []
    for (k, r), sub in parts.groupby(["k", "r"], sort=False):
        geom = _only(shapely.union_all(sub.geometry.values), POLY_KINDS if k in POLY_KIND_NAMES else LINE_KINDS)
        if geom is None:
            continue
        props = {"k": k} if not r else {"k": k, "r": int(r)}
        feats.append({"geometry": geom, "properties": props})
    if not feats:
        return b""
    return mapbox_vector_tile.encode(
        [{"name": "base", "features": feats}],
        # No make_valid: it drops whole multipolygons (the Tyne went missing), and MapLibre draws the
        # odd self-touching ring from quantising to the tile grid without complaint.
        default_options={"quantize_bounds": bounds, "extents": EXTENT})


def write_tiles(layers: gpd.GeoDataFrame, out: Path) -> dict:
    """Static vector tiles (tiles/z/x/y.pbf): the app only fetches and parses the tiles on screen,
    instead of one big GeoJSON for the whole region before the first frame."""
    import shutil

    shutil.rmtree(out, ignore_errors=True)
    merc = layers.to_crs("EPSG:3857")
    lon0, lat0, lon1, lat1 = layers.to_crs("EPSG:4326").total_bounds
    order = {"built": 0, "green": 1, "wood": 2, "water": 3, "waterline": 4, "rail": 5, "road": 6}
    merc = merc.iloc[merc["k"].map(order).argsort(kind="stable")]
    count, size = 0, 0
    for z in TILE_ZOOMS:
        px = WORLD / (256 * 2 ** z)  # metres per screen pixel at this zoom
        g = merc
        if z < TILE_ZOOMS[-1]:
            poly = g.geom_type.isin(["Polygon", "MultiPolygon"])
            g = g[~poly | (g.geometry.area >= (2 * px) ** 2)]
            g = g[(g["k"] != "road") | (g["r"] < 3) | (z >= 10)]
            g = g.set_geometry(g.geometry.simplify(px * 0.6, preserve_topology=False))
            g = g[~g.geometry.is_empty]
        tree = shapely.STRtree(g.geometry.values)
        for t in mercantile.tiles(lon0, lat0, lon1, lat1, z):
            b = mercantile.xy_bounds(t)
            pad = (b.right - b.left) * BUFFER / EXTENT
            idx = tree.query(shapely.box(b.left - pad, b.bottom - pad, b.right + pad, b.top + pad))
            if not len(idx):
                continue
            parts = g.iloc[np.sort(idx)].copy()
            parts["geometry"] = shapely.clip_by_rect(parts.geometry.values, b.left - pad, b.bottom - pad,
                                                     b.right + pad, b.top + pad)
            parts = parts[~parts.geometry.is_empty]
            data = _encode_tile(parts, (b.left, b.bottom, b.right, b.top))
            if not data:
                continue
            path = out / str(z) / str(t.x) / f"{t.y}.pbf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            count += 1
            size += len(data)
    return {"tiles": count, "bytes": size, "bounds": [round(float(v), 5) for v in (lon0, lat0, lon1, lat1)],
            "minzoom": TILE_ZOOMS[0], "maxzoom": TILE_ZOOMS[-1]}


def write_basemap(region: Region) -> dict:
    out = web_dir(region)
    xmin, ymin, xmax, ymax = region.bbox
    clip = shapely.box(xmin - 500, ymin - 500, xmax + 500, ymax + 500)

    def inside(g):
        g = g[g.geometry.intersects(clip)].copy()
        g["geometry"] = g.geometry.intersection(clip)
        return g[~g.geometry.is_empty]

    layers: list[gpd.GeoDataFrame] = []
    water = inside(overture.load_layer(region, "water"))
    polys = water[water.geom_type.isin(["Polygon", "MultiPolygon"])]
    layers.append(_layer(polys[polys["class"].isin(WATER_POLY)], "water", 4, min_area=200))
    lines = water[water.geom_type.isin(["LineString", "MultiLineString"])]
    layers.append(_layer(lines[lines["class"].isin(WATER_LINE)], "waterline", 4))

    land = inside(overture.load_layer(region, "land"))
    land = land[land.geom_type.isin(["Polygon", "MultiPolygon"])]
    layers.append(_layer(land[land["class"].isin(WOOD)], "wood", 6, min_area=1500))

    lu = inside(overture.load_layer(region, "land_use"))
    lu = lu[lu.geom_type.isin(["Polygon", "MultiPolygon"])]
    built = lu[lu["class"].isin(BUILT)]
    if not built.empty:  # dissolve so thousands of housing estates become a few shapes
        built = gpd.GeoDataFrame(geometry=[shapely.union_all(built.geometry.buffer(8).values).buffer(-8)], crs=built.crs)
        built = built.explode(index_parts=False)
    layers.append(_layer(built, "built", 6, min_area=2000))
    layers.append(_layer(lu[lu["class"].isin(GREEN)], "green", 6, min_area=2500))

    seg = inside(overture.load_layer(region, "segments"))
    seg = seg[seg.geom_type.isin(["LineString", "MultiLineString"])]
    roads = seg[(seg["subtype"] == "road") & seg["class"].isin(ROADS)].copy()
    roads["rank"] = roads["class"].map(ROADS)
    for rank in sorted(roads["rank"].unique()):
        sub = roads[roads["rank"] == rank]
        merged = shapely.line_merge(shapely.union_all(sub.geometry.values))
        g = gpd.GeoDataFrame(geometry=[merged], crs=roads.crs).explode(index_parts=False)
        layers.append(_layer(g, "road", 4, rank=int(rank)))
    rail = seg[(seg["subtype"] == "rail") & seg["class"].isin(RAIL)]
    if not rail.empty:
        merged = shapely.line_merge(shapely.union_all(rail.geometry.values))
        layers.append(_layer(gpd.GeoDataFrame(geometry=[merged], crs=rail.crs).explode(index_parts=False), "rail", 4))

    nodes = osm.extract(region)["nodes"]
    places = nodes[nodes["tags"].map(lambda t: t.get("place") in PLACES and bool(t.get("name")))]
    places = places[places.geometry.within(clip)].to_crs("EPSG:4326")
    labels = sorted(
        ([t["name"], round(p.x, 5), round(p.y, 5), PLACES[t["place"]]] for t, p in zip(places["tags"], places.geometry)),
        key=lambda r: r[3])

    layers = [g for g in layers if not g.empty]
    all_layers = gpd.GeoDataFrame(pd.concat(layers, ignore_index=True), crs=layers[0].crs)
    info = write_tiles(all_layers, out / "tiles")
    (out / "basemap.json").unlink(missing_ok=True)  # the old single-file basemap
    (out / "places.json").write_text(json.dumps(labels, separators=(",", ":"), ensure_ascii=False))
    counts = all_layers["k"].value_counts().to_dict()
    log.info("basemap: %s shapes, %d tiles, %.1f MB, %d place names", counts, info["tiles"], info["bytes"] / 1e6, len(labels))
    return {"features": counts, **info, "places": len(labels)}


if __name__ == "__main__":  # python -m spotfinder.basemap newcastle
    import sys

    from .config import load_region

    logging.basicConfig(level=logging.INFO)
    print(write_basemap(load_region(sys.argv[1] if len(sys.argv) > 1 else "newcastle")))
