from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from argus_atlas import gdal_ops
from argus_atlas.controls import build_controls
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import load_sidecar
from argus_atlas.models import AtlasError, JobSidecar
from argus_atlas.png import png_size
from tests.conftest import TEST_BBOX, TEST_LONG_SIDE, requires_gdal

pytestmark = requires_gdal


def test_controls_match_sidecar(job: tuple[Path, JobSidecar]) -> None:
    job_dir, sidecar = job
    assert sidecar.job_id == "aoi-1"
    assert set(sidecar.controls) == {"hillshade", "slope"}
    grid = sidecar.grid
    assert grid.long_side == TEST_LONG_SIDE and grid.width % 64 == 0 and grid.height % 64 == 0

    info = gdal_ops.gdalinfo_json(job_dir / "hillshade.tif")
    assert info["size"] == [grid.width, grid.height]
    for got, want in zip(info["geoTransform"], grid.geotransform, strict=True):
        assert math.isclose(got, want, abs_tol=1e-3)

    for kind, ref in sidecar.controls.items():
        png = job_dir / ref.path
        assert png_size(png) == (grid.width, grid.height), kind
        assert sha256_file(png) == ref.sha256
        assert not png.with_name(png.name + ".aux.xml").exists(), "model inputs must carry no georeferencing"

    # Sidecar round-trips and pins the DEM + the exact GDAL argv arrays.
    reloaded = load_sidecar(job_dir)
    assert reloaded == sidecar
    assert reloaded.dem.sha256 == sha256_file(Path(reloaded.dem.path))
    tools = [c.tool for c in reloaded.commands]
    assert tools[:3] == ["gdalwarp", "gdaldem", "gdaldem"]
    assert all(isinstance(c.argv, list) and c.argv for c in reloaded.commands)
    raw = json.loads((job_dir / "job.json").read_text())
    assert raw["grid"]["crs"] == "EPSG:3857" and raw["atlas_version"]
    assert reloaded.status == "controls"
    assert reloaded.zoom_range[1] == reloaded.native_zoom


def test_controls_with_land_mask(dem: Path, land: Path, tmp_path: Path) -> None:
    job_dir = tmp_path / "with-land"
    sidecar = build_controls(dem, TEST_BBOX, job_dir, land=land, long_side=TEST_LONG_SIDE)
    assert "land" in sidecar.controls and sidecar.land_source is not None
    land_png = job_dir / sidecar.controls["land"].path
    assert png_size(land_png) == (sidecar.grid.width, sidecar.grid.height)
    info = gdal_ops.gdalinfo_json(job_dir / "land_mask.tif")
    assert info["size"] == [sidecar.grid.width, sidecar.grid.height]
    # Western half burnt (255), eastern half empty (0): both values present.
    stats = gdal_ops.gdalinfo_json(job_dir / "land_mask.tif")
    assert stats["bands"][0]["type"] == "Byte"
    assert any(c.tool == "gdal_rasterize" for c in sidecar.commands)


def test_aoi_over_cap_fails_before_gdal_runs(dem: Path, tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="--bbox"):
        build_controls(dem, TEST_BBOX, tmp_path / "too-big", resolution=2.0)
    assert not (tmp_path / "too-big").exists()


def test_bad_inputs(dem: Path, tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="DEM not found"):
        build_controls(tmp_path / "missing.tif", TEST_BBOX, tmp_path / "x")
    with pytest.raises(AtlasError, match="invalid job id"):
        build_controls(dem, TEST_BBOX, tmp_path / "bad id!")
