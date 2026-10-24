"""Context around each 50 m cell: terrain shape, water, bridges, bandos, hazards, busyness."""

from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage

from . import legal
from .config import Region
from .heights import LIDAR_DIRS
from .sources import osm, overture

log = logging.getLogger(__name__)

CELL = 50
RES = legal.RES  # 5 m working grid
K = int(CELL / RES)  # 10 pixels per cell

# Old structures pilots call bandos. Castles and manors are left out: they're landmarks with
# owners and visitors, not derelict buildings.
# Town-wall towers, gates, forts and abbeys are landmarks with visitors, not bandos.
BANDO_HISTORIC = {"ruins", "mine", "mine_shaft", "bunker", "aircraft", "wreck", "lime_kiln", "kiln", "industrial",
                  "coaldrop"}
BANDO_MIN_H = 2.5   # something at least this tall must still be standing (LiDAR) to count as a real bando
BANDO_LOOK_M = 15   # ...within this far of the mapped outline
# Lifecycle-prefixed keys that mean a disused *structure*. A closed shop or car park
# (disused:amenity=parking, disused:shop=...) isn't one.
BANDO_PREFIXED = {f"{life}:{key}" for life in ("abandoned", "disused", "ruins")
                  for key in ("building", "man_made", "industrial", "power", "military", "aeroway", "bridge")}
BANDO_STRUCTURE_KEYS = {"building", "man_made", "industrial", "power", "military", "bridge"}
LANDMARK_MAN_MADE = {"lighthouse", "water_tower", "chimney", "windmill", "tower", "obelisk"}
LANDMARK_HISTORIC = {"castle", "monument", "fort", "ruins", "city_gate", "tower", "monastery", "abbey", "citywalls"}
# Land you can usually walk onto and fly from, if the rules allow: parks, commons, open access grass.
PUBLIC_LEISURE = {"park", "common", "recreation_ground", "nature_reserve", "dog_park", "village_green"}
PUBLIC_LANDUSE = {"recreation_ground", "village_green"}
FARM_LANDUSE = {"farmland", "farmyard", "orchard", "greenhouse_horticulture", "plant_nursery", "allotments"}
HAZARD_MAN_MADE = {"mast", "communications_tower", "crane"}


def _cell_min(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).min(axis=(1, 3))


def _cell_mean(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).mean(axis=(1, 3))


def _cell_max(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).max(axis=(1, 3))


def _is_bando(t: dict) -> bool:
    if t.get("building") in osm.BUILDING_BANDO:
        return True
    if "yes" in (t.get("abandoned"), t.get("disused"), t.get("ruins")) and any(k in t for k in BANDO_STRUCTURE_KEYS):
        return True
    if any(k in BANDO_PREFIXED for k in t):
        return True
    if t.get("abandoned:landuse") in {"industrial", "quarry", "railway", "military"}:
        return True
    # brownfield is left out on purpose: in OSM it's mostly cleared land or building sites
    return t.get("historic") in BANDO_HISTORIC


def _is_pit_marker(t: dict) -> bool:
    return t.get("historic") in {"mine", "mine_shaft"} and t.get("building") not in osm.BUILDING_BANDO \
        and not any(k.startswith(("ruins", "abandoned:", "disused:")) for k in t)


def load_dtm(region: Region) -> tuple[np.ndarray, str]:
    """DTM on the 5 m grid: EA LiDAR 2 m if the fetch job left one, else the terrain-tile mosaic."""
    shape_, transform = legal.grid(region)
    for base in [region.cache_dir, *[d / region.key for d in LIDAR_DIRS]]:
        p = base / "lidar_dtm_2m.tif"
        if p.exists():
            with rasterio.open(p) as src:
                dtm = src.read(1, out_shape=shape_, resampling=Resampling.average)
            fallback, _ = load_tile_dtm(region)
            dtm = np.where(np.isfinite(dtm), dtm, fallback)
            return dtm.astype("float32"), "EA LiDAR DTM (2 m, averaged to 5 m)"
    return load_tile_dtm(region)


def load_tile_dtm(region: Region) -> tuple[np.ndarray, str]:
    from .sources import terrain

    with rasterio.open(terrain.build_dtm(region)) as src:
        return src.read(1).astype("float32"), "AWS terrain tiles (EA 2 m LiDAR DTM in England)"


def terrain_features(dtm: np.ndarray) -> dict[str, np.ndarray]:
    gy, gx = np.gradient(dtm, RES)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    win = int(250 / RES) | 1
    relief = ndimage.maximum_filter(dtm, size=win) - ndimage.minimum_filter(dtm, size=win)
    tpi = dtm - ndimage.uniform_filter(dtm, size=int(600 / RES) | 1)
    return {
        "elev": _cell_mean(dtm),
        "relief_250": _cell_mean(relief),
        "slope_p90": _cell_max(ndimage.uniform_filter(slope, size=3)),
        "steep_frac": _cell_mean((slope >= 35).astype("float32")),
        "tpi_600": _cell_mean(tpi),
    }


def context_features(region: Region) -> dict[str, np.ndarray]:
    shape_, transform = legal.grid(region)
    o = osm.extract(region)
    areas, lines, nodes = o["areas"], o["lines"], o["nodes"]
    polys = lambda g: g[g.geom_type.isin(["Polygon", "MultiPolygon"])]

    def dist_to(geoms) -> np.ndarray:
        return _cell_min(legal.dist(legal.burn(list(geoms), shape_, transform).astype(bool)))

    # Water you'd notice from the air: rivers, lakes, the sea; not garden ponds.
    water = overture.load_layer(region, "water")
    big_water = water[water.geom_type.isin(["Polygon", "MultiPolygon"]) & (
        water["class"].isin(["river", "canal", "lake", "reservoir", "ocean", "lagoon", "bay"])
        | (water.geometry.area >= 3000))]
    rivers = lines[lines["tags"].map(lambda t: t.get("waterway") in {"river", "canal"})]

    seg = overture.load_layer(region, "segments")
    bridges_seg = seg[seg["is_bridge"].fillna(False).astype(bool) & (seg.geometry.length >= 25)]
    bridge_lines = lines[lines["tags"].map(lambda t: t.get("bridge") in {"viaduct", "aqueduct", "yes", "boardwalk"})]
    big_bridges = pd.concat([
        bridges_seg[bridges_seg.geometry.length >= 60].geometry,
        bridge_lines[bridge_lines["tags"].map(lambda t: t.get("bridge") in {"viaduct", "aqueduct"})].geometry,
    ])

    b_tags = pd.concat([areas["tags"][areas["tags"].map(_is_bando)], nodes["tags"][nodes["tags"].map(_is_bando)]])
    bando_all = pd.concat([areas[areas["tags"].map(_is_bando)].geometry, nodes[nodes["tags"].map(_is_bando)].geometry])
    standing = _standing_height(region, bando_all)
    # A pit or shaft marker is where a colliery was; whatever LiDAR sees there now is usually a new shed.
    standing[b_tags.map(_is_pit_marker).to_numpy() & (bando_all.geom_type == "Point").to_numpy()] = 0.0
    log.info("bandos: %d mapped, %d with something still standing", len(bando_all), int((standing >= BANDO_MIN_H).sum()))
    # Mapped with no LiDAR to check (NaN) still count; mapped but flat (an old pit site) only as a "site".
    bando_real = bando_all[~(standing < BANDO_MIN_H)]
    bando_site = bando_all[standing < BANDO_MIN_H]

    land = overture.load_layer(region, "land")
    cliffs = pd.concat([
        lines[lines["tags"].map(lambda t: t.get("natural") == "cliff")].geometry,
        land[land["class"].isin(["cliff", "bare_rock", "scree", "rock"])].geometry,
        areas[areas["tags"].map(lambda t: t.get("natural") in {"cliff", "bare_rock", "scree"})].geometry,
    ])
    quarry = areas[areas["tags"].map(lambda t: t.get("landuse") == "quarry" or "quarry" in str(t.get("abandoned:landuse", "")))]

    landmarks = pd.concat([
        nodes[nodes["tags"].map(lambda t: t.get("tourism") in {"viewpoint", "attraction"}
                                or t.get("man_made") in LANDMARK_MAN_MADE
                                or t.get("historic") in LANDMARK_HISTORIC)].geometry,
        polys(areas[areas["tags"].map(lambda t: t.get("historic") in LANDMARK_HISTORIC
                                      or t.get("tourism") == "attraction")]).geometry,
    ])
    hazards = pd.concat([
        lines[lines["tags"].map(lambda t: t.get("power") in {"line", "minor_line"})].geometry,
        nodes[nodes["tags"].map(lambda t: t.get("man_made") in HAZARD_MAN_MADE
                                or (t.get("power") == "generator" and t.get("generator:source") == "wind"))].geometry,
    ])
    parking = pd.concat([
        polys(areas[areas["tags"].map(lambda t: t.get("amenity") == "parking")]).geometry,
        nodes[nodes["tags"].map(lambda t: t.get("amenity") == "parking")].geometry,
    ])
    paths = seg[(seg["subtype"] == "road") & seg["class"].isin(
        ["footway", "path", "cycleway", "bridleway", "track", "pedestrian", "steps"])]
    roads_minor = seg[(seg["subtype"] == "road") & seg["class"].isin(
        ["residential", "service", "unclassified", "living_street", "tertiary", "secondary", "primary", "trunk"])]

    roads_busy = seg[(seg["subtype"] == "road") & seg["class"].isin(["motorway", "trunk", "primary"])]
    pa = polys(areas)
    public = pa[pa["tags"].map(lambda t: t.get("leisure") in PUBLIC_LEISURE or t.get("landuse") in PUBLIC_LANDUSE
                               or t.get("designation") == "common"
                               or t.get("access") in {"yes", "permissive"})].geometry
    farm = pa[pa["tags"].map(lambda t: t.get("landuse") in FARM_LANDUSE)].geometry

    def frac_of(geoms) -> np.ndarray:
        return _cell_mean(legal.burn(list(geoms), shape_, transform, all_touched=False).astype("float32"))

    out = {
        "dist_water": dist_to(pd.concat([big_water.geometry, rivers.geometry])),
        "dist_bridge": dist_to(big_bridges),
        "dist_footbridge": dist_to(bridge_lines.geometry),
        "dist_bando": dist_to(bando_real),
        "dist_bando_site": dist_to(bando_site),
        "dist_cliff": dist_to(cliffs),
        "dist_quarry": dist_to(quarry.geometry),
        "dist_landmark": dist_to(landmarks),
        "dist_hazard": dist_to(hazards),
        "dist_parking": dist_to(parking),
        "dist_path": dist_to(paths.geometry),
        "dist_road_minor": dist_to(roads_minor.geometry),
        "dist_road_busy": dist_to(roads_busy.geometry),
        "public_frac": frac_of(public),
        "farm_frac": frac_of(farm),
    }

    # Busyness: shops/cafes/etc. and buildings nearby, as counts per cell.
    places = overture.load_layer(region, "places")
    places = places[places["operating_status"].fillna("open") != "permanently_closed"]
    out["places_n"] = _counts(places.geometry, region)
    b = overture.load_layer(region, "buildings")
    out["bld_n"] = _counts(b.geometry.centroid, region)
    return out


def _standing_height(region: Region, geoms: gpd.GeoSeries) -> np.ndarray:
    """Tallest solid thing (LiDAR, low first-minus-last return so not a tree) on or next to each geometry.
    NaN where there's no LiDAR to look at."""
    from .heights import lidar_paths

    out = np.full(len(geoms), np.nan, dtype="float32")
    lp = lidar_paths(region)
    if not lp or len(geoms) == 0:
        return out
    from rasterio.windows import from_bounds

    with rasterio.open(lp["ndsm"]) as nd:
        por = rasterio.open(lp["porosity"]) if "porosity" in lp else None
        for i, g in enumerate(geoms):
            if g is None or g.is_empty:
                continue
            x0, y0, x1, y1 = g.buffer(BANDO_LOOK_M).bounds
            win = from_bounds(x0, y0, x1, y1, nd.transform).round_offsets().round_lengths()
            h = nd.read(1, window=win, boundless=True, fill_value=255)
            valid = h != 255
            if valid.mean() < 0.5:
                continue
            hm = np.where(valid, h / 4.0, 0)
            if por is not None:
                p = por.read(1, window=win, boundless=True, fill_value=255)
                hm = np.where((p != 255) & (p / 4.0 > 0.5 * hm) & (hm > 3), 0, hm)  # see-through: a tree
            out[i] = float(hm.max())
        if por is not None:
            por.close()
    return out


def _counts(points: gpd.GeoSeries, region: Region) -> np.ndarray:
    xmin, ymin, xmax, ymax = region.bbox
    n = int((xmax - xmin) / CELL)
    m = int((ymax - ymin) / CELL)
    pts = points[~points.is_empty]
    xs, ys = pts.x.to_numpy(), pts.y.to_numpy()
    keep = (xs >= xmin) & (xs < xmax) & (ys > ymin) & (ys <= ymax)
    c = ((xs[keep] - xmin) // CELL).astype(int)
    r = ((ymax - ys[keep]) // CELL).astype(int)
    out = np.zeros((m, n), dtype="float32")
    np.add.at(out, (np.clip(r, 0, m - 1), np.clip(c, 0, n - 1)), 1)
    return out


def legal_features(g: legal.LegalGrids) -> dict[str, np.ndarray]:
    a3 = legal.a3_ok(g).astype("float32")
    a1 = legal.a1_ok(g).astype("float32")
    flags = g.flags
    h, w = flags.shape
    fl = flags.reshape(h // K, K, w // K, K)
    any_flags = np.bitwise_or.reduce(np.bitwise_or.reduce(fl, axis=3), axis=1)
    return {
        "a3_frac": _cell_mean(a3),
        "a1_frac": _cell_mean(a1),
        "flags": any_flags.astype("float32"),
        "dist_a3_edge": _cell_min(np.minimum.reduce([g.dist_res, g.dist_com, g.dist_rec])),
    }
