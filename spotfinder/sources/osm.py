"""Raw OpenStreetMap features from Geofabrik extracts.

Overture is great for buildings and roads, but it drops some tags that matter
here (the Town Moor's leisure=common, for one). So land use, legal areas and
the FPV-interesting stuff (bandos, ruins, cliffs, viaducts, lone trees, power
lines) come straight from OSM.

Each region lists the Geofabrik county extracts it needs in regions.toml
(`osm = ["tyne-and-wear", ...]`). Missing ones are downloaded into
data/static/osm/ and MD5-checked. A region without a list reads every
extract in that folder, as before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path

import geopandas as gpd
import osmium
import pandas as pd
import requests
import shapely

from ..config import BNG, STATIC_DIR, Region

log = logging.getLogger(__name__)

OSM_DIR = STATIC_DIR / "osm"
SHARED_OSM_DIRS = [Path("/mnt/project-files/fpv-data/osm")]  # extracts other runs already fetched
GEOFABRIK = "https://download.geofabrik.de/europe/united-kingdom/england/{name}-latest.osm.pbf"

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
    "voltage", "location", "tunnel", "ref", "wikidata", "bridge:name", "building:condition",
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
            k.startswith(("abandoned:", "disused:", "ruins:")) for k, _ in tags) or tags.get("building:condition") == "ruinous"
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

    pbfs = pbf_paths(region)
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


def _find(name: str) -> Path | None:
    for d in [OSM_DIR, *SHARED_OSM_DIRS]:
        hits = [h for h in sorted(d.glob(f"{name}-*.osm.pbf"))  # name-latest or name-YYMMDD
                if re.fullmatch(rf"{re.escape(name)}-(latest|\d{{6}})\.osm\.pbf", h.name)]
        if hits:
            return hits[-1]
    return None


def download(name: str) -> Path:
    """Fetch one Geofabrik England extract and check its MD5."""
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    url = GEOFABRIK.format(name=name)
    dest = OSM_DIR / url.rsplit("/", 1)[1]
    s = requests.Session()
    s.headers["User-Agent"] = "fpv-spot-finder (personal hobby project)"
    md5 = s.get(url + ".md5", timeout=30)
    md5.raise_for_status()
    want = md5.text.split()[0]
    log.info("OSM: downloading %s", url)
    tmp = dest.with_suffix(f".{os.getpid()}.part")  # parallel queue jobs may want the same extract
    h = hashlib.md5()
    with s.get(url, stream=True, timeout=120) as resp, open(tmp, "wb") as f:
        resp.raise_for_status()
        for chunk in resp.iter_content(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        tmp.unlink()
        raise RuntimeError(f"MD5 mismatch for {url}")
    tmp.replace(dest)
    return dest


def pbf_paths(region: Region) -> list[Path]:
    """The region's extracts, local or shared, downloading any that are missing."""
    if not region.osm:
        return sorted(OSM_DIR.glob("*.osm.pbf"))
    out = []
    for name in region.osm:
        p = _find(name)
        if p is not None and p.parent != OSM_DIR:
            OSM_DIR.mkdir(parents=True, exist_ok=True)
            p = Path(shutil.copy2(p, OSM_DIR / p.name))
        out.append(p or download(name))
    return out


def tag(gdf: gpd.GeoDataFrame, key: str) -> pd.Series:
    return gdf["tags"].map(lambda t: t.get(key))


VIADUCT_NAME = re.compile(r"\b(viaduct|aqueduct)\b", re.I)
BRIDGE_NAME = re.compile(r"\b(bridge|viaduct|aqueduct|arch)\b", re.I)  # not "footbridge": a listed one still counts


def bridge_name(t: dict) -> str:
    """What a bridge is called: a railway over one carries the line's name, the bridge's is bridge:name."""
    return t.get("bridge:name") or t.get("name") or ""


def is_viaduct(t: dict) -> bool:
    """Tagged a viaduct or aqueduct, or a bridge called one (Plessey Viaduct is mapped as bridge=yes)."""
    return t.get("bridge") in {"viaduct", "aqueduct"} or (
        t.get("bridge") not in (None, "no") and bool(VIADUCT_NAME.search(bridge_name(t))))


def famous_bridge(t: dict) -> bool:
    """A listed or heritage bridge, or one with a Wikidata entry of its own (Causey Arch): a railway bridge
    often carries the line's Wikidata, so that only counts when the name is the bridge's."""
    if t.get("bridge") in (None, "no"):
        return False
    return bool(t.get("heritage") or t.get("listed_status")) or (
        bool(t.get("wikidata")) and bool(BRIDGE_NAME.search(bridge_name(t))))
