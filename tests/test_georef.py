from __future__ import annotations

from pathlib import Path

import pytest

from argus_atlas import gdal_ops
from argus_atlas.georef import AlignmentError, epsg_from_info, stamp, verify_alignment
from argus_atlas.models import JobSidecar
from argus_atlas.png import write_png
from tests.conftest import requires_gdal

pytestmark = requires_gdal


def test_stamp_passes_gate_and_writes_epsg3857(job: tuple[Path, JobSidecar]) -> None:
    job_dir, sidecar = job
    png = write_png(job_dir / "fantasy.png", sidecar.grid.width, sidecar.grid.height)
    tif = job_dir / "fantasy.tif"
    command = stamp(png, tif, sidecar.grid)
    assert command.tool == "gdal_translate" and tif.is_file()
    info = verify_alignment(tif, sidecar.grid)
    assert info["size"] == [sidecar.grid.width, sidecar.grid.height]
    assert epsg_from_info(info) == 3857
    assert not list(job_dir.glob(".fantasy.tif.part*"))


def test_one_pixel_off_fails_gate_without_geotiff(job: tuple[Path, JobSidecar]) -> None:
    job_dir, sidecar = job
    png = write_png(job_dir / "fantasy.png", sidecar.grid.width + 1, sidecar.grid.height)
    with pytest.raises(AlignmentError, match="canvas is"):
        stamp(png, job_dir / "fantasy.tif", sidecar.grid)
    assert png.is_file(), "the PNG is kept for inspection"
    assert not (job_dir / "fantasy.tif").exists()
    assert not list(job_dir.glob(".fantasy.tif.part*"))


def test_verify_alignment_rejects_wrong_corners_and_srs(job: tuple[Path, JobSidecar], tmp_path: Path) -> None:
    job_dir, sidecar = job
    png = write_png(job_dir / "fantasy.png", sidecar.grid.width, sidecar.grid.height)
    g = sidecar.grid
    shifted = tmp_path / "shifted.tif"
    gdal_ops.run(
        [
            "gdal_translate",
            "-a_srs",
            "EPSG:3857",
            "-a_ullr",
            repr(g.ulx + 5.0),
            repr(g.uly),
            repr(g.lrx + 5.0),
            repr(g.lry),
            str(png),
            str(shifted),
        ]
    )
    with pytest.raises(AlignmentError, match="corners"):
        verify_alignment(shifted, g)

    wrong_srs = tmp_path / "wrong_srs.tif"
    gdal_ops.run(
        ["gdal_translate", "-a_srs", "EPSG:4326", "-a_ullr", "10", "46", "11", "45", str(png), str(wrong_srs)],
    )
    with pytest.raises(AlignmentError, match="EPSG:4326"):
        verify_alignment(wrong_srs, g)
