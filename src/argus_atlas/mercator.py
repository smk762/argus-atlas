"""Web Mercator (EPSG:3857) arithmetic — dependency-free.

Enough spherical-Mercator maths to plan a canvas and a zoom range without
pyproj: lon/lat <-> metres, the ground resolution of a zoom level, and XYZ tile
extents (the slippy-map scheme gdal2tiles ``--xyz`` and PMTiles use, y counted
from the north).
"""

from __future__ import annotations

import math

from argus_atlas.models import MAX_LATITUDE, MAX_ZOOM, ZOOM_SPAN, BBox

EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # 20037508.342789244
TILE_SIZE = 256


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Project EPSG:4326 degrees to EPSG:3857 metres."""
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    x = math.radians(lon) * EARTH_RADIUS
    # asinh(tan(lat)) == ln(tan(pi/4 + lat/2)) but is exactly 0 at the equator (no float noise).
    y = math.asinh(math.tan(math.radians(lat))) * EARTH_RADIUS
    return x, y


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Unproject EPSG:3857 metres to EPSG:4326 degrees."""
    lon = math.degrees(x / EARTH_RADIUS)
    lat = math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2)
    return lon, lat


def bbox_to_mercator(bbox: BBox) -> tuple[float, float, float, float]:
    """``(xmin, ymin, xmax, ymax)`` metres for a lon/lat AOI."""
    xmin, ymin = lonlat_to_mercator(bbox.west, bbox.south)
    xmax, ymax = lonlat_to_mercator(bbox.east, bbox.north)
    return xmin, ymin, xmax, ymax


def mercator_to_bbox(xmin: float, ymin: float, xmax: float, ymax: float) -> BBox:
    west, south = mercator_to_lonlat(xmin, ymin)
    east, north = mercator_to_lonlat(xmax, ymax)
    return BBox(west=west, south=south, east=east, north=north)


def resolution_at_zoom(zoom: int, tile_size: int = TILE_SIZE) -> float:
    """Metres per pixel at *zoom* (at the equator, in projected units)."""
    return 2 * ORIGIN_SHIFT / (tile_size * (1 << zoom))


def native_zoom(resolution: float, max_zoom: int = MAX_ZOOM) -> int:
    """The zoom whose resolution is closest (in log space) to *resolution*,
    clipped to ``[0, max_zoom]`` — never a zoom finer than the canvas."""
    if resolution <= 0:
        raise ValueError("resolution must be positive")
    best = min(range(0, 31), key=lambda z: abs(math.log2(resolution_at_zoom(z) / resolution)))
    return max(0, min(max_zoom, best))


def zoom_range(native: int, span: int = ZOOM_SPAN) -> tuple[int, int]:
    """``(native - span, native)`` clipped at 0 — a short pyramid ending at native."""
    return max(0, native - span), native


def tile_bounds(z: int, x: int, y: int, tile_size: int = TILE_SIZE) -> tuple[float, float, float, float]:
    """EPSG:3857 ``(xmin, ymin, xmax, ymax)`` of XYZ tile ``z/x/y`` (y from the north)."""
    res = resolution_at_zoom(z, tile_size) * tile_size  # metres per tile
    xmin = -ORIGIN_SHIFT + x * res
    xmax = xmin + res
    ymax = ORIGIN_SHIFT - y * res
    ymin = ymax - res
    return xmin, ymin, xmax, ymax


def tiles_covering(bounds: tuple[float, float, float, float], z: int) -> tuple[int, int, int, int]:
    """Inclusive XYZ index range ``(xmin, ymin, xmax, ymax)`` of the tiles that
    cover *bounds* at zoom *z*."""
    xmin, ymin, xmax, ymax = bounds
    n = 1 << z
    size = 2 * ORIGIN_SHIFT / n

    def clamp(i: int) -> int:
        return max(0, min(n - 1, i))

    tx0 = clamp(int(math.floor((xmin + ORIGIN_SHIFT) / size)))
    tx1 = clamp(int(math.ceil((xmax + ORIGIN_SHIFT) / size)) - 1)
    ty0 = clamp(int(math.floor((ORIGIN_SHIFT - ymax) / size)))
    ty1 = clamp(int(math.ceil((ORIGIN_SHIFT - ymin) / size)) - 1)
    return tx0, ty0, max(tx0, tx1), max(ty0, ty1)


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float], eps: float = 1e-6) -> bool:
    """Whether two ``(xmin, ymin, xmax, ymax)`` boxes overlap by more than *eps*."""
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps
