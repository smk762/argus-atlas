from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from argus_atlas import gdal_ops
from argus_atlas.controls import build_controls
from argus_atlas.gdal_ops import run
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


def _value_counts(tif: Path, tmp: Path) -> dict[str, int]:
    """Pixel value histogram of a Byte raster via an XYZ dump (gdalinfo -stats skips NoData)."""
    xyz = tmp / (tif.stem + ".xyz")
    run(["gdal_translate", "-q", "-of", "XYZ", str(tif), str(xyz)])
    counts: dict[str, int] = {}
    for line in xyz.read_text().splitlines():
        v = line.split()[2]
        counts[v] = counts.get(v, 0) + 1
    return counts


def test_land_mask_really_burns_the_polygon(dem: Path, land: Path, tmp_path: Path) -> None:
    """The 4326 polygon must be reprojected before burning: about half the canvas is land."""
    job_dir = tmp_path / "land-burn"
    sidecar = build_controls(dem, TEST_BBOX, job_dir, land=land, long_side=TEST_LONG_SIDE)
    counts = _value_counts(job_dir / "land_mask.tif", tmp_path)
    total = sidecar.grid.width * sidecar.grid.height
    assert set(counts) == {"0", "255"}
    assert 0.35 < counts["255"] / total < 0.65
    assert sidecar.layer_sources["land"].path == str(land.resolve())
    assert b"tRNS" not in (job_dir / "land.png").read_bytes()  # no transparency chunk for the model
    assert "composite" not in sidecar.controls  # land alone paints nothing


def _geojson(path: Path, geometry: dict) -> Path:
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}
        )
    )
    return path


def test_layers_burn_masks_and_paint_composite(dem: Path, tmp_path: Path) -> None:
    b = TEST_BBOX
    mid_x, mid_y = (b.west + b.east) / 2, (b.south + b.north) / 2
    water = _geojson(
        tmp_path / "water.geojson",
        {
            "type": "Polygon",
            "coordinates": [[[b.west, b.south], [mid_x, b.south], [mid_x, mid_y], [b.west, mid_y], [b.west, b.south]]],
        },
    )
    roads = _geojson(
        tmp_path / "roads.geojson", {"type": "LineString", "coordinates": [[b.west, b.north], [b.east, b.south]]}
    )
    job_dir = tmp_path / "layers"
    sidecar = build_controls(dem, TEST_BBOX, job_dir, layers={"water": water, "roads": roads}, long_side=TEST_LONG_SIDE)
    assert set(sidecar.controls) == {"hillshade", "slope", "water", "roads", "composite"}
    assert sidecar.layers == ["roads", "water"]
    size = (sidecar.grid.width, sidecar.grid.height)
    for kind in ("water", "roads", "composite"):
        assert png_size(job_dir / sidecar.controls[kind].path) == size
    water_counts = _value_counts(job_dir / "water_mask.tif", tmp_path)
    assert 0.15 < water_counts["255"] / (size[0] * size[1]) < 0.35  # a quarter of the canvas
    roads_counts = _value_counts(job_dir / "roads_mask.tif", tmp_path)
    assert size[0] <= roads_counts["255"] < size[0] * 4  # a thin diagonal, all-touched
    info = gdal_ops.gdalinfo_json(job_dir / "composite.tif")
    assert len(info["bands"]) == 3 and info["size"] == list(size)
    assert all("noDataValue" not in band for band in info["bands"])
    tools = [c.tool for c in sidecar.commands]
    assert "ogr2ogr" in tools and tools.count("gdal_rasterize") == 2 and any(t.startswith("gdal_calc") for t in tools)
    # bakes copy every control, including the new ones
    from argus_atlas.jobs import copy_job

    copied = copy_job(job_dir, tmp_path / "layers-copy")
    assert set(copied.controls) == set(sidecar.controls)
    assert (tmp_path / "layers-copy" / "composite.png").is_file()


def test_bad_layer_names_are_refused(dem: Path, land: Path, tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="reserved"):
        build_controls(dem, TEST_BBOX, tmp_path / "x", layers={"hillshade": land}, long_side=TEST_LONG_SIDE)
    with pytest.raises(AtlasError, match="bad layer name"):
        build_controls(dem, TEST_BBOX, tmp_path / "y", layers={"Water Bodies": land}, long_side=TEST_LONG_SIDE)
    with pytest.raises(AtlasError, match="given twice"):
        build_controls(dem, TEST_BBOX, tmp_path / "z", land=land, layers={"land": land}, long_side=TEST_LONG_SIDE)


def test_sea_level_mask_from_dem(dem: Path, tmp_path: Path) -> None:
    """The test DEM ramps from ~300 m upward, so a sea level of 450 m floods a part of it."""
    job_dir = tmp_path / "sea"
    sidecar = build_controls(dem, TEST_BBOX, job_dir, sea_level=450.0, long_side=TEST_LONG_SIDE)
    assert "sea" in sidecar.controls and "composite" in sidecar.controls and sidecar.layers == []
    counts = _value_counts(job_dir / "sea_mask.tif", tmp_path)
    total = sidecar.grid.width * sidecar.grid.height
    assert set(counts) == {"0", "255"} and 0.05 < counts["255"] / total < 0.95
    assert any(c.tool.startswith("gdal_calc") for c in sidecar.commands)
