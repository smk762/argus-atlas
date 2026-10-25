from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_atlas.cli import app
from argus_atlas.models import JobSidecar
from tests.conftest import HAS_GDAL, TEST_BBOX, requires_gdal

runner = CliRunner()


def test_bake_refuses_without_allow_flag(tmp_path: Path) -> None:
    result = runner.invoke(app, ["bake", str(tmp_path), "--checkpoint", "c.safetensors"])
    assert result.exit_code == 3
    assert "ATLAS_ALLOW_BAKE" in result.output


def test_doctor_json_reports_and_exits_on_missing_gdal() -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    out = result.output  # may carry the stderr "GDAL is not installed" line after the JSON
    report = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert "gdal" in report and "backend" in report and report["bake_allowed"] is False
    assert result.exit_code == (0 if HAS_GDAL else 1)


@requires_gdal
def test_controls_inspect_and_tiles_round_trip(
    dem: Path, tmp_path: Path, models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "cli-job"
    result = runner.invoke(
        app, ["controls", "--dem", str(dem), "--bbox", TEST_BBOX.as_text(), "--out", str(job_dir), "--long-side", "256"]
    )
    assert result.exit_code == 0, result.output
    assert (job_dir / "job.json").is_file() and "native zoom" in result.output

    shown = runner.invoke(app, ["inspect", str(job_dir)])
    assert shown.exit_code == 0 and "cli-job [controls]" in shown.output

    # tiles before a bake is a clean input error
    assert runner.invoke(app, ["tiles", str(job_dir)]).exit_code == 2

    # bake through the fake backend (in-process; the CLI's comfyui path is covered by test_comfyui)
    from argus_atlas.bake import run_bake
    from argus_atlas.models import BakeParams
    from tests.fakebackend import FakeBackend

    run_bake(
        job_dir, BakeParams(checkpoint="sd_xl_base_1.0.safetensors", seed=1), FakeBackend(), models_root=models_dir
    )

    tiled = runner.invoke(app, ["tiles", str(job_dir / "fantasy.tif"), "--out", str(job_dir / "out.pmtiles")])
    assert tiled.exit_code == 0, tiled.output
    assert (job_dir / "out.pmtiles").is_file()
    sidecar = JobSidecar.model_validate_json((job_dir / "job.json").read_text())
    assert sidecar.tiles is not None and sidecar.tiles.max_zoom == sidecar.native_zoom

    too_fine = runner.invoke(app, ["tiles", str(job_dir), "--max-zoom", str(sidecar.native_zoom + 1)])
    assert too_fine.exit_code == 1 and "native zoom" in too_fine.output


def test_controls_bad_bbox_is_input_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["controls", "--dem", str(tmp_path / "x.tif"), "--bbox", "1,2,3", "--out", str(tmp_path / "j")]
    )
    assert result.exit_code == 2 and "west,south,east,north" in result.output


def test_chunk_refuses_to_bake_without_allow_flag(tmp_path: Path) -> None:
    result = runner.invoke(app, ["chunk", "--tile", "14/1/1", "--recipe", "x", "--dem", str(tmp_path / "d.tif")])
    assert result.exit_code == 3 and "--controls-only" in result.output


def test_chunk_bad_tile_is_an_input_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["chunk", "--tile", "14/1", "--recipe", "x", "--dem", "d.tif", "--controls-only", "-o", str(tmp_path / "j")],
    )
    assert result.exit_code == 2 and "z/x/y" in result.output


@requires_gdal
def test_chunk_controls_only_with_a_named_recipe(dem: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "antique.json").write_text(json.dumps({"name": "antique", "preset": "antique", "checkpoint": "c"}))
    monkeypatch.setenv("ATLAS_RECIPES_DIR", str(recipes))
    monkeypatch.setenv("ATLAS_JOBS_DIR", str(tmp_path / "jobs"))
    result = runner.invoke(
        app,
        ["chunk", "--tile", "14/8650/5890", "--recipe", "antique", "--dem", str(dem), "--size", "256", "--margin", "32"]
        + ["--controls-only"],
    )
    assert result.exit_code == 0, result.output
    job_dir = tmp_path / "jobs" / "antique-14-8650-5890"
    shown = runner.invoke(app, ["inspect", str(job_dir)])
    assert "chunk:    14/8650/5890" in shown.output and "recipe:   antique" in shown.output
