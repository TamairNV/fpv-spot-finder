"""Context around each 50 m cell: terrain shape, water, bridges, bandos, hazards, busyness."""

from __future__ import annotations

import logging
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage
from shapely.geometry import box

from . import bandos, legal
from .config import Region
from .heights import LIDAR_DIRS
from .sources import osm, overture

log = logging.getLogger(__name__)

CELL = 50
RES = legal.RES  # 5 m working grid
K = int(CELL / RES)  # 10 pixels per cell

LANDMARK_MAN_MADE = {"lighthouse", "water_tower", "chimney", "windmill", "tower", "obelisk"}
BEACH_NATURAL = {"beach", "sand", "shingle"}
BEACH_MIN_M2 = 1500  # smaller sand is a sandpit or a long-jump pit; golf bunkers are left out too
BRIDGE_UP_M = 4.0    # a bridge deck this high above the ground (LiDAR) is really in the air...
BRIDGE_SPAN_M = 30   # ...and a run of it this long is a bridge worth flying, if it crosses water
BRIDGE_BIG_M = 80    # ...or this long anywhere (a valley, a railway cutting)
FAMOUS_BRIDGE_M = 20  # a listed bridge, or one with its own Wikidata entry, this long counts whatever it crosses
LANDMARK_HISTORIC = {"castle", "monument", "fort", "ruins", "city_gate", "tower", "monastery", "abbey", "citywalls"}
# Icons: landmarks you'd build a cinematic shot round, with a name to them (a lighthouse, a castle, an abbey,
# a monument, a named pier or viaduct), rather than a viewpoint bench, a water tower or a chimney. A ruin or
# monument needs a Wikidata entry or a listing too: OSM has every war memorial, drinking fountain and old
# ferry landing as historic=monument or ruins.
ICON_MAN_MADE = {"lighthouse", "windmill", "obelisk"}
ICON_PIER = {"pier", "breakwater", "groyne"}
ICON_MAX_M2 = 10e4  # a sculpture or a priory, not a whole estate or park tagged as an attraction
# Heroes: icons that stand up, for a 5" cine shot to fly round on their own (the Angel of the North, Penshaw
# Monument, a castle on a mound): a lighthouse, windmill, castle or priory, or any other icon LiDAR sees
# HERO_UP_M+ tall on its footprint, or a whole site (Northumberlandia is a hill). Not bridges, which count
# as bridges, and not a war memorial or fountain mapped as a point, unless its name says column or tower.
HERO_KIND_MAN_MADE = {"lighthouse", "windmill"}
HERO_KIND_HISTORIC = {"castle", "abbey", "monastery"}
HERO_UP_M = 8.0
HERO_SITE_M2 = 2e4
HERO_NAME = re.compile(r"\b(column|obelisk|tower|castle|priory|abbey|lighthouse|windmill)\b", re.I)
# Land you can usually walk onto and fly from, if the rules allow: parks, commons, open access grass.
PUBLIC_LEISURE = {"park", "common", "recreation_ground", "nature_reserve", "dog_park", "village_green"}
PUBLIC_LANDUSE = {"recreation_ground", "village_green"}
FARM_LANDUSE = {"farmland", "farmyard", "orchard", "greenhouse_horticulture", "plant_nursery", "allotments"}
HAZARD_MAN_MADE = {"mast", "communications_tower", "crane"}


def stands_up(t: dict, up_m: float = 0.0, area_m2: float = 0.0) -> bool:
    """Whether an icon is a hero (see HERO_*); one mapped as a point can only say so by its kind or name."""
    return (t.get("man_made") in HERO_KIND_MAN_MADE or t.get("historic") in HERO_KIND_HISTORIC
            or up_m >= HERO_UP_M or area_m2 >= HERO_SITE_M2
            or (area_m2 == 0 and bool(HERO_NAME.search(t.get("name") or ""))))


def _cell_min(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).min(axis=(1, 3))


def _cell_mean(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).mean(axis=(1, 3))


def _cell_max(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // K, K, w // K, K).max(axis=(1, 3))


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
    # A bridge worth filming crosses water or is a viaduct; a 60 m flyover across a dual carriageway or a
    # lane over a culvert isn't. Measured on the span LiDAR sees in the air, not the flagged segment.
    water_union = pd.concat([big_water.geometry, rivers.geometry]).union_all()
    spans = _bridge_spans(region, bridges_seg)
    viaducts = bridge_lines[bridge_lines["tags"].map(osm.is_viaduct)]
    famous_spans = bridge_lines[bridge_lines["tags"].map(osm.famous_bridge)]
    big_bridges = pd.concat([
        spans[(spans.length >= BRIDGE_SPAN_M) & (spans.distance(water_union) <= 30)],
        spans[spans.length >= BRIDGE_BIG_M],
        viaducts.geometry,
        famous_spans[famous_spans.length >= FAMOUS_BRIDGE_M].geometry,  # Causey Arch: 30 m, but 24 m up a gorge
    ])
    log.info("bridges: %d flagged segments, %d spans in the air, %d worth flying", len(bridges_seg), len(spans),
             len(big_bridges))

    # Bandos: derelict buildings LiDAR says are still standing (see bandos.py; the map shows the same set).
    found = bandos.find(region)

    land = overture.load_layer(region, "land")
    cliffs = pd.concat([
        lines[lines["tags"].map(lambda t: t.get("natural") == "cliff")].geometry,
        land[land["class"].isin(["cliff", "bare_rock", "scree", "rock"])].geometry,
        areas[areas["tags"].map(lambda t: t.get("natural") in {"cliff", "bare_rock", "scree"})].geometry,
    ])
    quarry = areas[areas["tags"].map(lambda t: t.get("landuse") == "quarry" or "quarry" in str(t.get("abandoned:landuse", "")))]

    famous = lambda t: bool(t.get("wikidata") or t.get("heritage") or t.get("listed_status"))
    # A viaduct with a name (Nine Arches, Hownsgill) is a landmark whether or not it's listed, and so is a
    # bridge famous in its own right (Causey Arch); a little listed footbridge over a burn isn't.
    named_viaduct = lambda t: osm.is_viaduct(t) and bool(osm.bridge_name(t))
    deck = lambda t: t.get("man_made") == "bridge" and (famous(t) or named_viaduct(t)
                                                         or bool(osm.VIADUCT_NAME.search(osm.bridge_name(t))))
    viaduct_lines = lines[lines["tags"].map(lambda t: osm.is_viaduct(t) and (famous(t) or named_viaduct(t)))]
    famous_bridges = lines[lines["tags"].map(osm.famous_bridge)]
    famous_bridges = famous_bridges[famous_bridges.length >= FAMOUS_BRIDGE_M]
    landmarks = pd.concat([
        nodes[nodes["tags"].map(lambda t: t.get("tourism") in {"viewpoint", "attraction"}
                                or t.get("man_made") in LANDMARK_MAN_MADE
                                or t.get("historic") in LANDMARK_HISTORIC
                                or (t.get("tourism") == "artwork" and bool(t.get("wikidata"))))].geometry,
        polys(areas[areas["tags"].map(lambda t: t.get("historic") in LANDMARK_HISTORIC
                                      or t.get("tourism") == "attraction" or t.get("man_made") in LANDMARK_MAN_MADE
                                      or (t.get("tourism") == "artwork" and bool(t.get("wikidata"))) or deck(t))]).geometry,
        viaduct_lines.geometry,
        famous_bridges.geometry,
    ])
    named = lambda t: bool(t.get("name") or t.get("wikidata"))
    icon = lambda t: named(t) and (t.get("man_made") in ICON_MAN_MADE
                                   or (t.get("historic") in LANDMARK_HISTORIC and famous(t))
                                   or (t.get("tourism") in {"artwork", "attraction"} and bool(t.get("wikidata"))))
    icon_areas = polys(areas[areas["tags"].map(lambda t: icon(t) or deck(t))])
    icons = pd.concat([
        nodes[nodes["tags"].map(icon)].geometry,
        icon_areas[icon_areas.geometry.area <= ICON_MAX_M2].geometry,
        viaduct_lines.geometry,
        famous_bridges[famous_bridges["tags"].map(lambda t: bool(osm.bridge_name(t)))].geometry,
        lines[lines["tags"].map(lambda t: t.get("man_made") in ICON_PIER and named(t))].geometry,
    ])
    hero_areas = icon_areas[icon_areas["tags"].map(lambda t: icon(t) and "bridge" not in t and t.get("man_made") != "bridge")
                            & (icon_areas.geometry.area <= ICON_MAX_M2)]
    up = np.nan_to_num(bandos.standing_height(region, hero_areas.geometry, q=100))
    heroes = pd.concat([
        hero_areas[[stands_up(t, h, a) for t, h, a in zip(hero_areas["tags"], up, hero_areas.geometry.area)]].geometry,
        nodes[nodes["tags"].map(lambda t: icon(t) and stands_up(t))].geometry,
    ])
    log.info("icons: %d, %d of them heroes", len(icons), len(heroes))
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
                               or t.get("access") in {"yes", "permissive"} or osm.is_country_park(t))].geometry
    farm = pa[pa["tags"].map(lambda t: t.get("landuse") in FARM_LANDUSE)].geometry

    golf = polys(areas[areas["tags"].map(lambda t: t.get("leisure") == "golf_course")]).geometry
    beaches = pd.concat([
        polys(areas[areas["tags"].map(lambda t: t.get("natural") in BEACH_NATURAL)]).geometry,
        land[land["class"].isin(list(BEACH_NATURAL))].geometry,
    ])
    beaches = beaches[beaches.area >= BEACH_MIN_M2]
    if len(golf):
        beaches = beaches[~beaches.intersects(golf.union_all())]
    # The sea: whatever in the box isn't land (Overture's land polygons follow the coastline, so the
    # Tyne stops being sea at its mouth).
    box_ = box(*region.bbox)
    landmass = land[land["class"].isin(["land", "island"])].geometry.intersection(box_).union_all()
    sea = [box_.difference(landmass)] if not landmass.is_empty else []

    def frac_of(geoms) -> np.ndarray:
        return _cell_mean(legal.burn(list(geoms), shape_, transform, all_touched=False).astype("float32"))

    out = {
        "dist_water": dist_to(pd.concat([big_water.geometry, rivers.geometry])),
        "dist_bridge": dist_to(big_bridges),
        "dist_footbridge": dist_to(bridge_lines.geometry),
        "dist_bando": dist_to(found.geometry),
        "dist_beach": dist_to(beaches),
        "dist_sea": dist_to(sea),
        "dist_cliff": dist_to(cliffs),
        "dist_quarry": dist_to(quarry.geometry),
        "dist_landmark": dist_to(landmarks),
        "dist_icon": dist_to(icons),
        "dist_hero": dist_to(heroes),
        "dist_hazard": dist_to(hazards),
        "dist_parking": dist_to(parking),
        "dist_path": dist_to(paths.geometry),
        "dist_road_minor": dist_to(roads_minor.geometry),
        "dist_road_busy": dist_to(roads_busy.geometry),
        "public_frac": frac_of(public),
        "farm_frac": frac_of(farm),
        "beach_frac": frac_of(beaches),
    }

    # Busyness: shops/cafes/etc. and buildings nearby, as counts per cell.
    places = overture.load_layer(region, "places")
    places = places[places["operating_status"].fillna("open") != "permanently_closed"]
    out["places_n"] = _counts(places.geometry, region)
    b = overture.load_layer(region, "buildings")
    out["bld_n"] = _counts(b.geometry.centroid, region)
    return out


def _bridge_spans(region: Region, segs: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """The part of each bridge-flagged road or path that's really up in the air: runs where LiDAR sees
    the deck BRIDGE_UP_M+ above the ground. Overture flags a whole segment, so without this a 1 km lane
    with a culvert in it counts as a 1 km bridge. Without LiDAR, the segments as they are."""
    from shapely.geometry import LineString

    from .heights import lidar_paths

    lp = lidar_paths(region)
    if not lp or not len(segs):
        return segs.geometry
    step = 2.0
    pieces = []
    with rasterio.open(lp["ndsm"]) as nd:
        por = rasterio.open(lp["porosity"]) if "porosity" in lp else None
        for g in segs.geometry:
            if g is None or g.is_empty or g.geom_type != "LineString":
                continue
            d = np.arange(0, g.length, step)
            pts = [g.interpolate(x) for x in d]
            xy = [(p.x, p.y) for p in pts]
            h = np.array([v[0] for v in nd.sample(xy)], dtype="float32")
            up = (h != 255) & (h / 4.0 >= BRIDGE_UP_M)
            if por is not None:  # a road under trees isn't up in the air: crowns let the laser through
                pr = np.array([v[0] for v in por.sample(xy)], dtype="float32")
                up &= ~((pr != 255) & (pr > 0.5 * h))
            # runs of "up" samples, allowing a one-sample dropout
            up = up | (np.r_[up[1:], False] & np.r_[False, up[:-1]])
            edges = np.flatnonzero(np.diff(np.r_[0, up.astype(int), 0]))
            for i, j in zip(edges[::2], edges[1::2]):
                if j - i >= 2:
                    pieces.append(LineString([(p.x, p.y) for p in pts[i:j]]))
        if por is not None:
            por.close()
    return gpd.GeoSeries(pieces, crs=segs.crs)


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
