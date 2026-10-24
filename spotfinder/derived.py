"""Features worked out from the per-cell grids at scoring time (fast, so they're easy to tune):
how big the open field is, how good the gaps are, whether there's a view to the sunset, how easy it
is to walk in."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import ndimage

from .config import Region

CELL = 50
SUNSET_AZ = (225, 248, 270, 292, 315)  # where the sun sets over the year in the north of England, degrees
SUNSET_D = range(2, 41)                # cells out: 100 m .. 2 km


def add_derived(cells: dict[str, np.ndarray], region: Region) -> dict[str, np.ndarray]:
    out = dict(cells)
    out.update(field_size(cells))
    out.update(sunset_view(cells))
    out["dist_access"] = np.minimum.reduce([cells["dist_path"], cells["dist_road_minor"], cells["dist_parking"]])
    out.update(gap_quality(region, cells["gaps"].shape))
    for k in ("dist_bando_site", "dist_road_busy"):
        out.setdefault(k, np.full(cells["gaps"].shape, 1e6, dtype="float32"))
    for k in ("public_frac", "farm_frac", "treeline_m", "edge_w_m"):
        out.setdefault(k, np.zeros(cells["gaps"].shape, dtype="float32"))
    return out


def field_size(cells) -> dict[str, np.ndarray]:
    """How much open ground is around each cell: metres to the nearest cell that isn't mostly open
    (woods, buildings, water, roads). Hedges under 4 m don't stop you, so fields join up; this is
    the room to carry speed, not a field boundary."""
    open_ = ((cells["open_frac"] >= 0.75) & (cells["water_frac"] < 0.2) & (cells["road_frac"] < 0.15)
             & (cells["bld_frac"] < 0.05))
    return {"open_r_m": (ndimage.distance_transform_edt(open_) * CELL).astype("float32")}


def sunset_view(cells) -> dict[str, np.ndarray]:
    """Height of the skyline towards the setting sun, in degrees (low = a clear view of the sunset).
    Ground from the DTM plus woods and buildings on top; the nearest 100 m is left out because that's
    the spot itself."""
    elev = cells["elev"].astype("float32")
    surf = elev + np.where(cells["tall_frac"] >= 0.5, cells["h_p90"], 0.0)
    eye = elev + 15.0  # a quad hovering at 15 m
    H, W = elev.shape
    per_az = []
    for az in SUNSET_AZ:
        a = np.radians(az)
        dx, dy = np.sin(a), -np.cos(a)  # +x east, rows grow southwards
        best = np.full((H, W), -90.0, dtype="float32")
        for d in SUNSET_D:
            sr, sc = int(round(dy * d)), int(round(dx * d))  # rows grow southwards
            shifted = np.full((H, W), np.nan, dtype="float32")
            r0, r1 = max(0, -sr), min(H, H - sr)
            c0, c1 = max(0, -sc), min(W, W - sc)
            shifted[r0:r1, c0:c1] = surf[r0 + sr:r1 + sr, c0 + sc:c1 + sc]
            ang = np.degrees(np.arctan2(shifted - eye, d * CELL))
            best = np.fmax(best, ang)
        per_az.append(best)
    # The best bearing counts: you only need one good line to the sun.
    return {"sunset_horizon": np.min(per_az, axis=0)}


def gap_quality(region: Region, shape) -> dict[str, np.ndarray]:
    """Per-cell sums of gap quality, for a 5 inch (3-9 m wide, tall sides) and a whoop (2-5 m)."""
    path = region.out_dir / "gaps.parquet"
    out = {"gq5": np.zeros(shape, "float32"), "gqw": np.zeros(shape, "float32")}
    if not path.exists():
        return out
    g = pd.read_parquet(path)
    if "through" in g:
        g = g[g["through"]]
    xmin, _, _, ymax = region.bbox
    r = ((ymax - g["y"].to_numpy()) // CELL).astype(int)
    c = ((g["x"].to_numpy() - xmin) // CELL).astype(int)
    keep = (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    w, f = g["width_m"].to_numpy(), g["flank_h_m"].to_numpy()
    tall = np.clip((f - 5) / 8, 0, 1)  # 5 m sides barely count, 13 m+ full marks
    q5 = tall * np.where((w >= 3) & (w <= 9), 1.0, 0.3)
    qw = np.clip((f - 3) / 6, 0, 1) * np.where((w >= 2) & (w <= 5), 1.0, 0.25)
    np.add.at(out["gq5"], (r[keep], c[keep]), q5[keep])
    np.add.at(out["gqw"], (r[keep], c[keep]), qw[keep])
    return out
