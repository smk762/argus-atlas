"""GDAL as subprocesses: argv builders plus one runner.

GDAL is a **system package** (``gdal-bin``), never a pip module, and every
operation here is a CLI call (``gdalwarp``, ``gdaldem``, ``ogr2ogr``,
``gdal_rasterize``, ``gdal_calc``, ``gdal_translate``, ``gdalinfo``, ``gdal2tiles``). Builders return plain argv
lists so the exact commands can be recorded in the job sidecar and replayed;
:func:`run` executes one and turns a non-zero exit into a :class:`GdalError`
carrying the tail of stderr.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import structlog

from argus_atlas.models import CRS, AtlasError, GdalCommand, Grid

logger = structlog.get_logger()


class GdalError(AtlasError):
    """A GDAL tool is missing, or one exited non-zero."""


GDAL_TOOLS: tuple[str, ...] = ("gdalinfo", "gdalwarp", "gdaldem", "gdal_rasterize", "gdal_translate", "ogr2ogr")
# Debian/Ubuntu ship the tiler as gdal2tiles.py (python3-gdal); some builds drop the suffix.
TILER_CANDIDATES: tuple[str, ...] = ("gdal2tiles.py", "gdal2tiles")
# Same for the raster calculator used to paint the composite control.
CALC_CANDIDATES: tuple[str, ...] = ("gdal_calc.py", "gdal_calc")
# The seam blend of a chunk also needs the distance and gap-filling scripts (same package as gdal_calc).
PROXIMITY_CANDIDATES: tuple[str, ...] = ("gdal_proximity.py", "gdal_proximity")
FILLNODATA_CANDIDATES: tuple[str, ...] = ("gdal_fillnodata.py", "gdal_fillnodata")
# What gdal_proximity writes beyond its -maxdist (UInt16 output), so "far" is one comparison.
PROXIMITY_FAR = 65535

DEFAULT_TIMEOUT = 900.0


def gdal_tools() -> dict[str, str | None]:
    """Each GDAL tool this package calls -> its resolved path (or ``None``)."""
    return {name: shutil.which(name) for name in GDAL_TOOLS}


def gdal_version() -> str | None:
    """``gdalinfo --version`` output, or ``None`` when GDAL is not installed."""
    if shutil.which("gdalinfo") is None:
        return None
    try:
        out = subprocess.run(["gdalinfo", "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr).strip()
    return text or None


def require_gdal() -> str:
    """The GDAL version, or :class:`GdalError` naming what is missing."""
    missing = [name for name, path in gdal_tools().items() if path is None]
    if missing:
        raise GdalError(
            f"GDAL is required but these tools are not on PATH: {', '.join(missing)} — install gdal-bin (apt) or gdal (dnf)"
        )
    version = gdal_version()
    if version is None:
        raise GdalError("gdalinfo --version failed; GDAL is not usable")
    return version


def find_tiler() -> str | None:
    """Path of ``gdal2tiles.py`` (or ``gdal2tiles``), or ``None``."""
    for name in TILER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_calc() -> str | None:
    """Path of ``gdal_calc.py`` (or ``gdal_calc``), or ``None``."""
    return find_script(CALC_CANDIDATES)


def find_script(candidates: tuple[str, ...]) -> str | None:
    """Path of the first of *candidates* on PATH (GDAL's Python scripts, with or without ``.py``), or ``None``."""
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def run(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT, cwd: Path | None = None) -> GdalCommand:
    """Run one GDAL command; raise :class:`GdalError` on failure or a missing tool."""
    tool = Path(argv[0]).name
    logger.debug("gdal.run", argv=argv)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None, check=False
        )
    except FileNotFoundError as exc:
        raise GdalError(f"{tool} is not installed (not on PATH)") from exc
    except subprocess.TimeoutExpired as exc:
        raise GdalError(f"{tool} timed out after {timeout:.0f}s: {' '.join(argv)}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        raise GdalError(f"{tool} exited {proc.returncode}: {' '.join(argv)}\n{tail}")
    return GdalCommand(tool=tool, argv=list(argv))


def gdalinfo_json(path: Path, *, stats: bool = False) -> dict:
    """``gdalinfo -json`` of *path* as a dict; *stats* adds exact per-band statistics (NoData excluded)."""
    argv = ["gdalinfo", "-json", *(["-stats"] if stats else []), str(path)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise GdalError("gdalinfo is not installed (not on PATH)") from exc
    if proc.returncode != 0:
        raise GdalError(f"gdalinfo failed on {path}: {(proc.stderr or '').strip()[-400:]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise GdalError(f"gdalinfo -json returned unparseable output for {path}") from exc


# -- argv builders ---------------------------------------------------------------


def _te(grid: Grid) -> list[str]:
    xmin, ymin, xmax, ymax = grid.bounds
    return ["-te", repr(xmin), repr(ymin), repr(xmax), repr(ymax)]


def _ts(grid: Grid) -> list[str]:
    # -ts (exact pixel count) rather than -tr: with both -te and -ts the output
    # grid is exactly the sidecar's, with no rounding of extent/resolution.
    return ["-ts", str(grid.width), str(grid.height)]


RESAMPLING_METHODS: tuple[str, ...] = ("bilinear", "cubic", "cubicspline", "lanczos", "average")
# Above this DEM-cell-to-canvas-pixel ratio bilinear leaves a visible cell staircase in the
# hillshade (piecewise-linear surface, gradient jumps at every cell edge); a spline is smooth.
UPSAMPLE_SPLINE_RATIO = 2.0


def dem_pixel_metres(dem: Path, latitude: float) -> float | None:
    """The DEM's cell size in metres (geographic CRSs converted at *latitude*), or ``None`` if unknown."""
    try:
        info = gdalinfo_json(dem)
        gt = info["geoTransform"]
        wkt = str(info.get("coordinateSystem", {}).get("wkt", ""))
    except (GdalError, KeyError, TypeError):
        return None
    cell = (abs(float(gt[1])) + abs(float(gt[5]))) / 2
    if wkt.startswith(("GEOGCRS", "GEOGCS")):
        return cell * 111320.0 * max(0.1, math.cos(math.radians(latitude)))
    return cell


def choose_resampling(dem_metres: float | None, canvas_metres: float) -> str:
    """``cubicspline`` when the canvas is much finer than the DEM, else ``bilinear``."""
    if dem_metres is not None and dem_metres / canvas_metres >= UPSAMPLE_SPLINE_RATIO:
        return "cubicspline"
    return "bilinear"


def warp_argv(dem: Path, dst: Path, grid: Grid, *, resampling: str = "bilinear") -> list[str]:
    """Warp + clip a DEM onto the canvas grid in EPSG:3857 (Float32)."""
    if resampling not in RESAMPLING_METHODS:
        raise GdalError(f"unknown resampling {resampling!r}; choose from {', '.join(RESAMPLING_METHODS)}")
    return [
        "gdalwarp",
        "-overwrite",
        "-t_srs",
        CRS,
        "-r",
        resampling,
        *_te(grid),
        *_ts(grid),
        "-ot",
        "Float32",
        "-dstnodata",
        "-9999",
        "-co",
        "COMPRESS=DEFLATE",
        str(dem),
        str(dst),
    ]


def hillshade_argv(dem_3857: Path, dst: Path, *, azimuth: float = 315.0, altitude: float = 45.0) -> list[str]:
    """Byte hillshade. ``-compute_edges`` keeps the border lit, so the model is
    not handed a one-pixel black frame to imitate."""
    return [
        "gdaldem",
        "hillshade",
        "-compute_edges",
        "-az",
        repr(azimuth),
        "-alt",
        repr(altitude),
        "-co",
        "COMPRESS=DEFLATE",
        str(dem_3857),
        str(dst),
    ]


def slope_argv(dem_3857: Path, dst: Path) -> list[str]:
    """Slope in degrees (Float32)."""
    return ["gdaldem", "slope", "-compute_edges", "-co", "COMPRESS=DEFLATE", str(dem_3857), str(dst)]


def reproject_argv(
    vector: Path, dst: Path, grid: Grid, *, layer: str | None = None, where: str | None = None
) -> list[str]:
    """Reproject an OGR source to EPSG:3857 and clip it to the canvas, as GeoJSON.

    ``gdal_rasterize`` never reprojects (its ``-a_srs`` only labels the output),
    so every vector goes through this step before it is burnt.
    """
    xmin, ymin, xmax, ymax = grid.bounds
    argv = ["ogr2ogr", "-f", "GeoJSON", "-t_srs", CRS, "-clipdst", repr(xmin), repr(ymin), repr(xmax), repr(ymax)]
    if where:
        argv += ["-where", where]
    argv += [str(dst), str(vector)]
    if layer:
        argv.append(layer)
    return argv


def rasterize_argv(
    vector: Path, dst: Path, grid: Grid, *, all_touched: bool = False, burn: int = 255, init: int = 0
) -> list[str]:
    """Burn an EPSG:3857 vector (see :func:`reproject_argv`) to a 0/255 Byte mask on the canvas grid.

    *all_touched* burns every pixel a geometry touches: use it for line layers
    (roads, rivers) so a one-pixel road does not break up at 45°. *burn*/*init*
    swap for an inverted mask (the seam mask burns neighbours as 0 on 255).
    """
    return [
        "gdal_rasterize",
        "-burn",
        str(burn),
        "-ot",
        "Byte",
        "-init",
        str(init),
        "-a_srs",
        CRS,
        *_te(grid),
        *_ts(grid),
        *(["-at"] if all_touched else []),
        "-co",
        "COMPRESS=DEFLATE",
        str(vector),
        str(dst),
    ]


def osm_layer_argv(osm: Path, dst: Path, osm_layer: str, where: str) -> list[str]:
    """Extract one thematic layer from an ``.osm``/``.pbf`` file with GDAL's OSM
    driver (which assembles multipolygon relations) into EPSG:4326 GeoJSON."""
    return ["ogr2ogr", "-f", "GeoJSON", "-where", where, str(dst), str(osm), osm_layer]


def places_argv(
    src: Path, dst: Path, bbox: tuple[float, float, float, float], *, layer: str | None = None, where: str | None = None
) -> list[str]:
    """Copy the features of a places file inside a lon/lat *bbox* (west, south, east, north) to
    EPSG:4326 GeoJSON. ``-spat`` filters, it does not clip, so a point comes through whole or not at all."""
    argv = ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-spat", *map(repr, bbox), "-spat_srs", "EPSG:4326"]
    if where:
        argv += ["-where", where]
    argv += [str(dst), str(src)]
    if layer:
        argv.append(layer)
    return argv


# Letters gdal_calc accepts for its inputs: A is the hillshade, the rest are masks in paint order.
_CALC_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def composite_argv(
    calc: str,
    hillshade: Path,
    masks: list[tuple[Path, tuple[int, int, int], bool]],
    dst: Path,
) -> list[str]:
    """Paint masks over the hillshade into an RGB GeoTIFF with ``gdal_calc``.

    *masks* is ``[(mask_tif, (r, g, b), relief), ...]`` in paint order — the
    first entry ends up on top. With *relief* the colour is modulated by the
    hillshade (forests, towns); without it the area is flat (water, roads).
    """
    if len(masks) >= len(_CALC_LETTERS):
        raise GdalError(f"too many layers for gdal_calc ({len(masks)})")
    argv = [calc, "--quiet", "--overwrite", "--hideNoData", "--NoDataValue=none", "-A", str(hillshade)]
    for i, (path, _, _) in enumerate(masks):
        argv += [f"-{_CALC_LETTERS[i + 1]}", str(path)]
    for band in range(3):
        expr = "A"
        for i, (_, rgb, relief) in reversed(list(enumerate(masks))):
            letter = _CALC_LETTERS[i + 1]
            value = f"{rgb[band]}*(0.5+A/510.0)" if relief else str(rgb[band])
            expr = f"where({letter}>0,{value},{expr})"
        argv += ["--calc", expr]
    argv += ["--type", "Byte", "--co", "COMPRESS=DEFLATE", "--outfile", str(dst)]
    return argv


def sea_mask_argv(calc: str, dem_3857: Path, dst: Path, level: float) -> list[str]:
    """0/255 mask of DEM cells at or below *level* metres (NoData cells excluded), via ``gdal_calc``."""
    return [
        calc,
        "--quiet",
        "--overwrite",
        "--hideNoData",
        "--NoDataValue=none",
        "-A",
        str(dem_3857),
        "--calc",
        f"255*logical_and(A<={level!r},A>-9000)",
        "--type",
        "Byte",
        "--co",
        "COMPRESS=DEFLATE",
        "--outfile",
        str(dst),
    ]


def to_png_argv(
    src: Path, dst: Path, *, scale: tuple[float, float, int, int] | None = None, nodata_none: bool = False
) -> list[str]:
    """Export a raster as an 8-bit PNG, optionally rescaling ``src_min src_max -> dst_min dst_max``.

    *nodata_none* drops the NoData flag (``gdal_rasterize`` marks 0 as NoData,
    which would become a transparency chunk in the PNG).
    """
    argv = ["gdal_translate", "-of", "PNG", "-ot", "Byte"]
    if nodata_none:
        argv += ["-a_nodata", "none"]
    if scale is not None:
        argv += ["-scale", repr(scale[0]), repr(scale[1]), str(scale[2]), str(scale[3])]
    return argv + [str(src), str(dst)]


def stamp_argv(png: Path, tif: Path, grid: Grid, *, description: str | None = None) -> list[str]:
    """Georeference a painted PNG into a GeoTIFF from the sidecar grid. *description*
    (the bake payload JSON) goes into the standard ``TIFFTAG_IMAGEDESCRIPTION`` tag."""
    # Explicit driver: the gate writes to a ".part" temp name GDAL can't infer from.
    tags = (
        ["-mo", f"TIFFTAG_IMAGEDESCRIPTION={description}", "-mo", "TIFFTAG_SOFTWARE=argus-atlas"] if description else []
    )
    return [
        "gdal_translate",
        "-of",
        "GTiff",
        *tags,
        "-a_srs",
        CRS,
        "-a_ullr",
        repr(grid.ulx),
        repr(grid.uly),
        repr(grid.lrx),
        repr(grid.lry),
        "-co",
        "COMPRESS=DEFLATE",
        str(png),
        str(tif),
    ]


def rgb_copy_argv(src: Path, dst: Path, *, grey: bool = False) -> list[str]:
    """Copy a raster to a 3-band RGB GeoTIFF with no NoData flag; *grey* repeats band 1 three times."""
    bands = ["-b", "1", "-b", "1", "-b", "1"] if grey else ["-b", "1", "-b", "2", "-b", "3"]
    return [
        "gdal_translate",
        "-of",
        "GTiff",
        *bands,
        "-a_nodata",
        "none",
        "-co",
        "COMPRESS=DEFLATE",
        str(src),
        str(dst),
    ]


def paste_argv(src: Path, dst: Path) -> list[str]:
    """Warp *src* into the **existing** RGB raster *dst* (no ``-overwrite``).

    Chunks share one pixel lattice, so nearest-neighbour copies a neighbour's
    pixels exactly; *dst* keeps its own pixels wherever *src* has none. Bands map
    1:1 (an alpha band of *src* is used as its mask), so no ``-srcband``/``-dstband``,
    which only exist since GDAL 3.7.
    """
    return ["gdalwarp", "-r", "near", str(src), str(dst)]


def crop_argv(src: Path, dst: Path, xoff: int, yoff: int, width: int, height: int) -> list[str]:
    """Cut a pixel window out of *src*, keeping its georeferencing and metadata."""
    return [
        "gdal_translate",
        "-of",
        "GTiff",
        "-srcwin",
        str(xoff),
        str(yoff),
        str(width),
        str(height),
        "-co",
        "COMPRESS=DEFLATE",
        str(src),
        str(dst),
    ]


def tiles_argv(tiler: str, tif: Path, out_dir: Path, zmin: int, zmax: int) -> list[str]:
    """XYZ tiles for ``zmin..zmax`` only, no viewer HTML, from an EPSG:3857 raster."""
    return [
        tiler,
        "--xyz",
        "-p",
        "mercator",
        "-s",
        CRS,
        "-z",
        f"{zmin}-{zmax}",
        "-r",
        "bilinear",
        "-w",
        "none",
        "--processes=1",
        "-q",
        str(tif),
        str(out_dir),
    ]


def calc_argv(
    calc: str,
    inputs: dict[str, Path],
    expr: str,
    dst: Path,
    *,
    bands: dict[str, int] | None = None,
    all_bands: str | None = None,
    out_type: str = "Byte",
    nodata: float | None = None,
) -> list[str]:
    """One ``gdal_calc`` expression over lettered inputs into a GeoTIFF.

    Input NoData is ignored (``--hideNoData``) so every expression sees raw values;
    *nodata* only flags the output. *bands* picks a band per letter; *all_bands*
    runs the expression once per band of that letter's input.
    """
    argv = [calc, "--quiet", "--overwrite", "--hideNoData"]
    for letter, path in inputs.items():
        argv += [f"-{letter}", str(path)]
        if bands and letter in bands:
            argv.append(f"--{letter}_band={bands[letter]}")
    if all_bands:
        argv.append(f"--allBands={all_bands}")
    flag = "none" if nodata is None else repr(nodata)
    argv += ["--calc", expr, "--type", out_type, f"--NoDataValue={flag}"]
    return argv + ["--co", "COMPRESS=DEFLATE", "--outfile", str(dst)]


def proximity_argv(tool: str, src: Path, dst: Path, *, values: int, maxdist: int) -> list[str]:
    """Pixel distance from every pixel to the nearest pixel of *src* equal to *values* (0 on them).

    Pixels farther than *maxdist* get :data:`PROXIMITY_FAR`.
    """
    return [
        tool,
        "-q",
        str(src),
        str(dst),
        "-values",
        str(values),
        "-distunits",
        "PIXEL",
        "-maxdist",
        str(maxdist),
        "-nodata",
        str(PROXIMITY_FAR),
        "-ot",
        "UInt16",
        "-co",
        "COMPRESS=DEFLATE",
    ]


def fillnodata_argv(tool: str, src: Path, dst: Path, *, maxdist: int) -> list[str]:
    """Spread the valid pixels of *src* into its NoData up to *maxdist* px (inverse-distance), no smoothing."""
    return [tool, "-q", "-md", str(maxdist), "-si", "0", "-of", "GTiff", str(src), str(dst)]


def resize_argv(src: Path, dst: Path, width: int, height: int, *, method: str) -> list[str]:
    """Resample *src* to *width* x *height* px over the same extent (``average`` down, a spline up)."""
    return ["gdal_translate", "-of", "GTiff", "-outsize", str(width), str(height), "-r", method, str(src), str(dst)]


def to_tif_argv(src: Path, dst: Path, *, description: str | None = None) -> list[str]:
    """Write *src* (a VRT stack) as a Byte GeoTIFF, with the bake payload in ``TIFFTAG_IMAGEDESCRIPTION``."""
    tags = (
        ["-mo", f"TIFFTAG_IMAGEDESCRIPTION={description}", "-mo", "TIFFTAG_SOFTWARE=argus-atlas"] if description else []
    )
    # A stack of single-band rasters has no colour interpretation; label it RGB like a stamped fantasy.tif,
    # or gdalbuildvrt refuses to mosaic it with unblended chunks.
    return [
        "gdal_translate",
        "-of",
        "GTiff",
        "-ot",
        "Byte",
        "-colorinterp",
        "red,green,blue",
        *tags,
        "-co",
        "COMPRESS=DEFLATE",
        str(src),
        str(dst),
    ]


def stack_argv(bands: list[Path], dst_vrt: Path) -> list[str]:
    """One VRT whose bands are the single-band rasters *bands*, in order."""
    return ["gdalbuildvrt", "-q", "-separate", str(dst_vrt), *map(str, bands)]


def strip_aux(path: Path) -> None:
    """Remove the ``.aux.xml`` sidecar GDAL writes next to a PNG, so the model
    inputs really carry no georeferencing — ``job.json`` is the only authority."""
    aux = path.with_name(path.name + ".aux.xml")
    if aux.exists():
        aux.unlink()
