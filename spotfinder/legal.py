"""Where each drone may legally fly, on a 5 m grid.

UK rules this encodes (CAA, Open category, as of 2026):
- 5 inch at 800 g has no class mark, so it flies in A3: at least 150 m from
  residential, commercial, industrial and recreational areas, and never near
  uninvolved people (we use 50 m from buildings and busy roads as the proxy).
- 85 mm whoop is under 250 g, so A1: fine in towns, never over crowds.
- Both must stay out of Flight Restriction Zones (airports, prisons) and
  surface-level prohibited / restricted areas unless they have permission.
  Danger areas are flagged, since they're only dangerous when active.

None of this replaces checking the official drone map and getting the
landowner's OK before taking off.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage

from .config import BNG, Region
try:
    import edt as _edt
except ImportError:  # optional: falls back to scipy's (slower) transform
    _edt = None
from .sources import airspace, osm, overture

log = logging.getLogger(__name__)

RES = 5.0  # metres

A3_AREA_M = 150.0
A3_BUILDING_M = 50.0
A3_ROAD_M = 50.0

RESIDENTIAL_LANDUSE = {"residential", "garages"}
COMMERCIAL_LANDUSE = {"industrial", "commercial", "retail", "port", "depot", "quarry", "landfill"}
COMMERCIAL_AMENITY = {
    "school", "university", "college", "kindergarten", "childcare", "hospital", "clinic", "doctors",
    "place_of_worship", "community_centre", "library", "townhall", "prison", "fire_station", "police",
    "marketplace", "bus_station",
}
RECREATION_LEISURE = {
    "park", "common", "recreation_ground", "pitch", "playground", "golf_course", "sports_centre", "stadium",
    "track", "garden", "dog_park", "beach_resort", "picnic_site", "water_park", "miniature_golf",
    "fitness_station", "marina", "horse_riding", "bandstand",
}
RECREATION_LANDUSE = {"recreation_ground", "village_green"}
RECREATION_TOURISM = {"camp_site", "caravan_site", "picnic_site", "theme_park", "zoo", "attraction"}
NATURE_DESIGNATIONS = {"site_of_special_scientific_interest", "local_nature_reserve", "national_nature_reserve",
                       "special_protection_area", "special_area_of_conservation", "ramsar"}
# Pillboxes and gun emplacements from the war, all along the coast (one's on St Mary's Island): not MOD land.
MILITARY_RELICS = {"bunker", "trench"}
# Owners the map doesn't record yet, by Wikidata or OSM id (adding operator=* in OSM is the real fix).
KNOWN_OWNERS = {"Q7164734": "National Trust",  # Penshaw Monument
                "r11351599": "Forestry England"}  # Chopwell Wood

# Bits in the land-flag raster: things that usually need permission before take-off.
FLAG_NATIONAL_TRUST = 1
FLAG_MILITARY = 2
FLAG_NATURE_RESERVE = 4
FLAG_FORESTRY = 8
FLAG_WOODLAND_TRUST = 16
FLAG_CEMETERY = 32
FLAG_GOLF = 64
FLAG_RAILWAY = 128
FLAG_POWER_LINE = 256
FLAG_DANGER_AREA = 512
FLAG_CROWDS = 1024
FLAG_QUARRY = 2048
FLAG_COMMON = 4096
FLAG_ENGLISH_HERITAGE = 8192

FLAG_TEXT = {
    FLAG_NATIONAL_TRUST: "National Trust land: they ban drone take-off without a permit",
    FLAG_MILITARY: "MOD / military land: keep out",
    FLAG_NATURE_RESERVE: "Nature reserve or SSSI: byelaws often ban drones; watch for nesting birds",
    FLAG_FORESTRY: "Forestry England land: needs their permission for drones",
    FLAG_WOODLAND_TRUST: "Woodland Trust land: drones need permission",
    FLAG_CEMETERY: "Cemetery or churchyard: not a place to fly",
    FLAG_GOLF: "Golf course: private land, and flying golf balls",
    FLAG_RAILWAY: "Railway within 30 m: stay well clear of the line",
    FLAG_POWER_LINE: "Power lines within 30 m",
    FLAG_DANGER_AREA: "Inside a danger area: only fly when it's not active (check NOTAMs)",
    FLAG_CROWDS: "Busy pedestrian area: never fly over crowds",
    FLAG_QUARRY: "Quarry: private site with workers and vehicles; needs the operator's OK",
    FLAG_COMMON: "Common land / town moor: public open space, so it counts as a recreational area for A3",
    FLAG_ENGLISH_HERITAGE: "English Heritage site: no taking off or landing there, and they ask you not to fly over it",
}

NOFLY_FRZ = 1
NOFLY_PROHIBITED = 2
NOFLY_RESTRICTED = 3
NOFLY_TEXT = {NOFLY_FRZ: "Flight Restriction Zone", NOFLY_PROHIBITED: "Prohibited area",
              NOFLY_RESTRICTED: "Restricted area"}


@dataclass
class LegalGrids:
    nofly: np.ndarray        # uint8 code (0 = clear)
    nofly_id: np.ndarray     # int16 index into nofly_name (-1 none)
    nofly_name: list[str]    # zone names
    airspace_label: str      # which NATS dataset this came from
    zones: gpd.GeoDataFrame  # zones touching the region (BNG)
    dist_res: np.ndarray     # metres to residential / built-up
    dist_com: np.ndarray     # metres to commercial, industrial, schools, hospitals...
    dist_rec: np.ndarray     # metres to recreational areas
    dist_bld: np.ndarray     # metres to any building
    dist_road: np.ndarray    # metres to busy roads
    flags: np.ndarray        # uint16 bitmask
    transform: object


def parse_lower_ft(text: str | None) -> float:
    """Lower limit of an airspace block in feet (0 = surface)."""
    if not text:
        return 0.0
    t = text.upper()
    if "SFC" in t or "GND" in t:
        return 0.0
    m = re.search(r"FL\s*(\d+)", t)
    if m:
        return float(m.group(1)) * 100
    m = re.search(r"([\d,]+)\s*FT", t)
    if m:
        return float(m.group(1).replace(",", ""))
    return 0.0


def load_airspace(region: Region) -> tuple[gpd.GeoDataFrame, str]:
    zones, label = airspace.load()
    rows = []
    for _, z in zones.iterrows():
        name = z["name"]
        lower = parse_lower_ft(z["lower"])
        if lower > 400:  # starts above where drones fly
            continue
        if re.match(r"EGR\d*U", name) or "FRZ" in (z["text"] or ""):
            code = NOFLY_FRZ
        elif name.startswith("EGP"):
            code = NOFLY_PROHIBITED
        elif name.startswith("EGR"):
            code = NOFLY_RESTRICTED
        elif name.startswith("EGD"):
            code = 0  # danger areas become a flag, not a hard no
        else:
            continue
        rows.append({"name": name, "code": code, "danger": name.startswith("EGD"), "lower_ft": lower,
                     "upper": z["upper"], "text": z["text"], "geometry": z.geometry})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(BNG)
    xmin, ymin, xmax, ymax = region.bbox
    pad = 5000
    return gdf.cx[xmin - pad: xmax + pad, ymin - pad: ymax + pad].reset_index(drop=True), label


def grid(region: Region):
    xmin, ymin, xmax, ymax = region.bbox
    w, h = int((xmax - xmin) / RES), int((ymax - ymin) / RES)
    return (h, w), from_origin(xmin, ymax, RES, RES)


def burn(geoms, shape_, transform, value=1, dtype="uint8", all_touched=True) -> np.ndarray:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return np.zeros(shape_, dtype=dtype)
    return rasterize(((g, value) for g in geoms), out_shape=shape_, transform=transform, fill=0,
                     dtype=dtype, all_touched=all_touched)


def dist(mask: np.ndarray) -> np.ndarray:
    if not mask.any():
        return np.full(mask.shape, 1e6, dtype="float32")
    if _edt is not None:  # same exact distances as scipy, ~10x faster and multi-threaded
        return _edt.edt(~mask, parallel=os.cpu_count() or 4) * np.float32(RES)
    return (ndimage.distance_transform_edt(~mask) * RES).astype("float32")


def burn_points(pts: gpd.GeoSeries, shape_, transform) -> np.ndarray:
    """1 in every pixel holding a point: what burn() gives for points, without a Python loop over them."""
    out = np.zeros(shape_, dtype="uint8")
    if len(pts):
        cols = np.floor((pts.x.values - transform.c) / transform.a).astype(int)
        rows = np.floor((pts.y.values - transform.f) / transform.e).astype(int)
        ok = (rows >= 0) & (rows < shape_[0]) & (cols >= 0) & (cols < shape_[1])
        out[rows[ok], cols[ok]] = 1
    return out


def _has(tags: dict, keys: tuple[str, ...], needle: str) -> bool:
    return any(needle in str(tags.get(k, "")).lower() for k in keys)


def classify_areas(areas: gpd.GeoDataFrame) -> pd.DataFrame:
    """One boolean column per legal category for each OSM area."""
    t = areas["tags"]
    get = lambda k: t.map(lambda d: d.get(k))
    landuse, leisure, amenity = get("landuse"), get("leisure"), get("amenity")
    tourism, natural, designation = get("tourism"), get("natural"), get("designation")
    abandoned = t.map(lambda d: any(k in d for k in ("disused", "abandoned")) and
                      (d.get("disused") == "yes" or d.get("abandoned") == "yes"))
    ids = areas["osm_id"] if "osm_id" in areas else pd.Series("", index=areas.index)
    known = pd.Series([(KNOWN_OWNERS.get(d.get("wikidata")) or KNOWN_OWNERS.get(i) or "").lower()
                       for d, i in zip(t, ids)], index=areas.index)
    owner = lambda needle, keys=("operator", "owner", "name"): t.map(lambda d: _has(d, keys, needle)) | known.str.contains(
        needle, regex=False)
    return pd.DataFrame({
        "residential": landuse.isin(RESIDENTIAL_LANDUSE),
        "commercial": (landuse.isin(COMMERCIAL_LANDUSE) & ~abandoned) | amenity.isin(COMMERCIAL_AMENITY)
        | t.map(lambda d: d.get("power") in {"plant", "substation"} or d.get("man_made") == "wastewater_plant"),
        "recreational": leisure.isin(RECREATION_LEISURE) | landuse.isin(RECREATION_LANDUSE)
        | tourism.isin(RECREATION_TOURISM) | (natural == "beach") | (designation == "common")
        | t.map(osm.is_country_park),
        "common": (leisure == "common") | (designation == "common"),
        "military": (landuse == "military") | t.map(lambda d: "military" in d and d["military"] not in MILITARY_RELICS),
        "nature": (leisure == "nature_reserve") | designation.isin(NATURE_DESIGNATIONS)
        | (t.map(lambda d: d.get("boundary") == "protected_area")),
        "national_trust": owner("national trust"),
        "forestry": owner("forestry england") | owner("forestry commission"),
        "woodland_trust": owner("woodland trust"),
        # Not by name: "(English Heritage Scheduled Monument)" is a listing, not who runs it.
        "english_heritage": owner("english heritage", ("operator", "owner")),
        "cemetery": (landuse == "cemetery") | (amenity == "grave_yard"),
        "golf": leisure == "golf_course",
        "crowds": (amenity == "marketplace") | (leisure == "stadium") | (tourism == "theme_park"),
        "quarry": (landuse == "quarry") & ~abandoned,
    }, index=areas.index)


def build_legal(region: Region) -> LegalGrids:
    shape_, transform = grid(region)
    o = osm.extract(region)
    areas = o["areas"][o["areas"].geom_type.isin(["Polygon", "MultiPolygon"])]
    cls = classify_areas(areas)
    bld = overture.load_layer(region, "buildings")
    seg = overture.load_layer(region, "segments")
    lu = overture.load_layer(region, "land_use")

    res_mask = burn(areas[cls.residential].geometry, shape_, transform).astype(bool)
    com_mask = burn(areas[cls.commercial].geometry, shape_, transform).astype(bool)
    rec_mask = burn(areas[cls.recreational].geometry, shape_, transform).astype(bool)

    # Built-up areas that aren't mapped as residential: 4+ buildings within ~75 m.
    real_bld = bld[bld.geom_type.isin(["Polygon", "MultiPolygon"]) & (bld.geometry.area >= 15)]
    bld_mask = burn(real_bld.geometry, shape_, transform).astype(bool)
    centroids = burn_points(real_bld.geometry.centroid, shape_, transform)
    k = 31  # 155 m window
    density = ndimage.uniform_filter(centroids.astype("float32"), size=k) * k * k
    builtup = (density >= 4) & ndimage.binary_dilation(bld_mask, iterations=4)
    res_mask |= builtup

    busy_roads = seg[(seg["subtype"] == "road") & seg["class"].isin(
        ["motorway", "trunk", "primary", "secondary", "tertiary"])]
    road_mask = burn(busy_roads.geometry, shape_, transform).astype(bool)

    air, air_label = load_airspace(region)
    nofly = np.zeros(shape_, dtype="uint8")
    nofly_id = np.full(shape_, -1, dtype="int16")
    names: list[str] = []
    flags = np.zeros(shape_, dtype="uint16")
    for _, row in air.iterrows():
        m = burn([row.geometry], shape_, transform).astype(bool)
        if not m.any():
            continue
        if row["danger"]:
            flags[m] |= FLAG_DANGER_AREA
            continue
        names.append(row["name"])
        upd = m & ((nofly == 0) | (row["code"] < nofly))
        nofly[upd] = row["code"]
        nofly_id[upd] = len(names) - 1

    for bit, col in [
        (FLAG_NATIONAL_TRUST, "national_trust"), (FLAG_MILITARY, "military"), (FLAG_NATURE_RESERVE, "nature"),
        (FLAG_FORESTRY, "forestry"), (FLAG_WOODLAND_TRUST, "woodland_trust"), (FLAG_CEMETERY, "cemetery"),
        (FLAG_GOLF, "golf"), (FLAG_CROWDS, "crowds"), (FLAG_QUARRY, "quarry"), (FLAG_COMMON, "common"),
        (FLAG_ENGLISH_HERITAGE, "english_heritage"),
    ]:
        flags[burn(areas[cls[col]].geometry, shape_, transform).astype(bool)] |= bit
    pedestrian = lu[lu["subtype"] == "pedestrian"]
    flags[burn(pedestrian.geometry, shape_, transform).astype(bool)] |= FLAG_CROWDS

    lines = o["lines"]
    # Lines in tunnels don't count: the Metro runs under half of central Newcastle.
    rail = lines[lines["tags"].map(osm.is_active_railway)]
    flags[dist(burn(rail.geometry, shape_, transform).astype(bool)) <= 30] |= FLAG_RAILWAY
    power = lines[lines["tags"].map(osm.is_power_line)]
    flags[dist(burn(power.geometry, shape_, transform).astype(bool)) <= 30] |= FLAG_POWER_LINE

    log.info("legal grids: computing distances")
    return LegalGrids(
        nofly=nofly, nofly_id=nofly_id, nofly_name=names, airspace_label=air_label, zones=air,
        dist_res=dist(res_mask), dist_com=dist(com_mask), dist_rec=dist(rec_mask),
        dist_bld=dist(bld_mask), dist_road=dist(road_mask), flags=flags, transform=transform,
    )


def a3_ok(g: LegalGrids) -> np.ndarray:
    return (
        (g.nofly == 0)
        & (g.dist_res >= A3_AREA_M) & (g.dist_com >= A3_AREA_M) & (g.dist_rec >= A3_AREA_M)
        & (g.dist_bld >= A3_BUILDING_M) & (g.dist_road >= A3_ROAD_M)
    )


def a1_ok(g: LegalGrids) -> np.ndarray:
    return g.nofly == 0


def a3_reason(g: LegalGrids, r: int, c: int) -> str | None:
    """Why a 5 m pixel fails A3, in plain words (None if it passes)."""
    if g.nofly[r, c]:
        return f"Inside {NOFLY_TEXT[int(g.nofly[r, c])]} {g.nofly_name[int(g.nofly_id[r, c])]}"
    checks = [
        (g.dist_res[r, c], A3_AREA_M, "housing"),
        (g.dist_com[r, c], A3_AREA_M, "commercial/industrial/school/hospital land"),
        (g.dist_rec[r, c], A3_AREA_M, "a park or recreational area"),
        (g.dist_bld[r, c], A3_BUILDING_M, "a building"),
        (g.dist_road[r, c], A3_ROAD_M, "a busy road"),
    ]
    worst = min(checks, key=lambda x: x[0] / x[1])
    if worst[0] >= worst[1]:
        return None
    return f"{int(worst[0])} m from {worst[2]} (A3 needs {int(worst[1])} m)"
