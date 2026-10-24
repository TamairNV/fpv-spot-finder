"""End-to-end run for one region: data -> features -> scores -> spots -> web files."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

import numpy as np
from scipy import ndimage

from . import basemap, context, export, features, legal, rasterize, scoring
from .config import CONFIG_DIR, Region, load_seeds
from .heights import lidar_paths
from .sources import canopy, osm, overture, terrain

log = logging.getLogger(__name__)


def prepare(region: Region) -> None:
    """Fetch and grid everything the scorer needs (cached; safe to re-run)."""
    overture.fetch_all(region)
    osm.extract(region)
    canopy.build_chm(region)
    terrain.build_dtm(region)
    rasterize.build_vector_rasters(region)
    basemap.write_basemap(region)


def compute_cells(region: Region, use_lidar: bool = True, workers: int = 4) -> dict[str, np.ndarray]:
    """All per-cell features for the region, cached in data/out/<region>/cells.npz."""
    t0 = time.time()
    height_cells, gaps, lone = features.run(region, workers=workers, use_lidar=use_lidar)
    log.info("height features in %.0fs", time.time() - t0)
    g = legal.build_legal(region)
    cells = dict(height_cells)
    cells.update(context.legal_features(g))
    cells.update(context.context_features(region))
    dtm, dtm_src = context.load_dtm(region)
    cells.update(context.terrain_features(dtm))
    out = region.out_dir
    np.savez_compressed(out / "cells.npz", **cells)
    gaps.to_parquet(out / "gaps.parquet")
    lone.to_parquet(out / "lone.parquet")
    lp = lidar_paths(region) if use_lidar else None
    meta = {
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "heights": "EA LiDAR 1 m (first-return DSM minus DTM), Meta canopy model where LiDAR is missing"
        if lp else "Meta 1 m canopy height model + Overture building heights",
        "lidar_coverage": float(cells["lidar_frac"].mean()) if lp else 0.0,
        "terrain": dtm_src,
        "airspace": g.airspace_label,
        "overture_release": overture.RELEASE,
        "osm": sorted(p.name for p in osm.OSM_DIR.glob("*.osm.pbf")),
    }
    (out / "cells_meta.json").write_text(json.dumps(meta, indent=2))
    export.save_legal_grids(region, g)
    return cells


def refresh_legal(region: Region) -> dict[str, np.ndarray]:
    """Rebuild only the legal grids (airspace, A1/A3 distances, warning flags) and keep the rest."""
    cells = load_cells(region)
    g = legal.build_legal(region)
    cells.update(context.legal_features(g))
    np.savez_compressed(region.out_dir / "cells.npz", **cells)
    meta_path = region.out_dir / "cells_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["airspace"] = g.airspace_label
    meta_path.write_text(json.dumps(meta, indent=2))
    export.save_legal_grids(region, g)
    return cells


def refresh_context(region: Region) -> dict[str, np.ndarray]:
    """Recompute only the map-context features (bandos, water, bridges...) and keep the rest. Fast."""
    cells = load_cells(region)
    cells.update(context.context_features(region))
    np.savez_compressed(region.out_dir / "cells.npz", **cells)
    return cells


def load_cells(region: Region) -> dict[str, np.ndarray]:
    with np.load(region.out_dir / "cells.npz") as z:
        return {k: z[k] for k in z.files}


def load_ratings(region: Region) -> dict[str, tuple[list, list]]:
    """Thumbs up/down exported from the app (config/ratings/<region>.json): {profile: (up cells, down cells)}."""
    path = CONFIG_DIR / "ratings" / f"{region.key}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text()).get("ratings", {})
    out: dict[str, tuple[list, list]] = {}
    for k, r in data.items():
        profile, _, spot = k.partition("/")
        try:
            rc = tuple(int(v) for v in spot.split("-"))
        except ValueError:
            continue  # seeds and other non-cell ids
        if profile in scoring.PROFILES and len(rc) == 2 and r.get("v"):
            out.setdefault(profile, ([], []))[0 if r["v"] > 0 else 1].append(rc)
    log.info("ratings: %s", {k: (len(u), len(d)) for k, (u, d) in out.items()})
    return out


def score_and_export(region: Region, n_spots: int = 250, previews: int = 120, strict: bool = False) -> dict:
    """Score every cell and export the spots. By default legality is a label on each spot, not a filter:
    the app lists potential places and says which rules they meet. No-fly zones, MOD land and danger areas
    stay out either way. strict=True only lists spots each drone may fly under its Open subcategory."""
    cells = load_cells(region)
    meta = json.loads((region.out_dir / "cells_meta.json").read_text())
    seeds = load_seeds()
    results = {}
    land = (cells["water_frac"] < 0.6) & (cells["elev"] > -2)
    rated = load_ratings(region)
    H, W = land.shape

    def _near(rc):  # a rated spot's own cell and its neighbours
        r, c = rc
        return [(i, j) for i in range(max(r - 1, 0), min(r + 2, H)) for j in range(max(c - 1, 0), min(c + 2, W))]

    meta["ratings"] = {k: {"up": len(u), "down": len(d)} for k, (u, d) in rated.items()}
    meta["legal_mode"] = "strict" if strict else "label"
    for key, profile in scoring.PROFILES.items():
        style = scoring.components(cells, profile, legal_only=False)
        comps = scoring.components(cells, profile) if strict else style
        rule = scoring.rule_score(comps, profile)
        # MOD land and live danger areas are out whatever the score.
        keep_out = (cells["flags"].astype("int64") & (legal.FLAG_MILITARY | legal.FLAG_DANGER_AREA)) > 0
        # ...and so is anywhere the spot itself would reach onto them.
        keep_out = ndimage.binary_dilation(keep_out, structure=np.ones((3, 3)), iterations=profile.radius_cells + 1)
        if strict:
            eligible = land & (cells[profile.legal_key] >= profile.legal_min) & ~keep_out
            if profile.legal_window_min:
                eligible &= scoring.win_mean(cells[profile.legal_key], profile.radius_cells) >= profile.legal_window_min
        else:  # anywhere outside flight restriction zones
            eligible = land & (cells["a1_frac"] >= 0.9) & ~keep_out
        groups = [export.cells_within(region, s.bng, s.radius_m) for s in seeds if s.profile == key]
        up, down = rated.get(key, ([], []))
        sim = scoring.similarity(style, profile, groups + [_near(rc) for rc in up], land)
        if sim is not None and down:
            # places like the ones you binned get pulled down
            sim = np.clip(sim - 0.6 * scoring.similarity(style, profile, [_near(rc) for rc in down], land), 0, 1)
        seed_cells = [rc for g in groups for rc in g]
        # Rules scaled so the best 0.1% of the region reads ~100; taste is already 0..1 (1 = just like the seeds).
        top = float(np.percentile(rule[eligible], 99.9)) if eligible.any() else 1.0
        rule_n = np.clip(rule / max(top, 1e-6), 0, 1)
        final = rule_n if sim is None else 0.6 * rule_n + 0.4 * sim
        # Same blend on the place's style alone, ignoring any A3 mask: how your seed spots score.
        style_n = np.clip(scoring.rule_score(style, profile) / max(top, 1e-6), 0, 1)
        style_final = style_n if sim is None else 0.6 * style_n + 0.4 * sim
        spots = scoring.pick_spots(final, eligible, profile.spacing_m, n_spots)
        results[key] = dict(comps=comps, style=style, rule=rule, rule_rank=rule_n, sim=sim, final=final,
                            style_final=style_final, heat=scoring.pct_rank(final, land), eligible=eligible, spots=spots, seed_cells=seed_cells)
        log.info("%s: %d eligible cells, %d spots", key, int(eligible.sum()), len(spots))
    export.write_all(region, cells, results, meta, seeds, previews=previews)
    return results
