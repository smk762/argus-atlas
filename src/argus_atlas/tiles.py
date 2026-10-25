"""Cut a short XYZ pyramid from the aligned GeoTIFF and pack it as PMTiles.

The archive's JSON metadata carries the bake payload (``argus_bake``) so a
tileset can be ranked and reproduced without its job directory.

``gdal2tiles.py --xyz`` tiles the raster for ``zmin..zmax`` only — the zoom
range ends at the canvas's **native** zoom (never finer) — and is bounded to
the raster, so no tile lies outside the AOI. The directory is then packed with
the Python ``pmtiles`` library (the ``tiles`` extra). The Go ``pmtiles`` CLI
cannot pack a directory (it converts MBTiles), so the library is the packer in
the image; :func:`packer_status` reports both for ``doctor``.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.mercator import intersects, mercator_to_bbox, resolution_at_zoom, tile_bounds
from argus_atlas.models import AtlasError, FileRef, Grid, JobSidecar, TilesRecord

logger = structlog.get_logger()

PACKER_PYTHON = "pmtiles (python library)"
# A chunk sits exactly on tile edges; anything overlapping it by under half a pixel is a tiler sliver.
CHUNK_SLIVER_PX = 0.5
FANTASY_PMTILES = "fantasy.pmtiles"


class TilesError(AtlasError):
    """Tiling or packing failed."""


def packer_status() -> dict[str, str | None]:
    """Which PMTiles tooling is present: the Python library and/or the Go CLI."""
    try:
        import pmtiles  # noqa: F401

        library = PACKER_PYTHON
    except ImportError:
        library = None
    return {"library": library, "cli": shutil.which("pmtiles")}


def clip_zoom_range(requested: tuple[int, int], native: int) -> tuple[int, int]:
    """Clamp a requested ``(zmin, zmax)`` so nothing finer than *native* is cut."""
    zmin, zmax = requested
    if zmin < 0 or zmax < zmin:
        raise TilesError(f"bad zoom range {requested}")
    if zmax > native:
        raise TilesError(f"max zoom {zmax} is finer than the canvas's native zoom {native}")
    return zmin, zmax


def collect_xyz(
    xyz_dir: Path, grid: Grid, zmin: int, zmax: int, *, sliver_px: float = 0.0
) -> list[tuple[int, int, int, Path]]:
    """``(z, x, y, path)`` for every tile under *xyz_dir*, verified to be within
    the zoom range and to intersect the canvas.

    With *sliver_px*, a tile that overlaps the canvas by less than that many of
    its own pixels is dropped instead: gdal2tiles adds such slivers when a raster
    edge falls exactly on a tile edge (every chunk), and in a mosaic of chunks
    they would overwrite the neighbour's real tile.
    """
    tiles: list[tuple[int, int, int, Path]] = []
    for z_dir in sorted(p for p in xyz_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        z = int(z_dir.name)
        if not zmin <= z <= zmax:
            raise TilesError(f"tiler wrote zoom {z}, outside the requested range {zmin}-{zmax}")
        for x_dir in sorted(p for p in z_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            x = int(x_dir.name)
            for tile in sorted(x_dir.glob("*.png")):
                y = int(tile.stem)
                bounds = tile_bounds(z, x, y)
                if sliver_px and not intersects(bounds, grid.bounds, eps=sliver_px * resolution_at_zoom(z)):
                    logger.debug("tiles.sliver_dropped", tile=f"{z}/{x}/{y}")
                    continue
                if not intersects(bounds, grid.bounds):
                    raise TilesError(f"tile {z}/{x}/{y} lies outside the canvas")
                tiles.append((z, x, y, tile))
    if not tiles:
        raise TilesError(f"tiler produced no tiles under {xyz_dir}")
    return tiles


def pack_pmtiles(
    tiles: list[tuple[int, int, int, Path]], out: Path, grid: Grid, *, name: str, extra_metadata: dict | None = None
) -> int:
    """Write *tiles* (XYZ PNGs) as one PMTiles v3 archive; return the tile count."""
    try:
        from pmtiles.tile import Compression, TileType, zxy_to_tileid
        from pmtiles.writer import write
    except ImportError as exc:
        raise TilesError("packing PMTiles needs: pip install argus-atlas[tiles]") from exc

    zooms = sorted({z for z, _, _, _ in tiles})
    bbox = mercator_to_bbox(*grid.bounds)
    e7 = 10_000_000
    header = {
        "tile_type": TileType.PNG,
        "tile_compression": Compression.NONE,
        "min_zoom": zooms[0],
        "max_zoom": zooms[-1],
        "min_lon_e7": int(bbox.west * e7),
        "min_lat_e7": int(bbox.south * e7),
        "max_lon_e7": int(bbox.east * e7),
        "max_lat_e7": int(bbox.north * e7),
        "center_zoom": zooms[-1],
        "center_lon_e7": int((bbox.west + bbox.east) / 2 * e7),
        "center_lat_e7": int((bbox.south + bbox.north) / 2 * e7),
    }
    metadata = {
        "name": name,
        "format": "png",
        "type": "overlay",
        "bounds": bbox.as_text(),
        "minzoom": str(zooms[0]),
        "maxzoom": str(zooms[-1]),
        "generator": "argus-atlas",
        **(extra_metadata or {}),
    }
    ordered = sorted(((zxy_to_tileid(z, x, y), path) for z, x, y, path in tiles), key=lambda t: t[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    with write(str(out)) as writer:
        for tile_id, path in ordered:
            writer.write_tile(tile_id, path.read_bytes())
        writer.finalize(header, metadata)
    return len(ordered)


def build_tiles(
    tif: Path,
    out: Path,
    grid: Grid,
    *,
    zoom_range: tuple[int, int],
    native_zoom: int,
    name: str,
    work_dir: Path | None = None,
    extra_metadata: dict | None = None,
    sliver_px: float = 0.0,
) -> TilesRecord:
    """Tile *tif* for *zoom_range* with gdal2tiles and pack the result at *out*
    (*sliver_px*: see :func:`collect_xyz`)."""
    zmin, zmax = clip_zoom_range(zoom_range, native_zoom)
    tiler = gdal_ops.find_tiler()
    if tiler is None:
        raise TilesError("gdal2tiles.py is not on PATH — install python3-gdal (apt) or gdal-python-tools (dnf)")
    if not tif.is_file():
        raise TilesError(f"GeoTIFF not found: {tif}")

    tmp_root = tempfile.mkdtemp(prefix="atlas-xyz-", dir=str(work_dir) if work_dir else None)
    xyz_dir = Path(tmp_root) / "xyz"
    try:
        gdal_ops.run(gdal_ops.tiles_argv(tiler, tif, xyz_dir, zmin, zmax))
        tiles = collect_xyz(xyz_dir, grid, zmin, zmax, sliver_px=sliver_px)
        n = pack_pmtiles(tiles, out, grid, name=name, extra_metadata=extra_metadata)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    logger.info("tiles.done", archive=str(out), zmin=zmin, zmax=zmax, n_tiles=n)
    return TilesRecord(
        archive=FileRef(path=out.name, sha256=sha256_file(out)),
        min_zoom=zmin,
        max_zoom=zmax,
        native_zoom=native_zoom,
        n_tiles=n,
        tiler=Path(tiler).name,
        packer=PACKER_PYTHON,
        completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def tile_source(job_dir: Path, sidecar: JobSidecar) -> tuple[Path, Grid]:
    """The raster to tile and the grid it must sit on: the cropped tile of a chunk job, else ``fantasy.tif``."""
    from argus_atlas.georef import FANTASY_TIF

    if sidecar.bake is None or not sidecar.bake.aligned or sidecar.bake.geotiff is None:
        raise TilesError(f"{sidecar.job_id} has no aligned GeoTIFF — bake it first (or its alignment gate failed)")
    if sidecar.chunk is None:
        return job_dir / FANTASY_TIF, sidecar.grid
    if sidecar.chunk.image is None:
        raise TilesError(f"chunk {sidecar.chunk.tile} of {sidecar.job_id} was never cropped — bake it again")
    return job_dir / sidecar.chunk.image.path, sidecar.chunk.inner


def tile_job(
    job_dir: Path,
    sidecar: JobSidecar,
    *,
    out: Path | None = None,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
) -> TilesRecord:
    """Gate the job's raster again, cut its archive, and record it (and its timing) in ``job.json``."""
    from argus_atlas.georef import verify_alignment
    from argus_atlas.jobs import save_sidecar

    started = time.perf_counter()
    tif, grid = tile_source(job_dir, sidecar)
    zmin, zmax = sidecar.zoom_range
    gdal_ops.require_gdal()
    verify_alignment(tif, grid)  # the gate again, right before tiles are written
    record = build_tiles(
        tif,
        out or job_dir / FANTASY_PMTILES,
        grid,
        zoom_range=(min_zoom if min_zoom is not None else zmin, max_zoom if max_zoom is not None else zmax),
        native_zoom=sidecar.native_zoom,
        name=sidecar.job_id,
        work_dir=job_dir,
        extra_metadata=bake_metadata(sidecar),
        sliver_px=CHUNK_SLIVER_PX if sidecar.chunk is not None else 0.0,
    )
    sidecar.tiles = record
    sidecar.timings = {**sidecar.timings, "tiles": round(time.perf_counter() - started, 3)}
    save_sidecar(job_dir, sidecar)
    return record


def bake_metadata(sidecar) -> dict:  # noqa: ANN001 - JobSidecar
    """``extra_metadata`` for :func:`build_tiles`: the bake payload under ``argus_bake``."""
    if sidecar.bake is None:
        return {}
    from argus_atlas.metadata import bake_payload

    b = sidecar.bake
    return {
        "argus_bake": bake_payload(
            sidecar,
            b.params,
            checkpoint=b.checkpoint,
            lora=b.lora,
            backend=b.backend,
            engine_version=b.engine_version,
            workflow_sha256=b.workflow_sha256,
        )
    }
