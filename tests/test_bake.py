from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_atlas.backends.base import BackendError
from argus_atlas.bake import run_bake
from argus_atlas.georef import AlignmentError
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import copy_job, load_sidecar
from argus_atlas.models import AtlasError, BakeParams, JobSidecar, ProgressEvent
from tests.conftest import requires_gdal
from tests.fakebackend import FakeBackend

pytestmark = requires_gdal

PARAMS = BakeParams(checkpoint="sd_xl_base_1.0.safetensors", lora="fantasy_map.safetensors", seed=7)


def test_fake_bake_produces_aligned_geotiff(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    job_dir, _ = job
    events: list[ProgressEvent] = []
    sidecar = run_bake(job_dir, PARAMS, FakeBackend(), models_root=models_dir, progress=events.append)
    assert [e.type for e in events][0] == "controls"
    assert [e.type for e in events][-1] == "done"
    kinds = [e.type for e in events]
    assert kinds.index("controls") < kinds.index("generate") < kinds.index("georeference") < kinds.index("done")
    assert all(e.job_id == "aoi-1" and e.message for e in events)

    assert sidecar.status == "baked" and sidecar.bake is not None and sidecar.bake.aligned
    assert (job_dir / "fantasy.tif").is_file() and (job_dir / "fantasy.png").is_file()
    assert sidecar.bake.checkpoint.name == "sd_xl_base_1.0.safetensors" and len(sidecar.bake.checkpoint.sha256) == 64
    assert sidecar.bake.lora is not None and sidecar.bake.lora.name == "fantasy_map.safetensors"
    assert sidecar.bake.params.seed == 7 and sidecar.bake.backend == "fake"
    assert load_sidecar(job_dir) == sidecar


def test_off_by_one_fails_gate_keeps_png_records_failure(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    job_dir, _ = job
    events: list[ProgressEvent] = []
    with pytest.raises(AlignmentError):
        run_bake(job_dir, PARAMS, FakeBackend(offset=(0, 1)), models_root=models_dir, progress=events.append)
    assert [e.type for e in events][-1] == "error"
    assert (job_dir / "fantasy.png").is_file()
    assert not (job_dir / "fantasy.tif").exists()
    sidecar = load_sidecar(job_dir)
    assert sidecar.status == "unaligned"
    assert sidecar.bake is not None and not sidecar.bake.aligned and sidecar.bake.geotiff is None
    assert sidecar.bake.error and "canvas" in sidecar.bake.error


def test_missing_model_fails_before_generation(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    job_dir, _ = job
    backend = FakeBackend()
    params = PARAMS.model_copy(update={"checkpoint": "nope.safetensors"})
    with pytest.raises(BackendError, match="nope.safetensors"):
        run_bake(job_dir, params, backend, models_root=models_dir)
    assert backend.requests == [], "no generation should run when a model can't be pinned"
    assert load_sidecar(job_dir).bake is None


def test_backend_error_is_an_error_event(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    job_dir, _ = job
    events: list[ProgressEvent] = []
    with pytest.raises(BackendError, match="engine down"):
        run_bake(job_dir, PARAMS, FakeBackend(fail="engine down"), models_root=models_dir, progress=events.append)
    assert events[-1].type == "error" and "engine down" in events[-1].message


def test_tampered_control_is_refused(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    job_dir, _ = job
    (job_dir / "hillshade.png").write_bytes(b"not the control")
    with pytest.raises(AtlasError, match="changed since"):
        run_bake(job_dir, PARAMS, FakeBackend(), models_root=models_dir)


def test_copy_job_bakes_a_variant(job: tuple[Path, JobSidecar], models_dir: Path, tmp_path: Path) -> None:
    job_dir, _ = job
    variant = copy_job(job_dir, tmp_path / "aoi-1-seed9")
    assert variant.job_id == "aoi-1-seed9" and variant.bake is None
    assert (tmp_path / "aoi-1-seed9" / "hillshade.png").is_file()
    baked = run_bake(
        tmp_path / "aoi-1-seed9", PARAMS.model_copy(update={"seed": 9}), FakeBackend(), models_root=models_dir
    )
    assert baked.bake is not None and baked.bake.params.seed == 9
    assert load_sidecar(job_dir).bake is None, "the source job is untouched"


def test_bake_embeds_payload_in_png_and_geotiff(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    from argus_atlas import gdal_ops
    from argus_atlas.metadata import read_png_payload

    job_dir, _ = job
    params = BakeParams(checkpoint="sd_xl_base_1.0.safetensors", seed=11, preset="imhof", prompt="p", negative="n")
    sidecar = run_bake(job_dir, params, FakeBackend(), models_root=models_dir)
    payload = read_png_payload(job_dir / "fantasy.png")
    assert payload["params"]["preset"] == "imhof" and payload["params"]["seed"] == 11
    assert payload["checkpoint"]["sha256"] == sidecar.bake.checkpoint.sha256
    assert sidecar.bake.image.sha256 == sha256_file(job_dir / "fantasy.png")  # digest taken after embedding
    info = gdal_ops.gdalinfo_json(job_dir / "fantasy.tif")
    desc = info["metadata"][""]["TIFFTAG_IMAGEDESCRIPTION"]
    assert json.loads(desc)["job_id"] == sidecar.job_id
    assert info["metadata"][""]["TIFFTAG_SOFTWARE"] == "argus-atlas"
