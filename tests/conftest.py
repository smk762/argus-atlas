"""Shared fixtures: a synthetic DEM built with GDAL, a controls job, fake models.

GDAL is a system dependency. Tests that shell out to it are marked
``requires_gdal`` and *skip* when ``gdalinfo`` is absent — unless
``ATLAS_TEST_REQUIRE_GDAL=1`` (set in CI), in which case they run and fail
loudly, so a CI runner that lost its gdal-bin can't silently pass.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from argus_atlas.models import BBox, JobSidecar

HAS_GDAL = shutil.which("gdalinfo") is not None
REQUIRE_GDAL = os.environ.get("ATLAS_TEST_REQUIRE_GDAL") == "1"

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL and not REQUIRE_GDAL,
    reason="GDAL not installed (set ATLAS_TEST_REQUIRE_GDAL=1 to fail instead of skipping)",
)

# An AOI well inside the synthetic DEM (lon 10..10.2, lat 45..45.1).
TEST_BBOX = BBox(west=10.02, south=45.01, east=10.18, north=45.09)
# Small canvas so the GDAL round-trips stay fast.
TEST_LONG_SIDE = 256


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every ATLAS_* knob off so an operator shell can't flip the defaults under test."""
    for name in (
        "ATLAS_ALLOW_BAKE",
        "ATLAS_JOBS_DIR",
        "ATLAS_DEM_DIR",
        "ATLAS_MODELS_DIR",
        "ATLAS_WORKFLOW",
        "ATLAS_BACKEND",
        "ATLAS_CORS_ORIGINS",
        "COMFYUI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _skip_without_gdal() -> None:
    if not HAS_GDAL and not REQUIRE_GDAL:
        pytest.skip("GDAL not installed")


@pytest.fixture(scope="session")
def dem(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 128x64 Float32 GeoTIFF in EPSG:4326 with bumpy relief, built with GDAL
    from an ASCII grid (no rasterio/numpy needed)."""
    _skip_without_gdal()
    root = tmp_path_factory.mktemp("dem")
    ncols, nrows, cell = 128, 64, 0.0015625  # 0.2 x 0.1 degrees
    lines = [
        f"ncols {ncols}",
        f"nrows {nrows}",
        "xllcorner 10.0",
        "yllcorner 45.0",
        f"cellsize {cell}",
        "NODATA_value -9999",
    ]
    for r in range(nrows):
        row = []
        for c in range(ncols):
            z = 300 + 4 * c + 2 * r + 250 * math.sin(c / 9.0) * math.cos(r / 6.0)
            row.append(f"{z:.1f}")
        lines.append(" ".join(row))
    asc = root / "ramp.asc"
    asc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tif = root / "dem.tif"
    subprocess.run(
        ["gdal_translate", "-q", "-of", "GTiff", "-ot", "Float32", "-a_srs", "EPSG:4326", str(asc), str(tif)],
        check=True,
        capture_output=True,
    )
    return tif


@pytest.fixture(scope="session")
def land(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A GeoJSON land polygon covering the western half of TEST_BBOX."""
    root = tmp_path_factory.mktemp("land")
    b = TEST_BBOX
    mid = (b.west + b.east) / 2
    ring = [
        [b.west - 0.01, b.south - 0.01],
        [mid, b.south - 0.01],
        [mid, b.north + 0.01],
        [b.west - 0.01, b.north + 0.01],
    ]
    ring.append(ring[0])
    geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}],
    }
    path = root / "land.geojson"
    import json

    path.write_text(json.dumps(geojson), encoding="utf-8")
    return path


@pytest.fixture
def job(dem: Path, tmp_path: Path) -> tuple[Path, JobSidecar]:
    """A fresh controls job (hillshade + slope) in a temp dir."""
    from argus_atlas.controls import build_controls

    job_dir = tmp_path / "aoi-1"
    sidecar = build_controls(dem, TEST_BBOX, job_dir, long_side=TEST_LONG_SIDE)
    return job_dir, sidecar


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    """A ComfyUI-layout models tree with a tiny fake checkpoint and LoRA."""
    root = tmp_path / "models"
    (root / "checkpoints").mkdir(parents=True)
    (root / "loras").mkdir(parents=True)
    (root / "checkpoints" / "sd_xl_base_1.0.safetensors").write_bytes(b"checkpoint-bytes")
    (root / "loras" / "fantasy_map.safetensors").write_bytes(b"lora-bytes")
    return root
