from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_atlas import mosaic as mosaic_mod
from argus_atlas.chunks import chunk_job_id, parse_tile, run_chunk
from argus_atlas.cli import app
from argus_atlas.gdal_ops import gdalinfo_json
from argus_atlas.jobs import load_sidecar, save_sidecar
from argus_atlas.labels import LABELS_NAME
from argus_atlas.mercator import tiles_covering
from argus_atlas.models import Recipe
from argus_atlas.mosaic import MOSAIC_MANIFEST, MosaicError, merge_archives, mosaic, read_archive, style_chunks
from argus_atlas.recipes import recipe_record
from tests.conftest import requires_gdal
from tests.fakebackend import FakeBackend

runner = CliRunner()

# Two side-by-side z14 tiles inside the synthetic DEM, as in test_chunks.
A, B = "14/8650/5890", "14/8651/5890"
SIZE, MARGIN = 256, 32
RECIPE = recipe_record(Recipe(name="antique", preset="antique", checkpoint="sd_xl_base_1.0.safetensors"))


def _bake(dem: Path, style: Path, tile: str, models_dir: Path) -> Path:
    job_dir = style / chunk_job_id("antique", *parse_tile(tile))
    run_chunk(dem, tile, RECIPE, job_dir, backend=FakeBackend(), models_root=models_dir, size=SIZE, margin=MARGIN)
    return job_dir


def _label(job_dir: Path, *names: str) -> None:
    features = [
        {"type": "Feature", "properties": {"name": n, "rank": 1, "kind": "town"},
         "geometry": {"type": "Point", "coordinates": [10.05, 45.05]}}
        for n in names
    ]  # fmt: skip
    (job_dir / LABELS_NAME).write_text(json.dumps({"type": "FeatureCollection", "features": features}))


@pytest.fixture
def style(dem: Path, tmp_path: Path, models_dir: Path) -> Path:
    root = tmp_path / "antique"
    for tile in (A, B):
        _bake(dem, root, tile, models_dir)
    (root / ".block").mkdir()  # inputs dir of `chunks`: never a chunk
    return root


def test_merge_refuses_a_tile_two_archives_disagree_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Fake:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id
            self.tiles = type("T", (), {"archive": type("F", (), {"path": "fantasy.pmtiles"})()})()

    archives = {"a": {(14, 1, 1): b"x", (14, 2, 1): b"y"}, "b": {(14, 2, 1): b"y"}, "c": {(14, 1, 1): b"z"}}
    monkeypatch.setattr(mosaic_mod, "read_archive", lambda path: archives[path.parent.name])
    merged = merge_archives([(tmp_path / "a", Fake("a")), (tmp_path / "b", Fake("b"))])  # type: ignore[list-item]
    assert merged == {(14, 1, 1): (b"x", "a"), (14, 2, 1): (b"y", "a")}  # the same bytes twice is not a conflict
    with pytest.raises(MosaicError, match="14/1/1 differs between a and c"):
        merge_archives([(tmp_path / "a", Fake("a")), (tmp_path / "c", Fake("c"))])  # type: ignore[list-item]


@requires_gdal
def test_mosaic_xyz_has_every_chunk_tile_overviews_and_one_label(style: Path, tmp_path: Path) -> None:
    jobs = [style / chunk_job_id("antique", *parse_tile(t)) for t in (A, B)]
    _label(jobs[0], "Here", "There")
    _label(jobs[1], "Here")
    assert [d.name for d, _ in style_chunks(style)] == [j.name for j in jobs]
    out = tmp_path / "xyz"
    manifest = mosaic(style, out, overview_min=10)

    chunk_tiles = {k for j in jobs for k in read_archive(j / "fantasy.pmtiles")}
    written = {(int(p.parts[-3]), int(p.parts[-2]), int(p.stem)) for p in out.glob("*/*/*.png")}
    assert chunk_tiles <= written and len(chunk_tiles) == 2, "two adjacent chunks, no duplicate tiles"
    for z in range(10, 14):
        x0, y0, x1, y1 = tiles_covering(mosaic_mod.union_grid(style_chunks(style)).bounds, z)
        want = {(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
        assert {t for t in written if t[0] == z} == want, f"zoom {z} overviews cover the chunks"
    overview = next(out.glob("13/*/*.png"))
    bands = gdalinfo_json(overview, stats=True)["bands"]
    assert bands[-1]["maximum"] == 255 and bands[0]["maximum"] > 0, "overviews carry the painted pixels"
    assert (manifest["min_zoom"], manifest["max_zoom"], manifest["chunk_min_zoom"]) == (10, 14, 14)
    assert manifest["n_tiles"] == len(written)
    assert [c["tile"] for c in manifest["chunks"]] == [A, B]
    labels = json.loads((out / LABELS_NAME).read_text())
    assert sorted(f["properties"]["name"] for f in labels["features"]) == ["Here", "There"]
    assert json.loads((out / MOSAIC_MANIFEST).read_text()) == manifest
    assert any(c["argv"][0] == "gdalbuildvrt" for c in manifest["commands"])

    mosaic(style, out, overview_min=12)  # an earlier mosaic is replaced, not added to
    assert not list(out.glob("10/*/*.png"))


@requires_gdal
def test_mosaic_pmtiles_and_cli(style: Path, tmp_path: Path) -> None:
    out = tmp_path / "antique.pmtiles"
    result = runner.invoke(app, ["mosaic", str(style), "--out", str(out), "--overview-min", "12"])
    assert result.exit_code == 0, result.output
    tiles = read_archive(out)
    assert {z for z, _, _ in tiles} == {12, 13, 14}
    manifest = json.loads(out.with_suffix(".json").read_text())
    assert manifest["format"] == "pmtiles" and manifest["n_tiles"] == len(tiles)
    assert (tmp_path / f"antique.{LABELS_NAME}").is_file()

    stranger = tmp_path / "busy"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("mine")
    refused = runner.invoke(app, ["mosaic", str(style), "--out", str(stranger)])
    assert refused.exit_code == 1 and "not empty" in refused.output
    empty = tmp_path / "empty-style"
    empty.mkdir()
    nothing = runner.invoke(app, ["mosaic", str(empty), "--out", str(tmp_path / "x")])
    assert nothing.exit_code == 1 and "no baked chunks" in nothing.output


@requires_gdal
def test_mosaic_refuses_a_second_recipe_a_changed_archive_and_a_file_out(style: Path, tmp_path: Path) -> None:
    job_a, job_b = (style / chunk_job_id("antique", *parse_tile(t)) for t in (A, B))
    flat = tmp_path / "flat"
    flat.write_text("mine")
    with pytest.raises(MosaicError, match="not a directory"):
        mosaic(style, flat)
    assert flat.read_text() == "mine"

    sidecar = load_sidecar(job_b)
    assert sidecar.recipe is not None and sidecar.bake is not None
    sidecar.recipe = sidecar.recipe.model_copy(
        update={"recipe": sidecar.recipe.recipe.model_copy(update={"name": "inked"})}
    )
    rebaked = sidecar.model_copy(
        update={"bake": sidecar.bake.model_copy(update={"params": sidecar.bake.params.model_copy()})}
    )
    save_sidecar(job_b, rebaked)
    assert [d for d, _ in style_chunks(style)] == [job_a], "a chunk re-baked with other params is skipped"
    rebaked.bake.params.recipe = "inked"
    save_sidecar(job_b, rebaked)
    with pytest.raises(MosaicError, match="more than one recipe"):
        style_chunks(style)

    with (job_a / "fantasy.pmtiles").open("ab") as f:
        f.write(b"\0")
    with pytest.raises(MosaicError, match="changed since its job.json"):
        style_chunks(style)
