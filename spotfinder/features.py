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
from .heights import HeightReader, Tile

log = logging.getLogger(__name__)

CELL = 50        # metres per output cell
TILE = 1000      # metres per processing tile (core)
MARGIN = 40      # metres of context around each tile

TALL_M = 4.0     # anything this tall is an obstacle to weave around
OPEN_M = 1.5     # below this counts as open ground
GAP_FILL_R = 5.0  # gaps up to ~2 x this wide get found
GAP_MIN_W = 2.0
GAP_MAX_W = 12.0
GAP_MIN_FLANK = 5.0
LONE_RING = 10.0

CELL_FEATURES = [
    "tall_frac", "open_frac", "tree_frac", "bld_frac", "water_frac", "road_frac", "path_frac", "lowobs_frac",
    "h_p90", "tree_h_p90", "bld_h_mean", "bld_h_max", "bld_h_std",
    "edge_tree_m", "edge_bld_m", "gaps", "gaps_whoop", "gap_flank_h", "lone", "lone_h_max",
    "under_frac", "lidar_frac",
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


@dataclass
class TileResult:
    ix: int
    iy: int
    cells: dict[str, np.ndarray]
    gaps: list[tuple]
    lone: list[tuple]


def analyse(t: Tile, margin: int = MARGIN, size: int = TILE) -> tuple[dict, list, list]:
    ohm, trees = t.ohm, t.trees
    tall = ohm >= TALL_M
    road = t.ways == 1
    rail = t.ways == 3
    open_ = (ohm < OPEN_M) & ~t.water & ~road & ~rail
    lowobs = (ohm >= OPEN_M) & (ohm < TALL_M)

    # Distance from every non-tall pixel to the nearest tall one, and which tall pixel that is.
    dt_tall, inds = ndimage.distance_transform_edt(~tall, return_indices=True)

    # --- Gaps: morphological closing of the tall mask, minus the tall mask itself.
    dil = dt_tall <= GAP_FILL_R
    closed = ndimage.distance_transform_edt(dil) > GAP_FILL_R
    gap = closed & ~tall & (ohm < 2.5) & ~t.water & (dt_tall >= GAP_MIN_W / 2)
    glab, gn = ndimage.label(gap, structure=np.ones((3, 3)))
    gaps = []
    if gn:
        idx = np.arange(1, gn + 1)
        area = ndimage.sum(np.ones_like(ohm), glab, idx)
        width = 2 * ndimage.maximum(dt_tall, glab, idx)
        flank_src = ndimage.grey_dilation(np.where(tall, ohm, 0), size=(7, 7))
        flank = ndimage.mean(flank_src, glab, idx)
        com = ndimage.center_of_mass(gap, glab, idx)
        for a, w, f, (r, c) in zip(area, width, flank, com):
            if 3 <= a <= 400 and GAP_MIN_W <= w <= GAP_MAX_W and f >= GAP_MIN_FLANK:
                gaps.append((r, c, float(w), float(f), float(a)))

    # --- Lone tall objects with open space all round (trees to power-loop, pillars, towers).
    tlab, tn = ndimage.label(tall, structure=np.ones((3, 3)))
    lone = []
    if tn:
        idx = np.arange(1, tn + 1)
        area = ndimage.sum(np.ones_like(ohm), tlab, idx)
        hmax = ndimage.maximum(ohm, tlab, idx)
        nearest = tlab[inds[0], inds[1]]
        ring = (dt_tall > 0) & (dt_tall <= LONE_RING)
        ring_n = np.bincount(nearest[ring], minlength=tn + 1)[1:]
        ring_open = np.bincount(nearest[ring & open_], minlength=tn + 1)[1:]
        cand = (area >= 4) & (area <= 300) & (hmax >= 5) & (ring_n > 0) & (ring_open >= 0.8 * np.maximum(ring_n, 1))
        if cand.any():
            com = ndimage.center_of_mass(tall, tlab, idx[cand])
            for (r, c), h, a in zip(com, hmax[cand], area[cand]):
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
    }
    grid_n = size // n
    for key in ("gaps", "gaps_whoop", "gap_flank_h", "lone", "lone_h_max"):
        cells[key] = np.zeros((grid_n, grid_n), dtype="float32")
    keep_gaps, keep_lone = [], []
    for r, c, w, f, a in gaps:
        rr, cc = r - m, c - m
        if 0 <= rr < size and 0 <= cc < size:
            i, j = int(rr // n), int(cc // n)
            cells["gaps"][i, j] += 1
            if w <= 6:
                cells["gaps_whoop"][i, j] += 1
            cells["gap_flank_h"][i, j] = max(cells["gap_flank_h"][i, j], f)
            keep_gaps.append((t.x0 + c + 0.5, t.y1 - r - 0.5, w, f, a))
    for r, c, h, a in lone:
        rr, cc = r - m, c - m
        if 0 <= rr < size and 0 <= cc < size:
            i, j = int(rr // n), int(cc // n)
            cells["lone"][i, j] += 1
            cells["lone_h_max"][i, j] = max(cells["lone_h_max"][i, j], h)
            keep_lone.append((t.x0 + c + 0.5, t.y1 - r - 0.5, h, a))
    return {k: v.astype("float32") for k, v in cells.items()}, keep_gaps, keep_lone


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
    gaps_df = pd.DataFrame(gaps, columns=["x", "y", "width_m", "flank_h_m", "area_m2"])
    lone_df = pd.DataFrame(lone, columns=["x", "y", "height_m", "area_m2"])
    return out, gaps_df, lone_df
