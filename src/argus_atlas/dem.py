"""Fetch a Copernicus GLO-30 DEM for a bbox: the step everyone did by hand before ``controls``.

``fetch_dem`` names the 1x1° tiles a lon/lat AOI touches, checks the source has
each of them, and lets ``gdalwarp`` read just the needed windows of the COGs
(over ``/vsicurl/`` for the public bucket, from disk for a local mirror) into
one Float32 GeoTIFF that ``controls --dem`` accepts. The output stays in the
tiles' own EPSG:4326 grid: the window is snapped *inward* to their pixel
lattice, so its bounds sit inside the requested bbox and no pixel is resampled.
With *cover* it is snapped *outward* instead, so the output contains the whole
bbox (still on the lattice): what a chunk canvas needs right at its edge.

``<out>.json`` beside the GeoTIFF records the source, the tile keys, the
requested and written bounds, the output SHA256 and the gdalwarp argv;
``controls`` keeps hashing whatever ``--dem`` points at, so this file is only
the provenance of the download.

CLI only, like ``osm``: there is no server route, because a ``POST`` that
downloads and warps is an open-ended fetch plus a GDAL job on a host that
should only serve finished overlays.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.models import AtlasError, BBox

logger = structlog.get_logger()

DEFAULT_DEM_SOURCE = "https://copernicus-dem-30m.s3.amazonaws.com"
USER_AGENT = "argus-atlas (+https://github.com/smk762/argus-atlas)"
ATTRIBUTION = (
    "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved."
)
# 16 tiles of 3600x3600 Float32 is ~800 MB before the clip: past that, the AOI is not a canvas atlas can paint.
# A block of chunks can need more (a 9x9 block at z10 near 41°S touches 20): callers raise it with max_tiles.
MAX_TILES = 16
# Copernicus omits tiles that are all ocean and puts the sea at 0 m, so a missing tile allowed in is filled with 0.
SEA_LEVEL = 0.0
_SNAP_EPS = 1e-6  # of a pixel: absorbs float noise when a bbox edge already lies on the lattice


class DemSourceError(AtlasError):
    """The source could not be asked (network, HTTP 5xx): a runtime failure, not a bad request."""


def tile_key(lat: int, lon: int) -> str:
    """The GLO-30 key of the 1x1° tile whose south-west corner is (*lat*, *lon*)."""
    ns = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


def tiles_for(bbox: BBox) -> list[str]:
    """Keys of every tile the bbox touches, north to south, west to east."""
    lats = range(math.floor(bbox.south), math.ceil(bbox.north))
    lons = range(math.floor(bbox.west), math.ceil(bbox.east))
    return [tile_key(lat, lon) for lat in reversed(lats) for lon in lons]


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def tile_location(source: str, key: str) -> str:
    """What GDAL opens for *key*: ``/vsicurl/<url>`` for a bucket or mirror, a file path for a local dir."""
    if _is_url(source):
        return f"/vsicurl/{source.rstrip('/')}/{key}/{key}.tif"
    return str(Path(source).expanduser() / key / f"{key}.tif")


def tile_exists(source: str, key: str, *, timeout: float = 30.0) -> bool:
    """Whether the source has *key*. A 403/404 is "no such tile"; anything else is an error, not a gap."""
    if not _is_url(source):
        return Path(tile_location(source, key)).is_file()
    url = tile_location(source, key).removeprefix("/vsicurl/")
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return False
        raise DemSourceError(f"DEM source {source} answered HTTP {exc.code} for {key}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DemSourceError(f"DEM source {source} unreachable: {exc}") from exc


def snap_inward(bbox: BBox, origin: tuple[float, float], res: tuple[float, float]) -> tuple[float, float, float, float]:
    """``(xmin, ymin, xmax, ymax)``: the bbox shrunk to whole pixels of the lattice at *origin* (x, top y)."""
    (ox, oy), (rx, ry) = origin, res
    xmin = ox + math.ceil((bbox.west - ox) / rx - _SNAP_EPS) * rx
    xmax = ox + math.floor((bbox.east - ox) / rx + _SNAP_EPS) * rx
    ymax = oy - math.ceil((oy - bbox.north) / ry - _SNAP_EPS) * ry
    ymin = oy - math.floor((oy - bbox.south) / ry + _SNAP_EPS) * ry
    if xmax <= xmin or ymax <= ymin:
        raise AtlasError(f"bbox {bbox.as_text()} is smaller than one DEM pixel ({rx:.6g} x {ry:.6g} degrees)")
    return xmin, ymin, xmax, ymax


def snap_outward(
    bbox: BBox, origin: tuple[float, float], res: tuple[float, float]
) -> tuple[float, float, float, float]:
    """``(xmin, ymin, xmax, ymax)``: the bbox grown to whole pixels of the lattice at *origin* (x, top y)."""
    (ox, oy), (rx, ry) = origin, res
    xmin = ox + math.floor((bbox.west - ox) / rx + _SNAP_EPS) * rx
    xmax = ox + math.ceil((bbox.east - ox) / rx - _SNAP_EPS) * rx
    ymax = oy - math.floor((oy - bbox.north) / ry + _SNAP_EPS) * ry
    ymin = oy - math.ceil((oy - bbox.south) / ry - _SNAP_EPS) * ry
    return xmin, ymin, xmax, ymax


def _lattice(location: str) -> tuple[tuple[float, float], tuple[float, float]]:
    gt = gdal_ops.gdalinfo_json(location)["geoTransform"]  # type: ignore[arg-type]  # a /vsicurl/ path is a str
    return (gt[0], gt[3]), (gt[1], -gt[5])


def _check_count(bbox: BBox, keys: list[str], max_tiles: int) -> None:
    if len(keys) > max_tiles:
        raise AtlasError(
            f"bbox {bbox.as_text()} touches {len(keys)} DEM tiles; the cap is {max_tiles} (raise it with --max-tiles)"
        )


def _check_present(
    source: str, keys: list[str], allow_missing: bool, *, known: frozenset[str] = frozenset()
) -> tuple[list[str], list[str]]:
    """``(present, missing)`` keys, or AtlasError naming the missing ones when they may not be filled.

    *known* are keys already found present, so they are not asked about again.
    """
    present = [k for k in keys if k in known or tile_exists(source, k)]
    missing = [k for k in keys if k not in present]
    if missing and (not allow_missing or not present):
        where = f"under {source}" if not _is_url(source) else f"at {source}"
        hint = "" if allow_missing or not present else " (all-ocean tiles are absent by design: --allow-missing)"
        raise AtlasError(f"no DEM tile {', '.join(missing)} {where}{hint}")
    return present, missing


def fetch_dem(
    bbox: BBox,
    out: Path,
    *,
    source: str = DEFAULT_DEM_SOURCE,
    allow_missing: bool = False,
    cover: bool = False,
    max_tiles: int = MAX_TILES,
) -> dict:
    """Clip the GLO-30 tiles under *bbox* into *out*. Returns the manifest written to ``<out>.json``.

    A tile the source lacks is an error that names it, unless *allow_missing*, which fills
    it with sea level (Copernicus leaves out all-ocean tiles) as long as one tile exists.
    With *cover* the window snaps outward, so the output contains all of *bbox*; that can
    reach half a pixel into a tile the bbox itself does not touch, which is then fetched too.
    """
    if out.suffix.lower() not in (".tif", ".tiff"):
        raise AtlasError(f"the DEM is written as GeoTIFF: {out} must end in .tif")
    keys = tiles_for(bbox)
    _check_count(bbox, keys, max_tiles)
    gdal_ops.require_gdal()
    logger.info("dem.tiles", source=source, tiles=keys)
    present, missing = _check_present(source, keys, allow_missing)
    # The finest lattice wins; GLO-30 tiles in one latitude band share it, so nothing is resampled.
    origin, res = min(
        (_lattice(tile_location(source, k)) for k in present), key=lambda lattice: lattice[1][0] * lattice[1][1]
    )
    if cover:
        bounds = snap_outward(bbox, origin, res)
        xmin, ymin, xmax, ymax = bounds  # clamped: half a pixel past the antimeridian is not a tile
        grown = tiles_for(
            BBox(west=max(xmin, -180.0), south=max(ymin, -90.0), east=min(xmax, 180.0), north=min(ymax, 90.0))
        )
        extra = [k for k in grown if k not in keys]
        if extra:
            _check_count(bbox, grown, max_tiles)
            logger.info("dem.cover_tiles", tiles=extra)
            keys = grown
            present, missing = _check_present(source, grown, allow_missing, known=frozenset(present))
    else:
        bounds = snap_inward(bbox, origin, res)
    locations = [tile_location(source, k) for k in present]
    out.parent.mkdir(parents=True, exist_ok=True)
    command = gdal_ops.run(gdal_ops.dem_clip_argv(locations, out, bounds, res, fill=SEA_LEVEL))
    manifest = {
        "source": source,
        "product": "Copernicus GLO-30 DSM",
        "attribution": ATTRIBUTION,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "bbox": bbox.as_text(),
        "snap": "outward" if cover else "inward",
        "bounds": [round(v, 9) for v in bounds],
        "resolution": list(res),
        "tiles": present,
        "missing": missing,
        "filled": SEA_LEVEL if missing else None,
        "output": {"path": out.name, "sha256": sha256_file(out), "bytes": out.stat().st_size},
        "commands": [command.model_dump()],
    }
    out.with_suffix(".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("dem.done", tiles=present, missing=missing, bytes=manifest["output"]["bytes"])
    return manifest
