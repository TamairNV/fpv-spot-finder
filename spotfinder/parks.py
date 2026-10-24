"""Public parks and commons. The ranking alone favours woods and riversides out of town, so every park
of a decent size also gets a spot at its best place, and spots inside a park are labelled with it."""

from __future__ import annotations

import logging

import numpy as np
import shapely
from rasterio import features as rfeatures
from rasterio.transform import from_origin

from .config import Region
from .sources import osm

log = logging.getLogger(__name__)

CELL = 50
MIN_AREA_M2 = 40_000  # 4 ha: room for a few laps
PARK_LEISURE = {"park", "common", "recreation_ground"}


def load(region: Region) -> list[tuple[str, shapely.Geometry]]:
    """Named public parks, commons and recreation grounds of 4 ha or more. Pieces with the same name
    that touch (OSM often splits a park) count as one park; same-named parks apart stay separate."""
    areas = osm.extract(region)["areas"]
    is_park = areas["tags"].map(lambda t: bool(t.get("name"))
                                and (t.get("leisure") in PARK_LEISURE or t.get("landuse") == "recreation_ground")
                                and t.get("access") not in {"private", "no"})
    p = areas[is_park]
    out = []
    for name, g in p.groupby(p["tags"].map(lambda t: t["name"])):
        whole = shapely.union_all(g.geometry.values)
        near = shapely.union_all([geom.buffer(60) for geom in g.geometry.values])
        for part in getattr(near, "geoms", [near]):
            geom = whole.intersection(part)
            if geom.area >= MIN_AREA_M2:
                out.append((name, geom))
    log.info("parks: %d named parks and commons of 4 ha or more", len(out))
    return out


def grid(region: Region, parks: list, shape: tuple[int, int]) -> np.ndarray:
    """Which park each 50 m cell's centre is in (index + 1, 0 = none). A park inside a bigger one wins."""
    xmin, _, _, ymax = region.bbox
    order = sorted(range(len(parks)), key=lambda i: -parks[i][1].area)
    if not order:
        return np.zeros(shape, dtype="int32")
    return rfeatures.rasterize(((parks[i][1], i + 1) for i in order), out_shape=shape,
                               transform=from_origin(xmin, ymax, CELL, CELL), fill=0, dtype="int32")


def name_at(park_grid: np.ndarray, parks: list, r: int, c: int) -> str | None:
    i = int(park_grid[r, c])
    return parks[i - 1][0] if i else None


def picks(park_grid: np.ndarray, parks: list, score: np.ndarray, eligible: np.ndarray,
          spots: list[tuple[int, int]], spacing_m: float) -> list[tuple[int, int]]:
    """The best eligible cell of each park that has no spot in it yet, at least half the usual spacing
    from every other spot. Best first."""
    have = {int(park_grid[r, c]) for r, c in spots}
    taken = np.array(spots, dtype=float).reshape(-1, 2)
    min_d = spacing_m / 2 / CELL
    rows, cols = np.nonzero((park_grid > 0) & eligible)
    ids = park_grid[rows, cols]
    out = []
    for i in range(1, len(parks) + 1):
        if i in have:
            continue
        sel = ids == i
        if not sel.any():
            continue
        rr, cc = rows[sel], cols[sel]
        for j in np.argsort(-score[rr, cc]):
            r, c = int(rr[j]), int(cc[j])
            if len(taken) and np.hypot(taken[:, 0] - r, taken[:, 1] - c).min() < min_d:
                continue
            out.append((r, c))
            taken = np.vstack([taken, [r, c]])
            break
    return sorted(out, key=lambda rc: -score[rc])
