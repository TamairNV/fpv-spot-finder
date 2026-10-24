"""Bandos: derelict buildings and structures you can fly round and through, still standing.

What counts is deliberately narrow. A ruin in a park, a protected monument, a capped mine shaft, an old
pit site or a disused railway is history, not a bando, so it's left out. What's left is a mapped
building or structure tagged ruined, abandoned, derelict or disused, big enough to be a building,
not inside a park, garden or churchyard, and with LiDAR still seeing walls at least 4 m tall on its
footprint. A bando mapped only as a point takes the footprint of the building it sits in.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

from .config import Region, bng_to_lonlat
from .sources import osm, overture

log = logging.getLogger(__name__)

MIN_H = 4.0        # LiDAR must still see walls at least this tall: a storey and a bit
MIN_AREA = 100.0   # m² of footprint: a shed, pillbox or kiln isn't one
MIN_SITE = 300.0   # m² of footprints on the whole site: a derelict cottage or pub isn't somewhere to fly
PAD = 2.0          # metres round the footprint, for LiDAR and map not lining up exactly

# s on each point: still standing, or no LiDAR to tell
STANDING, UNKNOWN = 1, -1

# Lifecycle keys that mean a disused *structure*: a closed shop (disused:shop) or an old railway
# (abandoned:railway) isn't one.
LIFECYCLE = ("abandoned", "disused", "ruins")
STRUCTURE_KEYS = ("building", "industrial", "power", "military")
PREFIXED = {f"{life}:{key}" for life in LIFECYCLE for key in STRUCTURE_KEYS}
HISTORIC = {"ruins", "industrial", "coaldrop"}
# Ruins of these are monuments with visitors, not bandos.
# Lime kilns aren't on the list: one in a park is left out anyway, and a big derelict bank of them
# (Southwick) is a bando.
MONUMENT_RUINS = {"church", "chapel", "castle", "abbey", "priory", "monastery", "tower", "wall", "citywalls",
                  "city_wall", "bridge"}
MONUMENT_HISTORIC = {"castle", "fort", "monastery", "abbey", "church", "city_gate", "citywalls", "monument",
                     "memorial", "manor", "tower"}
MONUMENT_NAME = re.compile(r"\b(castle|fort|abbey|priory|chapel|church|cathedral|kirk|tower|hall|manor|palace|"
                           r"monastery|bastion|keep|folly)\b", re.I)
# A house that was once a lighthouse or a mill is somebody's home now.
LIVED_IN = {"residential", "house", "apartments", "detached", "semidetached_house", "terrace", "bungalow"}
# Land a ruin sits in when it's a park feature (a folly, an old mill in a dene, a churchyard ruin) or a
# museum piece.
PARK_LEISURE = {"park", "garden", "golf_course", "playground", "common", "village_green"}
PARK_LANDUSE = {"cemetery", "religious", "recreation_ground", "village_green"}
PARK_TOURISM = {"museum", "attraction", "theme_park", "zoo"}
CLUSTER_M = 20.0     # footprints this close are one site: a works, a row of ruins
SAME_KIND_M = 100.0  # ...or this close if they're the same kind of thing: a depot of sheds

OLD_LAND = {"industrial": "Old industrial building", "military": "Old military building"}
MAN_MADE = {"works": "Old works", "mineshaft": "Old pithead", "chimney": "Old chimney", "lighthouse": "Old lighthouse",
            "windmill": "Old windmill", "watermill": "Old watermill", "water_tower": "Old water tower",
            "pumping_station": "Old pumping station", "gasometer": "Old gasometer", "silo": "Old silo",
            "pier": "Old pier", "storage_tank": "Old tank"}
BUILDING = {"industrial": "Old factory", "factory": "Old factory", "warehouse": "Old warehouse",
            "hospital": "Old hospital", "school": "Old school", "church": "Old church", "chapel": "Old chapel",
            "farm": "Old farmhouse", "barn": "Old barn", "farm_auxiliary": "Old farm building",
            "house": "Derelict house", "hotel": "Old hotel", "office": "Old offices", "commercial": "Old offices",
            "retail": "Old shop", "pub": "Old pub", "train_station": "Old station", "hangar": "Old hangar",
            "mill": "Old mill", "power": "Old power station", "pier": "Old staithes"}


def is_bando(t: dict) -> bool:
    """Tagged as a ruined, abandoned, derelict or disused building or structure."""
    if any(k.startswith(("demolished:", "razed:")) for k in t):  # gone
        return False
    if t.get("building") in osm.BUILDING_BANDO or t.get("building:condition") == "ruinous":
        return True
    if any(t.get(life) == "yes" for life in LIFECYCLE) and any(k in t for k in ("man_made", *STRUCTURE_KEYS)):
        return True
    if t.get("building") in LIVED_IN:
        return False
    if any(k in PREFIXED for k in t) or any(t.get(f"{life}:man_made") in MAN_MADE for life in LIFECYCLE):
        return True
    return t.get("historic") in HISTORIC


def heritage(t: dict) -> bool:
    """A monument with a keeper or visitors (scheduled, a castle or chapel ruin, an attraction), not a bando.
    A listed building left to rot is still a bando."""
    old_ruin = (t.get("historic") == "ruins" or t.get("ruins") == "yes" or t.get("building") in {"ruins", "ruin"}) \
        and not any(t.get(life) == "yes" or f"{life}:building" in t for life in ("abandoned", "disused"))
    return bool(t.get("designation")) or t.get("heritage") == "1" or t.get("tourism") in PARK_TOURISM \
        or t.get("ruins") in MONUMENT_RUINS or t.get("historic") in MONUMENT_HISTORIC \
        or (old_ruin and bool(MONUMENT_NAME.search(t.get("name") or "")))


def kind(t: dict) -> str:
    """A short label for what the bando is, from its OSM tags."""
    life = {k.split(":", 1)[1]: v for k, v in t.items() if k.split(":", 1)[0] in LIFECYCLE and ":" in k}
    if "military" in life or "military" in t:
        return "Old military building"
    for mm in (life.get("man_made"), t.get("man_made")):
        if mm in MAN_MADE:
            return MAN_MADE[mm]
    if t.get("historic") == "coaldrop":
        return "Old coal drop"
    if "power" in life:
        return "Old power station"
    for b in (life.get("building"), t.get("building"), life.get("amenity"), t.get("amenity")):
        if b in BUILDING:
            return BUILDING[b]
    if "industrial" in life or t.get("historic") == "industrial" or t.get("landuse") == "industrial":
        return "Old industrial building"
    if t.get("building") in {"ruins", "ruin", "collapsed"} or t.get("historic") == "ruins" or t.get("ruins") == "yes":
        return "Ruins"
    if t.get("building") == "derelict":
        return "Derelict building"
    if t.get("abandoned") == "yes" or t.get("building") == "abandoned" or "building" in life:
        return "Abandoned building"
    return "Disused building"


def _osm_url(osm_id: str) -> str | None:
    kinds = {"n": "node", "w": "way", "r": "relation"}
    if osm_id and osm_id[0] in kinds and osm_id[1:].isdigit():
        return f"https://www.openstreetmap.org/{kinds[osm_id[0]]}/{osm_id[1:]}"
    return None


def _footprints(region: Region, points: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """The building each point sits in (empty where it's in none)."""
    b = overture.load_layer(region, "buildings")
    b = b[b.geom_type.isin(["Polygon", "MultiPolygon"])]
    hit = gpd.sjoin(points[["geometry"]], b[["geometry"]], predicate="within", how="left")
    hit = hit[~hit.index.duplicated()]
    out = gpd.GeoSeries([None] * len(points), index=points.index, crs=points.crs)
    found = hit["index_right"].notna()
    out[found] = b.geometry.loc[hit.loc[found, "index_right"].astype(int)].to_numpy()
    return out


def standing_height(region: Region, geoms: gpd.GeoSeries) -> np.ndarray:
    """How tall the solid stuff on each footprint still is (LiDAR, not see-through tree crowns): the 95th
    percentile, so walls of a roofless shell count but one stray pixel doesn't. NaN with no LiDAR."""
    from .heights import lidar_paths

    out = np.full(len(geoms), np.nan, dtype="float32")
    lp = lidar_paths(region)
    if not lp or len(geoms) == 0:
        return out
    with rasterio.open(lp["ndsm"]) as nd:
        por = rasterio.open(lp["porosity"]) if "porosity" in lp else None
        for i, g in enumerate(geoms):
            if g is None or g.is_empty:
                continue
            g = g.buffer(PAD)
            win = from_bounds(*g.bounds, nd.transform).round_offsets().round_lengths()
            h = nd.read(1, window=win, boundless=True, fill_value=255)
            inside = ~geometry_mask([g], out_shape=h.shape, transform=nd.window_transform(win))
            valid = inside & (h != 255)
            if valid.sum() < max(10, 0.5 * inside.sum()):
                continue
            hm = h / 4.0
            if por is not None:
                p = por.read(1, window=win, boundless=True, fill_value=255)
                hm = np.where((p != 255) & (p / 4.0 > 0.5 * hm) & (hm > 3), 0, hm)  # see-through: a tree
            out[i] = float(np.percentile(hm[valid], 95))
        if por is not None:
            por.close()
    return out


def find(region: Region) -> gpd.GeoDataFrame:
    """Every bando in the region: osm_id, tags, footprint geometry and standing height (m, NaN = no LiDAR)."""
    o = osm.extract(region)
    areas, nodes = o["areas"], o["nodes"]
    cand = pd.concat([f[f["tags"].map(lambda t: is_bando(t) and not heritage(t))] for f in (areas, nodes)],
                     ignore_index=True)
    cand = gpd.GeoDataFrame(cand, geometry="geometry", crs=areas.crs)
    pts = cand.geom_type == "Point"
    if pts.any():
        cand.loc[pts, "geometry"] = _footprints(region, cand[pts]).to_numpy()
    cand = cand[cand.geometry.notna() & ~cand.geometry.is_empty]
    cand = cand[cand.geom_type.isin(["Polygon", "MultiPolygon"]) & (cand.geometry.area >= MIN_AREA)]
    cand = cand.drop_duplicates(subset="geometry")  # a node and the way it sits in
    parks = areas[areas["tags"].map(lambda t: t.get("leisure") in PARK_LEISURE or t.get("landuse") in PARK_LANDUSE
                                    or t.get("amenity") == "grave_yard" or t.get("tourism") in PARK_TOURISM)]
    if len(parks):
        inside = gpd.sjoin(gpd.GeoDataFrame(geometry=cand.geometry.representative_point(), crs=cand.crs),
                           parks[["geometry"]], predicate="within").index
        cand = cand[~cand.index.isin(inside)]
    cand = cand.reset_index(drop=True)
    cand["standing"] = standing_height(region, cand.geometry)
    real = cand[~(cand["standing"] < MIN_H)].reset_index(drop=True)
    member = _groups(real)
    site_area = real.geometry.area.groupby(member).transform("sum")
    big = real[site_area >= MIN_SITE].reset_index(drop=True)
    log.info("bandos: %d tagged and big enough outside parks, %d still standing, %d on sites of %d m²+",
             len(cand), len(real), len(big), MIN_SITE)
    return big


def _groups(b: gpd.GeoDataFrame) -> pd.Series:
    """Which site each footprint belongs to (see sites), as a group number per row."""
    b = b.reset_index(drop=True)
    kinds = b["tags"].map(kind).to_numpy()
    parent = list(range(len(b)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if len(b):
        left, right = b.sindex.query(b.geometry.buffer(SAME_KIND_M), predicate="intersects")
        for i, j in zip(left, right):
            if i < j and (kinds[i] == kinds[j] or b.geometry[i].distance(b.geometry[j]) <= CLUSTER_M):
                parent[root(i)] = root(j)
    return pd.Series([root(i) for i in range(len(b))])


def sites(b: gpd.GeoDataFrame) -> list[dict]:
    """Group footprints into sites: within CLUSTER_M of each other, or SAME_KIND_M for the same kind
    (an ammunition depot's sheds are spread out on purpose). Each site gets the osm_id and tags of its biggest
    building, its name, kind, footprint area, building count, tallest height and a point to show."""
    if not len(b):
        return []
    b = b.reset_index(drop=True)
    member = _groups(b)
    out = []
    for _, idx in member.groupby(member).groups.items():
        m = b.loc[idx]
        big = m.geometry.area.idxmax()
        kinds = m["tags"].map(kind)
        names = [t["name"] for t in m["tags"] if t.get("name")]
        h = m["standing"]
        out.append({"osm_id": m.at[big, "osm_id"], "tags": m.at[big, "tags"], "name": names[0] if names else None,
                    "kind": kinds.mode().iloc[0], "area": float(m.geometry.area.sum()), "n": len(m),
                    "standing": float(h.max()) if h.notna().any() else np.nan,
                    "point": m.at[big, "geometry"].representative_point()})
    return out


def write(region: Region, out: Path, preview: Callable[[float, float, Path], dict] | None = None,
          found: gpd.GeoDataFrame | None = None) -> dict[str, int]:
    """bandos.geojson next to the spot lists: one point per bando with its kind, name and height, and a
    3D model (preview(x, y, path) -> its metadata) of each one LiDAR has seen."""
    b = find(region) if found is None else found
    xmin, ymin, xmax, ymax = region.bbox
    feats, counts = [], {"standing": 0, "unknown": 0}
    for site in sites(b):
        p, h = site["point"], site["standing"]
        if not (xmin <= p.x <= xmax and ymin <= p.y <= ymax):
            continue
        s = UNKNOWN if np.isnan(h) else STANDING
        counts["standing" if s == STANDING else "unknown"] += 1
        lon, lat = bng_to_lonlat(p.x, p.y)
        props = {"k": site["kind"], "s": s, "h": None if np.isnan(h) else round(h, 1), "a": int(round(site["area"]))}
        if site["n"] > 1:
            props["b"] = site["n"]
        if site["name"]:
            props["n"] = site["name"]
        osm_id = site["osm_id"]
        if url := _osm_url(str(osm_id)):
            props["osm"] = url
        if preview and s == STANDING:
            name = f"previews/b-{osm_id}.png"
            props["p"] = {"file": name, **preview(p.x, p.y, out / name)}
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                      "properties": props})
    # biggest last so they draw on top
    feats.sort(key=lambda f: f["properties"]["a"])
    (out / "bandos.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    log.info("bandos: %d on the map (%d seen standing by LiDAR, %d unchecked)", len(feats), counts["standing"],
             counts["unknown"])
    return counts
