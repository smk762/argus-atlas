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

The tile addressing (:func:`parse_tile`, :func:`chunk_grids`, :func:`neighbour_tiles`, …) lives in the
light :mod:`argus_atlas.addressing` and is re-exported here, so existing imports keep working.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.addressing import (
    CHUNK_SIZES as CHUNK_SIZES,
)
from argus_atlas.addressing import (
    DEFAULT_CHUNK_SIZE as DEFAULT_CHUNK_SIZE,
)
from argus_atlas.addressing import (
    DEFAULT_MARGIN as DEFAULT_MARGIN,
)
from argus_atlas.addressing import (
    MARGIN_MULTIPLE as MARGIN_MULTIPLE,
)
from argus_atlas.addressing import (
    TILE_PX as TILE_PX,
)
from argus_atlas.addressing import (
    canvas_bbox as canvas_bbox,
)
from argus_atlas.addressing import (
    chunk_bbox as chunk_bbox,
)
from argus_atlas.addressing import (
    chunk_grids as chunk_grids,
)
from argus_atlas.addressing import (
    chunk_job_id as chunk_job_id,
)
from argus_atlas.addressing import (
    chunk_native_zoom as chunk_native_zoom,
)
from argus_atlas.addressing import (
    neighbour_tiles as neighbour_tiles,
)
from argus_atlas.addressing import (
    parse_tile as parse_tile,
)
from argus_atlas.addressing import (
    validate_chunk as validate_chunk,
)
from argus_atlas.controls import CONTROL_FILES
from argus_atlas.georef import FANTASY_TIF, AlignmentError, verify_alignment
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import is_safe_id, load_sidecar, save_sidecar
from argus_atlas.mercator import intersects, tile_bounds
from argus_atlas.models import (
    COMPOSITE_KIND,
    CRS,
    SEAM_INIT_KIND,
    SEAM_MASK_KIND,
    AtlasError,
    ChunkNeighbour,
    ChunkRecord,
    FileRef,
    GdalCommand,
    JobSidecar,
    SeamBlend,
    relief_key,
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
# The hard 0/255 mask and the distance to the painted area the soft seam_mask is ramped from.
SEAM_FOOTPRINT_TIF = "seam_footprint.tif"
SEAM_DISTANCE_TIF = "seam_distance.tif"
# The painted canvas after tone matching and the seam correction; the crop is cut from it.
BLEND_TIF = "fantasy_blend.tif"
# px either side of the seam whose mean colours set the tone shift, and its cap in 8-bit levels.
TONE_STRIP = 32
TONE_LIMIT = 24.0
# px of the neighbours next to the seam the remaining difference is measured on, its cap in 8-bit
# levels, and the block size it is averaged over so only the slow colour difference is carried.
EDGE_STRIP = 8
RESIDUAL_LIMIT = 48
RESIDUAL_BLOCK = 16
# px into the tile the full-resolution part of that difference fades over.
SHARP_STRIP = 8
DEFAULT_MASK_FEATHER = 32
DEFAULT_FEATHER = 64


def validate_seams(size: int, margin: int, *, mask_feather: int, feather: int) -> None:
    """The soft mask lives in the margin; the pixel correction must not reach the tile's far half."""
    if not 0 <= mask_feather <= margin:
        raise AtlasError(f"mask feather must be between 0 and the margin ({margin} px), got {mask_feather}")
    if not 0 <= feather <= size // 2:
        raise AtlasError(f"seam feather must be between 0 and half the tile ({size // 2} px), got {feather}")


def _seam_tools() -> tuple[str, str, str]:
    """``gdal_calc``, ``gdal_proximity`` and ``gdal_fillnodata`` (python3-gdal / gdal-python-tools), or AtlasError."""
    found = [
        gdal_ops.find_script(c)
        for c in (gdal_ops.CALC_CANDIDATES, gdal_ops.PROXIMITY_CANDIDATES, gdal_ops.FILLNODATA_CANDIDATES)
    ]
    if None in found:
        raise AtlasError(
            "seam blending needs gdal_calc.py, gdal_proximity.py and gdal_fillnodata.py "
            "(python3-gdal / gdal-python-tools); or pass --mask-feather 0 --feather 0 --no-tone"
        )
    return found[0], found[1], found[2]  # type: ignore[return-value]


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
    relief: tuple[str, float] | None = None,
) -> list[ChunkNeighbour]:
    """Already-cropped chunks of *recipe* around *chunk* under *root*, each checked against its sidecar.

    Only chunks whose bake was resolved from *recipe* count: a chunk job re-baked
    with other params (``bake``, ``POST /bake/stream``) is not that recipe's chunk.
    A neighbour of another size (another pixel lattice), two jobs claiming one
    tile, a chunk of the recipe that overlaps *chunk* (the same tile in another job,
    or another zoom over the same ground), or a crop whose bytes no longer match
    its sidecar is an error, not a skip. A neighbour baked from another version of
    the recipe (its digest is not *recipe_sha256*) is used, with a warning, unless its
    relief (:func:`~argus_atlas.models.relief_key`) is not *relief*: a neighbour painted
    over a differently scaled hillshade is an error.
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
        image = (child / c.image.path).resolve()
        if not image.is_relative_to(real):
            raise AtlasError(f"neighbour {other.job_id}: its crop {c.image.path!r} is outside the job directory")
        if not image.is_file() or sha256_file(image) != c.image.sha256:
            raise AtlasError(f"neighbour {other.job_id}: {c.image.path} is missing or changed since its job.json")
        if relief is not None and relief_key(other.relief) != relief:
            mode, value = relief_key(other.relief)
            raise AtlasError(
                f"neighbour {other.job_id} ({c.tile}) has relief {mode} {value:g}, this chunk {relief[0]} {relief[1]:g}: "
                "bake into another style dir, or set the recipe's z_factor/vertical_exaggeration to match"
            )
        if recipe_sha256 is not None and other.recipe.sha256 != recipe_sha256:
            logger.warning("chunks.neighbour_recipe_changed", neighbour=other.job_id, tile=c.tile, recipe=recipe)
        found[c.tile] = ChunkNeighbour(
            tile=c.tile, job_id=other.job_id, image=FileRef(path=str(image), sha256=c.image.sha256)
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
    # gdal_proximity runs from the job dir: a relative job_dir would be joined twice there.
    job_dir = job_dir.resolve()
    started = time.perf_counter()
    grid = sidecar.grid
    commands: list[GdalCommand] = []
    base_kind = COMPOSITE_KIND if COMPOSITE_KIND in sidecar.controls else "hillshade"
    base = job_dir / CONTROL_FILES[base_kind][0]
    if not base.is_file():
        raise AtlasError(f"{base.name} is missing — rerun controls")

    init_tif, init_png = (job_dir / n for n in SEAM_FILES[SEAM_INIT_KIND])
    mask_tif, mask_png = (job_dir / n for n in SEAM_FILES[SEAM_MASK_KIND])
    # gdal_rasterize/gdal_proximity update an existing output in place (keeping an earlier run's
    # footprints and ignoring -init/-ts), so every derived raster of a previous seam run goes first.
    for stale in (init_tif, init_png, mask_tif, mask_png, job_dir / SEAM_FOOTPRINT_TIF, job_dir / SEAM_DISTANCE_TIF):
        stale.unlink(missing_ok=True)
        gdal_ops.strip_aux(stale)
    if SEAM_INIT_KIND in sidecar.controls or SEAM_MASK_KIND in sidecar.controls:
        # The files are gone: until this run records new ones, job.json must not point at them.
        dropped = {k: v for k, v in sidecar.controls.items() if k not in (SEAM_INIT_KIND, SEAM_MASK_KIND)}
        sidecar = sidecar.model_copy(update={"controls": dropped})
        save_sidecar(job_dir, sidecar)
    commands.append(gdal_ops.run(gdal_ops.rgb_copy_argv(base, init_tif, grey=base_kind == "hillshade")))
    for n in neighbours:
        commands.append(gdal_ops.run(gdal_ops.paste_argv(Path(n.image.path), init_tif)))

    footprints = job_dir / SEAM_FOOTPRINTS
    footprints.write_text(json.dumps(_footprints(neighbours)), encoding="utf-8")
    # A soft mask lets the model also repaint the neighbours' pixels next to the seam, fading to fully
    # kept over mask_feather px, so the latent has no hard edge to decode. Those pixels are in the margin.
    soft = sidecar.chunk.mask_feather if neighbours else 0
    hard_tif = job_dir / SEAM_FOOTPRINT_TIF if soft else mask_tif
    commands.append(gdal_ops.run(gdal_ops.rasterize_argv(footprints, hard_tif, grid, burn=0, init=255)))
    if soft:
        calc, proximity, _ = _seam_tools()
        distance = job_dir / SEAM_DISTANCE_TIF
        # gdal_proximity and gdal_fillnodata write scratch files into the working directory, which in the
        # image (/app) the baking user cannot write: run them from the job dir.
        commands.append(
            gdal_ops.run(gdal_ops.proximity_argv(proximity, hard_tif, distance, values=255, maxdist=soft), cwd=job_dir)
        )
        ramp = f"where(A==255,255,where(B<{soft},255*(1-B/{soft}),0))"
        commands.append(gdal_ops.run(gdal_ops.calc_argv(calc, {"A": hard_tif, "B": distance}, ramp, mask_tif)))
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
            "chunk": sidecar.chunk.model_copy(update={"neighbours": neighbours, "image": None, "blend": None}),
            "timings": {**sidecar.timings, "seams": round(time.perf_counter() - started, 3)},
        }
    )
    save_sidecar(job_dir, sidecar)
    logger.info("chunks.seams", job_id=sidecar.job_id, neighbours=[n.tile for n in neighbours])
    return sidecar


def _band_means(path: Path) -> list[float] | None:
    """Per-band means of *path* over its valid pixels, or None when a band has none."""
    try:
        info = gdal_ops.gdalinfo_json(path, stats=True)
    except gdal_ops.GdalError:
        return None
    finally:
        gdal_ops.strip_aux(path)
    means = [band.get("mean") for band in info.get("bands", [])]
    return [float(m) for m in means] if means and all(m is not None for m in means) else None


def blend_chunk(
    job_dir: Path, sidecar: JobSidecar, *, description: str | None = None
) -> tuple[SeamBlend | None, list[GdalCommand]]:
    """Match the painted canvas to the neighbours it continues, before the crop.

    The model sees the neighbours only through the VAE, so even a continued seam
    is a few levels off and each chunk drifts in tone. Two corrections, both
    measured against the neighbours' real pixels in ``seam_init``:

    * **tone** — one RGB offset for the whole canvas: the mean of the neighbours'
      strip along the seam minus the mean of the painted strip facing it (capped
      at :data:`TONE_LIMIT` levels);
    * **seam** — the difference that remains on the neighbours' side of the seam
      is spread into the tile (``gdal_fillnodata``) and faded out over *feather* px.

    ``fantasy.tif`` stays as painted; the result is ``fantasy_blend.tif`` on the
    same grid, gated like it. Nothing happens for a chunk without neighbours.
    """
    chunk = sidecar.chunk
    if chunk is None or not chunk.neighbours or (chunk.feather == 0 and not chunk.tone):
        return None, []
    calc, proximity, fill = _seam_tools()
    run = gdal_ops.run
    painted = job_dir / FANTASY_TIF
    theirs_rgb = job_dir / sidecar.controls[SEAM_INIT_KIND].path
    mask = job_dir / sidecar.controls[SEAM_MASK_KIND].path
    commands: list[GdalCommand] = []
    work = Path(tempfile.mkdtemp(prefix=".blend-", dir=job_dir))
    try:
        kept = work / "kept.tif"  # 255 on the neighbours' pixels (soft or hard), 0 on what was painted
        to_kept, to_painted = work / "to_kept.tif", work / "to_painted.tif"
        reach = max(chunk.feather, TONE_STRIP, EDGE_STRIP)
        # R (unused by the expression) gives kept.tif the canvas georeferencing, and with it every
        # distance and gap raster derived from it: the mask PNG has none, by design.
        commands.append(run(gdal_ops.calc_argv(calc, {"A": mask, "R": painted}, "255*(A<255)", kept)))
        # Scratch files of gdal_proximity/gdal_fillnodata land in the working directory: keep them in work/.
        commands.append(run(gdal_ops.proximity_argv(proximity, kept, to_kept, values=255, maxdist=reach), cwd=work))
        commands.append(run(gdal_ops.proximity_argv(proximity, kept, to_painted, values=0, maxdist=reach), cwd=work))

        offset = [0.0, 0.0, 0.0]
        if chunk.tone:
            theirs, ours = work / "tone_theirs.tif", work / "tone_ours.tif"
            # -1.0, not -1: numpy 2 (NEP 50) keeps where(cond, <uint8 band>, -1) uint8, so -1 wrapped to
            # 255 and every cell outside the strip was averaged in as white (GDAL 3.12 / numpy 2.3 in CI).
            strip = f"where((B==255)*(C<={TONE_STRIP}),A,-1.0)"
            commands.append(
                run(
                    gdal_ops.calc_argv(
                        calc,
                        {"A": theirs_rgb, "B": kept, "C": to_painted},
                        strip,
                        theirs,
                        all_bands="A",
                        out_type="Int16",
                        nodata=-1,
                    )
                )
            )
            strip = f"where((B==0)*(C<={TONE_STRIP}),A,-1.0)"
            commands.append(
                run(
                    gdal_ops.calc_argv(
                        calc,
                        {"A": painted, "B": kept, "C": to_kept},
                        strip,
                        ours,
                        all_bands="A",
                        out_type="Int16",
                        nodata=-1,
                    )
                )
            )
            m_theirs, m_ours = _band_means(theirs), _band_means(ours)
            if m_theirs and m_ours and len(m_theirs) >= 3 and len(m_ours) >= 3:
                pairs = zip(m_theirs[:3], m_ours[:3], strict=True)
                offset = [round(max(-TONE_LIMIT, min(TONE_LIMIT, t - o)), 2) for t, o in pairs]

        bands: list[Path] = []
        for b, off in enumerate(offset, start=1):
            out = work / f"band{b}.tif"
            if chunk.feather:
                gap, filled = work / f"gap{b}.tif", work / f"filled{b}.tif"
                # Capped: where the content itself disagrees across the seam, fade it rather than ghost it.
                residual = (
                    f"where((C==255)*(D<={EDGE_STRIP}),clip(A-(B+{off!r}),-{RESIDUAL_LIMIT},{RESIDUAL_LIMIT}),-9999)"
                )
                commands.append(
                    run(
                        gdal_ops.calc_argv(
                            calc,
                            {"A": theirs_rgb, "B": painted, "C": kept, "D": to_painted},
                            residual,
                            gap,
                            bands={"A": b, "B": b},
                            out_type="Float32",
                            nodata=-9999,
                        )
                    )
                )
                spread = gdal_ops.fillnodata_argv(fill, gap, filled, maxdist=chunk.feather + EDGE_STRIP)
                commands.append(run(spread, cwd=work))
                # Low-pass the correction: it should carry the colour difference along the seam, not the
                # neighbour's coastlines and rivers, which fillnodata would otherwise drag in as streaks.
                coarse, smooth = work / f"coarse{b}.tif", work / f"smooth{b}.tif"
                grid = sidecar.grid
                small = (max(1, grid.width // RESIDUAL_BLOCK), max(1, grid.height // RESIDUAL_BLOCK))
                commands.append(run(gdal_ops.resize_argv(filled, coarse, *small, method="average")))
                commands.append(
                    run(gdal_ops.resize_argv(coarse, smooth, grid.width, grid.height, method="cubicspline"))
                )
                # Two scales: the smooth correction fades over the feather; the fine remainder (the
                # line the VAE leaves right at the seam) only over SHARP_STRIP px, too short to streak.
                f, n = chunk.feather, min(SHARP_STRIP, chunk.feather)
                fine = f"where((B>-9000)*(S>-9000),B-S,0)*clip(1-C/{n},0,1)*(C<={n})"
                expr = f"clip(A+{off!r}+where(S>-9000,S,0)*clip(1-C/{f},0,1)*(C<={f})+{fine},0,255)"
                inputs = {"A": painted, "B": filled, "C": to_kept, "S": smooth}
                commands.append(run(gdal_ops.calc_argv(calc, inputs, expr, out, bands={"A": b})))
            else:
                commands.append(
                    run(gdal_ops.calc_argv(calc, {"A": painted}, f"clip(A+{off!r},0,255)", out, bands={"A": b}))
                )
            bands.append(out)

        stack = work / "blend.vrt"
        commands.append(run(gdal_ops.stack_argv(bands, stack)))
        dst = job_dir / BLEND_TIF
        part = dst.with_name(f".{dst.name}.part")
        part.unlink(missing_ok=True)
        commands.append(run(gdal_ops.to_tif_argv(stack, part, description=description)))
        try:
            verify_alignment(part, sidecar.grid)
        except AlignmentError:
            part.unlink(missing_ok=True)
            raise
        dst.unlink(missing_ok=True)
        part.replace(dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    logger.info("chunks.blend", job_id=sidecar.job_id, offset=offset, feather=chunk.feather)
    return SeamBlend(
        offset=offset, feather=chunk.feather, image=FileRef(path=dst.name, sha256=sha256_file(dst))
    ), commands


def crop_chunk(job_dir: Path, sidecar: JobSidecar) -> tuple[FileRef, GdalCommand]:
    """Cut the tile out of the aligned ``fantasy.tif`` (or its seam blend) and gate it against the tile's grid.

    Like :func:`argus_atlas.georef.stamp`, the crop is written to a temp name
    and renamed only once the gate passes.
    """
    chunk = sidecar.chunk
    if chunk is None:
        raise AtlasError(f"{sidecar.job_id} is not a chunk job")
    src = job_dir / (chunk.blend.image.path if chunk.blend is not None else FANTASY_TIF)
    dst = job_dir / CHUNK_TIF
    part = dst.with_name(f".{dst.name}.part")
    part.unlink(missing_ok=True)
    gdal_ops.strip_aux(part)  # a stale PAM sidecar would override the crop's own georeferencing
    command = gdal_ops.run(gdal_ops.crop_argv(src, part, chunk.margin, chunk.margin, chunk.size, chunk.size))
    try:
        verify_alignment(part, chunk.inner)
    except AlignmentError:
        part.unlink(missing_ok=True)
        gdal_ops.strip_aux(part)
        raise
    gdal_ops.strip_aux(part)
    dst.unlink(missing_ok=True)
    gdal_ops.strip_aux(dst)
    part.replace(dst)
    return FileRef(path=dst.name, sha256=sha256_file(dst)), command


def published_chunk(job_dir: Path) -> JobSidecar | None:
    """The job's sidecar if it holds a gated crop still on disk (what neighbours and the archive use), else None."""
    try:
        sidecar = load_sidecar(job_dir)
    except (OSError, AtlasError):
        return None
    image = sidecar.chunk.image if sidecar.chunk is not None else None
    if image is None:
        return None
    try:
        return sidecar if sha256_file(job_dir / image.path) == image.sha256 else None
    except OSError:  # missing, or gone/unreadable since: not a published crop
        return None
