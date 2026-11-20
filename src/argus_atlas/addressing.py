"""Chunk tile addressing: which XYZ tile a chunk is, its grids, its neighbours and its job id.

Pure arithmetic over :mod:`argus_atlas.mercator` and the :mod:`argus_atlas.models` types, kept apart
from :mod:`argus_atlas.chunks` (which re-exports all of it) so a caller that only needs the addressing
— argus-realms' deterministic game rules, the Pyodide arena — doesn't load GDAL, the bake stack or a
network client. ``test_addressing`` holds it to that.
"""

from __future__ import annotations

import re

from argus_atlas.mercator import mercator_to_bbox, tile_bounds
from argus_atlas.models import MAX_LONG_SIDE, MAX_ZOOM, SIDE_MULTIPLE, AtlasError, BBox, Grid

# 256·2^k: the chunk's native tiles are whole tiles of the XYZ grid.
CHUNK_SIZES: tuple[int, ...] = (256, 512, 1024)
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_MARGIN = 128
# Keeps the canvas a multiple of 64 and the seam on SDXL's 8-px latent grid.
MARGIN_MULTIPLE = SIDE_MULTIPLE // 2
TILE_PX = 256

# ASCII digits only (str.isdigit() also takes '²', which int() refuses), and few enough that
# int() and 1 << z stay cheap for any input; parse_tile caps the zoom before shifting.
_TILE = re.compile(r"(\d{1,2})/(\d{1,9})/(\d{1,9})", re.ASCII)


def parse_tile(text: str) -> tuple[int, int, int]:
    """``"z/x/y"`` -> ``(z, x, y)``: a zoom no finer than :data:`MAX_ZOOM`, x and y inside its tile grid."""
    match = _TILE.fullmatch(text.strip())
    if match is None:
        raise AtlasError(f"tile must be z/x/y with non-negative integers, got {text!r}")
    z, x, y = (int(p) for p in match.groups())
    if z > MAX_ZOOM:
        raise AtlasError(f"tile {text} is at zoom {z}, finer than {MAX_ZOOM}")
    n = 1 << z
    if x >= n or y >= n:
        raise AtlasError(f"tile {text} is outside zoom {z} (x and y must be below {n})")
    return z, x, y


def chunk_native_zoom(z: int, size: int) -> int:
    return z + (size // TILE_PX).bit_length() - 1


def validate_chunk(z: int, size: int, margin: int) -> None:
    """Refuse a size/margin that breaks the lattice, the canvas cap or the zoom cap."""
    if size not in CHUNK_SIZES:
        raise AtlasError(f"chunk size must be one of {', '.join(map(str, CHUNK_SIZES))} px, got {size}")
    if margin < 0 or margin % MARGIN_MULTIPLE:
        raise AtlasError(f"chunk margin must be a non-negative multiple of {MARGIN_MULTIPLE} px, got {margin}")
    if size + 2 * margin > MAX_LONG_SIDE:
        raise AtlasError(f"a {size} px chunk with a {margin} px margin is {size + 2 * margin} px, over {MAX_LONG_SIDE}")
    native = chunk_native_zoom(z, size)
    if native > MAX_ZOOM:
        raise AtlasError(f"a {size} px chunk at zoom {z} has native zoom {native}, finer than {MAX_ZOOM}")


def chunk_grids(
    z: int, x: int, y: int, *, size: int = DEFAULT_CHUNK_SIZE, margin: int = DEFAULT_MARGIN
) -> tuple[Grid, Grid]:
    """``(canvas, inner)``: the tile plus margin the model paints, and the tile itself."""
    validate_chunk(z, size, margin)
    xmin, _, xmax, ymax = tile_bounds(z, x, y)
    res = (xmax - xmin) / size
    inner = Grid(width=size, height=size, ulx=xmin, uly=ymax, pixel_width=res, pixel_height=-res)
    side = size + 2 * margin
    canvas = Grid(
        width=side, height=side, ulx=xmin - margin * res, uly=ymax + margin * res, pixel_width=res, pixel_height=-res
    )
    return canvas, inner


def chunk_bbox(z: int, x: int, y: int) -> BBox:
    """The tile's lon/lat extent (the job's AOI), clamped where edge tiles meet the antimeridian or the cap."""
    return mercator_to_bbox(*tile_bounds(z, x, y))


def canvas_bbox(z: int, x: int, y: int, *, size: int = DEFAULT_CHUNK_SIZE, margin: int = DEFAULT_MARGIN) -> BBox:
    """The lon/lat box of the chunk's canvas, margin included: what its DEM and OSM must cover."""
    canvas, _ = chunk_grids(z, x, y, size=size, margin=margin)
    return mercator_to_bbox(*canvas.bounds)


def neighbour_tiles(z: int, x: int, y: int) -> list[tuple[int, int, int]]:
    """The up-to-eight tiles around ``z/x/y`` (no wrap across the antimeridian)."""
    n = 1 << z
    return [
        (z, x + dx, y + dy)
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if (dx or dy) and 0 <= x + dx < n and 0 <= y + dy < n
    ]


def chunk_job_id(recipe: str, z: int, x: int, y: int) -> str:
    return f"{recipe}-{z}-{x}-{y}"
