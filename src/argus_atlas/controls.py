"""Plan the canvas and write the control rasters + job sidecar (no GPU).

:func:`plan_grid` turns an AOI into the EPSG:3857 pixel grid: the long side is
``--long-side`` (default 1024, cap 1536) unless an explicit ``--resolution`` is
given, both sides are rounded up to a multiple of 64, and the bounds are padded
symmetrically so the grid stays square-pixelled. An AOI that cannot fit the cap
at the requested resolution fails with the largest extent that would.

:func:`build_controls` then runs GDAL: warp the DEM onto that grid, derive a
hillshade and a slope, burn every vector layer the operator supplied (``--land``
is the layer named ``land``) to a 0/255 mask, paint the known layers over the
hillshade into an RGB *composite* (water blue, forest green, roads dark …) that
a template can use as the img2img init image, export 8-bit PNGs for the model,
verify the hillshade's grid against the plan, and write ``job.json``.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import require_safe_id, save_sidecar
from argus_atlas.mercator import bbox_to_mercator, mercator_to_bbox, native_zoom, zoom_range
from argus_atlas.models import (
    ALIGN_TOLERANCE,
    COMPOSITE_KIND,
    DEFAULT_LONG_SIDE,
    LAYER_NAME_PATTERN,
    MAX_LONG_SIDE,
    RESERVED_CONTROL_KINDS,
    SEA_KIND,
    SIDE_MULTIPLE,
    AtlasError,
    BBox,
    FileRef,
    GdalCommand,
    Grid,
    JobSidecar,
)
from argus_atlas.png import png_size

logger = structlog.get_logger()

WARPED_DEM = "dem_3857.tif"
# kind -> (GDAL raster, 8-bit PNG for the model)
CONTROL_FILES: dict[str, tuple[str, str]] = {
    "hillshade": ("hillshade.tif", "hillshade.png"),
    "slope": ("slope.tif", "slope.png"),
    COMPOSITE_KIND: ("composite.tif", "composite.png"),
}
# Layers painted into the composite, in paint order (first on top): colour and
# whether the hillshade modulates it. A layer not listed here is burnt as a
# mask only. Everything else (land, custom names) stays a plain mask.
COMPOSITE_PALETTE: dict[str, tuple[tuple[int, int, int], bool]] = {
    "roads": ((40, 35, 30), False),
    "rail": ((60, 50, 50), False),
    "rivers": ((70, 110, 160), False),
    "water": ((70, 110, 160), False),
    SEA_KIND: ((70, 110, 160), False),
    "buildings": ((120, 100, 90), False),
    "glaciers": ((235, 240, 250), True),
    "urban": ((170, 165, 160), True),
    "forest": ((80, 130, 70), True),
}
# Line layers are burnt all-touched so a one-pixel road survives diagonals.
LINE_LAYERS: frozenset[str] = frozenset({"roads", "rail", "rivers"})
# Slope degrees -> Byte. 0..60° covers alpine terrain; steeper clips to 255.
SLOPE_SCALE = (0.0, 60.0, 0, 255)


def _ceil_multiple(pixels: float, multiple: int = SIDE_MULTIPLE) -> int:
    # Tolerate float noise so an exact 1024.0000001 does not become 1088.
    return max(multiple, int(math.ceil(pixels - 1e-6) + multiple - 1) // multiple * multiple)


def plan_grid(
    bbox: BBox,
    *,
    long_side: int | None = None,
    resolution: float | None = None,
    max_long_side: int = MAX_LONG_SIDE,
) -> Grid:
    """The canvas grid for *bbox*: fit the long side, or honour an explicit resolution.

    Raises :class:`AtlasError` (naming the largest extent that fits) when the AOI
    needs more than *max_long_side* pixels at *resolution*.
    """
    if long_side is not None and resolution is not None:
        raise AtlasError("give --long-side or --resolution, not both")
    xmin, ymin, xmax, ymax = bbox_to_mercator(bbox)
    w_m, h_m = xmax - xmin, ymax - ymin

    if resolution is None:
        side = DEFAULT_LONG_SIDE if long_side is None else long_side
        if side % SIDE_MULTIPLE or not SIDE_MULTIPLE <= side <= max_long_side:
            raise AtlasError(
                f"long side must be a multiple of {SIDE_MULTIPLE} between {SIDE_MULTIPLE} and {max_long_side}, got {side}"
            )
        res = max(w_m, h_m) / side
    else:
        if resolution <= 0:
            raise AtlasError(f"resolution must be positive metres per pixel, got {resolution}")
        res = float(resolution)

    width = _ceil_multiple(w_m / res)
    height = _ceil_multiple(h_m / res)
    if max(width, height) > max_long_side:
        fit_m = max_long_side * res
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        fw, fh = min(w_m, fit_m), min(h_m, fit_m)
        fit = mercator_to_bbox(cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2)
        raise AtlasError(
            f"AOI needs a {width}x{height} px canvas at {res:.3f} m/px, over the {max_long_side} px cap. "
            f"At this resolution the largest extent that fits is {fit_m:.0f} m per side, e.g. "
            f"--bbox {fit.as_text()} (centred on the AOI); or drop --resolution to fit the whole AOI into --long-side."
        )

    pad_x = (width * res - w_m) / 2
    pad_y = (height * res - h_m) / 2
    return Grid(width=width, height=height, ulx=xmin - pad_x, uly=ymax + pad_y, pixel_width=res, pixel_height=-res)


def verify_raster_grid(path: Path, grid: Grid, *, tol: float = ALIGN_TOLERANCE) -> dict:
    """``gdalinfo -json`` *path* and check size + geotransform against *grid*."""
    info = gdal_ops.gdalinfo_json(path)
    size = info.get("size")
    if size != [grid.width, grid.height]:
        raise AtlasError(f"{path.name} is {size}, expected {[grid.width, grid.height]}")
    gt = info.get("geoTransform")
    if not gt or len(gt) != 6:
        raise AtlasError(f"{path.name} has no geotransform")
    expected = grid.geotransform
    if any(not math.isclose(a, b, abs_tol=tol) for a, b in zip(gt, expected, strict=True)):
        raise AtlasError(f"{path.name} geotransform {gt} does not match the planned grid {list(expected)}")
    return info


def validate_layer_name(name: str) -> str:
    if not re.match(LAYER_NAME_PATTERN, name):
        raise AtlasError(f"bad layer name {name!r}: lowercase letters, digits and _ only, up to 32 characters")
    if name in RESERVED_CONTROL_KINDS:
        raise AtlasError(f"layer name {name!r} is reserved (atlas writes that control itself)")
    return name


def build_controls(
    dem: Path,
    bbox: BBox,
    out_dir: Path,
    *,
    land: Path | None = None,
    layers: dict[str, Path] | None = None,
    sea_level: float | None = None,
    composite: bool = True,
    long_side: int | None = None,
    resolution: float | None = None,
    job_id: str | None = None,
) -> JobSidecar:
    """Warp the DEM, write hillshade/slope(/layer masks/composite) controls and ``job.json`` under *out_dir*.

    *layers* maps a control name to any OGR-readable vector file (GeoJSON, GPKG,
    shapefile …) in any CRS; *land* is shorthand for ``layers["land"]``. Each
    is reprojected and clipped to the canvas, then burnt to a 0/255 mask.
    *sea_level* (metres) adds a ``sea`` mask of every DEM cell at or below it —
    OSM has no ocean polygons, and Copernicus GLO-30 puts the sea at exactly 0.
    With *composite*, the layers in :data:`COMPOSITE_PALETTE` are painted over
    the hillshade into ``composite.png`` as well.
    """
    dem = dem.expanduser()
    if not dem.is_file():
        raise AtlasError(f"DEM not found: {dem}")
    sources: dict[str, Path] = {}
    if land is not None:
        sources["land"] = land
    for name, path in (layers or {}).items():
        if validate_layer_name(name) in sources:
            raise AtlasError(f"layer {name!r} given twice")
        sources[name] = path
    for name, path in sources.items():
        path = sources[name] = path.expanduser()
        if not path.is_file():
            raise AtlasError(f"{name} layer file not found: {path}")
    job_id = require_safe_id(job_id or out_dir.name, "job id")
    gdal_ops.require_gdal()
    paintable = sea_level is not None or any(n in COMPOSITE_PALETTE for n in sources)
    needs_calc = sea_level is not None or (composite and paintable)
    calc = gdal_ops.find_calc() if needs_calc else None
    if needs_calc and calc is None:
        raise AtlasError("the sea mask and the composite control need gdal_calc.py (python3-gdal / gdal-python-tools)")

    grid = plan_grid(bbox, long_side=long_side, resolution=resolution)
    out_dir.mkdir(parents=True, exist_ok=True)
    commands: list[GdalCommand] = []
    logger.info("controls.plan", job_id=job_id, width=grid.width, height=grid.height, resolution=grid.resolution)

    warped = out_dir / WARPED_DEM
    commands.append(gdal_ops.run(gdal_ops.warp_argv(dem, warped, grid)))

    hill_tif, hill_png = (out_dir / n for n in CONTROL_FILES["hillshade"])
    slope_tif, slope_png = (out_dir / n for n in CONTROL_FILES["slope"])
    commands.append(gdal_ops.run(gdal_ops.hillshade_argv(warped, hill_tif)))
    commands.append(gdal_ops.run(gdal_ops.slope_argv(warped, slope_tif)))
    commands.append(gdal_ops.run(gdal_ops.to_png_argv(hill_tif, hill_png)))
    commands.append(gdal_ops.run(gdal_ops.to_png_argv(slope_tif, slope_png, scale=SLOPE_SCALE)))

    controls: dict[str, FileRef] = {}
    pngs = {"hillshade": hill_png, "slope": slope_png}
    masks: dict[str, Path] = {}
    for name, source in sources.items():
        clipped = out_dir / f"{name}_3857.geojson"
        mask_tif, mask_png = out_dir / f"{name}_mask.tif", out_dir / f"{name}.png"
        clipped.unlink(missing_ok=True)  # ogr2ogr refuses to overwrite GeoJSON
        commands.append(gdal_ops.run(gdal_ops.reproject_argv(source, clipped, grid)))
        commands.append(gdal_ops.run(gdal_ops.rasterize_argv(clipped, mask_tif, grid, all_touched=name in LINE_LAYERS)))
        commands.append(gdal_ops.run(gdal_ops.to_png_argv(mask_tif, mask_png, nodata_none=True)))
        pngs[name] = mask_png
        masks[name] = mask_tif

    if sea_level is not None:
        sea_tif, sea_png = out_dir / f"{SEA_KIND}_mask.tif", out_dir / f"{SEA_KIND}.png"
        commands.append(gdal_ops.run(gdal_ops.sea_mask_argv(calc, warped, sea_tif, sea_level)))
        commands.append(gdal_ops.run(gdal_ops.to_png_argv(sea_tif, sea_png, nodata_none=True)))
        pngs[SEA_KIND] = sea_png
        masks[SEA_KIND] = sea_tif

    if calc is not None and composite and paintable:
        painted = [(masks[n], rgb, relief) for n, (rgb, relief) in COMPOSITE_PALETTE.items() if n in masks]
        comp_tif, comp_png = (out_dir / n for n in CONTROL_FILES[COMPOSITE_KIND])
        commands.append(gdal_ops.run(gdal_ops.composite_argv(calc, hill_tif, painted, comp_tif)))
        commands.append(gdal_ops.run(gdal_ops.to_png_argv(comp_tif, comp_png)))
        pngs[COMPOSITE_KIND] = comp_png

    # The hillshade raster must sit exactly on the planned grid — it is what the
    # model output is later checked against.
    verify_raster_grid(hill_tif, grid)
    for kind, png in pngs.items():
        gdal_ops.strip_aux(png)
        size = png_size(png)
        if size != (grid.width, grid.height):
            raise AtlasError(f"{png.name} is {size}, expected {(grid.width, grid.height)}")
        controls[kind] = FileRef(path=png.name, sha256=sha256_file(png))

    native = native_zoom(grid.resolution)
    layer_sources = {n: FileRef(path=str(p.resolve()), sha256=sha256_file(p)) for n, p in sources.items()}
    sidecar = JobSidecar(
        job_id=job_id,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        dem=FileRef(path=str(dem.resolve()), sha256=sha256_file(dem)),
        land_source=layer_sources.get("land"),
        layer_sources=layer_sources,
        bbox=bbox,
        grid=grid,
        resolution=grid.resolution,
        native_zoom=native,
        zoom_range=zoom_range(native),
        controls=controls,
        commands=commands,
    )
    save_sidecar(out_dir, sidecar)
    logger.info("controls.done", job_id=job_id, controls=sorted(controls))
    return sidecar
