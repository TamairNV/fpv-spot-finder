"""3D models made on request: the pipeline pre-makes previews only for each profile's best spots,
and the server builds any other spot's when someone opens it, from the region's rasters in
data/pipeline/cache/<region>. Each one is kept in data/models/<region>/, so it's made once.
"""
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("spotter.models")


class NotHere(Exception):
    pass


class Models:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "models"
        self.lock = threading.Lock()  # rasterio handles aren't shared between threads

    def _region(self, key: str):
        from spotfinder import export
        from spotfinder.config import load_regions

        region = load_regions().get(key)
        if region is None or not export.can_model(region):
            raise NotHere(key)
        return region

    def png(self, region: str, spot: str) -> Path:
        return self.dir / region / f"{spot}.png"

    def get(self, region: str, profile: str, r: int, c: int) -> dict:
        """The spot's preview and line, made now if nobody has opened it before."""
        from spotfinder import export, scoring

        if profile not in scoring.PROFILES:
            raise ValueError("unknown profile")
        spot = f"{r}-{c}"
        meta = self.dir / region / f"{spot}.{profile}.json"
        if meta.exists() and self.png(region, spot).exists():
            return json.loads(meta.read_text())
        with self.lock:
            if meta.exists() and self.png(region, spot).exists():
                return json.loads(meta.read_text())
            reg = self._region(region)
            xmin, ymin, xmax, ymax = reg.bbox
            if not (0 <= r < (ymax - ymin) // export.CELL and 0 <= c < (xmax - xmin) // export.CELL):
                raise ValueError("spot outside the region")
            out = export.spot_model(reg, r, c, profile, self.png(region, spot))
            out["preview"]["file"] = f"api/models/{region}/{spot}.png"
            tmp = meta.with_name(f".{os.getpid()}-{meta.name}")
            tmp.write_text(json.dumps(out))
            tmp.replace(meta)
            log.info("made 3D model %s/%s (%s)", region, spot, profile)
            return out
