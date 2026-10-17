"""One bake, end to end: verify controls, paint, stamp, gate, record.

:func:`run_bake` is the shared path under ``argus-atlas bake`` and
``POST /bake/stream``. It emits :class:`~argus_atlas.models.ProgressEvent`
frames in order — ``controls``, ``generate``, ``georeference``, then ``done``
or ``error`` — and leaves the job sidecar describing exactly what happened,
including a failed alignment gate (PNG kept, no GeoTIFF).

The ``ATLAS_ALLOW_BAKE`` policy is enforced by the callers (CLI, server), not
here: this module assumes the operator already opted in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas.backends.base import BackendError, BakeBackend, BakeRequest, hash_model, make_dir_resolver
from argus_atlas.georef import FANTASY_PNG, FANTASY_TIF, AlignmentError, stamp
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import load_sidecar, save_sidecar
from argus_atlas.models import AtlasError, BakeParams, BakeRecord, FileRef, JobSidecar, ProgressEvent

logger = structlog.get_logger()

ProgressSink = Callable[[ProgressEvent], None]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def verify_controls(job_dir: Path, sidecar: JobSidecar) -> dict[str, Path]:
    """The control PNGs on disk, checked against the sidecar's digests."""
    controls: dict[str, Path] = {}
    for kind, ref in sidecar.controls.items():
        path = job_dir / ref.path
        if not path.is_file():
            raise AtlasError(f"control {kind} missing: {path}")
        if sha256_file(path) != ref.sha256:
            raise AtlasError(f"control {kind} ({path.name}) has changed since job.json was written — rerun controls")
        controls[kind] = path
    if "hillshade" not in controls:
        raise AtlasError("job has no hillshade control")
    return controls


def run_bake(
    job_dir: Path,
    params: BakeParams,
    backend: BakeBackend,
    *,
    models_root: Path,
    progress: ProgressSink | None = None,
) -> JobSidecar:
    """Bake the job in *job_dir* with *params* through *backend*; return the updated sidecar.

    Raises :class:`AtlasError` (a :class:`BackendError` or :class:`AlignmentError`
    subclass where applicable) after emitting an ``error`` frame.
    """
    sidecar = load_sidecar(job_dir)
    job_id = sidecar.job_id

    def emit(kind: str, message: str) -> None:
        if progress is not None:
            progress(ProgressEvent(job_id=job_id, type=kind, message=message))  # type: ignore[arg-type]

    try:
        emit("controls", f"verifying {len(sidecar.controls)} control(s) against job.json")
        controls = verify_controls(job_dir, sidecar)
        resolve = make_dir_resolver(models_root)
        checkpoint = hash_model(resolve, params.checkpoint)
        lora = hash_model(resolve, params.lora) if params.lora else None
        emit("controls", f"controls verified ({', '.join(sorted(controls))}); models pinned")

        emit("generate", f"painting {sidecar.grid.width}x{sidecar.grid.height} through {backend.name}")
        png = job_dir / FANTASY_PNG
        output = backend.generate(
            BakeRequest(job_id=job_id, grid=sidecar.grid, params=params, controls=controls),
            png,
            progress=lambda msg: emit("generate", msg),
        )
        image_ref = FileRef(path=png.name, sha256=sha256_file(png))

        emit("georeference", "stamping the sidecar geotransform and checking alignment")
        tif = job_dir / FANTASY_TIF
        try:
            stamp(png, tif, sidecar.grid)
        except AlignmentError as exc:
            sidecar.bake = BakeRecord(
                params=params,
                backend=backend.name,
                engine_version=output.engine_version,
                checkpoint=checkpoint,
                lora=lora,
                workflow_sha256=output.workflow_sha256,
                image=image_ref,
                geotiff=None,
                aligned=False,
                error=str(exc),
                completed_at=_now(),
            )
            sidecar.tiles = None
            save_sidecar(job_dir, sidecar)
            raise

        sidecar.bake = BakeRecord(
            params=params,
            backend=backend.name,
            engine_version=output.engine_version,
            checkpoint=checkpoint,
            lora=lora,
            workflow_sha256=output.workflow_sha256,
            image=image_ref,
            geotiff=FileRef(path=tif.name, sha256=sha256_file(tif)),
            aligned=True,
            completed_at=_now(),
        )
        sidecar.tiles = None  # a new bake invalidates any earlier archive
        save_sidecar(job_dir, sidecar)
        emit("done", f"{tif.name} aligned to the canvas; run tiles to cut the archive")
        logger.info("bake.done", job_id=job_id)
        return sidecar
    except (AtlasError, BackendError) as exc:
        emit("error", str(exc))
        logger.warning("bake.failed", job_id=job_id, error=str(exc))
        raise
