"""Relief scaling: the Web Mercator correction, vertical exaggeration, and where they are recorded."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from argus_atlas import gdal_ops
from argus_atlas.chunks import find_neighbours, run_chunk
from argus_atlas.cli import app
from argus_atlas.controls import build_controls, resolve_relief
from argus_atlas.hashing import sha256_bytes
from argus_atlas.jobs import load_sidecar, save_sidecar
from argus_atlas.mercator import lonlat_to_mercator, scale_factor
from argus_atlas.models import AtlasError, Grid, Recipe, ReliefRecord, relief_key, relief_key_for
from argus_atlas.mosaic import MosaicError, style_chunks
from argus_atlas.recipes import recipe_digest, recipe_record, with_relief
from argus_atlas.server.app import create_app
from tests.conftest import TEST_BBOX, requires_gdal
from tests.fakebackend import FakeBackend

runner = CliRunner()


def _grid_at(lat: float, size: int = 256, res: float = 30.0) -> Grid:
    """A canvas centred on (0°, *lat*)."""
    cx, cy = lonlat_to_mercator(0.0, lat)
    half = size * res / 2
    return Grid(width=size, height=size, ulx=cx - half, uly=cy + half, pixel_width=res, pixel_height=-res)


@pytest.mark.parametrize(("lat", "expected"), [(0.0, 1.0), (-41.0, 1.3250), (65.0, 2.3662)])
def test_auto_corrects_for_mercator_at_the_canvas_centre(lat: float, expected: float) -> None:
    relief = resolve_relief(_grid_at(lat))
    assert relief.mode == "auto"
    assert relief.latitude == pytest.approx(lat, abs=1e-6)
    assert relief.base == pytest.approx(expected, abs=1e-4)
    assert relief.base == pytest.approx(1 / math.cos(math.radians(lat)))
    assert relief.z_factor == relief.base and relief.vertical_exaggeration == 1.0


def test_exaggeration_multiplies_the_correction_and_fixed_factors() -> None:
    grid = _grid_at(-41.0)
    ve = resolve_relief(grid, "auto", 1.5)
    assert ve.z_factor == pytest.approx(scale_factor(-41.0) * 1.5)
    off = resolve_relief(grid, 1.0)
    assert (off.mode, off.base, off.z_factor) == ("fixed", 1.0, 1.0)
    fixed = resolve_relief(grid, 2.0, 1.5)
    assert (fixed.mode, fixed.z_factor) == ("fixed", 3.0)
    for bad in ({"z_factor": 0.0}, {"z_factor": -1.0}, {"vertical_exaggeration": 0.0}, {"z_factor": "flat"}):
        with pytest.raises(AtlasError):
            resolve_relief(grid, **bad)


def test_gdaldem_argv_scale_heights_and_leave_z_1_as_before() -> None:
    src, dst = Path("dem.tif"), Path("out.tif")
    assert "-z" not in gdal_ops.hillshade_argv(src, dst) and "-s" not in gdal_ops.slope_argv(src, dst)
    hill = gdal_ops.hillshade_argv(src, dst, z_factor=2.0)
    assert hill[hill.index("-z") + 1] == "2.0"
    slope = gdal_ops.slope_argv(src, dst, z_factor=2.0)
    assert slope[slope.index("-s") + 1] == "0.5", "slope has no -z: heights x z is distances / z"


def test_relief_is_in_the_recipe_digest() -> None:
    auto = Recipe(name="antique", checkpoint="c")
    assert auto.z_factor == "auto" and auto.vertical_exaggeration == 1.0
    legacy = with_relief(auto, 1.0)
    exaggerated = with_relief(auto, vertical_exaggeration=1.5)
    digests = {recipe_digest(r) for r in (auto, legacy, exaggerated, with_relief(auto, 2.0))}
    assert len(digests) == 4, "every relief setting is its own recipe"
    # z_factor 1 (no correction, no exaggeration) keeps the digest chunks baked before 0.3.0 carry.
    before = {k: v for k, v in legacy.model_dump(mode="json").items() if k not in {"z_factor", "vertical_exaggeration"}}
    assert recipe_digest(legacy) == sha256_bytes(json.dumps(before, sort_keys=True, separators=(",", ":")).encode())
    assert with_relief(auto) is auto
    with pytest.raises(AtlasError):
        with_relief(auto, vertical_exaggeration=-1.0)
    assert Recipe.model_validate_json('{"name": "a", "checkpoint": "c", "z_factor": 1}').z_factor == 1.0
    with pytest.raises(ValueError):
        Recipe(name="a", checkpoint="c", z_factor=0)


def test_relief_keys() -> None:
    assert relief_key(None) == ("fixed", 1.0) == relief_key_for(1.0, 1.0)
    auto = ReliefRecord(mode="auto", latitude=-41.0, base=1.325, vertical_exaggeration=1.5, z_factor=1.9875)
    assert relief_key(auto) == relief_key_for("auto", 1.5) == ("auto", 1.5), "the base follows each chunk's latitude"
    assert relief_key_for(2.0, 1.5) == ("fixed", 3.0)


def test_cli_rejects_a_bad_z_factor(tmp_path: Path) -> None:
    args = ["controls", "--dem", str(tmp_path / "x.tif"), "--bbox", TEST_BBOX.as_text(), "--out", str(tmp_path / "j")]
    result = runner.invoke(app, [*args, "--z-factor", "steep"])
    assert result.exit_code == 2 and "--z-factor" in result.output
    assert runner.invoke(app, [*args, "--ve", "0"]).exit_code == 2
    commands = typer.main.get_command(app).commands  # type: ignore[attr-defined]
    for name in ("controls", "chunk", "chunks"):
        flags = {o for p in commands[name].params for o in p.opts}
        assert {"--z-factor", "--vertical-exaggeration"} <= flags, name


@requires_gdal
def test_controls_record_the_relief_and_steepen_the_slope(dem: Path, tmp_path: Path) -> None:
    flat = build_controls(dem, TEST_BBOX, tmp_path / "flat", long_side=256, z_factor=1.0)
    auto = build_controls(dem, TEST_BBOX, tmp_path / "auto", long_side=256)
    assert flat.relief is not None and flat.relief.z_factor == 1.0
    assert auto.relief is not None and auto.relief.mode == "auto"
    assert auto.relief.z_factor == pytest.approx(scale_factor(auto.relief.latitude))
    assert 45.0 < auto.relief.latitude < 45.1
    assert load_sidecar(tmp_path / "auto").relief == auto.relief
    hill = next(c for c in auto.commands if c.argv[:2] == ["gdaldem", "hillshade"])
    assert float(hill.argv[hill.argv.index("-z") + 1]) == pytest.approx(auto.relief.z_factor)
    assert flat.controls["hillshade"].sha256 != auto.controls["hillshade"].sha256

    def max_slope(job: str) -> float:
        info = gdal_ops.gdalinfo_json(tmp_path / job / "slope.tif", stats=True)
        for aux in (tmp_path / job).glob("slope.tif.aux.xml"):
            aux.unlink()
        return info["bands"][0]["maximum"]

    assert max_slope("auto") > max_slope("flat")


@requires_gdal
def test_cli_controls_take_the_exaggeration(dem: Path, tmp_path: Path) -> None:
    job_dir = tmp_path / "ve"
    result = runner.invoke(
        app,
        ["controls", "--dem", str(dem), "--bbox", TEST_BBOX.as_text(), "--out", str(job_dir), "--long-side", "256"]
        + ["--z-factor", "off", "--vertical-exaggeration", "1.5"],
    )
    assert result.exit_code == 0, result.output
    relief = load_sidecar(job_dir).relief
    assert relief is not None and (relief.mode, relief.z_factor) == ("fixed", 1.5)
    assert "relief    z 1.500" in result.output


@requires_gdal
def test_api_controls_take_the_relief(dem: Path, tmp_path: Path) -> None:
    roots = {k: tmp_path / k for k in ("jobs", "dem")}
    for p in roots.values():
        p.mkdir()
    shutil.copy(dem, roots["dem"] / "dem.tif")
    client = TestClient(create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"]))
    body = {"dem": "dem.tif", "bbox": TEST_BBOX.as_text(), "long_side": 256}
    assert client.post("/controls", json={**body, "z_factor": "steep"}).status_code == 422
    assert client.post("/controls", json={**body, "vertical_exaggeration": 0}).status_code == 422
    resp = client.post("/controls", json={**body, "job_id": "ve", "vertical_exaggeration": 2})
    assert resp.status_code == 200, resp.text
    relief = resp.json()["sidecar"]["relief"]
    assert relief["mode"] == "auto" and relief["z_factor"] == pytest.approx(2 * relief["base"])


@requires_gdal
def test_chunks_of_other_relief_are_not_neighbours_or_one_mosaic(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    a_tile, b_tile = "14/8650/5890", "14/8651/5890"
    auto = recipe_record(Recipe(name="antique", preset="antique", checkpoint="sd_xl_base_1.0.safetensors"))
    a = run_chunk(
        dem, a_tile, auto, tmp_path / "antique-14-8650-5890", backend=FakeBackend(), models_root=models_dir,
        size=256, margin=32,
    )  # fmt: skip
    assert a.relief is not None and a.relief.mode == "auto"
    legacy = recipe_record(with_relief(auto.recipe, 1.0))
    with pytest.raises(AtlasError, match="relief"):
        run_chunk(dem, b_tile, legacy, tmp_path / "antique-14-8651-5890", size=256, margin=32, controls_only=True)
    probe = a.chunk.model_copy(update={"x": 8651})  # type: ignore[union-attr]
    assert [n.tile for n in find_neighbours(tmp_path, "antique", probe, relief=("auto", 1.0))] == [a_tile]

    # A chunk from before 0.3.0 (no relief record) in the same style dir: the mosaic refuses the mix.
    old_dir = tmp_path / "antique-14-8651-5891"
    shutil.copytree(tmp_path / a.job_id, old_dir)
    old = load_sidecar(old_dir)
    assert old.chunk is not None
    save_sidecar(old_dir, old.model_copy(update={"job_id": old_dir.name, "relief": None}))
    with pytest.raises(MosaicError, match="different relief"):
        style_chunks(tmp_path)
