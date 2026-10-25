"""Chunks: one XYZ tile baked as its own canvas, continuing the neighbours already baked.

A chunk is tile ``z/x/y`` painted at *size* px — 256·2^k, so its native zoom is
exactly ``z + k`` and every tile of its pyramid lies inside it — plus *margin*
px on every side. Chunks of one size share a single pixel lattice, so the
grid is fixed by the tile id alone (:func:`chunk_grids`), not by an AOI.

Seams: before the bake, every already-baked neighbour of the same recipe is
pasted into the margin of a ``seam_init`` control, and a ``seam_mask`` control
marks those pixels as kept (0) and the rest as painted (255). A seam workflow
template denoises only the masked area, so the model paints up to the tile edge
while looking at the neighbour's pixels across it. After the alignment gate the
canvas is cropped back to the tile (``fantasy_chunk.tif``) and gated again
against the tile's own grid; the margin is thrown away, so neighbouring chunks
never overlap and a tile pyramid from each one can simply be laid side by side.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.controls import CONTROL_FILES, build_controls
from argus_atlas.georef import FANTASY_TIF, AlignmentError, verify_alignment
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import is_safe_id, load_sidecar, save_sidecar
from argus_atlas.mercator import intersects, mercator_to_bbox, tile_bounds
from argus_atlas.models import (
    COMPOSITE_KIND,
    CRS,
    MAX_LONG_SIDE,
    MAX_ZOOM,
    SEAM_INIT_KIND,
    SEAM_MASK_KIND,
    SIDE_MULTIPLE,
    AtlasError,
    BBox,
    ChunkNeighbour,
    ChunkRecord,
    FileRef,
    GdalCommand,
    Grid,
    JobSidecar,
    ProgressEvent,
    RecipeRecord,
)
from argus_atlas.png import png_size

logger = structlog.get_logger()

# The cropped tile. Shares the "fantasy" prefix so copy_job leaves it behind with the other bake outputs.
CHUNK_TIF = "fantasy_chunk.tif"
SEAM_FILES: dict[str, tuple[str, str]] = {
    SEAM_INIT_KIND: ("seam_init.tif", "seam_init.png"),
    SEAM_MASK_KIND: ("seam_mask.tif", "seam_mask.png"),
}
SEAM_FOOTPRINTS = "seam_neighbours.geojson"

# 256·2^k: the chunk's native tiles are whole tiles of the XYZ grid.
CHUNK_SIZES: tuple[int, ...] = (256, 512, 1024)
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_MARGIN = 128
# Keeps the canvas a multiple of 64 and the seam on SDXL's 8-px latent grid.
MARGIN_MULTIPLE = SIDE_MULTIPLE // 2
TILE_PX = 256

ProgressSink = Callable[[ProgressEvent], None]

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


def _overlaps(a: ChunkRecord, b: ChunkRecord) -> bool:
    """Whether two chunks cut tiles of a common zoom over common ground, so their archives would collide."""
    zooms = a.z <= chunk_native_zoom(b.z, b.size) and b.z <= chunk_native_zoom(a.z, a.size)
    return zooms and intersects(tile_bounds(a.z, a.x, a.y), tile_bounds(b.z, b.x, b.y))


def find_neighbours(
    root: Path,
    recipe: str,
    chunk: ChunkRecord,
    *,
    exclude: Path | None = None,
    recipe_sha256: str | None = None,
) -> list[ChunkNeighbour]:
    """Already-cropped chunks of *recipe* around *chunk* under *root*, each checked against its sidecar.

    Only chunks whose bake was resolved from *recipe* count: a chunk job re-baked
    with other params (``bake``, ``POST /bake/stream``) is not that recipe's chunk.
    A neighbour of another size (another pixel lattice), two jobs claiming one
    tile, a chunk of the recipe that overlaps *chunk* (the same tile in another job,
    or another zoom over the same ground), or a crop whose bytes no longer match
    its sidecar is an error, not a skip. A neighbour baked from another version of
    the recipe (its digest is not *recipe_sha256*) is used, with a warning.
    """
    wanted = {f"{z}/{x}/{y}" for z, x, y in neighbour_tiles(chunk.z, chunk.x, chunk.y)}
    found: dict[str, ChunkNeighbour] = {}
    if not root.is_dir():
        return []
    seen = {exclude.resolve()} if exclude is not None else set()
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_safe_id(child.name):
            continue
        real = child.resolve()
        if real in seen:
            continue  # this job itself, or a second name (a symlink) for a job already looked at
        seen.add(real)
        try:
            other = load_sidecar(child)
        except (OSError, AtlasError):
            continue  # not a readable job, as in JobStore.list
        c = other.chunk
        if c is None or other.recipe is None or other.recipe.recipe.name != recipe:
            continue
        if c.image is None or other.bake is None or other.bake.params.recipe != recipe:
            continue  # not baked from this recipe (or its crop failed): nothing to continue from
        if _overlaps(c, chunk):
            raise AtlasError(
                f"{other.job_id} already holds chunk {c.tile} of {recipe}, which overlaps {chunk.tile}: "
                "their archives would share tiles"
            )
        if c.tile not in wanted:
            continue
        if c.size != chunk.size:
            raise AtlasError(f"neighbour {other.job_id} ({c.tile}) is a {c.size} px chunk, this one is {chunk.size} px")
        if c.tile in found:
            raise AtlasError(f"two jobs hold chunk {c.tile} of {recipe}: {found[c.tile].job_id} and {other.job_id}")
        image = child / c.image.path
        if not image.is_file() or sha256_file(image) != c.image.sha256:
            raise AtlasError(f"neighbour {other.job_id}: {c.image.path} is missing or changed since its job.json")
        if recipe_sha256 is not None and other.recipe.sha256 != recipe_sha256:
            logger.warning("chunks.neighbour_recipe_changed", neighbour=other.job_id, tile=c.tile, recipe=recipe)
        found[c.tile] = ChunkNeighbour(
            tile=c.tile, job_id=other.job_id, image=FileRef(path=str(image.resolve()), sha256=c.image.sha256)
        )
    return [found[t] for t in sorted(found)]


def _footprints(neighbours: list[ChunkNeighbour]) -> dict:
    """The neighbours' tile rectangles as an EPSG:3857 FeatureCollection (gdal_rasterize never reprojects)."""
    features = []
    for n in neighbours:
        xmin, ymin, xmax, ymax = tile_bounds(*(int(v) for v in n.tile.split("/")))
        ring = [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]
        features.append(
            {"type": "Feature", "properties": {"tile": n.tile}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
        )
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"urn:ogc:def:crs:{CRS.replace(':', '::')}"}},
        "features": features,
    }


def build_seams(job_dir: Path, sidecar: JobSidecar, neighbours: list[ChunkNeighbour]) -> JobSidecar:
    """Write the ``seam_init``/``seam_mask`` controls of a chunk job and save its sidecar.

    ``seam_init`` is the composite (else the hillshade, as grey RGB) with every
    neighbour's pixels pasted over it; ``seam_mask`` is 255 everywhere except
    the neighbours' tiles. With no neighbours the mask is all 255 and the bake
    is a plain img2img of the init image.
    """
    if sidecar.chunk is None:
        raise AtlasError(f"{sidecar.job_id} is not a chunk job")
    started = time.perf_counter()
    grid = sidecar.grid
    commands: list[GdalCommand] = []
    base_kind = COMPOSITE_KIND if COMPOSITE_KIND in sidecar.controls else "hillshade"
    base = job_dir / CONTROL_FILES[base_kind][0]
    if not base.is_file():
        raise AtlasError(f"{base.name} is missing — rerun controls")

    init_tif, init_png = (job_dir / n for n in SEAM_FILES[SEAM_INIT_KIND])
    mask_tif, mask_png = (job_dir / n for n in SEAM_FILES[SEAM_MASK_KIND])
    init_tif.unlink(missing_ok=True)
    commands.append(gdal_ops.run(gdal_ops.rgb_copy_argv(base, init_tif, grey=base_kind == "hillshade")))
    for n in neighbours:
        commands.append(gdal_ops.run(gdal_ops.paste_argv(Path(n.image.path), init_tif)))

    footprints = job_dir / SEAM_FOOTPRINTS
    footprints.write_text(json.dumps(_footprints(neighbours)), encoding="utf-8")
    commands.append(gdal_ops.run(gdal_ops.rasterize_argv(footprints, mask_tif, grid, burn=0, init=255)))
    commands.append(gdal_ops.run(gdal_ops.to_png_argv(init_tif, init_png)))
    commands.append(gdal_ops.run(gdal_ops.to_png_argv(mask_tif, mask_png, nodata_none=True)))

    controls = dict(sidecar.controls)
    for kind, png in ((SEAM_INIT_KIND, init_png), (SEAM_MASK_KIND, mask_png)):
        gdal_ops.strip_aux(png)
        size = png_size(png)
        if size != (grid.width, grid.height):
            raise AtlasError(f"{png.name} is {size}, expected {(grid.width, grid.height)}")
        controls[kind] = FileRef(path=png.name, sha256=sha256_file(png))

    sidecar = sidecar.model_copy(
        update={
            "controls": controls,
            "commands": [*sidecar.commands, *commands],
            "chunk": sidecar.chunk.model_copy(update={"neighbours": neighbours, "image": None}),
            "timings": {**sidecar.timings, "seams": round(time.perf_counter() - started, 3)},
        }
    )
    save_sidecar(job_dir, sidecar)
    logger.info("chunks.seams", job_id=sidecar.job_id, neighbours=[n.tile for n in neighbours])
    return sidecar


def crop_chunk(job_dir: Path, sidecar: JobSidecar) -> tuple[FileRef, GdalCommand]:
    """Cut the tile out of the aligned ``fantasy.tif`` and gate it against the tile's grid.

    Like :func:`argus_atlas.georef.stamp`, the crop is written to a temp name
    and renamed only once the gate passes.
    """
    chunk = sidecar.chunk
    if chunk is None:
        raise AtlasError(f"{sidecar.job_id} is not a chunk job")
    src, dst = job_dir / FANTASY_TIF, job_dir / CHUNK_TIF
    part = dst.with_name(f".{dst.name}.part")
    part.unlink(missing_ok=True)
    command = gdal_ops.run(gdal_ops.crop_argv(src, part, chunk.margin, chunk.margin, chunk.size, chunk.size))
    try:
        verify_alignment(part, chunk.inner)
    except AlignmentError:
        part.unlink(missing_ok=True)
        gdal_ops.strip_aux(part)
        raise
    dst.unlink(missing_ok=True)
    part.replace(dst)
    return FileRef(path=dst.name, sha256=sha256_file(dst)), command


def _published(job_dir: Path) -> JobSidecar | None:
    """The job's sidecar if it holds a gated crop still on disk (what neighbours and the archive use), else None."""
    try:
        sidecar = load_sidecar(job_dir)
    except (OSError, AtlasError):
        return None
    image = sidecar.chunk.image if sidecar.chunk is not None else None
    if image is None:
        return None
    path = job_dir / image.path
    return sidecar if path.is_file() and sha256_file(path) == image.sha256 else None


def run_chunk(
    dem: Path,
    tile: str,
    recipe: RecipeRecord,
    out_dir: Path,
    *,
    backend=None,  # noqa: ANN001 - BakeBackend; None with controls_only
    models_root: Path | None = None,
    neighbours_root: Path | None = None,
    land: Path | None = None,
    layers: dict[str, Path] | None = None,
    sea_level: float | None = None,
    composite: bool = True,
    resampling: str | None = None,
    size: int = DEFAULT_CHUNK_SIZE,
    margin: int = DEFAULT_MARGIN,
    controls_only: bool = False,
    progress: ProgressSink | None = None,
) -> JobSidecar:
    """controls -> seams -> bake -> crop -> tiles for one chunk of one recipe.

    Neighbours are looked up under *neighbours_root* (default: the parent of
    *out_dir*, where the other chunks of a style usually live). With
    *controls_only* it stops after the seam controls — no GPU needed. A re-run
    that fails before its bake is recorded keeps the job's earlier crop published.
    """
    from argus_atlas.bake import run_bake
    from argus_atlas.recipes import recipe_params, recipe_seed
    from argus_atlas.tiles import tile_job

    z, x, y = parse_tile(tile)
    canvas, inner = chunk_grids(z, x, y, size=size, margin=margin)
    if not controls_only and (backend is None or models_root is None):
        raise AtlasError("baking a chunk needs a backend and a models root")
    job_id = out_dir.name
    previous = _published(out_dir)

    def emit(kind: str, message: str) -> None:
        if progress is not None:
            progress(ProgressEvent(job_id=job_id, type=kind, message=message))  # type: ignore[arg-type]

    def keep_previous() -> None:
        # A re-run that fails before a new bake is recorded must not unpublish the crop (and
        # archive) still on disk: until then the old record stays the truth about them.
        if previous is None:
            return
        try:
            if load_sidecar(out_dir).bake is None:
                save_sidecar(out_dir, previous)
                logger.info("chunks.kept_previous", job_id=job_id)
        except (OSError, AtlasError):
            pass

    try:
        emit("controls", f"chunk {z}/{x}/{y}: {canvas.width} px canvas ({size} px tile + {margin} px margin)")
        record = ChunkRecord(z=z, x=x, y=y, size=size, margin=margin, inner=inner)
        root = neighbours_root if neighbours_root is not None else out_dir.parent
        # Before anything is written, so a neighbour problem leaves this job as it was.
        neighbours = find_neighbours(root, recipe.recipe.name, record, exclude=out_dir, recipe_sha256=recipe.sha256)
        sidecar = build_controls(
            dem,
            chunk_bbox(z, x, y),
            out_dir,
            land=land,
            layers=layers,
            sea_level=sea_level,
            composite=composite,
            resampling=resampling,
            grid=canvas,
            chunk=record,
        )
        sidecar = sidecar.model_copy(update={"recipe": recipe})
        save_sidecar(out_dir, sidecar)
        sidecar = build_seams(out_dir, sidecar, neighbours)
        emit("controls", f"seams: {len(neighbours)} neighbour(s) of {recipe.recipe.name} pasted into the margin")
    except AtlasError as exc:
        keep_previous()
        emit("error", str(exc))
        raise
    if controls_only:
        return sidecar

    params = recipe_params(recipe.recipe, seed=recipe_seed(recipe.recipe, z, x, y))
    try:
        sidecar = run_bake(out_dir, params, backend, models_root=models_root, progress=progress)
    except AtlasError:
        keep_previous()  # run_bake has emitted the error frame
        raise
    try:
        emit("tiles", f"cutting zoom {sidecar.zoom_range[0]}-{sidecar.zoom_range[1]} from {CHUNK_TIF}")
        record_tiles = tile_job(out_dir, sidecar)
    except AtlasError as exc:
        emit("error", str(exc))
        raise
    emit("done", f"{record_tiles.n_tiles} tiles, zoom {record_tiles.min_zoom}-{record_tiles.max_zoom}")
    return load_sidecar(out_dir)
