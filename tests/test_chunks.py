from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from argus_atlas import gdal_ops
from argus_atlas.backends.base import BackendError
from argus_atlas.bake import run_bake
from argus_atlas.chunks import (
    CHUNK_TIF,
    chunk_bbox,
    chunk_grids,
    chunk_job_id,
    find_neighbours,
    neighbour_tiles,
    parse_tile,
    run_chunk,
)
from argus_atlas.georef import AlignmentError
from argus_atlas.jobs import copy_job, load_sidecar
from argus_atlas.mercator import mercator_to_bbox, resolution_at_zoom, tile_bounds
from argus_atlas.metadata import read_png_payload
from argus_atlas.models import AtlasError, BakeParams, ChunkRecord, Recipe
from argus_atlas.recipes import recipe_params, recipe_record
from argus_atlas.tiles import NotTileableError, TilesError, tile_job
from tests.conftest import requires_gdal
from tests.fakebackend import FakeBackend

# Three z14 tiles well inside the synthetic DEM (lon 10..10.2, lat 45..45.1): B east of A, C south-east of A.
A, B, C = "14/8650/5890", "14/8651/5890", "14/8651/5891"
SIZE, MARGIN = 256, 32
RECIPE = recipe_record(Recipe(name="antique", preset="antique", checkpoint="sd_xl_base_1.0.safetensors"))
# A bake of a chunk job that does not come from its recipe (`bake`, POST /bake/stream).
OTHER = BakeParams(checkpoint="sd_xl_base_1.0.safetensors", seed=7, preset="cyberpunk")


def test_parse_tile() -> None:
    assert parse_tile("14/8650/5890") == (14, 8650, 5890)
    for bad in ("14/8650", "a/b/c", "14/-1/2", "2/4/0", "1/0/2", "1/²/0", "15/0/0", "99999999999/0/0"):
        with pytest.raises(AtlasError):
            parse_tile(bad)


def test_edge_chunks_have_a_valid_archive_bbox() -> None:
    # The first and last XYZ columns sit on the antimeridian: float noise must not push the bounds past ±180.
    for tile in ("0/0/0", "2/0/0", "14/0/8191", "14/16383/8953"):
        _, inner = chunk_grids(*parse_tile(tile), size=SIZE, margin=MARGIN)
        bbox = mercator_to_bbox(*inner.bounds)
        assert -180.0 <= bbox.west < bbox.east <= 180.0


def test_unreadable_job_dirs_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "someone-elses-job").mkdir()

    def denied(job_dir: Path) -> None:
        raise PermissionError(13, "Permission denied", str(job_dir / "job.json"))

    monkeypatch.setattr("argus_atlas.chunks.load_sidecar", denied)
    _, inner = chunk_grids(*parse_tile(A), size=SIZE, margin=MARGIN)
    probe = ChunkRecord(z=14, x=8650, y=5890, size=SIZE, margin=MARGIN, inner=inner)
    assert find_neighbours(tmp_path, "antique", probe) == []


def test_chunk_grids_sit_on_the_tile_lattice() -> None:
    canvas, inner = chunk_grids(14, 8650, 5890, size=SIZE, margin=MARGIN)
    xmin, ymin, xmax, ymax = tile_bounds(14, 8650, 5890)
    assert inner.bounds == pytest.approx((xmin, ymin, xmax, ymax), abs=1e-6)
    assert (inner.width, inner.height) == (SIZE, SIZE)
    assert (canvas.width, canvas.height) == (SIZE + 2 * MARGIN, SIZE + 2 * MARGIN)
    assert canvas.resolution == pytest.approx(resolution_at_zoom(14))
    assert canvas.ulx == pytest.approx(xmin - MARGIN * canvas.resolution)
    # 1024 px at zoom 10 is native zoom 12: four tiles across, each exactly a pixel lattice tile
    _, big = chunk_grids(10, 900, 600, size=1024, margin=128)
    assert big.resolution == pytest.approx(resolution_at_zoom(12))


@pytest.mark.parametrize(
    ("z", "size", "margin", "match"),
    [
        (10, 768, 0, "one of"),
        (10, 1024, 48, "multiple of 32"),
        (10, 1024, 288, "over 1536"),
        (13, 1024, 128, "finer than 14"),
    ],
)
def test_chunk_validation(z: int, size: int, margin: int, match: str) -> None:
    with pytest.raises(AtlasError, match=match):
        chunk_grids(z, 0, 0, size=size, margin=margin)


def test_neighbours_and_ids() -> None:
    assert len(neighbour_tiles(14, 8650, 5890)) == 8
    assert sorted(neighbour_tiles(1, 0, 0)) == [(1, 0, 1), (1, 1, 0), (1, 1, 1)]
    assert chunk_job_id("antique", 14, 8650, 5890) == "antique-14-8650-5890"
    top = chunk_bbox(0, 0, 0)
    assert top.north <= 85.05112878 and top.west == pytest.approx(-180)


def _stats(path: Path) -> list[dict]:
    out = subprocess.run(["gdalinfo", "-json", "-stats", str(path)], capture_output=True, text=True, check=True)
    for aux in path.parent.glob(path.name + ".aux.xml"):
        aux.unlink()
    return json.loads(out.stdout)["bands"]


def _chunk(dem: Path, root: Path, tile: str, models_dir: Path, **kwargs):  # noqa: ANN202
    z, x, y = parse_tile(tile)
    return run_chunk(
        dem,
        tile,
        kwargs.pop("recipe", RECIPE),
        root / chunk_job_id("antique", z, x, y),
        backend=FakeBackend(),
        models_root=models_dir,
        size=SIZE,
        margin=MARGIN,
        **kwargs,
    )


@requires_gdal
def test_controls_only_first_chunk_paints_everything(dem: Path, tmp_path: Path) -> None:
    job_dir = tmp_path / "antique-14-8650-5890"
    sidecar = run_chunk(dem, A, RECIPE, job_dir, size=SIZE, margin=MARGIN, controls_only=True)
    assert sidecar.chunk is not None and sidecar.chunk.tile == A and sidecar.chunk.neighbours == []
    assert {"hillshade", "slope", "seam_init", "seam_mask"} <= set(sidecar.controls)
    assert (sidecar.grid.width, sidecar.grid.height) == (SIZE + 2 * MARGIN, SIZE + 2 * MARGIN)
    assert sidecar.zoom_range == (14, 14) and sidecar.native_zoom == 14
    assert sidecar.recipe == RECIPE and sidecar.bake is None
    assert {"controls", "seams"} <= set(sidecar.timings)
    mask = _stats(job_dir / "seam_mask.png")
    assert mask[0]["minimum"] == 255 and mask[0]["maximum"] == 255, "no neighbours: paint the whole canvas"
    assert len(_stats(job_dir / "seam_init.png")) == 3, "the init image is RGB even from a grey hillshade"


@requires_gdal
def test_chunk_bakes_crops_and_tiles_inside_the_tile(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    sidecar = _chunk(dem, tmp_path, A, models_dir)
    job_dir = tmp_path / sidecar.job_id
    assert sidecar.status == "tiled"
    assert sidecar.chunk is not None and sidecar.chunk.image is not None and sidecar.chunk.image.path == CHUNK_TIF
    assert sidecar.bake is not None and sidecar.bake.params.recipe == "antique"
    info = json.loads(
        subprocess.run(["gdalinfo", "-json", str(job_dir / CHUNK_TIF)], capture_output=True, text=True).stdout
    )
    assert info["size"] == [SIZE, SIZE]
    assert sidecar.tiles is not None and (sidecar.tiles.min_zoom, sidecar.tiles.max_zoom) == (14, 14)
    assert sidecar.tiles.n_tiles == 1, "a 256 px chunk at its native zoom is exactly its own tile"
    assert {"controls", "seams", "verify", "generate", "georeference", "crop", "tiles"} <= set(sidecar.timings)
    assert all(v >= 0 for v in sidecar.timings.values())
    assert any(c.argv[:1] == ["gdal_translate"] and "-srcwin" in c.argv for c in sidecar.commands)


@requires_gdal
def test_neighbours_are_pasted_into_the_margin(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    _chunk(dem, tmp_path, A, models_dir)
    b = _chunk(dem, tmp_path, B, models_dir)
    assert b.chunk is not None and [n.tile for n in b.chunk.neighbours] == [A]
    # A is west of B: the kept strip is the margin's width and the tile's height
    side = SIZE + 2 * MARGIN
    mask = _stats(tmp_path / b.job_id / "seam_mask.png")[0]
    assert mask["mean"] == pytest.approx(255 * (1 - MARGIN * SIZE / side**2), abs=0.01)

    c = _chunk(dem, tmp_path, C, models_dir)
    assert c.chunk is not None and [n.tile for n in c.chunk.neighbours] == [A, B]
    kept = MARGIN * MARGIN + MARGIN * SIZE  # A touches C's corner, B its top edge
    mask = _stats(tmp_path / c.job_id / "seam_mask.png")[0]
    assert mask["mean"] == pytest.approx(255 * (1 - kept / side**2), abs=0.01)


@requires_gdal
def test_seam_init_carries_the_neighbours_pixels(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a = _chunk(dem, tmp_path, A, models_dir)
    b = run_chunk(dem, B, RECIPE, tmp_path / "antique-14-8651-5890", size=SIZE, margin=MARGIN, controls_only=True)
    # The pixel just west of B's tile edge is A's easternmost column.
    canvas = b.grid
    xmin, _, _, ymax = tile_bounds(*parse_tile(B))
    px, py = MARGIN - 1, MARGIN + SIZE // 2
    x_m = canvas.ulx + (px + 0.5) * canvas.resolution
    y_m = canvas.uly - (py + 0.5) * canvas.resolution
    assert x_m < xmin

    def value(path: Path, x: float, y: float) -> list[str]:
        out = subprocess.run(
            ["gdallocationinfo", "-valonly", "-geoloc", str(path), repr(x), repr(y)],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    seam = tmp_path / b.job_id / "seam_init.tif"
    assert value(seam, x_m, y_m) == value(tmp_path / a.job_id / CHUNK_TIF, x_m, y_m)


@requires_gdal
def test_neighbour_rules(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a = _chunk(dem, tmp_path, A, models_dir)
    other = recipe_record(Recipe(name="neon", preset="cyberpunk", checkpoint="sd_xl_base_1.0.safetensors"))
    b_neon = run_chunk(dem, B, other, tmp_path / "neon-14-8651-5890", size=SIZE, margin=MARGIN, controls_only=True)
    assert b_neon.chunk is not None and b_neon.chunk.neighbours == [], "another recipe's chunks are not neighbours"

    probe = load_sidecar(tmp_path / "neon-14-8651-5890").chunk
    assert probe is not None
    assert [n.tile for n in find_neighbours(tmp_path, "antique", probe)] == [A]

    (tmp_path / a.job_id / CHUNK_TIF).write_bytes(b"tampered")
    with pytest.raises(AtlasError, match="changed since"):
        find_neighbours(tmp_path, "antique", probe)


@requires_gdal
def test_a_512_px_chunk_cuts_two_zooms_without_slivers(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    sidecar = run_chunk(
        dem,
        "13/4325/2945",
        RECIPE,
        tmp_path / "antique-13-4325-2945",
        backend=FakeBackend(),
        models_root=models_dir,
        size=512,
        margin=64,
    )
    assert sidecar.native_zoom == 14 and sidecar.zoom_range == (13, 14)
    assert sidecar.tiles is not None and sidecar.tiles.n_tiles == 1 + 4, "the tile itself plus its four children"


@requires_gdal
def test_tiles_never_start_coarser_than_the_chunk(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    sidecar = _chunk(dem, tmp_path, A, models_dir)
    with pytest.raises(TilesError, match="own zoom 14"):
        tile_job(tmp_path / sidecar.job_id, sidecar, min_zoom=13)


@requires_gdal
def test_a_bake_outside_the_recipe_is_not_its_chunk(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a = _chunk(dem, tmp_path, A, models_dir)
    a_dir = tmp_path / a.job_id
    # A variant baked next to A (`bake --out`) neither blocks A's neighbours nor stands in for A.
    variant = tmp_path / f"{a.job_id}-v2"
    copy_job(a_dir, variant)
    run_bake(variant, OTHER, FakeBackend(), models_root=models_dir)
    copied = load_sidecar(variant)
    assert copied.recipe is None and copied.chunk is not None and copied.chunk.image is not None
    b = _chunk(dem, tmp_path, B, models_dir)
    assert b.chunk is not None and [n.tile for n in b.chunk.neighbours] == [A]
    # Re-baking A itself with other params takes it out of the recipe's map, and its payload says so.
    run_bake(a_dir, OTHER, FakeBackend(), models_root=models_dir)
    assert load_sidecar(a_dir).recipe is None
    payload = read_png_payload(a_dir / "fantasy.png")
    assert payload is not None and payload["recipe_sha256"] is None
    c_dir = tmp_path / chunk_job_id("antique", *parse_tile(C))
    c = run_chunk(dem, C, RECIPE, c_dir, size=SIZE, margin=MARGIN, controls_only=True)
    assert c.chunk is not None and [n.tile for n in c.chunk.neighbours] == [B]


@requires_gdal
def test_a_failed_rebake_keeps_the_published_chunk(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a = _chunk(dem, tmp_path, A, models_dir)
    with pytest.raises(BackendError, match="engine down"):
        run_chunk(
            dem,
            A,
            RECIPE,
            tmp_path / a.job_id,
            backend=FakeBackend(fail="engine down"),
            models_root=models_dir,
            size=SIZE,
            margin=MARGIN,
        )
    kept = load_sidecar(tmp_path / a.job_id)
    assert kept.status == "tiled" and kept.chunk is not None and a.chunk is not None
    assert kept.chunk.image == a.chunk.image


@requires_gdal
def test_chunks_of_one_recipe_never_overlap(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    # 13/4325/2945 at 512 px also cuts zoom 14, A among its tiles.
    run_chunk(
        dem,
        "13/4325/2945",
        RECIPE,
        tmp_path / "antique-13-4325-2945",
        backend=FakeBackend(),
        models_root=models_dir,
        size=512,
        margin=64,
    )
    a_dir = tmp_path / chunk_job_id("antique", *parse_tile(A))
    with pytest.raises(AtlasError, match="overlaps 14/8650/5890"):
        run_chunk(dem, A, RECIPE, a_dir, size=SIZE, margin=MARGIN, controls_only=True)
    assert not a_dir.exists(), "refused before anything was written"


@requires_gdal
def test_a_symlink_to_a_chunk_is_not_a_second_holder(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a = _chunk(dem, tmp_path, A, models_dir)
    (tmp_path / "latest").symlink_to(tmp_path / a.job_id, target_is_directory=True)
    b_dir = tmp_path / chunk_job_id("antique", *parse_tile(B))
    b = run_chunk(dem, B, RECIPE, b_dir, size=SIZE, margin=MARGIN, controls_only=True)
    assert b.chunk is not None and [n.tile for n in b.chunk.neighbours] == [A]


@requires_gdal
def test_a_failed_crop_is_recorded(
    dem: Path, tmp_path: Path, models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / chunk_job_id("antique", *parse_tile(A))
    run_chunk(dem, A, RECIPE, job_dir, size=SIZE, margin=MARGIN, controls_only=True)
    crop = gdal_ops.crop_argv
    monkeypatch.setattr(gdal_ops, "crop_argv", lambda src, dst, xoff, yoff, w, h: crop(src, dst, xoff, yoff, w - 1, h))
    with pytest.raises(AlignmentError):
        run_bake(job_dir, recipe_params(RECIPE.recipe, seed=1), FakeBackend(), models_root=models_dir)
    sidecar = load_sidecar(job_dir)
    assert sidecar.bake is not None and (sidecar.bake.error or "").startswith("crop failed")
    assert "crop" in sidecar.timings
    with pytest.raises(NotTileableError, match="never cropped"):
        tile_job(job_dir, sidecar)
