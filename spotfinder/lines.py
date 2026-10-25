"""Flyable lines: what makes a spot fun is a run you can chain together, not how many trees it has.

Every gap you can fly through, lone tree to orbit, tall thing to dive and stretch of tree line or
wood's edge (for the whoop, a building's side too) to skim is a node. Two nodes are linked when they're a sensible distance apart and the
air between them is clear at low height (the 1 m height grid says nothing taller than ~3 m is in the
way), flying straight or with one bend round whatever's in the way. From each node we walk the links
greedily, preferring good obstacles, a change of move and a smooth heading, and keep the best run. A
spot's line score is the best run that starts in it.

Lines stay off people's homes and gardens, car parks, school and hospital grounds, playgrounds, roads,
working railways and power lines, and the 5 inch keeps clear of buildings altogether. A viaduct's
arches are obstacles too: LiDAR only sees the deck, so where the deck stands high above the ground
there's an arch every few metres to fly through, and a run can weave from one arch to the next under
the deck.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from .rasterize import KEEP_OFF

# Node kinds and what each is worth in a run.
GAP, LONE, DIVE, SKIM, ARCH = 0, 1, 2, 3, 4
KIND_NAME = {GAP: "gap", LONE: "orbit", DIVE: "dive", SKIM: "skim", ARCH: "arch"}
VALUE = {GAP: 1.0, LONE: 0.8, DIVE: 1.0, SKIM: 0.45, ARCH: 1.2}

CLEAR_H = 3.0       # flying low between obstacles: anything taller than this blocks a link
END_SKIP = 6.0      # metres at each end of a link that belong to a gap or tree line (trees use their crown)
SKIM_STEP = 25.0    # one skim node per this many metres of tree line
DIVE_H = 18.0       # a lone tree or pillar this tall is a dive target too
MAX_TURN = 115.0    # degrees: sharper than this and it's not a flowing line
MAX_NODES = 12
ARCH_MIN_H = 8.0    # a viaduct deck this high above the ground has arches you can fly through
ARCH_STEP = 15.0    # one arch node per this many metres of deck
ARCH_R = 8.0        # metres round an arch node that are the viaduct itself
WEAVE_MAX = 1.6 * ARCH_STEP  # arches this close are side by side: a run weaves from one to the next
WEAVE_ROOM = 4.0    # ...if there are this many metres of open air beside the deck, both sides, to turn in
MAX_ARCHES = 4      # ...for this many, so a long viaduct doesn't swamp every other line
BENDS = (0.25, -0.25, 0.4, -0.4)  # where a link that can't fly straight may bend: off its midpoint, by this
                                  # share of its length either side (at 0.4 it's 28% longer)
BEND_COST = 0.85    # a bend in the way there costs a little against flying straight
TAKEOFF_MIN = 8.0   # metres from the take-off to the first move's obstacle: not on top of it...
TAKEOFF_MAX = 90.0  # ...and close enough to see it


@dataclass(frozen=True)
class Lane:
    """A wall skimmed from the open beside it: `near`..`far` m off it, where it stands `wall` m or taller, with
    open air `room` m from it close by (along a field or a square, not down a ride through a wood or an alley)."""
    near: float
    far: float
    wall: float
    room: float
    r: float                 # metres round a lane node a link may skip: it's in the open, not on the wall
    buildings: bool = False  # a building's side too, not just the edge of a wood


WOOD_EDGE = Lane(near=3.5, far=6.0, wall=8.0, room=12.0, r=2.0)
WALLS = Lane(near=2.5, far=4.5, wall=6.0, room=8.0, r=1.5, buildings=True)  # a whoop's: closer in, and architecture


@dataclass(frozen=True)
class Style:
    min_link: float
    max_link: float
    gap_max_w: float
    corridor: int      # metres of clear air across the flight path (a quad plus margin)
    bld_clear: float   # metres a line keeps from any building (0: the whoop flies round architecture)
    road_clear: float  # ...and from a road, where the cars are
    lane: Lane | None = None  # walls it skims besides tree lines


FREESTYLE = Style(min_link=10.0, max_link=70.0, gap_max_w=12.0, corridor=3, bld_clear=12.0, road_clear=6.0,
                  lane=WOOD_EDGE)
WHOOP = Style(min_link=5.0, max_link=35.0, gap_max_w=5.0, corridor=3, bld_clear=0.0, road_clear=3.0, lane=WALLS)


def keep_out(land: np.ndarray | None, d_bld: np.ndarray | None, d_road: np.ndarray | None, style: Style) -> np.ndarray | None:
    """Pixels a line can't use: homes and gardens, car parks, school and hospital grounds, working railways,
    playgrounds and power lines (rasterize.KEEP_OFF), near a road, and for the 5 inch near any building.
    d_bld / d_road are metres to the nearest building / road pixel (None: there's none)."""
    out = None
    for m in (np.isin(land, KEEP_OFF) if land is not None else None,
              d_road <= style.road_clear if d_road is not None else None,
              d_bld <= style.bld_clear if d_bld is not None and style.bld_clear > 0 else None):
        if m is not None:
            out = m if out is None else out | m
    return out


def arches(deck: np.ndarray, ohm: np.ndarray) -> list[tuple]:
    """Arch positions under a viaduct deck: (row, col, deck height) every ARCH_STEP metres where the deck
    is ARCH_MIN_H or more above the ground."""
    high = deck & (ohm >= ARCH_MIN_H)
    if not high.any():
        return []
    # middle of the deck first, then keep any point ARCH_STEP from those already kept
    mid = ndimage.distance_transform_edt(deck)
    rr, cc = np.nonzero(high)
    out: list[tuple] = []
    for i in np.argsort(-mid[rr, cc], kind="stable"):
        r, c = rr[i], cc[i]
        if all((r - a) ** 2 + (c - b) ** 2 >= ARCH_STEP ** 2 for a, b, _ in out):
            out.append((float(r), float(c), float(ohm[r, c])))
    return out


def lane_mask(lane: Lane, dt_tall: np.ndarray, wall_h: np.ndarray, open_: np.ndarray) -> np.ndarray:
    """Where to skim a wall from: open ground `lane.near`..`lane.far` m off it, beside a field or a square
    rather than in a ride through a wood. `dt_tall` is metres to the nearest tall pixel and `wall_h` that
    pixel's height when it's a wall this lane skims (0 otherwise, and for a tree line, whose skim nodes sit
    on the belt itself)."""
    room = ndimage.maximum_filter(dt_tall, size=int(2 * lane.room + 1)) >= lane.room
    return open_ & (dt_tall >= lane.near) & (dt_tall <= lane.far) & (wall_h >= lane.wall) & room


def _thin(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One pixel of `mask` per SKIM_STEP square, so a long edge gives a chain of skim nodes."""
    rr, cc = np.nonzero(mask)
    key = (rr // SKIM_STEP).astype(np.int64) * 100000 + (cc // SKIM_STEP).astype(np.int64)
    _, first = np.unique(key, return_index=True)
    return rr[first], cc[first]


def nodes_from(gaps: list[tuple], lone: list[tuple], treeline: np.ndarray, style: Style,
               blocked: np.ndarray | None = None, arch: list[tuple] = (),
               lane: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """(N, 5) array of row, col, kind, quality (0..1), radius (m around it that's the obstacle itself),
    in tile pixels (1 m). Nothing inside `blocked`. `lane` is (lane_mask, wall heights) for a style that
    skims walls."""
    out = []
    for r, c, w, f, _a, th, _tf in gaps:
        if th and w <= style.gap_max_w:
            out.append((r, c, GAP, float(np.clip((f - 4) / 8, 0.2, 1.0)), END_SKIP))
    for r, c, h, a in lone:
        crown = float(np.sqrt(a / np.pi)) + style.corridor / 2 + 1.0
        out.append((r, c, DIVE if h >= DIVE_H else LONE, float(np.clip((h - 4) / 10, 0.2, 1.0)), max(crown, END_SKIP)))
    out.extend((float(r), float(c), SKIM, 1.0, END_SKIP) for r, c in zip(*_thin(treeline)))
    if lane is not None and style.lane:
        rr, cc = _thin(lane[0])
        q = np.clip((lane[1][rr, cc] - 4) / 8, 0.2, 1.0)  # a taller wall is a better skim
        out.extend((float(r), float(c), SKIM, float(v), style.lane.r) for r, c, v in zip(rr, cc, q))
    out.extend((r, c, ARCH, float(np.clip((h - 4) / 12, 0.3, 1.0)), ARCH_R) for r, c, h in arch)
    nodes = np.array(out, dtype="float64").reshape(-1, 5)
    if blocked is not None and len(nodes):
        ri = np.clip(np.rint(nodes[:, 0]).astype(int), 0, blocked.shape[0] - 1)
        ci = np.clip(np.rint(nodes[:, 1]).astype(int), 0, blocked.shape[1] - 1)
        nodes = nodes[~blocked[ri, ci]]
    return nodes


def room_to_weave(p: np.ndarray, q: np.ndarray, ohm: np.ndarray, deck: np.ndarray | None,
                  blocked: np.ndarray | None) -> bool:
    """Is there open air beside the deck, on both sides, between the arches at p and q (row, col)? A weave
    goes out through one arch, turns round the pier and comes back through the next, a third of the way up
    the arches, so anything lower than that doesn't matter but trees crowding the viaduct do."""
    h, w = ohm.shape
    at = lambda xy: (min(max(int(round(xy[0])), 0), h - 1), min(max(int(round(xy[1])), 0), w - 1))
    fly_h = max(CLEAR_H, 0.4 * min(ohm[at(p)], ohm[at(q)]))
    u = (q - p) / max(float(np.hypot(*(q - p))), 1e-6)
    m, n = (p + q) / 2, np.array([-u[1], u[0]])
    for side in (1, -1):
        for s in np.arange(ARCH_R, ARCH_R + WEAVE_ROOM + 1):
            rc = at(m + side * s * n)
            if deck is not None and deck[rc]:
                continue  # still under the deck
            if ohm[rc] >= fly_h or (blocked is not None and blocked[rc]):
                return False
    return True


def _clear(ohm: np.ndarray, a: np.ndarray, b: np.ndarray, ra: np.ndarray, rb: np.ndarray,
           max_len: float) -> np.ndarray:
    """For each segment a -> b (rows of (row, col)), is nothing CLEAR_H or taller on it, sampled every metre
    and skipping ra / rb metres at the ends (the obstacles themselves)?"""
    d = np.hypot(*(b - a).T)
    steps = np.arange(0.0, max_len + 1.0)
    t = steps[None, :] / np.maximum(d, 1e-6)[:, None]
    inside = (steps[None, :] >= ra[:, None]) & (steps[None, :] <= d[:, None] - rb[:, None])
    h, w = ohm.shape
    ri = np.clip(np.rint(a[:, :1] + t * (b[:, :1] - a[:, :1])).astype(int), 0, h - 1)
    ci = np.clip(np.rint(a[:, 1:2] + t * (b[:, 1:2] - a[:, 1:2])).astype(int), 0, w - 1)
    return ~((ohm[ri, ci] >= CLEAR_H) & inside).any(axis=1)


def links(nodes: np.ndarray, ohm: np.ndarray, style: Style, blocked: np.ndarray | None = None,
          deck: np.ndarray | None = None, via: dict | None = None) -> list[list[int]]:
    """Neighbour lists: pairs within range with clear air between them, and not over `blocked` ground.
    `deck` is the viaduct decks, where arches are. Pass a dict as `via` and a pair that can't fly straight
    may link with one bend round what's in the way (BENDS): via[(i, j)], i < j, is where it bends."""
    n = len(nodes)
    nb: list[list[int]] = [[] for _ in range(n)]
    if n < 2:
        return nb
    pairs = cKDTree(nodes[:, :2]).query_pairs(style.max_link, output_type="ndarray")
    if not len(pairs):
        return nb
    a, b = nodes[pairs[:, 0], :2], nodes[pairs[:, 1], :2]
    d = np.hypot(*(b - a).T)
    # Two arches of one viaduct sit side by side under the deck: neighbours link when there's room beside
    # the deck to weave between them (out through one arch, round the pier, back through the next), and
    # arches further apart don't link at all.
    both = (nodes[pairs[:, 0], 2] == ARCH) & (nodes[pairs[:, 1], 2] == ARCH)
    for i, j in pairs[both & (d <= WEAVE_MAX)]:
        if room_to_weave(nodes[i, :2], nodes[j, :2], ohm, deck, blocked):
            nb[i].append(int(j))
            nb[j].append(int(i))
    keep = (d >= style.min_link) & ~both
    pairs, a, b, d = pairs[keep], a[keep], b[keep], d[keep]
    if not len(pairs):
        return nb
    # Sample every metre along each link, skipping the ends, and check nothing tall is in the way across
    # the whole corridor: LiDAR speckle inside a wood leaves 1 m holes that aren't a way through.
    if blocked is not None:
        ohm = np.where(blocked, np.float32(99), ohm)
    ohm = ndimage.maximum_filter(ohm, size=style.corridor)
    ra, rb = nodes[pairs[:, 0], 4], nodes[pairs[:, 1], 4]
    ok = _clear(ohm, a, b, ra, rb, style.max_link)
    if via is not None:
        # The rest may bend once, off the midpoint, where both legs are clear: round a clump of trees
        # rather than through it.
        u = (b - a) / d[:, None]
        normal = np.stack([-u[:, 1], u[:, 0]], axis=1)
        left = ~ok
        zero = np.zeros(len(pairs))
        for f in BENDS:
            if not left.any():
                break
            w = (a + b) / 2 + f * d[:, None] * normal
            inside = (w[:, 0] >= 0) & (w[:, 0] <= ohm.shape[0] - 1) & (w[:, 1] >= 0) & (w[:, 1] <= ohm.shape[1] - 1)
            k = np.flatnonzero(left & inside)
            if not len(k):
                continue
            bent = _clear(ohm, a[k], w[k], ra[k], zero[k], style.max_link) & _clear(ohm, w[k], b[k], zero[k], rb[k], style.max_link)
            for i, j, wp in zip(pairs[k[bent], 0], pairs[k[bent], 1], w[k[bent]]):
                via[(int(min(i, j)), int(max(i, j)))] = (float(wp[0]), float(wp[1]))
            ok[k[bent]] = True
            left[k[bent]] = False
    for i, j in pairs[ok]:
        nb[i].append(int(j))
        nb[j].append(int(i))
    return nb


def _heading(frm: np.ndarray, to: np.ndarray) -> float:
    dv = to - frm
    return float(np.degrees(np.arctan2(dv[1], -dv[0])))


def _legs(nodes: np.ndarray, i: int, j: int, via: dict | None) -> list[np.ndarray]:
    """The points flown from node i to node j: straight, or by way of the bend."""
    w = via.get((min(i, j), max(i, j))) if via else None
    return [nodes[i, :2], nodes[j, :2]] if w is None else [nodes[i, :2], np.asarray(w), nodes[j, :2]]


def best_run(start: int, nodes: np.ndarray, nb: list[list[int]], via: dict | None = None) -> tuple[float, list[int]]:
    """Greedy walk from one node: best next obstacle by value, variety and a smooth heading. A link with a
    bend (`via`, from links) leaves along its first leg and arrives along its second."""
    path = [start]
    seen = {start}
    score = VALUE[int(nodes[start, 2])] * nodes[start, 3]
    heading = None
    kinds = {int(nodes[start, 2])}
    n_arch = int(nodes[start, 2] == ARCH)
    while len(path) < MAX_NODES:
        cur = path[-1]
        best, best_v, best_h = None, 0.0, None
        for j in nb[cur]:
            if j in seen or (nodes[j, 2] == ARCH and n_arch >= MAX_ARCHES):
                continue
            legs = _legs(nodes, cur, j, via)
            out_hd, hd = _heading(legs[0], legs[1]), _heading(legs[-2], legs[-1])
            if heading is not None:
                turn = abs((out_hd - heading + 180) % 360 - 180)
                if turn > MAX_TURN:
                    continue
                smooth = 1.0 - 0.3 * turn / MAX_TURN
            else:
                smooth = 1.0
            k = int(nodes[j, 2])
            v = VALUE[k] * nodes[j, 3] * smooth * (BEND_COST if len(legs) == 3 else 1.0)
            if k != int(nodes[cur, 2]):
                v *= 1.25  # a change of move: gap, then orbit, then a skim...
            if v > best_v:
                best, best_v, best_h = j, v, hd
        if best is None:
            break
        path.append(best)
        seen.add(best)
        kinds.add(int(nodes[best, 2]))
        n_arch += int(nodes[best, 2] == ARCH)
        score += best_v
        heading = best_h
    # Variety: a run of four different moves beats twelve skims along one hedge.
    score *= 1.0 + 0.15 * (len(kinds) - 1)
    return float(score), path


def all_runs(nodes: np.ndarray, nb: list[list[int]], via: dict | None = None) -> tuple[np.ndarray, list[list[int]]]:
    scores = np.zeros(len(nodes))
    paths: list[list[int]] = []
    for i in range(len(nodes)):
        if nb[i]:
            scores[i], p = best_run(i, nodes, nb, via)
        else:
            scores[i], p = VALUE[int(nodes[i, 2])] * nodes[i, 3], [i]
        paths.append(p)
    return scores, paths


def path_length(nodes: np.ndarray, path: list[int], via: dict | None = None) -> float:
    if len(path) < 2:
        return 0.0
    xy = np.concatenate([_legs(nodes, i, j, via)[:-1] for i, j in zip(path, path[1:])] + [nodes[path[-1:], :2]])
    return float(np.hypot(*np.diff(xy, axis=0).T).sum())


def takeoff(first: np.ndarray, first_r: float, solid: np.ndarray, open_: np.ndarray, style: Style,
            blocked: np.ndarray | None, hint: tuple[float, float] | None = None) -> tuple[int, int] | None:
    """Where to stand for a run starting at `first` (row, col, with `first_r` metres round it that are the
    obstacle itself): open ground off homes, car parks and roads, with a straight leg to the first move that
    stays off them too and doesn't go through anything `solid` (heights of walls, buildings, a viaduct deck:
    trees are left out, as the quad hops over those on the way in), as close to the first move, and to
    `hint` (the path-side place picked from the cells), as can be."""
    ok = open_ & ~blocked if blocked is not None else open_
    rr, cc = np.nonzero(ok[::3, ::3])
    rr, cc = rr * 3, cc * 3
    d = np.hypot(rr - first[0], cc - first[1])
    keep = (d >= first_r + TAKEOFF_MIN) & (d <= TAKEOFF_MAX)
    rr, cc, d = rr[keep], cc[keep], d[keep]
    if not len(rr):
        return None
    cost = d if hint is None else d + 0.5 * np.hypot(rr - hint[0], cc - hint[1])
    clear = ndimage.maximum_filter(np.where(blocked, np.float32(99), solid) if blocked is not None else solid,
                                   size=style.corridor)
    h, w = solid.shape
    for i in np.argsort(cost, kind="stable")[:300]:
        t = np.arange(0.0, max(d[i] - first_r, 0.0) + 1.0) / d[i]
        pr = np.clip(np.rint(rr[i] + t * (first[0] - rr[i])).astype(int), 0, h - 1)
        pc = np.clip(np.rint(cc[i] + t * (first[1] - cc[i])).astype(int), 0, w - 1)
        if not (clear[pr, pc] >= CLEAR_H).any():
            return int(rr[i]), int(cc[i])
    return None
