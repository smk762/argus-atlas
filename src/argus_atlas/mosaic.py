"""Mosaic the chunks of one style into one tile set, with overview zooms and merged labels.

A chunked bake leaves one ``fantasy.pmtiles`` per chunk job. :func:`mosaic` reads
every chunk archive under a style dir and writes them as one XYZ directory or one
PMTiles archive:

* **Chunk zooms** are copied byte for byte from the archives. Neighbouring chunks
  never share a tile, so a tile two archives both carry with different bytes is
  an error that names both jobs, never a silent overwrite.
* **Overviews** below the chunk zoom, down to *overview_min*, are cut by
  ``gdal2tiles`` from a ``gdalbuildvrt -addalpha`` mosaic of the chunks' gated crops,
  so the painted map does not vanish when a viewer zooms out. Gaps between chunks
  (skipped sea) stay transparent.
* **Labels**: every chunk's ``labels.geojson`` merged into one, a name at the same
  point kept once.
* **Manifest** (``mosaic.json``, or ``<stem>.json`` beside a ``.pmtiles``): the zoom
  range, bounds, tile count, every source chunk's job id, tile and archive SHA256,
  and the GDAL argv.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import is_safe_id, load_sidecar
from argus_atlas.labels import LABELS_NAME
from argus_atlas.mercator import mercator_to_bbox
from argus_atlas.models import AtlasError, Grid, JobSidecar
from argus_atlas.tiles import CHUNK_SLIVER_PX, TilesError, collect_xyz, pack_pmtiles

logger = structlog.get_logger()

MOSAIC_MANIFEST = "mosaic.json"
DEFAULT_OVERVIEW_MIN = 5

TileKey = tuple[int, int, int]


class MosaicError(TilesError):
    """The chunks cannot be laid into one tile set (none found, or two disagree on a tile)."""


def style_chunks(style_dir: Path) -> list[tuple[Path, JobSidecar]]:
    """``(job_dir, sidecar)`` of every chunk of the one recipe under *style_dir* with its crop and archive on disk.

    As in :func:`argus_atlas.chunks.find_neighbours`: a chunk job re-baked with other
    params (its bake was not resolved from its recipe) is not that recipe's chunk and
    is skipped, and a second name (a symlink) for a job is read once. Chunks of two
    recipes, or a crop or archive whose bytes no longer match ``job.json``, are an
    error, not a skip.
    """
    found = []
    seen: set[Path] = set()
    recipes: dict[str, str] = {}
    for job_dir in sorted(p for p in style_dir.iterdir() if p.is_dir() and is_safe_id(p.name)):
        real = job_dir.resolve()
        if real in seen:
            continue
        seen.add(real)
        try:
            sidecar = load_sidecar(job_dir)
        except (OSError, AtlasError):
            continue
        chunk, tiles, recipe, bake = sidecar.chunk, sidecar.tiles, sidecar.recipe, sidecar.bake
        if chunk is None or chunk.image is None or tiles is None or recipe is None or bake is None:
            continue
        if bake.params.recipe != recipe.recipe.name:
            continue  # re-baked with other params: not this recipe's chunk
        crop, archive = job_dir / chunk.image.path, job_dir / tiles.archive.path
        if not archive.is_file() or not crop.is_file():
            continue
        for ref, path in ((chunk.image, crop), (tiles.archive, archive)):
            if sha256_file(path) != ref.sha256:
                raise MosaicError(f"{sidecar.job_id}: {ref.path} changed since its job.json")
        recipes.setdefault(recipe.recipe.name, sidecar.job_id)
        found.append((job_dir, sidecar))
    if len(recipes) > 1:
        held = ", ".join(f"{name} ({job_id})" for name, job_id in sorted(recipes.items()))
        raise MosaicError(f"{style_dir} holds chunks of more than one recipe: {held}")
    return found


def read_archive(path: Path) -> dict[TileKey, bytes]:
    """Every tile of a PMTiles archive, keyed by ``(z, x, y)``."""
    try:
        from pmtiles.reader import MmapSource, all_tiles
    except ImportError as exc:
        raise TilesError("reading PMTiles needs: pip install argus-atlas[tiles]") from exc
    with path.open("rb") as f:
        return {tuple(zxy): bytes(data) for zxy, data in all_tiles(MmapSource(f))}  # type: ignore[misc]


def merge_archives(chunks: list[tuple[Path, JobSidecar]]) -> dict[TileKey, tuple[bytes, str]]:
    """All chunk tiles as ``{(z, x, y): (png, job_id)}``; MosaicError where two archives disagree."""
    merged: dict[TileKey, tuple[bytes, str]] = {}
    for job_dir, sidecar in chunks:
        assert sidecar.tiles is not None
        for key, data in read_archive(job_dir / sidecar.tiles.archive.path).items():
            held = merged.get(key)
            if held is not None and held[0] != data:
                z, x, y = key
                raise MosaicError(f"tile {z}/{x}/{y} differs between {held[1]} and {sidecar.job_id}")
            merged.setdefault(key, (data, sidecar.job_id))
    return merged


def union_grid(chunks: list[tuple[Path, JobSidecar]]) -> Grid:
    """A grid over every chunk's tile at the finest chunk resolution: the mosaic's bounds."""
    inners = [s.chunk.inner for _, s in chunks if s.chunk is not None]
    xmin, ymin = min(g.ulx for g in inners), min(g.lry for g in inners)
    xmax, ymax = max(g.lrx for g in inners), max(g.uly for g in inners)
    res = min(g.resolution for g in inners)
    return Grid(
        width=max(1, round((xmax - xmin) / res)),
        height=max(1, round((ymax - ymin) / res)),
        ulx=xmin,
        uly=ymax,
        pixel_width=res,
        pixel_height=-res,
    )


def build_overviews(
    crops: list[Path], grid: Grid, zmin: int, zmax: int, work_dir: Path
) -> tuple[list[tuple[int, int, int, Path]], list]:
    """XYZ tiles for ``zmin..zmax`` from a VRT of the chunk crops, plus the GDAL commands run."""
    tiler = gdal_ops.find_tiler()
    if tiler is None:
        raise TilesError("gdal2tiles.py is not on PATH — install python3-gdal (apt) or gdal-python-tools (dnf)")
    vrt, xyz = work_dir / "chunks.vrt", work_dir / "overviews"
    commands = [gdal_ops.run(gdal_ops.mosaic_vrt_argv(crops, vrt))]
    commands.append(gdal_ops.run(gdal_ops.tiles_argv(tiler, vrt, xyz, zmin, zmax)))
    return collect_xyz(xyz, grid, zmin, zmax, sliver_px=CHUNK_SLIVER_PX), commands


def merge_labels(chunks: list[tuple[Path, JobSidecar]]) -> dict:
    """One FeatureCollection of every chunk's labels, a name at the same point kept once."""
    seen: set[tuple[str, float, float]] = set()
    features = []
    for job_dir, _ in chunks:
        path = job_dir / LABELS_NAME
        if not path.is_file():
            continue
        try:
            collection = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MosaicError(f"{path} is not readable GeoJSON: {exc}") from exc
        for feature in collection.get("features") or []:
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue  # labels are points; anything else is not ours to place
            lon, lat = coords[:2]
            key = (str((feature.get("properties") or {}).get("name")), round(lon, 6), round(lat, 6))
            if key not in seen:
                seen.add(key)
                features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def _outputs(out: Path) -> tuple[bool, Path, Path]:
    """``(as_pmtiles, labels_path, manifest_path)`` for *out*."""
    if out.suffix.lower() == ".pmtiles":
        return True, out.with_name(f"{out.stem}.{LABELS_NAME}"), out.with_suffix(".json")
    return False, out / LABELS_NAME, out / MOSAIC_MANIFEST


def _check_dir(out: Path) -> None:
    """An XYZ output dir must be new, empty, or an earlier mosaic. Checked before any work is done."""
    if not out.exists():
        return
    if not out.is_dir():
        raise MosaicError(f"{out} exists and is not a directory")
    if any(out.iterdir()) and not (out / MOSAIC_MANIFEST).is_file():
        raise MosaicError(f"{out} is not empty and holds no {MOSAIC_MANIFEST}: refusing to write into it")


def _prepare_dir(out: Path) -> None:
    """Replace an earlier mosaic's zoom dirs in *out* (already checked by :func:`_check_dir`)."""
    if out.exists():
        for entry in out.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                shutil.rmtree(entry)
    out.mkdir(parents=True, exist_ok=True)


def mosaic(style_dir: Path, out: Path, *, overview_min: int = DEFAULT_OVERVIEW_MIN, name: str | None = None) -> dict:
    """Lay every chunk under *style_dir* into *out* (an XYZ dir, or a ``.pmtiles`` file). Returns the manifest."""
    if not style_dir.is_dir():
        raise FileNotFoundError(f"style dir not found: {style_dir}")
    chunks = style_chunks(style_dir)
    if not chunks:
        raise MosaicError(f"no baked chunks under {style_dir}")
    as_pmtiles, labels_path, manifest_path = _outputs(out)
    if not as_pmtiles:
        _check_dir(out)  # refuse before the tiling, not after it
    labels = merge_labels(chunks)  # a bad labels file fails here, before any output is touched
    gdal_ops.require_gdal()
    merged = merge_archives(chunks)
    grid = union_grid(chunks)
    # Chunks may start at different zooms: overviews run up to the finest start, so no chunk leaves a gap.
    starts = [s.tiles.min_zoom for _, s in chunks if s.tiles is not None]
    chunk_min, overview_max = min(starts), max(starts) - 1
    commands: list = []
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".atlas-mosaic-", dir=out.parent))  # beside out, so tiles are renamed in
    try:
        tiles: list[tuple[int, int, int, Path]] = []
        raw = work / "chunks"
        for (z, x, y), (data, _) in merged.items():
            path = raw / str(z) / str(x) / f"{y}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            tiles.append((z, x, y, path))
        if overview_min <= overview_max:
            crops = [d / s.chunk.image.path for d, s in chunks if s.chunk and s.chunk.image]
            overviews, commands = build_overviews(crops, grid, overview_min, overview_max, work)
            tiles += [t for t in overviews if t[:3] not in merged]  # a chunk's own tile wins
        zooms = sorted({t[0] for t in tiles})
        metadata = {"argus_mosaic": json.dumps({"style_dir": style_dir.name, "chunks": len(chunks)})}
        if as_pmtiles:
            pack_pmtiles(tiles, out, grid, name=name or style_dir.name, extra_metadata=metadata)
        else:
            _prepare_dir(out)
            for z, x, y, path in tiles:
                dst = out / str(z) / str(x) / f"{y}.png"
                dst.parent.mkdir(parents=True, exist_ok=True)
                path.replace(dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(json.dumps(labels, ensure_ascii=False) + "\n", encoding="utf-8")
    bbox = mercator_to_bbox(*grid.bounds)
    manifest = {
        "style_dir": str(style_dir),
        "output": out.name,
        "format": "pmtiles" if as_pmtiles else "xyz",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "bounds": bbox.as_text(),
        "min_zoom": zooms[0],
        "max_zoom": zooms[-1],
        "chunk_min_zoom": chunk_min,
        "n_tiles": len(tiles),
        "chunks": [
            {
                "job_id": s.job_id,
                "tile": s.chunk.tile if s.chunk else None,
                "archive_sha256": s.tiles.archive.sha256 if s.tiles else None,  # re-checked in style_chunks
            }
            for _, s in chunks
        ],
        "labels": {"path": labels_path.name, "count": len(labels["features"])},
        "commands": [c.model_dump() for c in commands],
    }
    if as_pmtiles:
        manifest["sha256"] = sha256_file(out)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("mosaic.done", out=str(out), chunks=len(chunks), tiles=len(tiles), zooms=f"{zooms[0]}-{zooms[-1]}")
    return manifest
