"""Score every 50 m cell for each drone profile, then pick the best spots.

Two ingredients, blended:
- rules: what FPV pilots look for, written down as numbers (gaps, lone trees,
  tree lines, open space, relief, bandos... for the 5 inch; architecture,
  whoop-sized gaps, water and bridges for the 85 mm);
- taste: how much a place resembles your seed spots, on the same features.
Legality is a gate, not a weight: illegal cells never become spots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

CELL = 50
TASTE = 0.4  # share of the final score that is "like your seed spots"; the rest is the rules
ANCHOR_REACH = 0.85   # a spot's pin moves onto a viaduct, landmark... up to this much of the spacing away
ANCHOR_MARGIN = 0.06  # ...if it scores at most this much less there
ANCHOR_PULL = 0.01    # ...and each step up in subject (beach, bando, bridge, icon) counts as this much more score


def sat(x, k):
    """0..1, saturating: k is the value that gets you ~63% of the way."""
    return 1.0 - np.exp(-np.maximum(x, 0) / k)


def ramp(x, lo, hi):
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def near(dist_m, full, zero):
    """1 when closer than `full` metres, fading to 0 at `zero` metres."""
    return np.clip((zero - dist_m) / (zero - full), 0.0, 1.0)


def win_sum(a, r):
    k = 2 * r + 1
    return ndimage.uniform_filter(a.astype("float64"), size=k, mode="constant") * k * k


def win_mean(a, r):
    return ndimage.uniform_filter(a.astype("float64"), size=2 * r + 1, mode="nearest")


def win_max(a, r):
    return ndimage.maximum_filter(a, size=2 * r + 1, mode="nearest")


def win_min(a, r):
    return ndimage.minimum_filter(a, size=2 * r + 1, mode="nearest")


def win_or(a, r):
    """Bitwise OR over the window (a max filter would lose bits)."""
    a = a.astype("int64")
    p = np.pad(a, r)
    out = np.zeros_like(a)
    h, w = a.shape
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= p[dy:dy + h, dx:dx + w]
    return out


@dataclass
class Profile:
    key: str
    label: str
    short: str                 # name on the phone's drone switch
    drone: str
    category: str
    radius_cells: int          # neighbourhood that makes up "a spot"
    spacing_m: float           # minimum distance between reported spots
    legal_key: str             # which legal fraction gates it
    legal_min: float           # ...at the spot's own cell
    legal_window_min: float = 0.0  # ...averaged over the whole spot
    weights: dict[str, float] = field(default_factory=dict)
    style_features: list[str] = field(default_factory=list)
    anchors: dict[str, float] = field(default_factory=dict)  # what a pin snaps onto: distance feature -> metres,
                                                              # strongest subject first

    def anchor(self, f: dict[str, np.ndarray]) -> np.ndarray | None:
        """Cells on something worth flying, for pick_spots: 0 for none, else higher for a stronger subject."""
        if not self.anchors:
            return None
        out = np.zeros(next(iter(f.values())).shape, dtype="int8")
        for i, (k, m) in enumerate(self.anchors.items()):
            if k in f:
                out = np.maximum(out, np.where(f[k] <= m, len(self.anchors) - i, 0).astype("int8"))
        return out


PROFILES = {
    "freestyle5": Profile(
        key="freestyle5", label="5\" freestyle", short="5\"", drone="5 inch, 800 g (no class mark)",
        category="Open A3", radius_cells=2, spacing_m=400, legal_key="a3_frac", legal_min=0.6, legal_window_min=0.5,
        # "flow" is the best run you can chain through the spot; gaps, trees and edges still count on their own.
        # Viaducts and bridges are dive and gap spots in their own right, and a derelict building still
        # standing is the classic freestyle spot; public land is where you can turn up.
        weights={"flow": 0.22, "gaps": 0.12, "lone": 0.08, "edges": 0.10, "space": 0.10, "mix": 0.06, "vertical": 0.08,
                 "relief": 0.10, "bando": 0.16, "water": 0.04, "bridge": 0.12, "beach": 0.08, "public": 0.12, "sunset": 0.04,
                 "busy": -0.15, "hazard": -0.2, "permission": -0.08, "wet": -0.2,
                 "remote": -0.10, "farm": -0.05, "road": -0.12},
        style_features=["flow", "gaps", "lone", "edges", "space", "mix", "vertical", "relief", "water", "public"],
        anchors={"dist_bridge": 50, "dist_bando": 50, "dist_landmark": 50, "dist_beach": 25},
    ),
    "cine5": Profile(
        key="cine5", label="5\" cinematic", short="5\" cine", drone="5 inch, 800 g (no class mark)",
        category="Open A3", radius_cells=3, spacing_m=600, legal_key="a3_frac", legal_min=0.6, legal_window_min=0.5,
        # Long smooth lines over big scenery (rivers, coast, hills, a sunset to fly into) round a subject: a
        # lighthouse, castle, viaduct, bridge or ruin. Busy only costs a little: you film at first light.
        weights={"water": 0.18, "relief": 0.16, "bridge": 0.14, "landmark": 0.16, "sunset": 0.10, "space": 0.10,
                 "edges": 0.08, "bando": 0.08, "coast": 0.16,
                 "busy": -0.06, "hazard": -0.2, "permission": -0.08, "wet": -0.2, "remote": -0.08, "road": -0.10},
        style_features=["water", "relief", "bridge", "landmark", "sunset", "space", "edges", "bando"],
        anchors={"dist_icon": 50, "dist_bridge": 50, "dist_bando": 50, "dist_beach": 25},
    ),
    "cine85": Profile(
        key="cine85", label="85 mm cinematic", short="85 mm", drone="85 mm whoop, under 250 g", category="Open A1",
        radius_cells=1, spacing_m=250, legal_key="a1_frac", legal_min=0.9,
        # Busy places only cost a little: a whoop at a quiet hour is how the Helix gets flown.
        weights={"architecture": 0.24, "flow": 0.14, "gaps": 0.06, "trees": 0.14, "under": 0.03, "water": 0.14,
                 "bridge": 0.12, "landmark": 0.10, "plaza": 0.06, "relief": 0.04, "bando": 0.06, "sunset": 0.04,
                 "public": 0.04,
                 "busy": -0.06, "crowds": -0.08, "permission": -0.08, "wet": -0.2, "remote": -0.04},
        style_features=["architecture", "flow", "gaps", "trees", "under", "water", "bridge", "plaza", "relief"],
        anchors={"dist_bridge": 25, "dist_landmark": 25},
    ),
}

COMPONENT_TEXT = {
    "flow": "a run you can chain: gaps, orbits, dives and skims with clear air between",
    "gaps": "gaps between trees or buildings to fly through",
    "lone": "lone trees or pillars to orbit and power-loop",
    "edges": "tree lines and edges to skim along",
    "space": "open ground to carry speed",
    "mix": "a good mix of obstacles and open space",
    "vertical": "tall stuff to dive and climb",
    "relief": "slopes, banks or cliffs to dive",
    "bando": "abandoned / ruined structures nearby",
    "water": "water nearby for reflections",
    "bridge": "a bridge or viaduct nearby",
    "beach": "a beach: open sand to carry speed along the water",
    "coast": "the coast: a beach or the sea in shot",
    "architecture": "interesting buildings and edges",
    "trees": "trees to weave through",
    "under": "tall, see-through trees that may have room underneath",
    "landmark": "a landmark in shot",
    "plaza": "open plazas between buildings",
    "sunset": "a clear view of the sunset or sunrise for golden hour",
    "public": "public open space to fly from",
    "busy": "busy with people",
    "hazard": "power lines, masts or cranes close by",
    "permission": "land that needs the owner's permission",
    "wet": "mostly water",
    "remote": "a long walk from any path or road",
    "farm": "farmland: crops or livestock, ask the farmer",
    "road": "a fast main road right next to it",
    "crowds": "a crowded place",
}


def components(f: dict[str, np.ndarray], p: Profile, legal_only: bool = True) -> dict[str, np.ndarray]:
    """Component scores per cell. With legal_only, the A3 profiles only count features in the part of
    the neighbourhood they may actually fly; without, it's the place's style regardless of rules,
    which is what the taste match compares (your seed spots needn't be A3-legal)."""
    r = p.radius_cells
    flags = f["flags"].astype("int64")
    from . import legal

    if p.legal_key == "a3_frac" and legal_only:
        L = f["a3_frac"].astype("float64")
        f = dict(f)
        for k in ("gaps", "gq5", "lone", "edge_tree_m", "edge_bld_m", "treeline_m", "open_frac", "line5"):
            f[k] = f[k] * L
        for k in ("h_p90", "relief_250", "steep_frac"):
            f[k] = np.where(L >= 0.5, f[k], 0.0)

    c: dict[str, np.ndarray] = {}
    perm_bits = (legal.FLAG_NATIONAL_TRUST | legal.FLAG_FORESTRY | legal.FLAG_WOODLAND_TRUST
                 | legal.FLAG_NATURE_RESERVE | legal.FLAG_CEMETERY | legal.FLAG_GOLF | legal.FLAG_QUARRY)
    c["permission"] = ((win_or(flags, r) & perm_bits) > 0).astype("float64")
    # Power lines and main roads: full marks off if the middle of the spot is on one, otherwise up to half,
    # by how much of the spot is near one. One crossing a corner doesn't sink a viaduct.
    share = lambda d, full, zero: np.maximum(near(d, full, zero), 0.5 * win_mean(near(d, full, zero), r))
    # The things a spot is about (a bridge, a bando, a beach, a landmark) are measured from its middle,
    # so the best cell, and the pin, is on the viaduct rather than in the field next to it.
    wet = ramp(np.clip(f["water_frac"] - f.get("beach_frac", 0), 0, 1), 0.3, 0.8)  # the beach isn't the sea
    # Golden hour: either you look down on the sunset (or the sunrise: the coast here faces east), or
    # there's a tree line / wall facing the sunset with open ground in front to film from.
    c["sunset"] = np.maximum.reduce([ramp(-win_min(f["sunset_horizon"], r), 0.0, 3.0),
                                     ramp(-win_min(f["sunrise_horizon"], r), 0.0, 3.0),
                                     0.6 * sat(win_sum(f["edge_w_m"], r), 600 * (2 * r + 1))])
    c["remote"] = ramp(win_min(f["dist_access"], r), 250, 600)
    c["hazard"] = share(f["dist_hazard"], 15, 50)
    if p.key == "freestyle5":
        c["flow"] = ramp(win_max(f["line5"], r), 2, 10)
        c["gaps"] = sat(win_sum(f["gq5"], r), 20)
        c["lone"] = sat(win_sum(f["lone"] * ramp(f["lone_h_max"], 5, 12), r), 12)
        c["edges"] = 0.6 * sat(win_sum(f["treeline_m"], r), 250) + 0.4 * sat(win_sum(f["edge_tree_m"] + f["edge_bld_m"], r), 700)
        open_m = win_mean(f["open_frac"], r)
        tall_m = win_mean(f["tall_frac"], r)
        c["space"] = 0.5 * ramp(open_m, 0.3, 0.7) + 0.5 * ramp(win_max(f["open_r_m"], r), 50, 200)
        c["mix"] = np.clip(1 - np.abs(tall_m - 0.25) / 0.25, 0, 1)
        # Height only counts with open air beside it to dive into: tree faces at a wood's edge, a grassy
        # hill. Inside a wood, or down a valley side that's trees all the way, it's canopy to hop over.
        c["vertical"] = ramp(win_max(f["h_p90"] * ramp(f["open_frac"], 0.15, 0.4), r), 10, 25)
        dive_air = ramp(f["open_frac"], 0.2, 0.5)
        c["relief"] = np.maximum(ramp(win_max(f["relief_250"] * dive_air, r), 8, 45),
                                 0.6 * win_max(f["steep_frac"] * dive_air, r) ** 0.5)
        c["bando"] = near(f["dist_bando"], 30, 350)  # a derelict building LiDAR says is still standing
        c["water"] = near(win_min(f["dist_water"], r), 50, 250)
        c["bridge"] = near(f["dist_bridge"], 20, 250)
        c["beach"] = near(f["dist_beach"], 0, 200)
        c["public"] = ramp(win_mean(np.maximum(f["public_frac"], f["beach_frac"]), r), 0.1, 0.5)
        c["busy"] = sat(win_sum(f["places_n"], r + 1), 3) * 0.6 + sat(win_sum(f["path_frac"], r), 1.5) * 0.4
        c["wet"] = wet
        c["farm"] = ramp(win_mean(f["farm_frac"], r), 0.3, 0.9)
        c["road"] = share(f["dist_road_busy"], 30, 120)
    elif p.key == "cine5":
        # a river or the sea in shot, and how much of it: a big river or the sea fills the frame, a burn doesn't
        c["water"] = (0.6 * near(win_min(f["dist_water"], r), 30, 300)
                      + 0.4 * ramp(win_mean(np.clip(f["water_frac"] - f["beach_frac"], 0, 1), r), 0.03, 0.2))
        c["relief"] = np.maximum(ramp(win_max(f["relief_250"], r), 10, 60), 0.7 * win_max(f["steep_frac"], r) ** 0.5)
        c["bridge"] = near(f["dist_bridge"], 60, 450)
        # a named lighthouse, castle, monument, pier... counts in full; a viewpoint or water tower only partly
        c["landmark"] = np.maximum(near(f["dist_icon"], 60, 600), 0.6 * near(f["dist_landmark"], 80, 550))
        c["space"] = ramp(win_max(f["open_r_m"], r), 75, 300)
        c["edges"] = sat(win_sum(f["treeline_m"], r), 400)
        c["bando"] = near(f["dist_bando"], 40, 400)
        # a beach by the sea (a river's shingle isn't the coast), or the sea itself
        c["coast"] = np.maximum(near(f["dist_beach"], 0, 300) * near(f["dist_sea"], 300, 1000), near(f["dist_sea"], 50, 500))
        c["busy"] = sat(win_sum(f["places_n"], r + 1), 4) * 0.6 + sat(win_sum(f["path_frac"], r), 2.5) * 0.4
        c["wet"] = wet
        # filming from the air, a main road beside the subject only matters as far as it fills the spot
        c["road"] = win_mean(near(f["dist_road_busy"], 30, 120), r)
    else:
        c["architecture"] = (0.5 * sat(win_sum(f["edge_bld_m"], r), 300)
                             + 0.25 * ramp(win_max(f["bld_h_std"], r), 2, 10)
                             + 0.25 * ramp(win_max(f["bld_h_max"], r), 10, 30))
        c["flow"] = ramp(win_max(f["linew"], r), 1.5, 8)
        c["gaps"] = sat(win_sum(f["gqw"], r), 10)
        c["trees"] = sat(win_sum(f["edge_tree_m"], r), 300) * ramp(win_max(f["tree_h_p90"], r), 4, 12)
        # First-minus-last return only says light gets through the crown, not where the branches start,
        # so this is a hint, kept small, and only for tall trees (low bushy ones are never "under-able").
        c["under"] = (ramp(win_mean(f["under_frac"], r), 0.2, 0.6) * ramp(win_mean(f["tree_frac"], r), 0.05, 0.3)
                      * ramp(win_max(f["tree_h_p90"], r), 12, 18))
        c["water"] = near(win_min(f["dist_water"], r), 20, 150)
        c["bridge"] = np.maximum(near(win_min(f["dist_bridge"], r), 40, 200),
                                 0.5 * near(win_min(f["dist_footbridge"], r), 15, 60))
        c["landmark"] = near(win_min(f["dist_landmark"], r), 50, 250)
        bld_m = win_mean(f["bld_frac"], r)
        c["plaza"] = ramp(win_mean(f["open_frac"], r), 0.2, 0.5) * ramp(bld_m, 0.05, 0.25)
        c["relief"] = ramp(win_max(f["relief_250"], r), 8, 40)
        c["bando"] = near(win_min(f["dist_bando"], r), 40, 200)
        c["public"] = ramp(win_mean(f["public_frac"], r), 0.1, 0.5)
        c["busy"] = sat(win_sum(f["places_n"], r + 1), 6) * 0.7 + sat(win_sum(f["road_frac"], r), 2) * 0.3
        c["crowds"] = ((win_or(flags, r) & legal.FLAG_CROWDS) > 0).astype("float64")
        c["wet"] = ramp(f["water_frac"], 0.5, 0.9)
    return c


def rule_score(c: dict[str, np.ndarray], p: Profile) -> np.ndarray:
    pos = sum(w for w in p.weights.values() if w > 0)
    s = sum(p.weights[k] * c[k] for k in p.weights)
    return np.clip(s / pos, 0, 1)


def similarity(c: dict[str, np.ndarray], p: Profile, groups, valid: np.ndarray) -> np.ndarray | None:
    """0..1, how close each cell's style is to the nearest seed (robust z-space).

    `groups` is a list of seeds, each a list of (row, col) cells or, for a seed in another region, its
    mean raw style vector (see `style_vector`); a flat list of cells is one seed.
    Each seed is compared on its own, so two different favourite spots don't blur into a third style."""
    if not groups:
        return None
    if isinstance(groups[0], tuple):
        groups = [groups]
    feats = np.stack([c[k] for k in p.style_features], axis=-1)
    v = feats[valid]
    med = np.median(v, axis=0)
    iqr = np.subtract(*np.percentile(v, [90, 10], axis=0))
    iqr = np.where(iqr < 1e-6, 1.0, iqr)
    z = (feats - med) / iqr
    best = None
    for cells in groups:
        if isinstance(cells, np.ndarray):
            seed = (cells - med) / iqr  # raw features are in the same units in every region
        elif not cells:
            continue
        else:
            seed = np.mean([z[r, cc] for r, cc in cells], axis=0)
        d = np.sqrt(((z - seed) ** 2).mean(axis=-1))
        scale = np.median(d[valid])
        sim = 1.0 / (1.0 + (d / max(scale, 1e-6)) ** 2)
        best = sim if best is None else np.maximum(best, sim)
    return best


def pct_rank(a: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros_like(a, dtype="float64")
    vals = a[valid]
    order = vals.argsort().argsort()
    out[valid] = order / max(len(vals) - 1, 1)
    return out


def pick_spots(score: np.ndarray, eligible: np.ndarray, spacing_m: float, n: int,
               anchor: np.ndarray | None = None, anchor_margin: float = ANCHOR_MARGIN) -> list[tuple[int, int]]:
    """Greedy non-max suppression on the score grid, best first.

    `anchor` marks cells on something worth flying (a viaduct, landmark, bando, beach), higher for a
    stronger subject. When one is within reach of a spot and scores nearly as well, the pin goes there,
    with a stronger subject pulling a little harder: the best cell of a spot is often the field next to
    the viaduct, or the beach below the lighthouse, but the viaduct or the lighthouse is the spot."""
    cand = np.argwhere(eligible)
    order = np.argsort(-score[eligible])
    picked: list[tuple[int, int]] = []
    min_d = spacing_m / CELL
    taken = np.zeros_like(eligible)
    rad = int(np.ceil(min_d))
    yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    disk = (yy ** 2 + xx ** 2) <= min_d ** 2
    reach = (yy ** 2 + xx ** 2) <= (ANCHOR_REACH * min_d) ** 2
    on = np.where(eligible, anchor, 0) if anchor is not None else None
    H, W = score.shape

    def window(r, c):
        r0, r1 = max(r - rad, 0), min(r + rad + 1, H)
        c0, c1 = max(c - rad, 0), min(c + rad + 1, W)
        return (slice(r0, r1), slice(c0, c1)), (slice(r0 - r + rad, r1 - r + rad), slice(c0 - c + rad, c1 - c + rad))

    for i in order:
        r, c = cand[i]
        if taken[r, c]:
            continue
        win, k = window(r, c)
        pin = (int(r), int(c))
        if on is not None:
            ok = (on[win] > 0) & reach[k] & ~taken[win] & (score[win] >= score[r, c] - anchor_margin)
            if ok.any():
                j = np.argmax(np.where(ok, score[win] + ANCHOR_PULL * on[win], -np.inf))
                pin = (win[0].start + j // ok.shape[1], win[1].start + j % ok.shape[1])
        picked.append(pin)
        taken[win] |= disk[k]
        if pin != (r, c):
            win, k = window(*pin)
            taken[win] |= disk[k]
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda rc: -score[rc])


def style_vector(c: dict[str, np.ndarray], p: Profile, cells) -> np.ndarray:
    """A seed's mean raw style features over its cells, to carry it into another region."""
    feats = np.stack([c[k] for k in p.style_features], axis=-1)
    return np.mean([feats[r, cc] for r, cc in cells], axis=0)


def blend(rule_n: np.ndarray, sim: np.ndarray | None) -> np.ndarray:
    """The final score: rules, plus taste when there are seed spots or ratings to compare with."""
    return rule_n if sim is None else (1 - TASTE) * rule_n + TASTE * sim


def soft_cap(x: np.ndarray, knee: float = 0.9) -> np.ndarray:
    """Clip to 0..1 but bend smoothly above `knee`, so places better than the reference region's best
    still rank above each other instead of all tying at 100."""
    x = np.clip(x, 0, None)
    return np.where(x <= knee, x, knee + (1 - knee) * (1 - np.exp(-(x - knee) / (1 - knee))))
