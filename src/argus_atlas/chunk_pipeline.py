"""The chunk job end to end: controls, seams, bake, blend, crop and tiles.

It sits above :mod:`argus_atlas.chunks` (the steps) and :mod:`argus_atlas.bake`, which uses some of those steps.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from argus_atlas.addressing import DEFAULT_CHUNK_SIZE, DEFAULT_MARGIN, chunk_bbox, chunk_grids, parse_tile
from argus_atlas.bake import ProgressSink, run_bake
from argus_atlas.chunks import (
    CHUNK_TIF,
    DEFAULT_FEATHER,
    DEFAULT_MASK_FEATHER,
    build_seams,
    find_neighbours,
    published_chunk,
    validate_seams,
)
from argus_atlas.controls import build_controls
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import load_sidecar, save_sidecar
from argus_atlas.models import AtlasError, ChunkRecord, JobSidecar, ProgressEvent, RecipeRecord, relief_key_for

logger = structlog.get_logger()


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
    mask_feather: int = DEFAULT_MASK_FEATHER,
    feather: int = DEFAULT_FEATHER,
    tone: bool = True,
    controls_only: bool = False,
    progress: ProgressSink | None = None,
) -> JobSidecar:
    """controls -> seams -> bake -> blend -> crop -> tiles for one chunk of one recipe.

    Neighbours are looked up under *neighbours_root* (default: the parent of
    *out_dir*, where the other chunks of a style usually live). With
    *controls_only* it stops after the seam controls — no GPU needed. A re-run
    that fails before its bake is recorded keeps the job's earlier crop published.
    """
    from argus_atlas.recipes import recipe_params, recipe_seed
    from argus_atlas.tiles import tile_job

    job_id = out_dir.name

    def emit(kind: str, message: str) -> None:
        if progress is not None:
            progress(ProgressEvent(job_id=job_id, type=kind, message=message))  # type: ignore[arg-type]

    try:  # nothing is written yet, but the stream still ends with an error frame
        z, x, y = parse_tile(tile)
        canvas, inner = chunk_grids(z, x, y, size=size, margin=margin)
        validate_seams(size, margin, mask_feather=mask_feather, feather=feather)
        if not controls_only and (backend is None or models_root is None):
            raise AtlasError("baking a chunk needs a backend and a models root")
        previous = published_chunk(out_dir)
    except Exception as exc:
        emit("error", str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}")
        raise

    def keep_previous() -> None:
        # A re-run that fails before a new bake is recorded must not unpublish the crop (and
        # archive) still on disk: until then the old record stays the truth about them.
        if previous is None:
            return
        try:
            if load_sidecar(out_dir).bake is not None:
                return
            png = out_dir / previous.bake.image.path if previous.bake is not None else None
            if png is not None and (not png.is_file() or sha256_file(png) != previous.bake.image.sha256):
                return  # the canvas was repainted before the failure: the old record no longer describes it
            save_sidecar(out_dir, previous)
            logger.info("chunks.kept_previous", job_id=job_id)
        except (OSError, AtlasError):
            pass

    try:
        emit("controls", f"chunk {z}/{x}/{y}: {canvas.width} px canvas ({size} px tile + {margin} px margin)")
        record = ChunkRecord(
            z=z, x=x, y=y, size=size, margin=margin, inner=inner, mask_feather=mask_feather, feather=feather, tone=tone
        )
        root = neighbours_root if neighbours_root is not None else out_dir.parent
        # Before anything is written, so a neighbour problem leaves this job as it was.
        relief = relief_key_for(recipe.recipe.z_factor, recipe.recipe.vertical_exaggeration)
        neighbours = find_neighbours(
            root, recipe.recipe.name, record, exclude=out_dir, recipe_sha256=recipe.sha256, relief=relief
        )
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
            z_factor=recipe.recipe.z_factor,
            vertical_exaggeration=recipe.recipe.vertical_exaggeration,
        )
        sidecar = sidecar.model_copy(update={"recipe": recipe})
        save_sidecar(out_dir, sidecar)
        sidecar = build_seams(out_dir, sidecar, neighbours)
        emit("controls", f"seams: {len(neighbours)} neighbour(s) of {recipe.recipe.name} pasted into the margin")
    except Exception as exc:  # any failure, not only ours: the earlier crop stays published
        keep_previous()
        emit("error", str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}")
        raise
    if controls_only:
        return sidecar

    params = recipe_params(recipe.recipe, seed=recipe_seed(recipe.recipe, z, x, y))
    try:
        sidecar = run_bake(out_dir, params, backend, models_root=models_root, progress=progress)
    except Exception:
        keep_previous()  # run_bake has emitted the error frame
        raise
    try:
        emit("tiles", f"cutting zoom {sidecar.zoom_range[0]}-{sidecar.zoom_range[1]} from {CHUNK_TIF}")
        record_tiles = tile_job(out_dir, sidecar)
    except Exception as exc:  # every failure ends the stream with an error frame
        emit("error", str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}")
        raise
    emit("done", f"{record_tiles.n_tiles} tiles, zoom {record_tiles.min_zoom}-{record_tiles.max_zoom}")
    return load_sidecar(out_dir)
