"""A light basemap the app can draw with no tile server.

Water, woods, built-up land, green space, main roads, railways and place names, simplified to a
few metres. The app falls back to it when the online basemap can't load: offline in the field,
or inside hosts that block map tiles.
"""

from __future__ import annotations

import json
import logging

import geopandas as gpd
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


def _round(geom, nd=5):
    return shapely.set_precision(geom, 10 ** -nd) if geom is not None else None


def _features(g: gpd.GeoDataFrame, kind: str, tol: float, min_area: float = 0.0, extra=None) -> list[dict]:
    if g.empty:
        return []
    g = g.copy()
    if min_area:
        g = g[g.geometry.area >= min_area]
    g["geometry"] = g.geometry.simplify(tol, preserve_topology=False)
    g = g[~g.geometry.is_empty & g.geometry.notna()]
    g = g.to_crs("EPSG:4326")
    out = []
    for i, geom in enumerate(g.geometry.values):
        geom = _round(geom)
        if geom is None or geom.is_empty:
            continue
        props = {"k": kind}
        if extra is not None:
            props.update(extra(g.iloc[i]))
        out.append({"type": "Feature", "geometry": shapely.geometry.mapping(geom), "properties": props})
    return out


def _collapse(feats: list[dict]) -> list[dict]:
    """One Multi* feature per kind (and road rank): same drawing, a fraction of the JSON."""
    groups: dict[tuple, list] = {}
    for f in feats:
        key = (f["properties"]["k"], f["properties"].get("r"))
        geom = f["geometry"]
        parts = geom["coordinates"] if geom["type"].startswith("Multi") else [geom["coordinates"]]
        poly = geom["type"] in ("Polygon", "MultiPolygon")
        groups.setdefault(key + (poly,), []).extend(parts)
    out = []
    for (k, r, poly), parts in groups.items():
        props = {"k": k} if r is None else {"k": k, "r": r}
        out.append({"type": "Feature", "properties": props,
                    "geometry": {"type": "MultiPolygon" if poly else "MultiLineString", "coordinates": parts}})
    return out


def write_basemap(region: Region) -> dict:
    out = web_dir(region)
    xmin, ymin, xmax, ymax = region.bbox
    clip = shapely.box(xmin - 500, ymin - 500, xmax + 500, ymax + 500)

    def inside(g):
        g = g[g.geometry.intersects(clip)].copy()
        g["geometry"] = g.geometry.intersection(clip)
        return g[~g.geometry.is_empty]

    feats: list[dict] = []
    water = inside(overture.load_layer(region, "water"))
    polys = water[water.geom_type.isin(["Polygon", "MultiPolygon"])]
    feats += _features(polys[polys["class"].isin(WATER_POLY)], "water", 4, min_area=200)
    lines = water[water.geom_type.isin(["LineString", "MultiLineString"])]
    feats += _features(lines[lines["class"].isin(WATER_LINE)], "waterline", 4)

    land = inside(overture.load_layer(region, "land"))
    land = land[land.geom_type.isin(["Polygon", "MultiPolygon"])]
    feats += _features(land[land["class"].isin(WOOD)], "wood", 6, min_area=1500)

    lu = inside(overture.load_layer(region, "land_use"))
    lu = lu[lu.geom_type.isin(["Polygon", "MultiPolygon"])]
    built = lu[lu["class"].isin(BUILT)]
    if not built.empty:  # dissolve so thousands of housing estates become a few shapes
        built = gpd.GeoDataFrame(geometry=[shapely.union_all(built.geometry.buffer(8).values).buffer(-8)], crs=built.crs)
        built = built.explode(index_parts=False)
    feats += _features(built, "built", 6, min_area=2000)
    feats += _features(lu[lu["class"].isin(GREEN)], "green", 6, min_area=2500)

    seg = inside(overture.load_layer(region, "segments"))
    seg = seg[seg.geom_type.isin(["LineString", "MultiLineString"])]
    roads = seg[(seg["subtype"] == "road") & seg["class"].isin(ROADS)].copy()
    roads["rank"] = roads["class"].map(ROADS)
    for rank in sorted(roads["rank"].unique()):
        sub = roads[roads["rank"] == rank]
        merged = shapely.line_merge(shapely.union_all(sub.geometry.values))
        g = gpd.GeoDataFrame(geometry=[merged], crs=roads.crs).explode(index_parts=False)
        feats += _features(g, "road", 4, extra=lambda _r, rank=int(rank): {"r": rank})
    rail = seg[(seg["subtype"] == "rail") & seg["class"].isin(RAIL)]
    if not rail.empty:
        merged = shapely.line_merge(shapely.union_all(rail.geometry.values))
        feats += _features(gpd.GeoDataFrame(geometry=[merged], crs=rail.crs).explode(index_parts=False), "rail", 4)

    nodes = osm.extract(region)["nodes"]
    places = nodes[nodes["tags"].map(lambda t: t.get("place") in PLACES and bool(t.get("name")))]
    places = places[places.geometry.within(clip)].to_crs("EPSG:4326")
    labels = sorted(
        ([t["name"], round(p.x, 5), round(p.y, 5), PLACES[t["place"]]] for t, p in zip(places["tags"], places.geometry)),
        key=lambda r: r[3])

    order = {"built": 0, "green": 1, "wood": 2, "water": 3, "waterline": 4, "rail": 5, "road": 6}
    feats = _collapse(feats)
    feats.sort(key=lambda f: (order[f["properties"]["k"]], -f["properties"].get("r", 0)))
    data = {"type": "FeatureCollection", "features": feats}
    (out / "basemap.json").write_text(json.dumps(data, separators=(",", ":")))
    (out / "places.json").write_text(json.dumps(labels, separators=(",", ":"), ensure_ascii=False))
    counts = pd.Series([f["properties"]["k"] for f in feats]).value_counts().to_dict()
    size = (out / "basemap.json").stat().st_size
    log.info("basemap: %s features, %.1f MB, %d place names", counts, size / 1e6, len(labels))
    return {"features": counts, "bytes": size, "places": len(labels)}


if __name__ == "__main__":  # python -m spotfinder.basemap newcastle
    import sys

    from .config import load_region

    logging.basicConfig(level=logging.INFO)
    print(write_basemap(load_region(sys.argv[1] if len(sys.argv) > 1 else "newcastle")))
