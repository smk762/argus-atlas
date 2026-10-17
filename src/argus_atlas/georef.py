"""Stamp the painted PNG into a GeoTIFF, then gate it against the sidecar.

The model never sees a geotransform; ``gdal_translate -a_srs -a_ullr`` assigns
the sidecar's. The **alignment gate** then re-reads the GeoTIFF with
``gdalinfo -json`` and requires, before any tile is written:

* width and height equal to the control canvas,
* SRS EPSG:3857,
* corner coordinates matching the sidecar within ``ALIGN_TOLERANCE`` map units.

On failure the PNG is kept, no ``fantasy.tif`` is left behind, and
:class:`AlignmentError` says which check failed.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.models import ALIGN_TOLERANCE, EPSG_CODE, AtlasError, GdalCommand, Grid
from argus_atlas.png import png_size

logger = structlog.get_logger()

FANTASY_PNG = "fantasy.png"
FANTASY_TIF = "fantasy.tif"

_EPSG_ID = re.compile(r'ID\["EPSG",\s*(\d+)\]')
_EPSG_AUTH = re.compile(r'AUTHORITY\["EPSG",\s*"(\d+)"\]')


class AlignmentError(AtlasError):
    """The painted image does not sit on the sidecar grid."""


def check_png_canvas(png: Path, grid: Grid) -> tuple[int, int]:
    """The PNG's size, or :class:`AlignmentError` if it is not the canvas size."""
    size = png_size(png)
    if size != (grid.width, grid.height):
        raise AlignmentError(f"{png.name} is {size[0]}x{size[1]}, canvas is {grid.width}x{grid.height}")
    return size


def epsg_from_info(info: dict) -> int | None:
    """The EPSG code of a ``gdalinfo -json`` result (STAC field, else parsed from WKT)."""
    stac = info.get("stac") or {}
    code = stac.get("proj:epsg")
    if isinstance(code, int):
        return code
    wkt = (info.get("coordinateSystem") or {}).get("wkt") or ""
    ids = _EPSG_ID.findall(wkt) or _EPSG_AUTH.findall(wkt)
    return int(ids[-1]) if ids else None


def verify_alignment(tif: Path, grid: Grid, *, tol: float = ALIGN_TOLERANCE) -> dict:
    """Gate *tif* against *grid*; return the ``gdalinfo -json`` dict on success."""
    info = gdal_ops.gdalinfo_json(tif)
    size = info.get("size")
    if size != [grid.width, grid.height]:
        raise AlignmentError(f"{tif.name} is {size}, canvas is {[grid.width, grid.height]}")
    epsg = epsg_from_info(info)
    if epsg != EPSG_CODE:
        raise AlignmentError(f"{tif.name} SRS is EPSG:{epsg}, expected EPSG:{EPSG_CODE}")
    corners = info.get("cornerCoordinates") or {}
    try:
        ulx, uly = corners["upperLeft"]
        lrx, lry = corners["lowerRight"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AlignmentError(f"{tif.name} has no corner coordinates") from exc
    if not grid.corners_match(ulx, uly, lrx, lry, tol):
        raise AlignmentError(
            f"{tif.name} corners ({ulx}, {uly})-({lrx}, {lry}) differ from the sidecar "
            f"({grid.ulx}, {grid.uly})-({grid.lrx}, {grid.lry}) by more than {tol}"
        )
    return info


def stamp(png: Path, tif: Path, grid: Grid) -> GdalCommand:
    """Write *tif* from *png* with the sidecar grid, passing the alignment gate.

    The PNG size is checked first (no GeoTIFF is attempted for a wrong-size
    image); the GeoTIFF is written to a temp name and renamed only once the
    gate passes, so a failed gate never leaves a ``fantasy.tif`` behind.
    """
    check_png_canvas(png, grid)
    part = tif.with_name(f".{tif.name}.part")
    if part.exists():
        part.unlink()
    command = gdal_ops.run(gdal_ops.stamp_argv(png, part, grid))
    try:
        verify_alignment(part, grid)
    except AlignmentError:
        part.unlink(missing_ok=True)
        gdal_ops.strip_aux(part)
        raise
    if tif.exists():
        tif.unlink()
    part.replace(tif)
    logger.info("georef.stamped", tif=str(tif), width=grid.width, height=grid.height)
    return command
