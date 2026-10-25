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

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas.backends.base import BackendError, BakeBackend, BakeRequest, hash_model, make_dir_resolver
from argus_atlas.georef import FANTASY_PNG, FANTASY_TIF, AlignmentError, stamp
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import CONTROL_STAGES, load_sidecar, save_sidecar
from argus_atlas.metadata import bake_payload, embed_png, payload_json
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
    # A new bake replaces the timings of the last one; the control stages stay.
    timings = {k: v for k, v in sidecar.timings.items() if k in CONTROL_STAGES}
    clock = [time.perf_counter()]

    def lap(stage: str) -> None:
        now = time.perf_counter()
        timings[stage] = round(now - clock[0], 3)
        clock[0] = now

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
        lap("verify")

        emit("generate", f"painting {sidecar.grid.width}x{sidecar.grid.height} through {backend.name}")
        png = job_dir / FANTASY_PNG
        output = backend.generate(
            BakeRequest(job_id=job_id, grid=sidecar.grid, params=params, controls=controls),
            png,
            progress=lambda msg: emit("generate", msg),
        )
        payload = bake_payload(
            sidecar,
            params,
            checkpoint=checkpoint,
            lora=lora,
            backend=backend.name,
            engine_version=output.engine_version,
            workflow_sha256=output.workflow_sha256,
        )
        embed_png(png, payload)  # metadata only; pixels untouched, the gate sees the same image
        image_ref = FileRef(path=png.name, sha256=sha256_file(png))
        lap("generate")

        emit("georeference", "stamping the sidecar geotransform and checking alignment")
        tif = job_dir / FANTASY_TIF
        try:
            stamp(png, tif, sidecar.grid, description=payload_json(payload))
        except AlignmentError as exc:
            lap("georeference")
            sidecar.timings = timings
            if sidecar.chunk is not None:
                sidecar.chunk = sidecar.chunk.model_copy(update={"image": None})
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
        lap("georeference")
        sidecar.timings = timings
        if sidecar.chunk is not None:
            sidecar.chunk = sidecar.chunk.model_copy(update={"image": None})
        save_sidecar(job_dir, sidecar)
        if sidecar.chunk is not None:
            from argus_atlas.chunks import crop_chunk

            emit("georeference", f"cropping the {sidecar.chunk.margin} px margin off chunk {sidecar.chunk.tile}")
            image, command = crop_chunk(job_dir, sidecar)
            sidecar.chunk = sidecar.chunk.model_copy(update={"image": image})
            sidecar.commands = [*sidecar.commands, command]
            lap("crop")
            sidecar.timings = timings
            save_sidecar(job_dir, sidecar)
        emit("done", f"{tif.name} aligned to the canvas; run tiles to cut the archive")
        logger.info("bake.done", job_id=job_id)
        return sidecar
    except (AtlasError, BackendError) as exc:
        emit("error", str(exc))
        logger.warning("bake.failed", job_id=job_id, error=str(exc))
        raise
