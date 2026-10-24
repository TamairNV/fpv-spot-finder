"""UK drone restriction zones from the NATS digital datasets.

NATS publishes the UAS restriction dataset (FRZs, prohibited, restricted and
danger areas) every AIRAC cycle as a KMZ:
https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/
Files look like EG_UAS_FR_DS_AREA1_FULL_YYYYMMDD.kmz (YYYYMMDD = effective date).
Drop them in data/static/nats/ and we pick the newest one already in force.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon

from ..config import STATIC_DIR

log = logging.getLogger(__name__)

NATS_DIR = STATIC_DIR / "nats"
KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
PATTERN = re.compile(r"EG_UAS_FR_DS_AREA1_FULL_(\d{8})\.kmz$")


def pick_dataset(today: dt.date | None = None, folder: Path = NATS_DIR) -> tuple[Path, dt.date]:
    today = today or dt.date.today()
    found = []
    for p in folder.glob("EG_UAS_FR_DS_AREA1_FULL_*.kmz"):
        m = PATTERN.search(p.name)
        if m:
            found.append((dt.datetime.strptime(m.group(1), "%Y%m%d").date(), p))
    if not found:
        raise FileNotFoundError(
            f"No NATS UAS restriction KMZ in {folder}. Download EG_UAS_FR_DS_AREA1_FULL_<date>.kmz from "
            "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/"
        )
    in_force = [f for f in found if f[0] <= today]
    eff, path = max(in_force) if in_force else min(found)
    if not in_force:
        log.warning("all airspace datasets are in the future; using %s", path.name)
    return path, eff


def _text(desc: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html.unescape(desc or ""))
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def _rings(poly_el) -> Polygon | None:
    rings = []
    for tag in ("k:outerBoundaryIs", "k:innerBoundaryIs"):
        for b in poly_el.findall(tag, KML_NS):
            coords = b.findtext(".//k:coordinates", default="", namespaces=KML_NS)
            pts = [tuple(float(v) for v in tok.split(",")[:2]) for tok in coords.split()]
            if len(pts) >= 3:
                rings.append(pts)
    if not rings:
        return None
    poly = Polygon(rings[0], rings[1:])
    return poly if poly.is_valid else poly.buffer(0)


def read_kmz(path: Path) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(path) as z:
        kml_name = next(n for n in z.namelist() if n.lower().endswith(".kml"))
        root = ET.fromstring(z.read(kml_name))
    rows = []
    for folder in root.iter(f"{{{KML_NS['k']}}}Folder"):
        kind = folder.findtext("k:name", default="", namespaces=KML_NS)
        for pm in folder.findall("k:Placemark", KML_NS):
            polys = [p for p in (_rings(el) for el in pm.iter(f"{{{KML_NS['k']}}}Polygon")) if p is not None]
            if not polys:
                continue
            text = _text(pm.findtext("k:description", default="", namespaces=KML_NS))
            lower = re.search(r"Lower limit:\s*([^\n]+)", text)
            upper = re.search(r"Upper limit:\s*([^\n]+)", text)
            rows.append({
                "name": pm.findtext("k:name", default="", namespaces=KML_NS),
                "kind": kind,
                "lower": lower.group(1).strip() if lower else None,
                "upper": upper.group(1).strip() if upper else None,
                "text": text[:800],
                "geometry": polys[0] if len(polys) == 1 else MultiPolygon(
                    [g for p in polys for g in (p.geoms if hasattr(p, "geoms") else [p])]),
            })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def load(today: dt.date | None = None) -> tuple[gpd.GeoDataFrame, str]:
    path, eff = pick_dataset(today)
    gdf = read_kmz(path)
    log.info("airspace: %d zones from %s (effective %s)", len(gdf), path.name, eff)
    return gdf, f"NATS {path.name} (effective {eff.isoformat()})"
