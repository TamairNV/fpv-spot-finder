"""Shots for the 5" cine: a way to film what a spot is named after. A lighthouse, castle, monument, ruin or
sea stack gets an orbit, a bridge or viaduct a pass along its side at deck height, and cliffs a pass along
them level with the top. A shot keeps off the ground a line
keeps off (lines.keep_out: homes, car parks, roads, school grounds...) and clear of anything that sticks up
into it, so round a lighthouse with its keepers' cottages beside it the orbit comes out as an arc round the
sea side. A shot is flown level, so where the ground rises (a bridge's valley sides, the hill a monument stands
on) the ground and what's on it have to stay under it too. In tile pixels (1 m, rows run south), with bearings
in degrees clockwise from north, and heights in the frame of `ground` (the ground's height, m: above sea level
from the DTM, or none for flat ground at 0)."""

from __future__ import annotations

import numpy as np
import shapely
from scipy import ndimage

ORBIT_OUT = (25.0, 35.0, 50.0, 70.0)  # metres out from the subject's edge to try, closest first
ORBIT_MIN_DEG = 90.0    # less clear arc than this isn't an orbit...
ORBIT_GOOD_DEG = 180.0  # ...and the closest ring with this much is the one to fly
ORBIT_STEP_DEG = 3.0    # at most this between points checked round a ring, and at most STEP_M apart
SUBJECT_MAX_R = 60.0    # a subject wider than this (a castle's whole grounds) is too big to orbit
ORBIT_H = (20.0, 60.0)  # an orbit flies OVER_M over the subject's top, within these over its ground
PASS_H = (10.0, 60.0)   # a pass flies at the deck's height, within these over the lowest ground under it
OVER_M = 5.0            # anything within this of the shot's height sticks up into it
PASS_OUT = (25.0, 35.0, 50.0)  # metres off the side of a bridge, closest first
PASS_MIN_M = 60.0       # a clear stretch shorter than this isn't a pass
PASS_MAX_M = 400.0      # a long viaduct gets this much of it, round the spot
STEP_M = 2.0            # metres between the points checked along a shot (the corridor covers the rest)
CORRIDOR = 3            # metres across the flight path that have to be clear


def flyable(top: np.ndarray, blocked: np.ndarray | None, alt: float) -> np.ndarray:
    """Where a shot flown level at `alt` can go: off keep-off ground, with nothing within OVER_M of `alt` under
    it, across the whole corridor. `top` is the height of the top of everything (the ground plus the trees,
    buildings and decks over water on it)."""
    bad = top >= alt - OVER_M
    if blocked is not None:
        bad = bad | blocked
    return ~ndimage.binary_dilation(bad, structure=np.ones((CORRIDOR, CORRIDOR), bool))


def _at(ok: np.ndarray, r: np.ndarray, c: np.ndarray) -> np.ndarray:
    """`ok` at each point (row, col), False off the tile."""
    h, w = ok.shape
    ri, ci = np.rint(r).astype(int), np.rint(c).astype(int)
    inside = (ri >= 0) & (ri < h) & (ci >= 0) & (ci < w)
    out = np.zeros(len(ri), bool)
    out[inside] = ok[ri[inside], ci[inside]]
    return out


def longest_run(ok: np.ndarray, circular: bool = False) -> tuple[int, int]:
    """(start, length) of the longest run of True in `ok`, wrapping round from the end to the start if
    `circular`."""
    n = len(ok)
    if ok.all():
        return 0, n
    seq = np.concatenate([ok, ok]) if circular else ok
    best, start, cur = (0, 0), 0, 0
    for i, v in enumerate(seq):
        if not v:
            cur = 0
            continue
        if cur == 0:
            start = i
        cur += 1
        if cur > best[1]:
            best = (start, cur)
    return best[0] % n, min(best[1], n)


def _ground(air: np.ndarray, ground: np.ndarray | None) -> np.ndarray:
    return np.zeros_like(air) if ground is None else ground


def orbit(air: np.ndarray, blocked: np.ndarray | None, centre: tuple[float, float], subject_r: float,
          subject_h: float, ground: np.ndarray | None = None) -> dict | None:
    """An orbit round a subject at `centre` (row, col) whose edge is `subject_r` m out from it and whose top
    is `subject_h` m over the ground it stands on: {"r", "h" (over that ground), "alt", "from", "deg"}, flown
    level at "alt", clockwise from bearing "from" through "deg" degrees (360 all the way round). None if it's
    too big to orbit or no ring has ORBIT_MIN_DEG of clear arc."""
    if subject_r > SUBJECT_MAX_R:
        return None
    g = _ground(air, ground)
    h = float(np.clip(subject_h + OVER_M, *ORBIT_H))
    alt = float(g[int(round(centre[0])), int(round(centre[1]))]) + h
    ok = flyable(air + g, blocked, alt)
    best = None
    for out in ORBIT_OUT:
        r = float(round(subject_r + out))
        step = min(ORBIT_STEP_DEG, float(np.degrees(STEP_M / r)))
        bearings = np.arange(0.0, 360.0, step)
        rr = centre[0] - r * np.cos(np.radians(bearings))
        cc = centre[1] + r * np.sin(np.radians(bearings))
        start, n = longest_run(_at(ok, rr, cc), circular=True)
        deg = 360.0 if n == len(bearings) else max(n - 1, 0) * step
        if best is None or deg > best["deg"]:
            best = {"r": round(r), "h": round(h), "alt": alt, "from": float(bearings[start]), "deg": deg}
        if deg >= ORBIT_GOOD_DEG:
            break
    return best if best["deg"] >= ORBIT_MIN_DEG else None


def sample(a: np.ndarray, r: np.ndarray, c: np.ndarray) -> np.ndarray:
    """`a` at each point (row, col), clamped to the tile."""
    return a[np.clip(np.rint(r).astype(int), 0, a.shape[0] - 1), np.clip(np.rint(c).astype(int), 0, a.shape[1] - 1)]


def _resample(line: shapely.LineString, step: float) -> np.ndarray:
    """Points `step` apart along a line, both ends included, as (row, col) rows."""
    d = np.append(np.arange(0.0, line.length, step), line.length)
    return shapely.get_coordinates(shapely.line_interpolate_point(line, d))


def pass_along(air: np.ndarray, blocked: np.ndarray | None, line, deck: float,
               ground: np.ndarray | None = None) -> dict | None:
    """A pass beside something long, a bridge's deck or a line of cliffs, running along `line` ((row, col)
    points) with its top at `deck`: the longest clear stretch, PASS_MIN_M or more, of the lines PASS_OUT m off
    either side of it, flown level with the top (PASS_H over the lowest ground under it). As {"path" (its
    points, row/col, STEP_M apart), "look" (the nearest point of `line` to each, to film), "a", "b" (its ends),
    "off", "side" (the bearing from the line to its middle), "h" (over the lowest ground along it: the river,
    the valley floor, the beach), "alt", "len"}; None if none is."""
    line = shapely.LineString(np.asarray(line, float))
    if line.length < PASS_MIN_M:
        return None
    g = _ground(air, ground)
    axis = _resample(line, STEP_M)
    low = float(sample(g, axis[:, 0], axis[:, 1]).min())
    alt = low + float(np.clip(deck - low, *PASS_H))
    ok = flyable(air + g, blocked, alt)
    best = None
    for off in PASS_OUT:
        for side in (1.0, -1.0):
            curve = line.offset_curve(side * off)
            if curve.is_empty:
                continue
            pts = _resample(max(shapely.get_parts(curve), key=lambda q: q.length), STEP_M)
            start, k = longest_run(_at(ok, pts[:, 0], pts[:, 1]))
            run = max(k - 1, 0) * STEP_M
            if run >= PASS_MIN_M and (best is None or run > best["len"]):
                path = pts[start:start + k]
                look = shapely.get_coordinates(shapely.line_interpolate_point(
                    line, shapely.line_locate_point(line, shapely.points(path))))
                v = path[len(path) // 2] - look[len(path) // 2]
                under = sample(g, path[:, 0], path[:, 1]).min()
                best = {"path": path, "look": look, "a": tuple(path[0]), "b": tuple(path[-1]), "off": off,
                        "side": float(np.degrees(np.arctan2(v[1], -v[0])) % 360), "h": round(alt - float(under)),
                        "alt": alt, "len": round(run)}
    return best
