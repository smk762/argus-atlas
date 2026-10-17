"""Cut a short XYZ pyramid from the aligned GeoTIFF and pack it as PMTiles.

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
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.mercator import intersects, mercator_to_bbox, tile_bounds
from argus_atlas.models import AtlasError, FileRef, Grid, TilesRecord

logger = structlog.get_logger()

PACKER_PYTHON = "pmtiles (python library)"
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


def collect_xyz(xyz_dir: Path, grid: Grid, zmin: int, zmax: int) -> list[tuple[int, int, int, Path]]:
    """``(z, x, y, path)`` for every tile under *xyz_dir*, verified to be within
    the zoom range and to intersect the canvas."""
    tiles: list[tuple[int, int, int, Path]] = []
    for z_dir in sorted(p for p in xyz_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        z = int(z_dir.name)
        if not zmin <= z <= zmax:
            raise TilesError(f"tiler wrote zoom {z}, outside the requested range {zmin}-{zmax}")
        for x_dir in sorted(p for p in z_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            x = int(x_dir.name)
            for tile in sorted(x_dir.glob("*.png")):
                y = int(tile.stem)
                if not intersects(tile_bounds(z, x, y), grid.bounds):
                    raise TilesError(f"tile {z}/{x}/{y} lies outside the canvas")
                tiles.append((z, x, y, tile))
    if not tiles:
        raise TilesError(f"tiler produced no tiles under {xyz_dir}")
    return tiles


def pack_pmtiles(tiles: list[tuple[int, int, int, Path]], out: Path, grid: Grid, *, name: str) -> int:
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
) -> TilesRecord:
    """Tile *tif* for *zoom_range* with gdal2tiles and pack the result at *out*."""
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
        tiles = collect_xyz(xyz_dir, grid, zmin, zmax)
        n = pack_pmtiles(tiles, out, grid, name=name)
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
