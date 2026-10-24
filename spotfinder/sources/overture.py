"""Overture Maps layers (OSM-derived land use, water, roads and paths, buildings, places).

Overture publishes GeoParquet on a public S3 bucket, sorted spatially, so a bbox
filter only pulls the row groups we need: a 40 x 40 km region is tens of MB, not
the whole planet.
"""

from __future__ import annotations

import logging
import os

import geopandas as gpd
import pandas as pd
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import shapely

from ..config import BNG, Region

log = logging.getLogger(__name__)

RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-10-21.0")
BUCKET = "overturemaps-us-west-2"

# theme, type, columns to keep (besides id/geometry)
LAYERS: dict[str, tuple[str, str, list[str]]] = {
    "land_use": ("base", "land_use", ["subtype", "class", "names", "source_tags"]),
    "land": ("base", "land", ["subtype", "class", "names", "source_tags"]),
    "water": ("base", "water", ["subtype", "class", "names", "source_tags"]),
    "infrastructure": ("base", "infrastructure", ["subtype", "class", "names", "source_tags"]),
    "buildings": ("buildings", "building", ["subtype", "class", "names", "height", "num_floors"]),
    "segments": ("transportation", "segment", ["subtype", "class", "names", "road_flags"]),
    "places": ("places", "place", ["basic_category", "taxonomy", "names", "confidence", "operating_status"]),
}


def _filesystem() -> pafs.S3FileSystem:
    kwargs = {"anonymous": True, "region": "us-west-2"}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        kwargs["proxy_options"] = proxy
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("AWS_CA_BUNDLE")
    if ca:
        kwargs["tls_ca_file_path"] = ca
    return pafs.S3FileSystem(**kwargs)


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if "names" in df:
        df["name"] = df.pop("names").map(lambda n: (n or {}).get("primary") if isinstance(n, dict) else None)
    if "source_tags" in df:
        df["tags"] = df.pop("source_tags").map(lambda t: dict(t) if t is not None else {})
    if "taxonomy" in df:
        df["category"] = df.pop("taxonomy").map(lambda t: t.get("primary") if isinstance(t, dict) else None)
    if "road_flags" in df:
        df["is_bridge"] = df.pop("road_flags").map(_has_bridge_flag)
    return df


def _has_bridge_flag(flags) -> bool:
    if flags is None:
        return False
    for f in flags:
        values = f.get("values")
        if values is not None and "is_bridge" in list(values):
            return True
    return False


def fetch_layer(region: Region, layer: str, refresh: bool = False) -> gpd.GeoDataFrame:
    """Return the layer clipped to the region bbox (plus a 1 km pad), in BNG, cached as GeoParquet."""
    cache = region.cache_dir / f"overture_{layer}.parquet"
    if cache.exists() and not refresh:
        return gpd.read_parquet(cache)

    theme, typ, cols = LAYERS[layer]
    west, south, east, north = region.lonlat_bbox(pad_m=1000)
    dataset = ds.dataset(
        f"{BUCKET}/release/{RELEASE}/theme={theme}/type={typ}/", filesystem=_filesystem(), format="parquet"
    )
    bbox_filter = (
        (ds.field("bbox", "xmin") <= east)
        & (ds.field("bbox", "xmax") >= west)
        & (ds.field("bbox", "ymin") <= north)
        & (ds.field("bbox", "ymax") >= south)
    )
    log.info("Overture %s: fetching %s/%s", layer, theme, typ)
    table = dataset.to_table(filter=bbox_filter, columns=["id", "geometry", *cols])
    df = _flatten(table.to_pandas())
    gdf = gpd.GeoDataFrame(df, geometry=shapely.from_wkb(df.pop("geometry")), crs="EPSG:4326").to_crs(BNG)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
    # Parquet can't store dict columns with mixed keys well; keep tags as JSON-ish strings on disk.
    out = gdf.copy()
    if "tags" in out:
        out["tags"] = out["tags"].map(lambda t: "" if not t else repr(sorted(t.items())))
    out.to_parquet(cache)
    log.info("Overture %s: %d features", layer, len(gdf))
    return gdf if "tags" not in gdf else _restore_tags(out)


def _restore_tags(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    import ast

    gdf = gdf.copy()
    gdf["tags"] = gdf["tags"].map(lambda s: dict(ast.literal_eval(s)) if s else {})
    return gdf


def load_layer(region: Region, layer: str) -> gpd.GeoDataFrame:
    gdf = fetch_layer(region, layer)
    if "tags" in gdf and len(gdf) and isinstance(gdf["tags"].iloc[0], str):
        gdf = _restore_tags(gdf)
    return gdf


def fetch_all(region: Region, refresh: bool = False) -> None:
    for layer in LAYERS:
        fetch_layer(region, layer, refresh=refresh)
