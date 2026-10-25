"""Grade the scorer against real spots (config/known_spots.toml): `python -m spotfinder eval`.

Scores the region exactly as `score` does but writes nothing to the app. For each known spot and each
profile it should suit, it says whether a listed spot lands on it and at what rank, the best cell's
percentile, and which components lifted or sank it, so a miss comes with its reason."""

from __future__ import annotations

import json
import logging
import math
import tomllib
from dataclasses import dataclass, field
from unittest import mock

import numpy as np

from . import export, pipeline, scoring
from .config import CONFIG_DIR, Region, lonlat_to_bng

log = logging.getLogger(__name__)

TOP = 50          # "found": a listed spot this high or better lands on it
CLOSE_M = 100     # ...within the known spot's radius plus this, plus the profile's own window (a pin that
                  # frames the subject from the next field still counts)


@dataclass(frozen=True)
class KnownSpot:
    name: str
    lat: float
    lon: float
    radius_m: float = 150.0
    profiles: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    source: str = ""
    why: str = ""
    approx: bool = False
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def bng(self) -> tuple[float, float]:
        return lonlat_to_bng(self.lon, self.lat)


def load_known() -> list[KnownSpot]:
    path = CONFIG_DIR / "known_spots.toml"
    if not path.exists():
        return []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return [KnownSpot(name=s["name"], lat=float(s["lat"]), lon=float(s["lon"]), radius_m=float(s.get("radius_m", 150)),
                      profiles=tuple(s.get("profiles", ())), kinds=tuple(s.get("kinds", ())), source=s.get("source", ""),
                      why=s.get("why", ""), approx=bool(s.get("approx", False)))
            for s in raw.get("spot", [])]


def load_bando_checks() -> tuple[list[dict], list[dict]]:
    path = CONFIG_DIR / "known_spots.toml"
    if not path.exists():
        return [], []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("bando", []), raw.get("not_bando", [])


def grade_bandos(region: Region) -> dict | None:
    """Real bandos with a dot on the app's bando layer, and parks that wrongly have one."""
    path = export.WEB_DATA_DIR / region.key / "bandos.geojson"
    yes, no = load_bando_checks()
    if not path.exists() or not (yes or no):
        return None
    pts = [(lonlat_to_bng(*f["geometry"]["coordinates"]), f["properties"])
           for f in json.loads(path.read_text())["features"]]

    def near(b):
        x, y = lonlat_to_bng(b["lon"], b["lat"])
        return [p for (px, py), p in pts if math.hypot(px - x, py - y) <= b.get("radius_m", 100)]

    hits = [(b["name"], near(b)) for b in yes]
    wrong = [(b["name"], near(b)) for b in no]
    return dict(n_dots=len(pts), found=[n for n, h in hits if h], missed=[n for n, h in hits if not h],
                false=[(n, sorted({p.get("n") or p.get("k", "?") for p in h})) for n, h in wrong if h])


def bando_report(b: dict | None) -> str:
    if b is None:
        return "bandos: no bandos.geojson yet (run `score`) or no checks in known_spots.toml"
    out = [f"bandos ({b['n_dots']} on the map): {len(b['found'])}/{len(b['found']) + len(b['missed'])} real ones "
           f"found, {len(b['false'])} parks with a dot"]
    out += [f"  missed: {n}" for n in b["missed"]]
    out += [f"  wrongly on: {n} ({', '.join(map(str, k))[:80]})" for n, k in b["false"]]
    return "\n".join(out)


def _score(region: Region) -> tuple[dict, dict]:
    """score_and_export's results without touching web/public or config/score_scale.json."""
    scale = CONFIG_DIR / "score_scale.json"
    saved = scale.read_text() if scale.exists() else None
    try:
        with mock.patch.object(export, "write_all"), mock.patch.object(export, "write_regions_index"):
            results = pipeline.score_and_export(region, previews=0)
    finally:
        if saved is not None:
            scale.write_text(saved)
    return results, pipeline.derived.add_derived(pipeline.load_cells(region), region)


def grade(region: Region, known: list[KnownSpot] | None = None) -> list[dict]:
    known = load_known() if known is None else known
    results, cells = _score(region)
    H, W = next(iter(results.values()))["final"].shape
    rows = []
    for spot in known:
        x, y = spot.bng
        inside = [(r, c) for r, c in export.cells_within(region, (x, y), spot.radius_m) if 0 <= r < H and 0 <= c < W]
        if not inside:
            continue
        for key in spot.profiles:
            res = results[key]
            p = scoring.PROFILES[key]
            final, heat = res["final"], res["heat"]
            r, c = max(inside, key=lambda rc: final[rc])
            listed = [s for s in res["spots"] if s not in res["park_spots"]]
            reach = spot.radius_m + CLOSE_M + p.radius_cells * scoring.CELL
            near = []
            for i, (sr, sc) in enumerate(listed):
                sx, sy = export.cell_center(region, sr, sc)
                d = math.hypot(sx - x, sy - y)
                if d <= reach:
                    near.append((i + 1, d))
            rank = min(near)[0] if near else None
            park = any(math.hypot(*(np.subtract(export.cell_center(region, *s), (x, y)))) <= reach
                       for s in res["park_spots"])
            pos = sum(w for w in p.weights.values() if w > 0)
            contrib = {k: float(p.weights[k] * res["comps"][k][r, c] / pos) for k in p.weights}
            rows.append(dict(
                spot=spot.name, profile=key, kinds=list(spot.kinds), source=spot.source,
                status="found" if rank and rank <= TOP else "listed" if rank else "park" if park else "missed",
                rank=rank, n_listed=len(listed), score=round(100 * float(final[r, c])),
                pct=round(100 * float(heat[r, c]), 1), eligible=bool(res["eligible"][r, c]),
                rule=round(100 * float(res["rule_rank"][r, c])),
                taste=None if res["sim"] is None else round(100 * float(res["sim"][r, c])),
                lifts=sorted(((k, round(v, 3)) for k, v in contrib.items() if v > 0.005), key=lambda kv: -kv[1])[:4],
                sinks=sorted(((k, round(v, 3)) for k, v in contrib.items() if v < -0.005), key=lambda kv: kv[1]),
                raw={k: round(float(cells[k][r, c]), 1) for k in
                     ("dist_hazard", "dist_bridge", "dist_water", "dist_bando", "dist_landmark", "dist_road_busy",
                      "a1_frac", "a3_frac", "water_frac") if k in cells},
            ))
    return rows


def report(rows: list[dict]) -> str:
    by = {s: sum(r["status"] == s for r in rows) for s in ("found", "listed", "park", "missed")}
    out = [f"{len(rows)} spot/profile pairs: {by['found']} found (top {TOP}), {by['listed']} listed lower, "
           f"{by['park']} only as a park pick, {by['missed']} missed", ""]
    out.append(f"{'status':7} {'rank':>5} {'pct':>5} {'scr':>4}  {'profile':10} spot  |  sinks")
    for r in sorted(rows, key=lambda r: (["missed", "park", "listed", "found"].index(r["status"]), -r["pct"])):
        sinks = ", ".join(f"{k} {v:+.2f}" for k, v in r["sinks"]) or "-"
        elig = "" if r["eligible"] else " [not eligible]"
        out.append(f"{r['status']:7} {r['rank'] or '-':>5} {r['pct']:5.1f} {r['score']:4d}  {r['profile']:10} "
                   f"{r['spot']}{elig}  |  {sinks}")
    kinds: dict[str, list[int]] = {}
    for r in rows:
        for k in r["kinds"]:
            kinds.setdefault(k, [0, 0])
            kinds[k][0] += r["status"] == "found"
            kinds[k][1] += 1
    out += ["", "found by kind: " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(kinds.items(), key=lambda kv: kv[1][0] / kv[1][1]))]
    return "\n".join(out)


def run(region: Region, json_out=None) -> list[dict]:
    rows = grade(region)
    bandos = grade_bandos(region)
    print(report(rows))
    print()
    print(bando_report(bandos))
    if json_out:
        json_out.write_text(json.dumps({"spots": rows, "bandos": bandos}, indent=1))
        log.info("wrote %s", json_out)
    return rows
