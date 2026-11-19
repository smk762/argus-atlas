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
from argus_atlas.provenance import generator
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
    assert sidecar.bake.generator == generator()
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
    assert (tmp_path / "aoi-1-seed9").stat().st_mode & 0o777 == job_dir.stat().st_mode & 0o777, "not mkdtemp's 0700"
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
    assert payload["atlas_commit"] == generator().commit
    assert sidecar.bake.image.sha256 == sha256_file(job_dir / "fantasy.png")  # digest taken after embedding
    info = gdal_ops.gdalinfo_json(job_dir / "fantasy.tif")
    desc = info["metadata"][""]["TIFFTAG_IMAGEDESCRIPTION"]
    assert json.loads(desc)["job_id"] == sidecar.job_id
    assert info["metadata"][""]["TIFFTAG_SOFTWARE"] == "argus-atlas"


def test_failed_generation_keeps_previous_bake_consistent(job: tuple[Path, JobSidecar], models_dir: Path) -> None:
    """A backend that dies mid-paint never touches fantasy.png, so job.json still describes it."""
    job_dir, _ = job
    first = run_bake(job_dir, PARAMS, FakeBackend(), models_root=models_dir)

    class Partial(FakeBackend):
        def generate(self, request, out_png, progress=None):  # noqa: ANN001, ANN202
            out_png.write_bytes(b"\x89PNG half a canvas")
            raise BackendError("connection dropped")

    with pytest.raises(BackendError):
        run_bake(job_dir, PARAMS, Partial(), models_root=models_dir)
    after = load_sidecar(job_dir)
    assert after.bake == first.bake
    assert sha256_file(job_dir / "fantasy.png") == after.bake.image.sha256


def test_unexpected_failure_after_paint_invalidates_record(
    job: tuple[Path, JobSidecar], models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fantasy.png replaced, then a non-gate failure: the old bake/tiles claims are dropped, not kept stale."""
    job_dir, _ = job
    run_bake(job_dir, PARAMS, FakeBackend(), models_root=models_dir)

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("argus_atlas.bake.stamp", boom)
    events: list[ProgressEvent] = []
    with pytest.raises(OSError, match="disk full"):
        run_bake(
            job_dir,
            PARAMS.model_copy(update={"seed": 8}),
            FakeBackend(),
            models_root=models_dir,
            progress=events.append,
        )
    assert events[-1].type == "error" and "disk full" in events[-1].message
    after = load_sidecar(job_dir)
    assert after.bake is None and after.tiles is None


def test_copy_job_refuses_an_existing_job_or_itself(
    job: tuple[Path, JobSidecar], models_dir: Path, tmp_path: Path
) -> None:
    job_dir, _ = job
    baked = copy_job(job_dir, tmp_path / "taken")
    run_bake(tmp_path / "taken", PARAMS, FakeBackend(), models_root=models_dir)
    with pytest.raises(AtlasError, match="already exists"):
        copy_job(job_dir, tmp_path / "taken")
    assert load_sidecar(tmp_path / "taken").bake is not None and baked.job_id == "taken"
    with pytest.raises(AtlasError, match="onto itself"):
        copy_job(job_dir, job_dir)
    (tmp_path / "empty").mkdir()
    assert copy_job(job_dir, tmp_path / "empty").job_id == "empty"
    assert not list(tmp_path.glob(".*.part"))


def test_model_resolver_follows_a_symlink_the_operator_placed_under_the_root(tmp_path: Path) -> None:
    from argus_atlas.backends.base import make_dir_resolver

    store = tmp_path / "fast"
    store.mkdir()
    (store / "juggernaut.safetensors").write_bytes(b"weights")
    root = tmp_path / "models"
    (root / "checkpoints").mkdir(parents=True)
    (root / "checkpoints" / "juggernaut.safetensors").symlink_to(store / "juggernaut.safetensors")
    (root / "loras").symlink_to(store, target_is_directory=True)  # a split model tree
    resolve = make_dir_resolver(root)
    assert resolve("juggernaut.safetensors") == root / "checkpoints" / "juggernaut.safetensors"
    assert resolve("loras/juggernaut.safetensors") == root / "loras" / "juggernaut.safetensors"
    for escape in ("../fast/juggernaut.safetensors", str(store / "juggernaut.safetensors")):
        with pytest.raises(FileNotFoundError):
            resolve(escape)
