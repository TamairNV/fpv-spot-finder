"""Flyable lines: what makes a spot fun is a run you can chain together, not how many trees it has.

Every gap you can fly through, lone tree to orbit, tall thing to dive and stretch of tree line to
skim is a node. Two nodes are linked when they're a sensible distance apart and the air between
them is clear at low height (the 1 m height grid says nothing taller than ~3 m is in the way).
From each node we walk the links greedily, preferring good obstacles, a change of move and a
smooth heading, and keep the best run. A spot's line score is the best run that starts in it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

# Node kinds and what each is worth in a run.
GAP, LONE, DIVE, SKIM = 0, 1, 2, 3
KIND_NAME = {GAP: "gap", LONE: "orbit", DIVE: "dive", SKIM: "skim"}
VALUE = {GAP: 1.0, LONE: 0.8, DIVE: 1.0, SKIM: 0.45}

CLEAR_H = 3.0       # flying low between obstacles: anything taller than this blocks a link
END_SKIP = 6.0      # metres at each end of a link that belong to a gap or tree line (trees use their crown)
SKIM_STEP = 25.0    # one skim node per this many metres of tree line
DIVE_H = 18.0       # a lone tree or pillar this tall is a dive target too
MAX_TURN = 115.0    # degrees: sharper than this and it's not a flowing line
MAX_NODES = 12


@dataclass(frozen=True)
class Style:
    min_link: float
    max_link: float
    gap_max_w: float
    corridor: int  # metres of clear air across the flight path (a quad plus margin)


FREESTYLE = Style(min_link=10.0, max_link=70.0, gap_max_w=12.0, corridor=3)
WHOOP = Style(min_link=5.0, max_link=35.0, gap_max_w=5.0, corridor=3)


def nodes_from(gaps: list[tuple], lone: list[tuple], treeline: np.ndarray, style: Style) -> np.ndarray:
    """(N, 5) array of row, col, kind, quality (0..1), radius (m around it that's the obstacle itself),
    in tile pixels (1 m)."""
    out = []
    for r, c, w, f, _a, th, _tf in gaps:
        if th and w <= style.gap_max_w:
            out.append((r, c, GAP, float(np.clip((f - 4) / 8, 0.2, 1.0)), END_SKIP))
    for r, c, h, a in lone:
        crown = float(np.sqrt(a / np.pi)) + style.corridor / 2 + 1.0
        out.append((r, c, DIVE if h >= DIVE_H else LONE, float(np.clip((h - 4) / 10, 0.2, 1.0)), max(crown, END_SKIP)))
    rr, cc = np.nonzero(treeline)
    if len(rr):
        # thin to one point per SKIM_STEP square so a long tree line gives a chain of skim nodes
        key = (rr // SKIM_STEP).astype(np.int64) * 100000 + (cc // SKIM_STEP).astype(np.int64)
        _, first = np.unique(key, return_index=True)
        out.extend((float(rr[i]), float(cc[i]), SKIM, 1.0, END_SKIP) for i in first)
    return np.array(out, dtype="float64").reshape(-1, 5)


def links(nodes: np.ndarray, ohm: np.ndarray, style: Style) -> list[list[int]]:
    """Neighbour lists: pairs within range with clear air between them."""
    n = len(nodes)
    nb: list[list[int]] = [[] for _ in range(n)]
    if n < 2:
        return nb
    pairs = cKDTree(nodes[:, :2]).query_pairs(style.max_link, output_type="ndarray")
    if not len(pairs):
        return nb
    a, b = nodes[pairs[:, 0], :2], nodes[pairs[:, 1], :2]
    d = np.hypot(*(b - a).T)
    keep = d >= style.min_link
    pairs, a, b, d = pairs[keep], a[keep], b[keep], d[keep]
    if not len(pairs):
        return nb
    # Sample every metre along each link, skipping the ends, and check nothing tall is in the way across
    # the whole corridor: LiDAR speckle inside a wood leaves 1 m holes that aren't a way through.
    ohm = ndimage.maximum_filter(ohm, size=style.corridor)
    steps = np.arange(0.0, style.max_link + 1.0)
    t = steps[None, :] / d[:, None]
    ra = nodes[pairs[:, 0], 4][:, None]
    rb = nodes[pairs[:, 1], 4][:, None]
    inside = (steps[None, :] >= ra) & (steps[None, :] <= d[:, None] - rb)
    pr = a[:, :1] + t * (b[:, :1] - a[:, :1])
    pc = a[:, 1:2] + t * (b[:, 1:2] - a[:, 1:2])
    h, w = ohm.shape
    ri = np.clip(np.rint(pr).astype(int), 0, h - 1)
    ci = np.clip(np.rint(pc).astype(int), 0, w - 1)
    blocked = ((ohm[ri, ci] >= CLEAR_H) & inside).any(axis=1)
    for i, j in pairs[~blocked]:
        nb[i].append(int(j))
        nb[j].append(int(i))
    return nb


def best_run(start: int, nodes: np.ndarray, nb: list[list[int]]) -> tuple[float, list[int]]:
    """Greedy walk from one node: best next obstacle by value, variety and a smooth heading."""
    path = [start]
    seen = {start}
    score = VALUE[int(nodes[start, 2])] * nodes[start, 3]
    heading = None
    kinds = {int(nodes[start, 2])}
    while len(path) < MAX_NODES:
        cur = path[-1]
        best, best_v, best_h = None, 0.0, None
        for j in nb[cur]:
            if j in seen:
                continue
            dv = nodes[j, :2] - nodes[cur, :2]
            hd = np.degrees(np.arctan2(dv[1], -dv[0]))
            if heading is not None:
                turn = abs((hd - heading + 180) % 360 - 180)
                if turn > MAX_TURN:
                    continue
                smooth = 1.0 - 0.3 * turn / MAX_TURN
            else:
                smooth = 1.0
            k = int(nodes[j, 2])
            v = VALUE[k] * nodes[j, 3] * smooth
            if k != int(nodes[cur, 2]):
                v *= 1.25  # a change of move: gap, then orbit, then a skim...
            if v > best_v:
                best, best_v, best_h = j, v, hd
        if best is None:
            break
        path.append(best)
        seen.add(best)
        kinds.add(int(nodes[best, 2]))
        score += best_v
        heading = best_h
    # Variety: a run of four different moves beats twelve skims along one hedge.
    score *= 1.0 + 0.15 * (len(kinds) - 1)
    return float(score), path


def all_runs(nodes: np.ndarray, nb: list[list[int]]) -> tuple[np.ndarray, list[list[int]]]:
    scores = np.zeros(len(nodes))
    paths: list[list[int]] = []
    for i in range(len(nodes)):
        if nb[i]:
            scores[i], p = best_run(i, nodes, nb)
        else:
            scores[i], p = VALUE[int(nodes[i, 2])] * nodes[i, 3], [i]
        paths.append(p)
    return scores, paths


def path_length(nodes: np.ndarray, path: list[int]) -> float:
    xy = nodes[path, :2]
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(path) > 1 else 0.0
