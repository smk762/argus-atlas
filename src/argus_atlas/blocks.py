"""Plan and bake a block of chunks: the loop every caller of ``chunk`` used to write.

A block is a set of tiles at one zoom, baked with one recipe into one style dir.
:func:`block` puts them in **seam order** (north to south, west to east, so each
chunk after the first finds its west and north neighbours already baked) and
returns the lon/lat box of every canvas, margins included, so the DEM and OSM
are fetched **once** for the block. :func:`land_tiles` drops the chunks with no
land under a land polygon (one ``gdal_rasterize`` of the block at a few pixels
per tile). :func:`run_block` then bakes the rest, skipping chunks already baked
with the same recipe, so a re-run after a failure picks up where it stopped.

The DEM and OSM extract land in an *inputs* dir (default ``<style dir>/.block``,
which is not a job id, so neighbour lookups never see it) and are reused while
their manifests still match the block. Point a second recipe's run at the same
inputs dir to fetch nothing twice.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import structlog
from pydantic import BaseModel

from argus_atlas import gdal_ops
from argus_atlas.chunks import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MARGIN,
    chunk_grids,
    chunk_job_id,
    chunk_native_zoom,
    published_chunk,
    validate_chunk,
)
from argus_atlas.jobs import load_sidecar
from argus_atlas.mercator import bbox_to_mercator, mercator_to_bbox, tile_bounds, tiles_covering
from argus_atlas.models import AtlasError, BBox, Grid, JobSidecar, RecipeRecord

logger = structlog.get_logger()

Tile = tuple[int, int, int]
LogSink = Callable[[str], None]

# Pixels per tile side when the land polygon is burnt over the block: enough to catch a small
# island (all-touched), cheap for a block of hundreds of chunks.
LAND_PX = 8
MAX_RINGS = 16
# The most chunks one block may hold: the square --rings MAX_RINGS makes, for --bbox too.
MAX_BLOCK_TILES = (2 * MAX_RINGS + 1) ** 2
INPUTS_DIR = ".block"


def tile_text(tile: Tile) -> str:
    return "/".join(map(str, tile))


def ring_tiles(z: int, x: int, y: int, rings: int) -> list[Tile]:
    """The ``(2·rings+1)²`` square of tiles centred on ``z/x/y``, clipped at the edges of the world."""
    if not 0 <= rings <= MAX_RINGS:
        raise AtlasError(f"rings must be between 0 and {MAX_RINGS}, got {rings}")
    n = 1 << z
    return [
        (z, tx, ty)
        for ty in range(max(0, y - rings), min(n, y + rings + 1))
        for tx in range(max(0, x - rings), min(n, x + rings + 1))
    ]


def bbox_tiles(bbox: BBox, z: int) -> list[Tile]:
    """Every tile at zoom *z* that the lon/lat box touches (at most :data:`MAX_BLOCK_TILES`)."""
    x0, y0, x1, y1 = tiles_covering(bbox_to_mercator(bbox), z)
    count = (x1 - x0 + 1) * (y1 - y0 + 1)
    if count > MAX_BLOCK_TILES:
        raise AtlasError(f"the bbox touches {count} tiles at zoom {z}, over the {MAX_BLOCK_TILES}-chunk block limit")
    return [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def block(
    tiles: list[Tile], *, size: int = DEFAULT_CHUNK_SIZE, margin: int = DEFAULT_MARGIN
) -> tuple[list[Tile], BBox]:
    """``(ordered_tiles, canvas_bbox)``: the tiles in seam order and the lon/lat box of all their canvases.

    Seam order is north to south, then west to east, so every chunk's west and north
    neighbours (when they are in the block) are baked before it.
    """
    if not tiles:
        raise AtlasError("a block needs at least one tile")
    zooms = {t[0] for t in tiles}
    if len(zooms) > 1:
        raise AtlasError(f"a block is one zoom; got {', '.join(map(str, sorted(zooms)))}")
    ordered = sorted(set(tiles), key=lambda t: (t[2], t[1]))
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    for z, x, y in ordered:
        canvas, _ = chunk_grids(z, x, y, size=size, margin=margin)
        cx0, cy0, cx1, cy1 = canvas.bounds
        xmin, ymin, xmax, ymax = min(xmin, cx0), min(ymin, cy0), max(xmax, cx1), max(ymax, cy1)
    return ordered, mercator_to_bbox(xmin, ymin, xmax, ymax)


def _read_ascii_grid(path: Path) -> list[list[int]]:
    rows = []
    for line in path.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if parts and not parts[0][0].isalpha():
            rows.append([int(float(v)) for v in parts])
    return rows


def land_tiles(tiles: list[Tile], land: Path, *, work_dir: Path | None = None) -> list[Tile]:
    """The tiles (in the order given) whose own tile, margin excluded, has land under *land*.

    *land* is any OGR polygon file in any CRS. It is reprojected and clipped to the block,
    burnt all-touched at :data:`LAND_PX` px per tile, and each tile's cells are read back.
    """
    if not land.is_file():
        raise FileNotFoundError(f"land polygon file not found: {land}")
    if not tiles:
        return []
    gdal_ops.require_gdal()
    z = tiles[0][0]
    x0, x1 = min(t[1] for t in tiles), max(t[1] for t in tiles)
    y0, y1 = min(t[2] for t in tiles), max(t[2] for t in tiles)
    west, _, east, north = tile_bounds(z, x0, y0)
    res = (east - west) / LAND_PX
    grid = Grid(
        width=(x1 - x0 + 1) * LAND_PX,
        height=(y1 - y0 + 1) * LAND_PX,
        ulx=west,
        uly=north,
        pixel_width=res,
        pixel_height=-res,
    )
    with tempfile.TemporaryDirectory(prefix="atlas-land-", dir=work_dir) as tmp:
        vector, mask, text = Path(tmp) / "land.geojson", Path(tmp) / "land.tif", Path(tmp) / "land.asc"
        gdal_ops.run(gdal_ops.reproject_argv(land, vector, grid))
        gdal_ops.run(gdal_ops.rasterize_argv(vector, mask, grid, all_touched=True, burn=1))
        gdal_ops.run(["gdal_translate", "-q", "-of", "AAIGrid", str(mask), str(text)])
        cells = _read_ascii_grid(text)
    kept = []
    for t in tiles:
        col, row = (t[1] - x0) * LAND_PX, (t[2] - y0) * LAND_PX
        if any(any(r[col : col + LAND_PX]) for r in cells[row : row + LAND_PX]):
            kept.append(t)
    return kept


def chunk_done(job_dir: Path, recipe: RecipeRecord, *, size: int, margin: int) -> JobSidecar | None:
    """The job's sidecar if it holds this recipe's gated crop and its archive at this size and margin."""
    sidecar = published_chunk(job_dir)
    if sidecar is None or sidecar.chunk is None or sidecar.tiles is None or sidecar.recipe is None:
        return None
    if sidecar.recipe.sha256 != recipe.sha256 or (sidecar.chunk.size, sidecar.chunk.margin) != (size, margin):
        return None
    return sidecar if (job_dir / sidecar.tiles.archive.path).is_file() else None


class BlockPlan(BaseModel):
    """What ``chunks --plan`` prints and :func:`run_block` works through."""

    recipe: str
    zoom: int
    size: int
    margin: int
    native_zoom: int
    bbox: BBox
    tiles: list[str]
    sea: list[str] = []
    done: list[str] = []

    @property
    def todo(self) -> list[str]:
        return [t for t in self.tiles if t not in self.done]


def plan_block(
    tiles: list[Tile],
    recipe: RecipeRecord,
    out_dir: Path,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    margin: int = DEFAULT_MARGIN,
    land: Path | None = None,
) -> BlockPlan:
    """Order the block, drop its all-sea chunks (with *land*) and mark the ones already baked."""
    z = tiles[0][0] if tiles else 0
    validate_chunk(z, size, margin)
    ordered, bbox = block(tiles, size=size, margin=margin)
    kept = land_tiles(ordered, land.expanduser()) if land is not None else ordered
    name = recipe.recipe.name
    done = [t for t in kept if chunk_done(out_dir / chunk_job_id(name, *t), recipe, size=size, margin=margin)]
    return BlockPlan(
        recipe=name,
        zoom=z,
        size=size,
        margin=margin,
        native_zoom=chunk_native_zoom(z, size),
        bbox=bbox,
        tiles=[tile_text(t) for t in kept],
        sea=[tile_text(t) for t in ordered if t not in kept],
        done=[tile_text(t) for t in done],
    )


def _manifest(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _covering(previous: dict, bbox: BBox) -> BBox | None:
    """A fetch manifest's requested box if it contains *bbox* (a sub-block reuses a bigger fetch), else None."""
    try:
        have = BBox.parse(previous["bbox"])
    except (KeyError, TypeError, AttributeError, AtlasError):
        return None
    eps = 1e-6  # manifests record the box to 6 decimals (BBox.as_text)
    inside = (
        have.west - eps <= bbox.west
        and have.south - eps <= bbox.south
        and have.east + eps >= bbox.east
        and have.north + eps >= bbox.north
    )
    return have if inside else None


def block_dem(
    bbox: BBox,
    inputs: Path,
    *,
    source: str,
    allow_missing: bool = False,
    max_tiles: int | None = None,
    log: LogSink | None = None,
) -> Path:
    """``<inputs>/dem.tif`` covering *bbox*: reused while its manifest's box contains it, else fetched."""
    from argus_atlas.dem import MAX_TILES, fetch_dem

    out = inputs / "dem.tif"
    previous = _manifest(out.with_suffix(".json"))
    if out.is_file() and previous and previous.get("snap") == "outward" and _covering(previous, bbox):
        if log:
            log(f"dem: reusing {out}")
        return out
    if log:
        log(f"dem: fetching Copernicus GLO-30 for {bbox.as_text()}")
    fetch_dem(
        bbox,
        out,
        source=source,
        allow_missing=allow_missing,
        cover=True,
        max_tiles=MAX_TILES if max_tiles is None else max_tiles,
    )
    return out


def block_osm(bbox: BBox, inputs: Path, layers: list[str], zoom: int, *, url: str, log: LogSink | None = None) -> dict:
    """``<inputs>/osm``: one Overpass call for the block, reused while its manifest covers the request.

    A refetch for a missing layer keeps the previous fetch's box (when it contains *bbox*) and
    its layers, so runs that share the inputs dir with different ``--osm-layers`` or a smaller
    block do not evict each other's inputs.
    """
    from argus_atlas.osm import fetch_osm

    out = inputs / "osm"
    previous = _manifest(out / "osm.json")
    covering = _covering(previous, bbox) if previous and previous.get("zoom") == zoom else None
    if previous and covering is not None:
        written = previous.get("layers") or {}
        skipped = previous.get("skipped") or []
        have = {n for n, rec in written.items() if (out / rec.get("path", "")).is_file()}
        if set(layers) <= have | set(skipped):
            if log:
                log(f"osm: reusing {out}")
            return previous
        bbox, layers = covering, list(dict.fromkeys([*layers, *written, *skipped]))
    if log:
        log(f"osm: fetching {', '.join(layers)} at zoom {zoom}")
    return fetch_osm(bbox, out, layers, zoom, url=url)


def run_block(
    plan: BlockPlan,
    recipe: RecipeRecord,
    out_dir: Path,
    *,
    dem: Path | None,
    layers: dict[str, Path] | None = None,
    places: Path | None = None,
    sea_level: float | None = None,
    backend=None,  # noqa: ANN001 - BakeBackend; None with controls_only
    models_root: Path | None = None,
    controls_only: bool = False,
    log: LogSink | None = None,
    progress=None,  # noqa: ANN001 - ProgressSink, passed to each chunk
) -> list[JobSidecar]:
    """Bake the plan's chunks in order, skipping the done ones; label each with *places* when given.

    Stops at the first failure: later chunks would seam against a gap. A re-run resumes.
    Done chunks are skipped with *controls_only* too: rebuilding their controls would
    replace the sidecar that publishes their crop to the neighbours.
    """
    from argus_atlas.chunks import parse_tile, run_chunk
    from argus_atlas.labels import label_job

    todo = plan.todo
    if todo and dem is None:
        raise AtlasError("baking a block needs a DEM")
    out_dir.mkdir(parents=True, exist_ok=True)
    baked = []
    for i, text in enumerate(todo, 1):
        z, x, y = parse_tile(text)
        job_dir = out_dir / chunk_job_id(recipe.recipe.name, z, x, y)
        if log:
            log(f"[{i}/{len(todo)}] {text} -> {job_dir.name}")
        sidecar = run_chunk(
            dem,  # type: ignore[arg-type] - checked above
            text,
            recipe,
            job_dir,
            backend=backend,
            models_root=models_root,
            neighbours_root=out_dir,
            layers=layers,
            sea_level=sea_level,
            size=plan.size,
            margin=plan.margin,
            controls_only=controls_only,
            progress=progress,
        )
        baked.append(sidecar)
    if places is not None and not controls_only:
        for text in plan.tiles:
            job_dir = out_dir / chunk_job_id(recipe.recipe.name, *parse_tile(text))
            if load_sidecar(job_dir).labels is None:
                record = label_job(job_dir, places)
                if log:
                    log(f"labels: {text} {record.count} name(s)")
    return baked
