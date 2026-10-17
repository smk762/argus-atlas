"""GDAL as subprocesses: argv builders plus one runner.

GDAL is a **system package** (``gdal-bin``), never a pip module, and every
operation here is a CLI call (``gdalwarp``, ``gdaldem``, ``gdal_rasterize``,
``gdal_translate``, ``gdalinfo``, ``gdal2tiles``). Builders return plain argv
lists so the exact commands can be recorded in the job sidecar and replayed;
:func:`run` executes one and turns a non-zero exit into a :class:`GdalError`
carrying the tail of stderr.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import structlog

from argus_atlas.models import CRS, AtlasError, GdalCommand, Grid

logger = structlog.get_logger()


class GdalError(AtlasError):
    """A GDAL tool is missing, or one exited non-zero."""


GDAL_TOOLS: tuple[str, ...] = ("gdalinfo", "gdalwarp", "gdaldem", "gdal_rasterize", "gdal_translate")
# Debian/Ubuntu ship the tiler as gdal2tiles.py (python3-gdal); some builds drop the suffix.
TILER_CANDIDATES: tuple[str, ...] = ("gdal2tiles.py", "gdal2tiles")

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


def gdalinfo_json(path: Path) -> dict:
    """``gdalinfo -json`` of *path* as a dict."""
    argv = ["gdalinfo", "-json", str(path)]
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


def warp_argv(dem: Path, dst: Path, grid: Grid) -> list[str]:
    """Warp + clip a DEM onto the canvas grid in EPSG:3857 (bilinear, Float32)."""
    return [
        "gdalwarp",
        "-overwrite",
        "-t_srs",
        CRS,
        "-r",
        "bilinear",
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


def rasterize_argv(vector: Path, dst: Path, grid: Grid) -> list[str]:
    """Burn a land polygon file (any OGR format) to a 0/255 Byte mask on the canvas grid."""
    return [
        "gdal_rasterize",
        "-burn",
        "255",
        "-ot",
        "Byte",
        "-init",
        "0",
        "-a_srs",
        CRS,
        *_te(grid),
        *_ts(grid),
        "-co",
        "COMPRESS=DEFLATE",
        str(vector),
        str(dst),
    ]


def to_png_argv(src: Path, dst: Path, *, scale: tuple[float, float, int, int] | None = None) -> list[str]:
    """Export a raster as an 8-bit PNG, optionally rescaling ``src_min src_max -> dst_min dst_max``."""
    argv = ["gdal_translate", "-of", "PNG", "-ot", "Byte"]
    if scale is not None:
        argv += ["-scale", repr(scale[0]), repr(scale[1]), str(scale[2]), str(scale[3])]
    return argv + [str(src), str(dst)]


def stamp_argv(png: Path, tif: Path, grid: Grid) -> list[str]:
    """Georeference a painted PNG into a GeoTIFF from the sidecar grid."""
    return [
        "gdal_translate",
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


def strip_aux(path: Path) -> None:
    """Remove the ``.aux.xml`` sidecar GDAL writes next to a PNG, so the model
    inputs really carry no georeferencing — ``job.json`` is the only authority."""
    aux = path.with_name(path.name + ".aux.xml")
    if aux.exists():
        aux.unlink()
