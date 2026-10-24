"""Regions, seed spots and on-disk paths."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pyproj import Transformer

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
STATIC_DIR = DATA_DIR / "static"
WEB_DATA_DIR = ROOT / "web" / "public" / "data"

BNG = "EPSG:27700"
WGS84 = "EPSG:4326"

_to_wgs84 = Transformer.from_crs(BNG, WGS84, always_xy=True)
_to_bng = Transformer.from_crs(WGS84, BNG, always_xy=True)


def bng_to_lonlat(x, y):
    return _to_wgs84.transform(x, y)


def lonlat_to_bng(lon, lat):
    return _to_bng.transform(lon, lat)


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    bbox: tuple[int, int, int, int]  # BNG metres: xmin, ymin, xmax, ymax
    osm: tuple[str, ...] = ()  # Geofabrik England county extracts covering the box, e.g. "tyne-and-wear"

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    def lonlat_bbox(self, pad_m: float = 0.0) -> tuple[float, float, float, float]:
        """West, south, east, north in WGS84 covering the (padded) BNG box."""
        xmin, ymin, xmax, ymax = self.bbox
        xs = [xmin - pad_m, xmax + pad_m]
        ys = [ymin - pad_m, ymax + pad_m]
        pts = [bng_to_lonlat(x, y) for x in xs for y in ys]
        return (
            min(p[0] for p in pts),
            min(p[1] for p in pts),
            max(p[0] for p in pts),
            max(p[1] for p in pts),
        )

    @property
    def cache_dir(self) -> Path:
        d = DATA_DIR / "cache" / self.key
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def out_dir(self) -> Path:
        d = DATA_DIR / "out" / self.key
        d.mkdir(parents=True, exist_ok=True)
        return d


@dataclass(frozen=True)
class Seed:
    name: str
    profile: str
    lon: float
    lat: float
    radius_m: float = 75.0
    note: str = ""

    @property
    def bng(self) -> tuple[float, float]:
        return lonlat_to_bng(self.lon, self.lat)


@dataclass
class Settings:
    region: Region
    seeds: list[Seed] = field(default_factory=list)


def load_regions(grid: bool = True) -> dict[str, Region]:
    """Hand-made regions (regions.toml) first, then the generated country grid (regions_grid.toml)."""
    raw = {}
    for name in ("regions.toml", "regions_grid.toml") if grid else ("regions.toml",):
        if (CONFIG_DIR / name).exists():
            with open(CONFIG_DIR / name, "rb") as f:
                raw |= tomllib.load(f)
    return {k: Region(key=k, name=r["name"], bbox=tuple(int(v) for v in r["bbox"]), osm=tuple(r.get("osm", ())))
            for k, r in raw.items()}


def load_region(key: str) -> Region:
    regions = load_regions()
    if key not in regions:
        raise SystemExit(f"Unknown region '{key}'. Known: {', '.join(regions)}")
    return regions[key]


def load_seeds() -> list[Seed]:
    path = CONFIG_DIR / "seeds.toml"
    if not path.exists():
        return []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return [
        Seed(
            name=s["name"],
            profile=s["profile"],
            lon=float(s["lon"]),
            lat=float(s["lat"]),
            radius_m=float(s.get("radius_m", 75.0)),
            note=s.get("note", ""),
        )
        for s in raw.get("seed", [])
    ]
