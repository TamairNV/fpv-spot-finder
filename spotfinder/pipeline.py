"""End-to-end run for one region: data -> features -> scores -> spots -> web files."""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import time

import numpy as np
from scipy import ndimage

from . import basemap, context, derived, export, features, legal, parks, rasterize, scoring
from .config import CONFIG_DIR, Region, load_regions, load_seeds
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
    rasterize.build_land_raster(region)
    basemap.write_basemap(region)


def compute_cells(region: Region, use_lidar: bool = True, workers: int = 4) -> dict[str, np.ndarray]:
    """All per-cell features for the region, cached in data/out/<region>/cells.npz."""
    g = legal.build_legal(region)  # first: it's quick, and fails fast on a missing NATS file
    t0 = time.time()
    height_cells, gaps, lone = features.run(region, workers=workers, use_lidar=use_lidar)
    log.info("height features in %.0fs", time.time() - t0)
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
        "osm": [p.name for p in osm.pbf_paths(region)],
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


def _inside(region: Region, seed, shape) -> list | None:
    cells = export.cells_within(region, seed.bng, seed.radius_m)
    return cells if all(0 <= r < shape[0] and 0 <= c < shape[1] for r, c in cells) else None


@functools.cache
def _region_style(key: str, profile_key: str) -> tuple[dict, tuple[int, int]] | None:
    """Another region's style features for a profile, if it has been through `features`."""
    other = load_regions()[key]
    if not (other.out_dir / "cells.npz").exists():
        return None
    style = scoring.components(derived.add_derived(load_cells(other), other), scoring.PROFILES[profile_key], legal_only=False)
    return style, next(iter(style.values())).shape


SEED_STYLES = CONFIG_DIR / "seed_styles.json"  # seeds' style vectors, so other machines can use them


def _home_style(seed, profile_key: str) -> np.ndarray | None:
    """Style of a seed that lies outside the region being scored, measured in a region that has it."""
    for other in load_regions().values():
        got = _region_style(other.key, profile_key)
        cells = got and _inside(other, seed, got[1])
        if cells:
            log.info("seed %s (%s): style taken from %s", seed.name, profile_key, other.key)
            vec = scoring.style_vector(got[0], scoring.PROFILES[profile_key], cells)
            saved = json.loads(SEED_STYLES.read_text()) if SEED_STYLES.exists() else {}
            saved[f"{profile_key}/{seed.name}"] = {"features": list(scoring.PROFILES[profile_key].style_features),
                                                   "values": [round(float(v), 5) for v in vec]}
            SEED_STYLES.write_text(json.dumps(saved, indent=1))
            return vec
    # Not scored here (e.g. a fresh clone working through other regions): use the copy kept in config.
    saved = json.loads(SEED_STYLES.read_text()).get(f"{profile_key}/{seed.name}") if SEED_STYLES.exists() else None
    if saved and saved["features"] == list(scoring.PROFILES[profile_key].style_features):
        return np.array(saved["values"], dtype="float64")
    log.warning("seed %s isn't inside any scored region; skipping it", seed.name)
    return None


def foreign_ratings(region: Region) -> dict[str, tuple[list, list]]:
    """Thumbs up/down given in other regions, as style vectors: your taste travels with you."""
    out: dict[str, tuple[list, list]] = {}
    for other in load_regions().values():
        if other.key == region.key:
            continue
        for key, (up, down) in load_ratings(other).items():
            got = _region_style(other.key, key)
            if got is None:
                continue
            style, (H, W) = got
            for i, rcs in enumerate((up, down)):
                for r, c in rcs:
                    near = [(a, b) for a in range(max(r - 1, 0), min(r + 2, H)) for b in range(max(c - 1, 0), min(c + 2, W))]
                    out.setdefault(key, ([], []))[i].append(scoring.style_vector(style, scoring.PROFILES[key], near))
    return out


def score_scale(region: Region, key: str, mode: str, own_top: float) -> float:
    """The rule score that reads as 100. Every region uses the reference region's (the first in
    regions.toml) once it has one, so a 90 in Durham means the same as a 90 in Newcastle.
    Kept in config/score_scale.json so a fresh clone scores other regions on the same scale."""
    ref = next(iter(load_regions().values()))
    path = CONFIG_DIR / "score_scale.json"
    scale = json.loads(path.read_text()) if path.exists() else {}
    if region.key == ref.key:
        scale.setdefault(mode, {})[key] = own_top
        path.write_text(json.dumps(scale, indent=1))
        return own_top
    return scale.get(mode, {}).get(key, own_top)


def seed_group(region: Region, seed, profile, shape):
    """The seed's cells if it's in this region, else its style carried over from its own region."""
    return _inside(region, seed, shape) or _home_style(seed, profile.key)


def score_and_export(region: Region, n_spots: int = 250, previews: int = 100_000, strict: bool = False) -> dict:
    """Score every cell and export the spots. By default legality is a label on each spot, not a filter:
    the app lists potential places and says which rules they meet. No-fly zones, MOD land and danger areas
    stay out either way. strict=True only lists spots each drone may fly under its Open subcategory."""
    cells = derived.add_derived(load_cells(region), region)
    meta = json.loads((region.out_dir / "cells_meta.json").read_text())
    seeds = load_seeds()
    results = {}
    land = ((cells["water_frac"] - cells["beach_frac"]) < 0.6) & (cells["elev"] > -2)  # a beach is land
    rated = load_ratings(region)
    H, W = land.shape

    def _near(rc):  # a rated spot's own cell and its neighbours
        r, c = rc
        return [(i, j) for i in range(max(r - 1, 0), min(r + 2, H)) for j in range(max(c - 1, 0), min(c + 2, W))]

    foreign = foreign_ratings(region)
    park_list = parks.load(region)
    park_grid = parks.grid(region, park_list, land.shape)
    meta["ratings"] = {k: {"up": len(u), "down": len(d)} for k, (u, d) in rated.items()}
    meta["ratings_other_regions"] = {k: {"up": len(u), "down": len(d)} for k, (u, d) in foreign.items()}
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
        if "inst_frac" in cells:  # never a spot on a school, hospital or prison's grounds
            eligible &= cells["inst_frac"] < scoring.INSTITUTION_MAX
        groups = [seed_group(region, s, profile, (H, W)) for s in seeds if s.profile == key]
        groups = [g for g in groups if g is not None]
        up, down = rated.get(key, ([], []))
        f_up, f_down = foreign.get(key, ([], []))
        sim = scoring.similarity(style, profile, groups + [_near(rc) for rc in up] + f_up, land)
        if sim is not None and (down or f_down):
            # places like the ones you binned get pulled down
            sim = np.clip(sim - 0.6 * scoring.similarity(style, profile, [_near(rc) for rc in down] + f_down, land), 0, 1)
        seed_cells = [rc for g in groups if not isinstance(g, np.ndarray) for rc in g]
        # Rules scaled so the best cell of the reference region reads 100 (see score_scale); taste is
        # already 0..1 (1 = just like the seeds).
        top = float(rule[eligible].max()) if eligible.any() else 1.0
        top = score_scale(region, key, meta["legal_mode"], top)
        rule_n = scoring.soft_cap(rule / max(top, 1e-6))
        final = scoring.blend(rule_n, sim)
        # Same blend on the place's style alone, ignoring any A3 mask: how your seed spots score.
        style_n = scoring.soft_cap(scoring.rule_score(style, profile) / max(top, 1e-6))
        style_final = scoring.blend(style_n, sim)
        spots = scoring.pick_spots(final, eligible, profile.spacing_m, n_spots, anchor=profile.anchor(cells))
        # every park without a spot gets one at its best place, listed after the ranking
        park_spots = parks.picks(park_grid, park_list, final, eligible, spots, profile.spacing_m)
        spots += park_spots
        results[key] = dict(top=top, comps=comps, style=style, rule=rule, rule_rank=rule_n, sim=sim, final=final,
                            style_final=style_final, heat=scoring.pct_rank(final, land), eligible=eligible, spots=spots,
                            park_spots=set(park_spots), seed_cells=seed_cells)
        log.info("%s: %d eligible cells, %d spots + %d park spots", key, int(eligible.sum()), len(spots) - len(park_spots),
                 len(park_spots))
    export.write_all(region, cells, results, meta, seeds, previews=previews, parks=(park_grid, park_list))
    export.write_regions_index()
    return results
