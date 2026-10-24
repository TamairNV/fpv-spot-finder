"""Every mapped bando as a point for the map: what it is, and whether LiDAR still sees something standing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import context
from .config import Region, bng_to_lonlat
from .sources import osm

log = logging.getLogger(__name__)

# s on each point: still standing, old site with nothing left (or a pit marker), no LiDAR to tell
STANDING, SITE, UNKNOWN = 1, 0, -1

OLD_LAND = {"quarry": "Old quarry", "railway": "Old railway", "industrial": "Old industrial site",
            "military": "Old military land"}
HISTORIC = {"mine": "Old pit", "mine_shaft": "Mine shaft", "bunker": "Bunker", "aircraft": "Aircraft wreck",
            "wreck": "Wreck", "lime_kiln": "Old lime kiln", "kiln": "Old kiln", "coaldrop": "Coal drop",
            "industrial": "Old industrial site"}
MAN_MADE = {"adit": "Old adit", "mineshaft": "Mine shaft", "lighthouse": "Old lighthouse", "chimney": "Old chimney",
            "windmill": "Old windmill", "watermill": "Old watermill", "bridge": "Old bridge"}
RUINS = {"bridge": "Ruined bridge", "church": "Ruined church", "chapel": "Ruined chapel", "wall": "Old wall",
         "dry_dock": "Old dry dock", "lime": "Old lime kiln"}


def kind(t: dict) -> str:
    """A short label for what the bando is, from its OSM tags."""
    life = {k.split(":", 1)[1]: v for k, v in t.items() if k.split(":", 1)[0] in ("abandoned", "disused", "ruins")
            and ":" in k}
    if t.get("abandoned:landuse") in OLD_LAND:
        return OLD_LAND[t["abandoned:landuse"]]
    if "aeroway" in life:
        return "Old airfield"
    if t.get("ruins") in RUINS:
        return RUINS[t["ruins"]]
    if "bunker" in (t.get("military"), life.get("military"), t.get("historic")):
        return "Bunker"
    if "military" in life or "military" in t:
        return "Old military building"
    for mm in (t.get("man_made"), life.get("man_made")):
        if mm in MAN_MADE:
            return MAN_MADE[mm]
    if t.get("historic") in HISTORIC:
        return HISTORIC[t["historic"]]
    if "bridge" in life:
        return "Old bridge"
    if t.get("building") in {"ruins", "ruin", "collapsed"} or t.get("historic") == "ruins" or t.get("ruins") == "yes":
        return "Ruins"
    if t.get("abandoned") == "yes" or t.get("building") in {"abandoned", "derelict"} or "building" in life:
        return "Abandoned building"
    if t.get("disused") == "yes" or t.get("building") == "disused":
        return "Disused building"
    return "Bando"


def _osm_url(osm_id: str) -> str | None:
    kinds = {"n": "node", "w": "way", "r": "relation"}
    if osm_id and osm_id[0] in kinds and osm_id[1:].isdigit():
        return f"https://www.openstreetmap.org/{kinds[osm_id[0]]}/{osm_id[1:]}"
    return None


def write(region: Region, out: Path, preview: Callable[[float, float, Path], dict] | None = None) -> dict[str, int]:
    """bandos.geojson next to the spot lists: one point per mapped bando with its kind, name and status,
    and a 3D model (preview(x, y, path) -> its metadata) of each one still standing."""
    o = osm.extract(region)
    frames = [f[f["tags"].map(context._is_bando)] for f in (o["areas"], o["nodes"])]
    b = pd.concat(frames, ignore_index=True)
    standing = context._standing_height(region, b.geometry)
    pit = b["tags"].map(context._is_pit_marker).to_numpy() & (b.geom_type == "Point").to_numpy()
    standing[pit] = 0.0
    xmin, ymin, xmax, ymax = region.bbox
    feats, counts = [], {"standing": 0, "site": 0, "unknown": 0}
    for (osm_id, t, g), h in zip(b[["osm_id", "tags", "geometry"]].itertuples(index=False), standing):
        if g is None or g.is_empty:
            continue
        p = g if g.geom_type == "Point" else g.representative_point()
        if not (xmin <= p.x <= xmax and ymin <= p.y <= ymax):
            continue
        s = UNKNOWN if np.isnan(h) else STANDING if h >= context.BANDO_MIN_H else SITE
        counts[{STANDING: "standing", SITE: "site", UNKNOWN: "unknown"}[s]] += 1
        lon, lat = bng_to_lonlat(p.x, p.y)
        props = {"k": kind(t), "s": s, "h": None if np.isnan(h) else round(float(h), 1),
                 "a": int(round(g.area))}
        if t.get("name"):
            props["n"] = t["name"]
        if url := _osm_url(str(osm_id)):
            props["osm"] = url
        if preview and s == STANDING:
            name = f"previews/b-{osm_id}.png"
            props["p"] = {"file": name, **preview(p.x, p.y, out / name)}
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                      "properties": props})
    # standing ones last so they draw on top
    feats.sort(key=lambda f: f["properties"]["s"] == STANDING)
    (out / "bandos.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    log.info("bandos: %d on the map (%d still standing, %d old sites, %d unchecked)", len(feats),
             counts["standing"], counts["site"], counts["unknown"])
    return counts
