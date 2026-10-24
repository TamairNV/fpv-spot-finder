"""Write everything the web app reads: spots, heatmaps, legal layers, 3D previews."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from shapely import STRtree
from shapely.geometry import Point, mapping

from . import legal, scoring
from .config import WEB_DATA_DIR, Region, Seed, bng_to_lonlat
from .heights import HeightReader
from .sources import osm

log = logging.getLogger(__name__)

CELL = 50
LEGAL_RES = 20  # metres per pixel in the legal lookup/overlay images
PREVIEW_M = 320
PREVIEW_RES = 2

# Codes in the legal lookup image (blue channel).
CODE_CLEAR, CODE_FRZ, CODE_RESTRICTED = 0, 1, 2
CODE_A3_RES, CODE_A3_COM, CODE_A3_REC, CODE_A3_BLD, CODE_A3_ROAD = 10, 11, 12, 13, 14


def web_dir(region: Region) -> Path:
    d = WEB_DATA_DIR / region.key
    (d / "previews").mkdir(parents=True, exist_ok=True)
    return d


def cell_of(region: Region, xy) -> tuple[int, int]:
    xmin, _, _, ymax = region.bbox
    return int((ymax - xy[1]) // CELL), int((xy[0] - xmin) // CELL)


def cells_within(region: Region, xy, radius_m: float) -> list[tuple[int, int]]:
    r0, c0 = cell_of(region, xy)
    k = int(math.ceil(radius_m / CELL))
    out = []
    for dr in range(-k, k + 1):
        for dc in range(-k, k + 1):
            x, y = cell_center(region, r0 + dr, c0 + dc)
            if math.hypot(x - xy[0], y - xy[1]) <= radius_m + CELL / 2:
                out.append((r0 + dr, c0 + dc))
    return out


def cell_center(region: Region, r: int, c: int) -> tuple[float, float]:
    xmin, _, _, ymax = region.bbox
    return xmin + (c + 0.5) * CELL, ymax - (r + 0.5) * CELL


def corners_lonlat(region: Region) -> list[list[float]]:
    """MapLibre image-source corners: top-left, top-right, bottom-right, bottom-left."""
    xmin, ymin, xmax, ymax = region.bbox
    return [list(bng_to_lonlat(x, y)) for x, y in ((xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin))]


def mercator_frame(region: Region, ground_res: float) -> tuple[object, int, int, list[list[float]]]:
    """A Web Mercator grid covering the region; MapLibre stretches images linearly in Mercator,
    so overlays have to be on this grid to line up (BNG differs by tens of metres mid-image)."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    from .config import BNG

    left, bottom, right, top = transform_bounds(BNG, "EPSG:3857", *region.bbox, densify_pts=50)
    lat_mid = math.radians(bng_to_lonlat(*((region.bbox[0] + region.bbox[2]) / 2,
                                           (region.bbox[1] + region.bbox[3]) / 2))[1])
    res = ground_res / math.cos(lat_mid)
    w, h = int(math.ceil((right - left) / res)), int(math.ceil((top - bottom) / res))
    transform = from_origin(left, top, res, res)
    import pyproj

    to_ll = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    r_, b_ = left + w * res, top - h * res
    corners = [list(to_ll.transform(x, y)) for x, y in ((left, top), (r_, top), (r_, b_), (left, b_))]
    return transform, w, h, corners


def to_mercator(region: Region, arr: np.ndarray, src_res: float, ground_res: float, resampling) -> tuple[np.ndarray, list]:
    from rasterio.transform import from_origin
    from rasterio.warp import reproject

    from .config import BNG

    dst_transform, w, h, corners = mercator_frame(region, ground_res)
    xmin, _, _, ymax = region.bbox
    bands = arr[None] if arr.ndim == 2 else np.moveaxis(arr, -1, 0)
    out = np.zeros((bands.shape[0], h, w), dtype=arr.dtype)
    for i in range(bands.shape[0]):
        reproject(source=np.ascontiguousarray(bands[i]), destination=out[i],
                  src_transform=from_origin(xmin, ymax, src_res, src_res), src_crs=BNG,
                  dst_transform=dst_transform, dst_crs="EPSG:3857", resampling=resampling)
    return (out[0] if arr.ndim == 2 else np.moveaxis(out, 0, -1)), corners


def os_grid_ref(x: float, y: float, digits: int = 6) -> str:
    """British National Grid reference, e.g. NZ 233 656."""
    e100k, n100k = int(x // 100000), int(y // 100000)
    l1 = (19 - n100k) - (19 - n100k) % 5 + (e100k + 10) // 5
    l2 = (19 - n100k) * 5 % 25 + e100k % 5
    letters = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
    half = digits // 2
    e = int((x % 100000) // (10 ** (5 - half)))
    n = int((y % 100000) // (10 ** (5 - half)))
    return f"{letters[l1]}{letters[l2]}{e:0{half}d}{n:0{half}d}"


# ---------------------------------------------------------------- legal layers

def save_legal_grids(region: Region, g: legal.LegalGrids) -> None:
    """Summarise the 5 m legal grids to 20 m (BNG) and keep them, plus the zone outlines."""
    k = int(LEGAL_RES / legal.RES)
    h, w = g.nofly.shape
    blk = lambda a: a.reshape(h // k, k, w // k, k)
    code = np.full((h, w), CODE_CLEAR, dtype="uint8")
    code[g.dist_road < legal.A3_ROAD_M] = CODE_A3_ROAD
    code[g.dist_bld < legal.A3_BUILDING_M] = CODE_A3_BLD
    code[g.dist_rec < legal.A3_AREA_M] = CODE_A3_REC
    code[g.dist_com < legal.A3_AREA_M] = CODE_A3_COM
    code[g.dist_res < legal.A3_AREA_M] = CODE_A3_RES
    code[g.nofly >= legal.NOFLY_PROHIBITED] = CODE_RESTRICTED
    code[g.nofly == legal.NOFLY_FRZ] = CODE_FRZ
    # Most restrictive code in each 20 m pixel: no-fly beats A3 reasons beats clear.
    order = np.array([CODE_CLEAR, CODE_A3_ROAD, CODE_A3_BLD, CODE_A3_REC, CODE_A3_COM, CODE_A3_RES,
                      CODE_RESTRICTED, CODE_FRZ], dtype="uint8")
    rank = np.zeros(256, dtype="uint8")
    rank[order] = np.arange(len(order), dtype="uint8")
    code20 = order[blk(rank[code]).max(axis=(1, 3))]
    flags20 = np.bitwise_or.reduce(np.bitwise_or.reduce(blk(g.flags), axis=3), axis=1)
    np.savez_compressed(region.out_dir / "legal20.npz", code=code20, flags=flags20)

    zones = g.zones.to_crs("EPSG:4326")
    feats = []
    for _, z in zones.iterrows():
        kind = "Danger area" if z["danger"] else legal.NOFLY_TEXT.get(int(z["code"]), "Restricted area")
        feats.append({"type": "Feature", "geometry": mapping(z.geometry.simplify(0.0001)),
                      "properties": {"name": z["name"], "kind": kind, "upper": z["upper"], "text": z["text"]}})
    (region.out_dir / "zones.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def write_legal_layers(region: Region) -> list:
    """Legal lookup image (R,G = flag bits, B = code) and display overlays, on the Mercator grid."""
    from rasterio.enums import Resampling

    out = web_dir(region)
    with np.load(region.out_dir / "legal20.npz") as z:
        code20, flags20 = z["code"], z["flags"]
    lookup = np.stack([flags20 & 0xFF, flags20 >> 8, code20], axis=-1).astype("uint8")
    lookup, corners = to_mercator(region, lookup, LEGAL_RES, LEGAL_RES, Resampling.nearest)
    Image.fromarray(lookup, "RGB").save(out / "legal_lookup.png", optimize=True)
    code = lookup[..., 2]

    def overlay(nogo_a3: bool) -> np.ndarray:
        rgba = np.zeros(code.shape + (4,), dtype="uint8")
        if nogo_a3:
            rgba[code >= 10] = [245, 158, 11, 70]
        rgba[np.isin(code, [CODE_FRZ, CODE_RESTRICTED])] = [220, 38, 38, 120]
        return rgba

    Image.fromarray(overlay(True), "RGBA").save(out / "legal_freestyle5.png", optimize=True)
    Image.fromarray(overlay(False), "RGBA").save(out / "legal_cine85.png", optimize=True)
    (out / "zones.geojson").write_text((region.out_dir / "zones.geojson").read_text())
    return corners


# ---------------------------------------------------------------- naming

# Named places a pilot would recognise a spot by. Hotels, gyms and bowling alleys are named too, but
# "By Premier Inn" says little about the place.
NOT_A_PLACE = {"hotel", "hostel", "guest_house", "motel", "apartment", "chalet", "fitness_centre", "sports_hall",
               "bowling_alley", "amusement_arcade", "dance", "escape_game", "adult_gaming_centre", "sauna",
               "tanning_salon", "trampoline_park", "indoor_play", "information", "artwork", "gallery"}


class Namer:
    def __init__(self, region: Region):
        o = osm.extract(region)
        a, n, ln = o["areas"], o["nodes"], o["lines"]
        named = lambda g: g[g["tags"].map(lambda t: bool(t.get("name")))]
        good_area = named(a)[named(a)["tags"].map(lambda t: (any(k in t for k in (
            "leisure", "natural", "historic", "water", "place", "military", "tourism"))
            or t.get("landuse") in {"forest", "meadow", "recreation_ground", "quarry", "brownfield", "reservoir"})
            and t.get("tourism") not in NOT_A_PLACE and t.get("leisure") not in NOT_A_PLACE)]
        good_area = good_area[good_area.geometry.area < 5e6]
        self.areas = good_area.reset_index(drop=True)
        self.area_tree = STRtree(self.areas.geometry.values)
        pts = named(n)
        self.places = pts[pts["tags"].map(lambda t: t.get("place") in {
            "city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality"})].reset_index(drop=True)
        self.place_tree = STRtree(self.places.geometry.values)
        # (memorial points are mostly plaques)
        feats = pts[pts["tags"].map(lambda t: any(k in t for k in ("natural", "historic", "tourism", "man_made"))
                                    and t.get("tourism") not in NOT_A_PLACE and t.get("historic") != "memorial")]
        rivers = named(ln)[named(ln)["tags"].map(lambda t: t.get("waterway") in {"river", "canal"} or "bridge" in t)]
        self.feats = pd.concat([feats, rivers]).reset_index(drop=True)
        self.feat_tree = STRtree(self.feats.geometry.values)

    def _nearest(self, tree, frame, p, max_d):
        if len(frame) == 0:
            return None, None
        i = tree.nearest(p)
        if i is None:
            return None, None
        d = frame.geometry.iloc[i].distance(p)
        return (frame.iloc[i], d) if d <= max_d else (None, None)

    def name(self, x: float, y: float) -> tuple[str, str]:
        p = Point(x, y)
        title = None
        cont = [i for i in self.area_tree.query(p, predicate="intersects")]
        if cont:
            best = min(cont, key=lambda i: self.areas.geometry.iloc[i].area)
            title = self.areas["tags"].iloc[best]["name"]
        else:
            row, d = self._nearest(self.area_tree, self.areas, p, 250)
            if row is not None:
                title = f"By {row['tags']['name']}"
            else:
                row, d = self._nearest(self.feat_tree, self.feats, p, 300)
                if row is not None:
                    title = f"Near {row['tags']['name']}"
        loc_row, _ = self._nearest(self.place_tree, self.places, p, 6000)
        locality = loc_row["tags"]["name"] if loc_row is not None else ""
        if not title:
            title = f"Spot near {locality}" if locality else "Unnamed spot"
        return title, locality


# ---------------------------------------------------------------- previews

def write_preview(reader: HeightReader, dtm: np.ndarray, region: Region, x: float, y: float, path: Path) -> dict:
    """160 x 160 px at 2 m: R = class*64, G = height above ground (0.25 m), B = ground (0.5 m above min)."""
    half = PREVIEW_M // 2
    x0, y0 = int(round(x - half)), int(round(y - half))
    t = reader.tile(x0, y0, PREVIEW_M, 0)
    n = PREVIEW_M // PREVIEW_RES
    pool = lambda a, fn: fn(a.reshape(n, PREVIEW_RES, n, PREVIEW_RES), axis=(1, 3))
    h = pool(t.ohm, np.max)
    bld = pool(t.bld.astype("uint8"), np.max) > 0
    tree = pool((t.trees >= 2).astype("uint8"), np.max) > 0
    water = pool(t.water.astype("uint8"), np.mean) > 0.5
    path_ = pool((t.ways == 2).astype("uint8"), np.max) > 0
    road = pool((t.ways == 1).astype("uint8"), np.mean) > 0.4
    cls = np.zeros((n, n), dtype="uint8")  # 0 ground
    cls[path_] = 3  # paths drawn as their own class
    cls[road] = 2
    cls[water] = 1
    cls[tree] = 4
    cls[bld] = 5
    xmin, _, _, ymax = region.bbox
    # Ground: sample the 5 m DTM at each 2 m pixel centre (nearest is fine at this scale).
    cols = ((x0 + (np.arange(n) + 0.5) * PREVIEW_RES - xmin) / legal.RES).astype(int)
    rows = ((ymax - (y0 + PREVIEW_M - (np.arange(n) + 0.5) * PREVIEW_RES)) / legal.RES).astype(int)
    cols = np.clip(cols, 0, dtm.shape[1] - 1)
    rows = np.clip(rows, 0, dtm.shape[0] - 1)
    ground = dtm[np.ix_(rows, cols)]
    from scipy import ndimage

    ground = ndimage.uniform_filter(ground, size=3)
    gmin = float(np.nanmin(ground))
    rgb = np.stack([
        cls * 40,
        np.clip(np.round(h * 4), 0, 255),
        np.clip(np.round((ground - gmin) * 2), 0, 255),
    ], axis=-1).astype("uint8")
    Image.fromarray(rgb, "RGB").save(path, optimize=True)
    return {"size": n, "res": PREVIEW_RES, "ground_min": round(gmin, 1),
            "lidar": bool(t.lidar.mean() > 0.5)}


# ---------------------------------------------------------------- spots

def _heat_png(region: Region, final: np.ndarray, eligible: np.ndarray, path: Path, block: int = 1) -> list:
    """Heat overlay. block > 1 max-pools first (for the zoomed-out layer, where 25 m pixels vanish)."""
    from rasterio.enums import Resampling

    s = np.where(eligible, final, 0.0).astype("float32")
    if block > 1:
        h, w = s.shape
        s = s[: h - h % block, : w - w % block].reshape(h // block, block, w // block, block).max(axis=(1, 3))
    s, corners = to_mercator(region, s, CELL * block, CELL * block / 2, Resampling.nearest)
    t = np.clip((s - 0.7) / 0.3, 0, 1)  # top 30% of the region gets colour
    # dark teal -> amber -> hot pink, transparent below the cut
    stops = np.array([[20, 120, 140], [250, 190, 40], [240, 40, 120]], dtype=float)
    seg = np.clip(t * 2, 0, 2)
    i = np.minimum(seg.astype(int), 1)
    f = (seg - i)[..., None]
    rgb = stops[i] * (1 - f) + stops[i + 1] * f
    alpha = np.where(t > 0, 60 + 170 * t, 0)
    rgba = np.concatenate([rgb, alpha[..., None]], axis=-1).astype("uint8")
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)
    return corners


def _gpx(feats: list[dict], title: str) -> str:
    """Waypoints for the spots, for OS Maps, Google My Maps, Gaia, a GPS..."""
    from xml.sax.saxutils import escape

    pts = []
    for f in feats:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        desc = f"Score {p['score']}. " + "; ".join(p["drivers"]) + ". " + p["legal"]["summary"]
        if p["legal"]["flags"]:
            desc += " Warnings: " + "; ".join(p["legal"]["flags"])
        pts.append(f'  <wpt lat="{lat}" lon="{lon}"><name>{escape(f"#{p["rank"]} {p["title"]}")}</name>'
                   f'<desc>{escape(desc)}</desc><sym>Flag</sym></wpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="fpv-spot-finder" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
            f"  <metadata><name>{escape(title)}</name></metadata>\n" + "\n".join(pts) + "\n</gpx>\n")


def _drivers(comp_vals: dict[str, float], profile: scoring.Profile) -> list[str]:
    pos = [(k, comp_vals[k] * w) for k, w in profile.weights.items() if w > 0 and k in scoring.COMPONENT_TEXT]
    pos.sort(key=lambda kv: -kv[1])
    return [scoring.COMPONENT_TEXT[k] for k, v in pos[:3] if v > 0.02]


def _flags_text(flag_bits: int) -> list[str]:
    return [txt for bit, txt in legal.FLAG_TEXT.items() if flag_bits & bit and bit != legal.FLAG_COMMON]


def write_all(region: Region, cells: dict, results: dict, meta: dict, seeds: list[Seed], previews: int = 120) -> None:
    out = web_dir(region)
    for old in (out / "previews").glob("*.png"):  # spot ids change between runs
        old.unlink()
    legal_corners = write_legal_layers(region)
    namer = Namer(region)
    gaps = pd.read_parquet(region.out_dir / "gaps.parquet")
    lone = pd.read_parquet(region.out_dir / "lone.parquet")
    gap_tree = STRtree([Point(xy) for xy in zip(gaps.x, gaps.y)]) if len(gaps) else None
    lone_tree = STRtree([Point(xy) for xy in zip(lone.x, lone.y)]) if len(lone) else None
    reader = HeightReader(region)
    from .context import load_dtm

    dtm, _ = load_dtm(region)
    done_previews: dict[tuple[int, int], dict] = {}

    for key, res in results.items():
        profile = scoring.PROFILES[key]
        heat_corners = _heat_png(region, res["heat"], res["eligible"], out / f"heat_{key}.png")
        heat_lo_corners = _heat_png(region, res["heat"], res["eligible"], out / f"heatlo_{key}.png", block=4)
        feats = []
        for rank, (r, c) in enumerate(res["spots"], start=1):
            x, y = cell_center(region, r, c)
            lon, lat = bng_to_lonlat(x, y)
            comp_vals = {k: round(float(v[r, c]), 3) for k, v in res["comps"].items()}
            title, locality = namer.name(x, y)
            R = profile.radius_cells
            H, W = res["final"].shape
            win = (slice(max(r - R, 0), min(r + R + 1, H)), slice(max(c - R, 0), min(c + R + 1, W)))
            ws = lambda k: float(cells[k][win].sum())
            wm = lambda k: float(cells[k][win].mean())
            wmax = lambda k: float(cells[k][win].max())
            wmin = lambda k: float(cells[k][win].min())
            flag_bits = int(np.bitwise_or.reduce(cells["flags"][win].astype("int64").ravel()))
            # Take-off: the most open, legal, path-adjacent cell in the window.
            best, best_cost = (r, c), 1e9
            for rr in range(win[0].start, win[0].stop):
                for cc in range(win[1].start, win[1].stop):
                    legal_frac = cells[profile.legal_key][rr, cc]
                    if cells["open_frac"][rr, cc] < 0.5 or legal_frac < 0.9:
                        continue
                    cost = cells["dist_path"][rr, cc] + 0.5 * CELL * math.hypot(rr - r, cc - c)
                    if cost < best_cost:
                        best, best_cost = (rr, cc), cost
            tx, ty = cell_center(region, *best)
            tlon, tlat = bng_to_lonlat(tx, ty)
            near_gaps, near_lone = [], []
            radius = (R + 0.5) * CELL + 25
            if gap_tree is not None:
                idx = gap_tree.query(Point(x, y).buffer(radius))
                sub = gaps.iloc[idx].sort_values("flank_h_m", ascending=False).head(30)
                for _, gq in sub.iterrows():
                    glon, glat = bng_to_lonlat(gq.x, gq.y)
                    near_gaps.append([round(glon, 6), round(glat, 6), round(gq.width_m, 1), round(gq.flank_h_m, 1),
                                      round(gq.x - x, 1), round(gq.y - y, 1)])
            if lone_tree is not None:
                idx = lone_tree.query(Point(x, y).buffer(radius))
                sub = lone.iloc[idx].sort_values("height_m", ascending=False).head(15)
                for _, lq in sub.iterrows():
                    llon, llat = bng_to_lonlat(lq.x, lq.y)
                    near_lone.append([round(llon, 6), round(llat, 6), round(lq.height_m, 1),
                                      round(lq.x - x, 1), round(lq.y - y, 1)])
            spot_id = f"{r}-{c}"
            preview = None
            if rank <= previews:
                if (r, c) not in done_previews:
                    pm = write_preview(reader, dtm, region, x, y, out / "previews" / f"{spot_id}.png")
                    done_previews[(r, c)] = pm
                preview = {"file": f"previews/{spot_id}.png", **done_previews[(r, c)],
                           "center": [round(lon, 6), round(lat, 6)], "bng": [x, y]}
            gr = os_grid_ref(x, y)
            a3_clear = wm("a3_frac")
            if key == "freestyle5":
                a_txt = (f"Open A3: {round(100 * a3_clear)}% of this spot is clear of the A3 distances (150 m from "
                         "houses, businesses and parks; 50 m from buildings and busy roads). Take off and fly in the "
                         "clear part; the amber overlay shows the no-go edges.")
            else:
                a_txt = "Open A1 (under 250 g): outside flight restriction zones. Never fly over crowds."
            props = {
                "id": spot_id, "profile": key, "rank": rank,
                "score": round(100 * float(res["final"][r, c])), "rules": round(100 * float(res["rule_rank"][r, c])),
                "taste": None if res["sim"] is None else round(100 * float(res["sim"][r, c])),
                "title": title, "locality": locality, "gridref": gr,
                "components": comp_vals, "drivers": _drivers(comp_vals, profile),
                "stats": {
                    "gaps": int(ws("gaps")), "gaps_whoop": int(ws("gaps_whoop")), "lone_trees": int(ws("lone")),
                    "edge_m": int(ws("edge_tree_m") + ws("edge_bld_m")), "open_pct": round(100 * wm("open_frac")),
                    "tree_pct": round(100 * wm("tree_frac")), "bld_pct": round(100 * wm("bld_frac")),
                    "max_height_m": round(wmax("h_p90"), 1), "relief_m": round(wmax("relief_250"), 1),
                    "water_m": int(wmin("dist_water")), "bando_m": int(wmin("dist_bando")),
                    "bridge_m": int(wmin("dist_bridge")), "path_m": int(wmin("dist_path")),
                    "parking_m": int(wmin("dist_parking")), "hazard_m": int(wmin("dist_hazard")),
                    "places": int(ws("places_n")), "buildings": int(ws("bld_n")),
                    "lidar_pct": round(100 * wm("lidar_frac")),
                },
                "legal": {"summary": a_txt, "flags": _flags_text(flag_bits), "a3_clear_pct": round(100 * a3_clear),
                          "a3_edge_m": int(wmin("dist_a3_edge"))},
                "takeoff": {"lon": round(tlon, 6), "lat": round(tlat, 6), "dx": tx - x, "dy": ty - y,
                            "path_m": int(cells["dist_path"][best]), "parking_m": int(cells["dist_parking"][best])},
                "gaps_list": near_gaps, "lone_list": near_lone, "preview": preview,
                "links": {
                    "satellite": f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},250m/data=!3m1!1e3",
                    "streetview": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lon:.6f}",
                    "geograph": f"https://www.geograph.org.uk/gridref/{gr[:2]}{gr[2:4]}{gr[5:7]}",
                    "osmaps": f"https://explore.osmaps.com/pin?lat={lat:.6f}&lon={lon:.6f}&zoom=16",
                    "dronemap": "https://dronesafetymap.co.uk/",
                },
            }
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                          "properties": props})
        (out / f"spots_{key}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
        (out / f"spots_{key}.gpx").write_text(_gpx(feats, f"{profile.label} spots, {region.name}"))
        log.info("wrote %d %s spots", len(feats), key)

    seed_out = []
    H, W = cells["a3_frac"].shape
    for s in seeds:
        res = results.get(s.profile)
        r, c = cell_of(region, s.bng)
        entry = {"name": s.name, "profile": s.profile, "lon": s.lon, "lat": s.lat, "radius_m": s.radius_m,
                 "note": s.note, "a3_frac": round(float(cells["a3_frac"][r, c]), 2),
                 "a1_frac": round(float(cells["a1_frac"][r, c]), 2),
                 "flags": _flags_text(int(cells["flags"][r, c]))}
        if res is not None:
            # Scored like a spot: at the best cell of the area you named, not just its middle.
            br, bc = max((rc for rc in cells_within(region, s.bng, s.radius_m) if 0 <= rc[0] < H and 0 <= rc[1] < W),
                         key=lambda rc: res["style_final"][rc])
            entry["components"] = {k: round(float(v[br, bc]), 3) for k, v in res["style"].items()}
            entry["score"] = round(100 * float(res["style_final"][br, bc]))
        if (r, c) not in done_previews:
            x, y = cell_center(region, r, c)
            done_previews[(r, c)] = write_preview(reader, dtm, region, x, y, out / "previews" / f"{r}-{c}.png")
        x, y = cell_center(region, r, c)
        lon, lat = bng_to_lonlat(x, y)
        entry["preview"] = {"file": f"previews/{r}-{c}.png", **done_previews[(r, c)],
                            "center": [round(lon, 6), round(lat, 6)], "bng": [x, y]}
        seed_out.append(entry)

    xmin, ymin, xmax, ymax = region.bbox
    site_meta = {
        "region": {"key": region.key, "name": region.name, "bbox_bng": [xmin, ymin, xmax, ymax],
                   "corners": corners_lonlat(region), "center": list(bng_to_lonlat((xmin + xmax) / 2, (ymin + ymax) / 2)),
                   "heat_corners": heat_corners, "heat_lo_corners": heat_lo_corners,
                   "legal_corners": legal_corners,
                   "legal_res_m": LEGAL_RES, "cell_m": CELL},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": meta,
        "profiles": {k: {"label": p.label, "drone": p.drone, "category": p.category,
                         "weights": p.weights, "radius_m": (p.radius_cells + 0.5) * CELL,
                         "components": {c: scoring.COMPONENT_TEXT.get(c, c) for c in p.weights}}
                     for k, p in scoring.PROFILES.items()},
        "legal_codes": {
            str(CODE_CLEAR): "Clear of restriction zones; OK for A3 distances",
            str(CODE_FRZ): "Flight Restriction Zone: no flying without permission from the airport/prison",
            str(CODE_RESTRICTED): "Prohibited or restricted area",
            str(CODE_A3_RES): "Within 150 m of housing (A3 no-go; fine for a sub-250 g whoop)",
            str(CODE_A3_COM): "Within 150 m of commercial/industrial land, a school or a hospital (A3 no-go)",
            str(CODE_A3_REC): "Within 150 m of a park or recreational area (A3 no-go)",
            str(CODE_A3_BLD): "Within 50 m of a building (A3 no-go)",
            str(CODE_A3_ROAD): "Within 50 m of a busy road (A3 no-go)",
        },
        "flag_bits": {str(b): t for b, t in legal.FLAG_TEXT.items()},
        "seeds": seed_out,
        "preview": {"size_m": PREVIEW_M, "res_m": PREVIEW_RES,
                    "classes": ["ground", "water", "road", "path", "tree", "building"]},
    }
    (out / "meta.json").write_text(json.dumps(site_meta, indent=2))
