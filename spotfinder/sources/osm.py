"""Raw OpenStreetMap features from Geofabrik extracts.

Overture is great for buildings and roads, but it drops some tags that matter
here (the Town Moor's leisure=common, for one). So land use, legal areas and
the FPV-interesting stuff (bandos, ruins, cliffs, viaducts, lone trees, power
lines) come straight from OSM.

Put the county extracts covering the region in data/static/osm/
(e.g. tyne-and-wear, northumberland, durham from download.geofabrik.de).
"""

from __future__ import annotations

import logging

import geopandas as gpd
import osmium
import pandas as pd
import shapely

from ..config import BNG, STATIC_DIR, Region

log = logging.getLogger(__name__)

OSM_DIR = STATIC_DIR / "osm"

AREA_KEYS = {
    "landuse", "leisure", "amenity", "natural", "historic", "military", "man_made", "tourism",
    "boundary", "designation", "aeroway", "abandoned", "disused", "ruins", "sport", "place",
    "abandoned:building", "disused:building", "ruins:building", "abandoned:amenity", "disused:amenity",
    "abandoned:landuse", "disused:landuse", "abandoned:man_made", "disused:man_made", "water", "wetland",
}
BUILDING_BANDO = {"ruins", "abandoned", "collapsed", "ruin", "disused", "derelict"}
LINE_TAGS = {
    "power": {"line", "minor_line", "cable"},
    "railway": {"rail", "light_rail", "tram", "narrow_gauge", "abandoned", "disused", "subway", "preserved"},
    "natural": {"cliff", "ridge", "arete", "tree_row"},
    "waterway": {"river", "canal", "stream"},
    "man_made": {"embankment", "pier", "breakwater", "groyne", "pipeline"},
    "barrier": {"city_wall", "retaining_wall"},
    "aerialway": None,
}
NODE_TAGS = {
    "natural": {"tree", "peak", "cliff", "rock", "cave_entrance"},
    "man_made": {"tower", "mast", "chimney", "water_tower", "silo", "lighthouse", "windmill", "crane",
                 "communications_tower"},
    "historic": None,
    "tourism": {"viewpoint", "artwork", "attraction"},
    "power": {"tower", "generator"},
    "amenity": {"parking"},
    "leisure": {"skatepark", "pitch", "playground"},
    "abandoned": None, "disused": None, "ruins": None,
    "place": {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality"},
}
KEEP_TAG_KEYS = AREA_KEYS | set(LINE_TAGS) | set(NODE_TAGS) | {
    "name", "operator", "owner", "access", "building", "bridge", "layer", "height", "ele", "denotation",
    "leaf_type", "protect_class", "protection_title", "heritage", "listed_status", "generator:source",
    "voltage", "location", "tunnel", "ref", "wikidata",
}


def _keep(tags) -> dict:
    return {k: v for k, v in tags if k in KEEP_TAG_KEYS or k.startswith(("abandoned:", "disused:", "ruins:"))}


class _Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.areas, self.lines, self.nodes = [], [], []

    def area(self, a):
        tags = a.tags
        interesting = any(k in tags for k in AREA_KEYS) or tags.get("building") in BUILDING_BANDO or any(
            k.startswith(("abandoned:", "disused:", "ruins:")) for k, _ in tags)
        if not interesting:
            return
        try:
            geom = self.wkb.create_multipolygon(a)
        except Exception:
            return
        kind = "w" if a.from_way() else "r"
        self.areas.append((f"{kind}{a.orig_id()}", geom, _keep(tags)))

    def way(self, w):
        tags = w.tags
        hit = any(k in tags and (vals is None or tags[k] in vals) for k, vals in LINE_TAGS.items())
        hit = hit or tags.get("bridge") in {"viaduct", "aqueduct"} or (
            "bridge" in tags and tags.get("highway") in {"footway", "path", "cycleway", "bridleway"})
        if not hit or w.is_closed() and "natural" not in tags and "bridge" not in tags:
            return
        try:
            geom = self.wkb.create_linestring(w)
        except Exception:
            return
        self.lines.append((f"w{w.id}", geom, _keep(tags) | {"highway": tags.get("highway", "")}))

    def node(self, n):
        tags = n.tags
        if not any(k in tags and (vals is None or tags[k] in vals) for k, vals in NODE_TAGS.items()):
            return
        self.nodes.append((f"n{n.id}", self.wkb.create_point(n), _keep(tags)))


def _frame(rows) -> gpd.GeoDataFrame:
    if not rows:
        return gpd.GeoDataFrame({"osm_id": [], "tags": []}, geometry=[], crs=BNG)
    ids, geoms, tags = zip(*rows)
    gdf = gpd.GeoDataFrame({"osm_id": ids, "tags": tags}, geometry=shapely.from_wkb(list(geoms), on_invalid="ignore"),
                           crs="EPSG:4326")
    gdf = gdf[gdf.geometry.notna()].drop_duplicates("osm_id")
    return gdf.to_crs(BNG).reset_index(drop=True)


def extract(region: Region, refresh: bool = False) -> dict[str, gpd.GeoDataFrame]:
    paths = {k: region.cache_dir / f"osm_{k}.parquet" for k in ("areas", "lines", "nodes")}
    if all(p.exists() for p in paths.values()) and not refresh:
        out = {}
        for k, p in paths.items():
            g = gpd.read_parquet(p)
            g["tags"] = g["tags"].map(lambda s: dict(eval(s)) if s else {})  # written by us below
            out[k] = g
        return out

    pbfs = sorted(OSM_DIR.glob("*.osm.pbf"))
    if not pbfs:
        raise FileNotFoundError(f"No .osm.pbf extracts in {OSM_DIR}; get them from download.geofabrik.de")
    col = _Collector()
    for pbf in pbfs:
        log.info("OSM: reading %s", pbf.name)
        col.apply_file(str(pbf), locations=True, idx="flex_mem")

    xmin, ymin, xmax, ymax = region.bbox
    pad = 1000
    out = {}
    for k, rows in (("areas", col.areas), ("lines", col.lines), ("nodes", col.nodes)):
        g = _frame(rows)
        g = g.cx[xmin - pad: xmax + pad, ymin - pad: ymax + pad].reset_index(drop=True)
        disk = g.copy()
        disk["tags"] = disk["tags"].map(lambda t: repr(sorted(t.items())))
        disk.to_parquet(paths[k])
        out[k] = g
        log.info("OSM %s: %d", k, len(g))
    return out


def tag(gdf: gpd.GeoDataFrame, key: str) -> pd.Series:
    return gdf["tags"].map(lambda t: t.get(key))
