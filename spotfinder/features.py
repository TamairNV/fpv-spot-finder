"""Turn 1 m heights into the things an FPV pilot actually looks for.

Per 50 m cell we count gaps you can fly through, lone trees to power-loop,
tree lines and building edges to skim, open ground to carry speed, and so on.
Everything is geometry on the height grid; no imagery, no labels.
"""

from __future__ import annotations

import logging
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import ndimage
from tqdm import tqdm

from .config import Region
from . import lines
from .heights import HeightReader, Tile
from .rasterize import KEEP_OFF, VIADUCT_CODE

try:
    import edt as _edt_mod
except ImportError:
    _edt_mod = None

log = logging.getLogger(__name__)

CELL = 50        # metres per output cell
TILE = 1000      # metres per processing tile (core)
MARGIN = 40      # metres of context around each tile
ARCH_W = 8.0     # how wide we take a viaduct arch to be, for the gap counts

TALL_M = 4.0     # anything this tall is an obstacle to weave around
OPEN_M = 1.5     # below this counts as open ground
GAP_FILL_R = 5.0  # gaps up to ~2 x this wide get found
GAP_MIN_W = 2.0
GAP_MAX_W = 12.0
GAP_MIN_FLANK = 5.0
LONE_RING = 10.0
ORBIT_R = 10.0       # an orbit needs this much clear air all round the crown...
ORBIT_BLOCK_H = 3.0  # ...with nothing this tall in it (another tree, a bush, a wall)
ORBIT_MAX_BLOCKED = 0.03
THROUGH_R = 8     # metres out from a gap's middle that must be open on both sides to fly through it
TREELINE_MIN_LEN = 40.0  # a tree belt this long and...
TREELINE_MAX_W = 15.0    # ...this narrow is a tree line / hedgerow to skim

CELL_FEATURES = [
    "tall_frac", "open_frac", "tree_frac", "bld_frac", "water_frac", "road_frac", "path_frac", "lowobs_frac",
    "h_p90", "tree_h_p90", "bld_h_mean", "bld_h_max", "bld_h_std",
    "edge_tree_m", "edge_bld_m", "gaps", "gaps_whoop", "gap_flank_h", "lone", "lone_h_max",
    "under_frac", "lidar_frac", "gaps_all", "treeline_m", "edge_w_m",
    "line5", "line5_m", "line5_n", "linew", "linew_m", "linew_n",
]

_reader: HeightReader | None = None


def _init(region: Region, use_lidar: bool):
    global _reader
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    _reader = HeightReader(region, use_lidar=use_lidar)


def _blocks(a: np.ndarray, n: int) -> np.ndarray:
    """(H, W) -> (H/n, W/n, n*n) so we can reduce per cell."""
    h, w = a.shape
    return a.reshape(h // n, n, w // n, n).transpose(0, 2, 1, 3).reshape(h // n, w // n, n * n)


def _edt(mask: np.ndarray) -> np.ndarray:
    """Distance to the nearest False pixel; the edt package is the same transform, several times faster."""
    if _edt_mod is not None:
        return _edt_mod.edt(mask, black_border=False, parallel=1)
    return ndimage.distance_transform_edt(mask)


class _Labels:
    """Per-label count / max / mean / centre from ndimage.label output with bincount, which beats
    ndimage.sum/maximum/center_of_mass several times over (they sort or loop per call)."""

    def __init__(self, lab: np.ndarray, n: int):
        self.rows, self.cols = np.nonzero(lab)
        self.lab = lab[self.rows, self.cols]
        self.n = n
        self.count = np.bincount(self.lab, minlength=n + 1)[1:].astype("float64")

    def _vals(self, a):
        return a[self.rows, self.cols]

    def max(self, a: np.ndarray) -> np.ndarray:
        out = np.full(self.n + 1, -np.inf)
        np.maximum.at(out, self.lab, self._vals(a))
        return out[1:]

    def mean(self, a: np.ndarray) -> np.ndarray:
        return np.bincount(self.lab, weights=self._vals(a), minlength=self.n + 1)[1:] / self.count

    def centre(self) -> tuple[np.ndarray, np.ndarray]:
        w = lambda v: np.bincount(self.lab, weights=v, minlength=self.n + 1)[1:] / self.count
        return w(self.rows), w(self.cols)


@dataclass
class TileResult:
    ix: int
    iy: int
    cells: dict[str, np.ndarray]
    gaps: list[tuple]
    lone: list[tuple]


def analyse(t: Tile, margin: int = MARGIN, size: int = TILE, want_lines: bool = False):
    """Per-cell features, gaps and lone trees for one tile. With want_lines, also the flyable runs found
    in it: {style name: (nodes, scores, paths, keep-out mask, bends)} in tile pixels, plus "solid" (heights
    of everything but tree canopy) and "open" (open ground)."""
    ohm, trees = leaf_on(t)
    tall = ohm >= TALL_M
    road = t.ways == 1
    rail = t.ways == 3
    open_ = (ohm < OPEN_M) & ~t.water & ~road & ~rail
    lowobs = (ohm >= OPEN_M) & (ohm < TALL_M)

    # Distance from every non-tall pixel to the nearest tall one, and which tall pixel that is.
    dt_tall, inds = ndimage.distance_transform_edt(~tall, return_indices=True)

    # --- Gaps: morphological closing of the tall mask, minus the tall mask itself.
    dil = dt_tall <= GAP_FILL_R
    closed = _edt(dil) > GAP_FILL_R
    gap = closed & ~tall & (ohm < 2.5) & ~t.water & (dt_tall >= GAP_MIN_W / 2)
    glab, gn = ndimage.label(gap, structure=np.ones((3, 3)))
    gaps = []
    if gn:
        g = _Labels(glab, gn)
        area = g.count
        width = 2 * g.max(dt_tall)
        flank_src = ndimage.grey_dilation(np.where(tall, ohm, 0), size=(7, 7))
        flank = g.mean(flank_src)
        tree_frac = g.mean(ndimage.grey_dilation((trees >= TALL_M).astype("float32"), size=(7, 7)))
        com = list(zip(*g.centre()))
        through = _through(ohm < OPEN_M, com, ohm.shape)  # leaf_on already filled the canopy speckle
        for a, w, f, (r, c), th, tf in zip(area, width, flank, com, through, tree_frac):
            if 3 <= a <= 400 and GAP_MIN_W <= w <= GAP_MAX_W and f >= GAP_MIN_FLANK:
                gaps.append((r, c, float(w), float(f), float(a), bool(th), float(tf)))

    # --- Lone tall objects with open space all round (trees to power-loop, pillars, towers).
    tlab, tn = ndimage.label(tall, structure=np.ones((3, 3)))
    lone = []
    if tn:
        tl = _Labels(tlab, tn)
        area = tl.count
        hmax = tl.max(ohm)
        nearest = tlab[inds[0], inds[1]]
        ring = (dt_tall > 0) & (dt_tall <= LONE_RING)
        ring_n = np.bincount(nearest[ring], minlength=tn + 1)[1:]
        ring_open = np.bincount(nearest[ring & open_], minlength=tn + 1)[1:]
        cand = (area >= 4) & (area <= 300) & (hmax >= 5) & (ring_n > 0) & (ring_open >= 0.8 * np.maximum(ring_n, 1))
        # That ring only covers the ground closer to this crown than to any other, so a tree in a
        # loose clump passes it. An orbit needs a full circle of clear air round the crown.
        cand &= _orbit_clear(tlab, ohm, cand)
        if cand.any():
            cr, cc = tl.centre()
            for r, c, h, a in zip(cr[cand], cc[cand], hmax[cand], area[cand]):
                lone.append((r, c, float(h), float(a)))

    # --- Edges: tall pixels touching open ground (tree lines, building walls).
    nb_open = np.zeros_like(open_)
    nb_open[1:, :] |= open_[:-1, :]
    nb_open[:-1, :] |= open_[1:, :]
    nb_open[:, 1:] |= open_[:, :-1]
    nb_open[:, :-1] |= open_[:, 1:]
    edge = tall & nb_open
    is_tree = trees >= TALL_M
    edge_tree = edge & is_tree
    edge_bld = edge & t.bld
    # Edges lit by the setting sun with open ground in front to film from: open to the west.
    west_open = np.zeros_like(open_)
    west_open[:, 3:] = open_[:, :-3]
    edge_w = tall & west_open

    # --- Tree lines: long, narrow belts of trees (hedgerow trees, avenues).
    tree_tall = is_tree & ~t.bld
    blab, bn = ndimage.label(tree_tall, structure=np.ones((3, 3)))
    treeline = np.zeros_like(tree_tall)
    belt = np.zeros_like(tree_tall)
    if bn:
        objs = ndimage.find_objects(blab)
        b_area = np.bincount(blab.ravel(), minlength=bn + 1)[1:]
        line_ids = np.zeros(bn + 1, dtype=bool)
        for i, sl in enumerate(objs):
            if sl is None or b_area[i] < 40:
                continue
            length = float(np.hypot(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start))
            if length >= TREELINE_MIN_LEN and b_area[i] / length <= TREELINE_MAX_W:
                line_ids[i + 1] = True
        belt = line_ids[blab]
        treeline = edge_tree & belt
    # --- Wood edges: a wall of trees along a field, skimmed from the open a few metres off it. (A tree line's
    # skim nodes sit on the belt itself.)
    near_r, near_c = inds
    wall_h = np.where(tree_tall[near_r, near_c] & ~belt[near_r, near_c], trees[near_r, near_c], np.float32(0))
    lane = (lines.wood_lane(dt_tall, wall_h, open_), wall_h)

    # --- Under-canopy room (LiDAR only): last returns reach well below the canopy top.
    if t.porosity is not None:
        tree_px = trees >= 5
        under = tree_px & (np.nan_to_num(t.porosity) >= 0.5 * trees)
    else:
        tree_px = trees >= 5
        under = np.zeros_like(tree_px)

    m = margin
    core = (slice(m, m + size), slice(m, m + size))
    n = CELL
    B = lambda a: _blocks(a[core], n)
    mean = lambda a: B(a.astype("float32")).mean(axis=2)
    ssum = lambda a: B(a.astype("float32")).sum(axis=2)

    def pct(vals, mask, q):
        v = B(np.where(mask, vals, np.nan))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = np.nanpercentile(v, q, axis=2)
        return np.nan_to_num(out)

    def masked_stat(vals, mask, fn):
        v = B(np.where(mask, vals, np.nan))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return np.nan_to_num(fn(v, axis=2))

    cells = {
        "tall_frac": mean(tall), "open_frac": mean(open_), "tree_frac": mean(trees >= 3),
        "bld_frac": mean(t.bld), "water_frac": mean(t.water), "road_frac": mean(road),
        "path_frac": mean(t.ways == 2), "lowobs_frac": mean(lowobs),
        "h_p90": pct(ohm, tall, 90), "tree_h_p90": pct(trees, trees >= 3, 90),
        "bld_h_mean": masked_stat(t.bldh, t.bld, np.nanmean), "bld_h_max": masked_stat(t.bldh, t.bld, np.nanmax),
        "bld_h_std": masked_stat(t.bldh, t.bld, np.nanstd),
        "edge_tree_m": ssum(edge_tree), "edge_bld_m": ssum(edge_bld),
        "under_frac": np.nan_to_num(ssum(under) / np.maximum(ssum(tree_px), 1)),
        "lidar_frac": mean(t.lidar),
        "treeline_m": ssum(treeline), "edge_w_m": ssum(edge_w),
    }
    grid_n = size // n
    for key in ("gaps", "gaps_all", "gaps_whoop", "gap_flank_h", "lone", "lone_h_max"):
        cells[key] = np.zeros((grid_n, grid_n), dtype="float32")
    # Nothing in people's gardens, on car parks, in school grounds, playgrounds or by the tracks or a power line
    # counts, and a viaduct's arches are gaps too.
    home = np.isin(t.land, KEEP_OFF) if t.land is not None else None
    at_home = lambda r, c: home is not None and bool(home[min(int(round(r)), home.shape[0] - 1),
                                                         min(int(round(c)), home.shape[1] - 1)])
    gaps = [g for g in gaps if not at_home(g[0], g[1])]
    lone = [o for o in lone if not at_home(o[0], o[1])]
    arch = lines.arches(t.land == VIADUCT_CODE, t.ohm) if t.land is not None else []
    keep_gaps, keep_lone = [], []
    for r, c, w, f, a, th, tf in gaps + [(r, c, ARCH_W, h, ARCH_W * 10, True, 0.0) for r, c, h in arch]:
        rr, cc = r - m, c - m
        if 0 <= rr < size and 0 <= cc < size:
            i, j = int(rr // n), int(cc // n)
            cells["gaps_all"][i, j] += 1
            # Only gaps with open air on both sides count: a hole in a wood's canopy isn't a line.
            if th:
                cells["gaps"][i, j] += 1
                if w <= 6:
                    cells["gaps_whoop"][i, j] += 1
                cells["gap_flank_h"][i, j] = max(cells["gap_flank_h"][i, j], f)
            keep_gaps.append((t.x0 + c + 0.5, t.y1 - r - 0.5, w, f, a, th, tf))
    for r, c, h, a in lone:
        rr, cc = r - m, c - m
        if 0 <= rr < size and 0 <= cc < size:
            i, j = int(rr // n), int(cc // n)
            cells["lone"][i, j] += 1
            cells["lone_h_max"][i, j] = max(cells["lone_h_max"][i, j], h)
            keep_lone.append((t.x0 + c + 0.5, t.y1 - r - 0.5, h, a))
    # --- Flyable lines: chain the obstacles into runs and keep the best run starting in each cell.
    runs = {}
    # Over a river the heights are zeroed (bridges, water noise), but a line flown low along it still can't
    # go through the trees hanging over it.
    air = np.maximum(ohm, t.over_water) if t.over_water is not None else ohm
    d_bld = ndimage.distance_transform_edt(~t.bld) if t.bld.any() else None
    d_road = ndimage.distance_transform_edt(~road) if road.any() else None
    for name, style in (("line5", lines.FREESTYLE), ("linew", lines.WHOOP)):
        blocked = lines.keep_out(t.land, d_bld, d_road, style)
        nodes = lines.nodes_from(gaps, lone, treeline, style, blocked, arch, lane)
        via: dict = {}
        nb = lines.links(nodes, air, style, blocked, t.land == VIADUCT_CODE if t.land is not None else None, via)
        scores, paths = lines.all_runs(nodes, nb, via)
        runs[name] = (nodes, scores, paths, blocked, via)
        for k in (name, name + "_m", name + "_n"):
            cells[k] = np.zeros((grid_n, grid_n), dtype="float32")
        for i in np.argsort(scores):  # best last, so it wins the cell
            rr, cc = nodes[i, 0] - m, nodes[i, 1] - m
            if 0 <= rr < size and 0 <= cc < size:
                ci, cj = int(rr // n), int(cc // n)
                cells[name][ci, cj] = scores[i]
                cells[name + "_m"][ci, cj] = lines.path_length(nodes, paths[i], via)
                cells[name + "_n"][ci, cj] = len(paths[i])
    out = {k: v.astype("float32") for k, v in cells.items()}
    if want_lines:
        runs["solid"], runs["open"] = np.where(trees >= lines.CLEAR_H, np.float32(0), ohm), open_
        return out, keep_gaps, keep_lone, runs
    return out, keep_gaps, keep_lone


def leaf_on(t: Tile) -> tuple[np.ndarray, np.ndarray]:
    """Summer canopy from winter LiDAR. Much of the EA survey was flown with no leaves on, so bushy
    deciduous crowns come out speckled with 1-2 m holes that aren't there when you fly. Close the tree
    canopy by a metre (fills those holes, not real gaps, which are 2 m+ wide) and give the filled
    pixels the height of the crown round them. Only LiDAR pixels; the canopy model is already smooth."""
    canopy = t.trees >= 3.0
    if not t.lidar.any():
        return t.ohm, t.trees
    filled = ndimage.binary_closing(canopy, structure=np.ones((3, 3)), border_value=0) & ~canopy & t.lidar & ~t.bld
    if not filled.any():
        return t.ohm, t.trees
    crown = ndimage.grey_closing(t.trees, size=(3, 3))
    trees = np.where(filled, crown, t.trees)
    return np.maximum(t.ohm, trees), trees


def _orbit_clear(tlab: np.ndarray, ohm: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """For each candidate blob: is the band ORBIT_R m round its crown free of anything over ORBIT_BLOCK_H
    (other trees, bushes, walls)? A 3 m max filter stops LiDAR speckle in a hedge reading as gaps."""
    ok = np.zeros(len(cand), dtype=bool)
    blocked_all = ndimage.maximum_filter(ohm, size=3) >= ORBIT_BLOCK_H
    pad = int(ORBIT_R) + 2
    h, w = ohm.shape
    for i, sl in enumerate(ndimage.find_objects(tlab)):
        if sl is None or not cand[i]:
            continue
        r0, r1 = max(sl[0].start - pad, 0), min(sl[0].stop + pad, h)
        c0, c1 = max(sl[1].start - pad, 0), min(sl[1].stop + pad, w)
        own = tlab[r0:r1, c0:c1] == i + 1
        # the crown itself plus a metre of its own fringe, then the band round it
        d = ndimage.distance_transform_edt(~own)
        band = (d > 1.5) & (d <= ORBIT_R)
        blocked = blocked_all[r0:r1, c0:c1] & band
        ok[i] = blocked.sum() <= ORBIT_MAX_BLOCKED * max(band.sum(), 1)
    return ok


def _through(open_: np.ndarray, com, shape) -> np.ndarray:
    """For each gap centre: is there open ground THROUGH_R m out on both sides along some axis?"""
    if not com:
        return np.zeros(0, dtype=bool)
    rc = np.rint(np.array(com)).astype(int)
    h, w = shape
    ok = np.zeros(len(rc), dtype=bool)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        k = THROUGH_R if dy == 0 or dx == 0 else int(round(THROUGH_R / 1.414))
        hit = np.ones(len(rc), dtype=bool)
        for sgn in (1, -1):
            rr = np.clip(rc[:, 0] + sgn * dy * k, 0, h - 1)
            cc = np.clip(rc[:, 1] + sgn * dx * k, 0, w - 1)
            hit &= open_[rr, cc]
        ok |= hit
    return ok


def _work(args) -> TileResult:
    ix, iy, x0, y0 = args
    t = _reader.tile(x0, y0, TILE, MARGIN)
    cells, gaps, lone = analyse(t)
    return TileResult(ix, iy, cells, gaps, lone)


def run(region: Region, workers: int = 4, use_lidar: bool = True) -> tuple[dict[str, np.ndarray], pd.DataFrame, pd.DataFrame]:
    """Region-wide (rows, cols) arrays per feature at 50 m (row 0 = north), plus gap and lone-tree points."""
    xmin, ymin, xmax, ymax = region.bbox
    nx, ny = (xmax - xmin) // TILE, (ymax - ymin) // TILE
    per = TILE // CELL
    out = {k: np.zeros((ny * per, nx * per), dtype="float32") for k in CELL_FEATURES}
    jobs = [(ix, iy, xmin + ix * TILE, ymax - (iy + 1) * TILE) for iy in range(ny) for ix in range(nx)]
    gaps, lone = [], []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(region, use_lidar)) as pool:
        for res in tqdm(pool.map(_work, jobs, chunksize=4), total=len(jobs), desc="height features"):
            sl = (slice(res.iy * per, (res.iy + 1) * per), slice(res.ix * per, (res.ix + 1) * per))
            for k in CELL_FEATURES:
                out[k][sl] = res.cells[k]
            gaps.extend(res.gaps)
            lone.extend(res.lone)
    gaps_df = pd.DataFrame(gaps, columns=["x", "y", "width_m", "flank_h_m", "area_m2", "through", "tree_frac"])
    lone_df = pd.DataFrame(lone, columns=["x", "y", "height_m", "area_m2"])
    return out, gaps_df, lone_df
