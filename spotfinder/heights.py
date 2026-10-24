"""Read 1 m height tiles: EA LiDAR where we have it, tree-height model + building guesses elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from .config import Region

LIDAR_DIRS = [Path("/mnt/project-files/fpv-data")]  # shared folder the fetch job writes to


@dataclass
class Tile:
    x0: int          # BNG of the top-left corner of the array (incl. margin)
    y1: int
    ohm: np.ndarray  # object height above ground, m (trees, buildings, anything)
    trees: np.ndarray
    bldh: np.ndarray
    bld: np.ndarray  # building footprint mask
    water: np.ndarray
    ways: np.ndarray  # 1 road, 2 path, 3 rail
    porosity: np.ndarray | None  # first - last return, m (LiDAR only)
    lidar: np.ndarray  # mask of pixels that came from LiDAR
    land: np.ndarray | None = None  # homes and gardens, car parks, viaduct decks (rasterize.*_CODE)


def lidar_paths(region: Region) -> dict[str, Path] | None:
    names = {"ndsm": "lidar_ndsm_1m.tif", "porosity": "lidar_porosity_1m.tif"}
    for base in [region.cache_dir, *[d / region.key for d in LIDAR_DIRS]]:
        found = {k: base / v for k, v in names.items()}
        if found["ndsm"].exists():
            return {k: p for k, p in found.items() if p.exists()}
    return None


class HeightReader:
    """Opens the region rasters once per process and cuts tiles out of them."""

    def __init__(self, region: Region, use_lidar: bool = True):
        self.region = region
        c = region.cache_dir
        self.ds = {
            "chm": rasterio.open(c / "chm_1m.tif"),
            "bld": rasterio.open(c / "bld_dm_1m.tif"),
            "water": rasterio.open(c / "water_1m.tif"),
            "ways": rasterio.open(c / "ways_1m.tif"),
        }
        if (c / "land_1m.tif").exists():
            self.ds["land"] = rasterio.open(c / "land_1m.tif")
        lp = lidar_paths(region) if use_lidar else None
        self.has_lidar = lp is not None
        if lp:
            self.ds["ndsm"] = rasterio.open(lp["ndsm"])
            if "porosity" in lp:
                self.ds["porosity"] = rasterio.open(lp["porosity"])

    def _read(self, key: str, win: Window, fill) -> np.ndarray:
        return self.ds[key].read(1, window=win, boundless=True, fill_value=fill)

    def tile(self, x0: int, y0: int, size: int, margin: int) -> Tile:
        """Tile whose core is [x0, x0+size) x [y0, y0+size) in BNG, padded by `margin` metres."""
        xmin, _, _, ymax = self.region.bbox
        col = x0 - margin - xmin
        row = ymax - (y0 + size + margin)
        n = size + 2 * margin
        win = Window(col, row, n, n)
        chm = self._read("chm", win, 0).astype("float32")
        bldh = self._read("bld", win, 0).astype("float32") / 10.0
        water = self._read("water", win, 0) > 0
        ways = self._read("ways", win, 0)
        bld = bldh > 0
        trees = np.where(bld | water, 0.0, chm).astype("float32")
        porosity = None
        lidar = np.zeros_like(bld)
        if self.has_lidar:
            nd = self._read("ndsm", win, 255)
            lidar = nd != 255
            h = nd.astype("float32") / 4.0
            # LiDAR knows the real heights: trees/walls/anything off-footprint, and roofs on footprints.
            trees = np.where(lidar, np.where(bld | water, 0.0, h), trees)
            roof = np.where(lidar & bld, h, bldh)
            # A footprint LiDAR sees as flat ground is probably demolished (or not built when flown).
            bldh = np.where(lidar & bld & (h < 1.5), 0.0, roof)
            bld = bldh > 0
            if "porosity" in self.ds:
                p = self._read("porosity", win, 255)
                porosity = np.where(p == 255, np.nan, p.astype("float32") / 4.0)
        ohm = np.maximum(trees, bldh)
        land = self._read("land", win, 0) if "land" in self.ds else None
        return Tile(x0=x0 - margin, y1=y0 + size + margin, ohm=ohm, trees=trees, bldh=bldh, bld=bld,
                    water=water, ways=ways, porosity=porosity, lidar=lidar, land=land)
