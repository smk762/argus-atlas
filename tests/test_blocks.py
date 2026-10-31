from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_atlas.blocks import (
    bbox_tiles,
    block,
    chunk_done,
    land_tiles,
    plan_block,
    ring_tiles,
    run_block,
)
from argus_atlas.chunks import chunk_grids, chunk_job_id
from argus_atlas.cli import app
from argus_atlas.jobs import load_sidecar
from argus_atlas.mercator import bbox_to_mercator, mercator_to_bbox, tile_bounds
from argus_atlas.models import AtlasError, Recipe
from argus_atlas.recipes import recipe_record
from tests.conftest import requires_gdal
from tests.fakebackend import FakeBackend

runner = CliRunner()

# A 2x2 block of z14 tiles inside the synthetic DEM (lon 10..10.2, lat 45..45.1), as in test_chunks.
Z, X0, Y0 = 14, 8650, 5890
SIZE, MARGIN = 256, 32
RECIPE = recipe_record(Recipe(name="antique", preset="antique", checkpoint="sd_xl_base_1.0.safetensors"))


def _geojson(path: Path, features: list[dict]) -> Path:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    return path


def _tile_square(z: int, x: int, y: int, inset: float = 0.25) -> dict:
    """A polygon inside one tile, *inset* of its side from every edge (lon/lat)."""
    xmin, ymin, xmax, ymax = tile_bounds(z, x, y)
    dx, dy = (xmax - xmin) * inset, (ymax - ymin) * inset
    b = mercator_to_bbox(xmin + dx, ymin + dy, xmax - dx, ymax - dy)
    ring = [[b.west, b.south], [b.east, b.south], [b.east, b.north], [b.west, b.north], [b.west, b.south]]
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_ring_tiles_are_a_square_clipped_at_the_world_edge() -> None:
    tiles = ring_tiles(10, 1009, 641, 3)
    assert len(tiles) == 49 and (10, 1006, 638) in tiles and (10, 1012, 644) in tiles
    assert ring_tiles(1, 0, 0, 1) == [(1, 0, 0), (1, 1, 0), (1, 0, 1), (1, 1, 1)]
    assert ring_tiles(5, 3, 3, 0) == [(5, 3, 3)]
    with pytest.raises(AtlasError, match="rings"):
        ring_tiles(5, 3, 3, 99)


def test_bbox_tiles_cover_the_box() -> None:
    xmin, ymin, xmax, ymax = tile_bounds(Z, X0, Y0)
    inside = mercator_to_bbox(xmin + 10, ymin + 10, xmax + (xmax - xmin) / 2, ymax - 10)
    assert bbox_tiles(inside, Z) == [(Z, X0, Y0), (Z, X0 + 1, Y0)]
    with pytest.raises(AtlasError, match="block limit"):
        bbox_tiles(mercator_to_bbox(*tile_bounds(6, 33, 22)), Z)


def test_block_is_in_seam_order_and_covers_every_canvas() -> None:
    tiles = ring_tiles(10, 1009, 641, 3)
    ordered, bbox = block(list(reversed(tiles)), size=1024, margin=128)
    seen: set[tuple[int, int, int]] = set()
    for z, x, y in ordered:
        for before in ((z, x - 1, y), (z, x, y - 1), (z, x - 1, y - 1)):  # west, north, north-west
            assert before not in tiles or before in seen
        seen.add((z, x, y))
    xmin, ymin, xmax, ymax = bbox_to_mercator(bbox)
    for t in tiles:
        canvas, _ = chunk_grids(*t, size=1024, margin=128)
        cx0, cy0, cx1, cy1 = canvas.bounds
        assert xmin <= cx0 + 1e-6 and cx1 <= xmax + 1e-6 and ymin <= cy0 + 1e-6 and cy1 <= ymax + 1e-6
    assert block([(Z, X0, Y0), (Z, X0, Y0)], size=SIZE, margin=MARGIN)[0] == [(Z, X0, Y0)]
    with pytest.raises(AtlasError, match="one zoom"):
        block([(Z, X0, Y0), (Z - 1, X0 // 2, Y0 // 2)])
    with pytest.raises(AtlasError, match="at least one"):
        block([])


@requires_gdal
def test_land_tiles_keeps_only_tiles_with_land(tmp_path: Path) -> None:
    tiles = ring_tiles(Z, X0, Y0, 1)
    land = _geojson(tmp_path / "land.geojson", [_tile_square(Z, X0, Y0), _tile_square(Z, X0 + 1, Y0 + 1, 0.45)])
    assert land_tiles(tiles, land) == [(Z, X0, Y0), (Z, X0 + 1, Y0 + 1)]
    empty = _geojson(tmp_path / "none.geojson", [])
    assert land_tiles(tiles, empty) == []
    with pytest.raises(FileNotFoundError):
        land_tiles(tiles, tmp_path / "missing.geojson")


@requires_gdal
def test_run_block_bakes_in_order_resumes_and_labels(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    style = tmp_path / "antique"
    tiles = [(Z, X0 + dx, Y0 + dy) for dy in (0, 1) for dx in (0, 1)]
    land = _geojson(tmp_path / "land.geojson", [_tile_square(*t) for t in tiles[:3]])
    plan = plan_block(tiles, RECIPE, style, size=SIZE, margin=MARGIN, land=land)
    assert plan.tiles == [f"{Z}/{X0}/{Y0}", f"{Z}/{X0 + 1}/{Y0}", f"{Z}/{X0}/{Y0 + 1}"]
    assert plan.sea == [f"{Z}/{X0 + 1}/{Y0 + 1}"] and plan.done == [] and plan.todo == plan.tiles

    c = mercator_to_bbox(*tile_bounds(Z, X0, Y0))
    lon, lat = (c.west + c.east) / 2, (c.south + c.north) / 2
    places = _geojson(
        tmp_path / "places.geojson",
        [
            {
                "type": "Feature",
                "properties": {"name": "Here", "place": "village"},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        ],
    )
    lines: list[str] = []
    baked = run_block(
        plan, RECIPE, style, dem=dem, places=places, backend=FakeBackend(), models_root=models_dir, log=lines.append
    )
    assert [s.chunk.tile for s in baked if s.chunk] == plan.tiles
    assert lines[0].startswith("[1/3]")
    first, second = (load_sidecar(style / chunk_job_id("antique", Z, X0 + dx, Y0)) for dx in (0, 1))
    assert first.chunk is not None and first.chunk.neighbours == []
    assert second.chunk is not None and [n.tile for n in second.chunk.neighbours] == [plan.tiles[0]]
    assert first.labels is not None and first.labels.count == 1
    for text in plan.tiles:
        assert chunk_done(style / chunk_job_id("antique", *map(int, text.split("/"))), RECIPE, size=SIZE, margin=MARGIN)

    again = plan_block(tiles, RECIPE, style, size=SIZE, margin=MARGIN, land=land)
    assert again.done == again.tiles and again.todo == []
    assert run_block(again, RECIPE, style, dem=dem, backend=FakeBackend(), models_root=models_dir) == []
    assert run_block(again, RECIPE, style, dem=dem, controls_only=True) == []  # never unpublishes a done chunk
    assert all(chunk_done(style / chunk_job_id("antique", *t), RECIPE, size=SIZE, margin=MARGIN) for t in tiles[:3])
    other = recipe_record(Recipe(name="antique", preset="tolkien", checkpoint="sd_xl_base_1.0.safetensors"))
    assert plan_block(tiles, other, style, size=SIZE, margin=MARGIN, land=land).done == []  # a changed recipe rebakes


@requires_gdal
def test_cli_chunks_plan_and_controls_only(dem: Path, tmp_path: Path) -> None:
    recipe = tmp_path / "antique.json"
    recipe.write_text(json.dumps({"name": "antique", "preset": "antique", "checkpoint": "sd_xl_base_1.0.safetensors"}))
    style = tmp_path / "style"
    common = ["--recipe", str(recipe), "--out", str(style), "--size", str(SIZE), "--margin", str(MARGIN)]
    plan = runner.invoke(app, ["chunks", "--tile", f"{Z}/{X0}/{Y0}", "--rings", "1", "--plan", *common])
    assert plan.exit_code == 0, plan.output
    assert "block of 9 chunk(s) at z14" in plan.output and f"{Z}/{X0 - 1}/{Y0 - 1}" in plan.output
    assert not style.exists(), "--plan writes nothing"

    run = runner.invoke(
        app,
        ["chunks", "--tile", f"{Z}/{X0}/{Y0}", "--controls-only", "--dem", str(dem), "--osm-layers", "", *common],
    )
    assert run.exit_code == 0, run.output
    assert (style / chunk_job_id("antique", Z, X0, Y0) / "job.json").is_file()

    both = runner.invoke(app, ["chunks", "--tile", "1/0/0", "--bbox", "0,0,1,1", "--plan", *common])
    assert both.exit_code == 2 and "exactly one of" in both.output
    stray = runner.invoke(app, ["chunks", "--tile", "1/0/0", "--zoom", "3", "--plan", *common])
    assert stray.exit_code == 2 and "--zoom goes with --bbox" in stray.output
    bad = runner.invoke(app, ["chunks", "--tile", "1/0/0", "--plan", "--osm-layers", "places", *common])
    assert bad.exit_code == 2 and "places" in bad.output


def test_block_inputs_are_fetched_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_atlas import blocks

    calls: list[str] = []
    bbox = mercator_to_bbox(*tile_bounds(Z, X0, Y0))

    def fake_dem(box, out, **kwargs):  # noqa: ANN001, ANN202
        calls.append("dem")
        assert kwargs["cover"] is True
        out.write_bytes(b"tif")
        out.with_suffix(".json").write_text(json.dumps({"bbox": box.as_text(), "snap": "outward"}))

    def fake_osm(box, out, layers, zoom, **kwargs):  # noqa: ANN001, ANN202
        calls.append("osm")
        out.mkdir(parents=True, exist_ok=True)
        manifest = {"bbox": box.as_text(), "zoom": zoom, "layers": {n: {"path": f"{n}.geojson"} for n in layers[1:]}}
        manifest["skipped"] = layers[:1]
        for n in layers[1:]:
            (out / f"{n}.geojson").write_text("{}")
        (out / "osm.json").write_text(json.dumps(manifest))
        return manifest

    monkeypatch.setattr("argus_atlas.dem.fetch_dem", fake_dem)
    monkeypatch.setattr("argus_atlas.osm.fetch_osm", fake_osm)
    for _ in range(2):
        assert blocks.block_dem(bbox, tmp_path, source="x") == tmp_path / "dem.tif"
        blocks.block_osm(bbox, tmp_path, ["water", "roads"], 14, url="x")
    assert calls == ["dem", "osm"]
    blocks.block_osm(bbox, tmp_path, ["water", "roads", "forest"], 14, url="x")  # a new layer: fetch again
    blocks.block_osm(bbox, tmp_path, ["water", "roads"], 14, url="x")  # the refetch kept the earlier layers
    blocks.block_osm(bbox, tmp_path, ["water", "forest"], 13, url="x")  # another zoom: fetch again
    assert calls == ["dem", "osm", "osm", "osm"]
    inner = mercator_to_bbox(*tile_bounds(Z + 1, 2 * X0, 2 * Y0))
    blocks.block_osm(inner, tmp_path, ["water", "forest"], 13, url="x")  # a box inside the last fetch: reused
    assert blocks.block_dem(inner, tmp_path, source="x") == tmp_path / "dem.tif"
    assert calls == ["dem", "osm", "osm", "osm"]
    (tmp_path / "osm" / "forest.geojson").unlink()
    blocks.block_osm(bbox, tmp_path, ["water", "forest"], 13, url="x")  # a layer file gone: fetch again
    assert calls == ["dem", "osm", "osm", "osm", "osm"]
