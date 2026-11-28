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

import os
import time
from collections.abc import Callable
from pathlib import Path

import structlog
from argus_cortex.timestamps import utc_now_iso

from argus_atlas.backends.base import BakeBackend, BakeRequest, hash_model, make_dir_resolver
from argus_atlas.georef import FANTASY_PNG, FANTASY_TIF, AlignmentError, stamp
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import CONTROL_STAGES, load_sidecar, save_sidecar
from argus_atlas.metadata import bake_payload, embed_png, payload_json
from argus_atlas.models import (
    AtlasError,
    BakeParams,
    BakeRecord,
    FileRef,
    GdalCommand,
    JobSidecar,
    ProgressEvent,
)
from argus_atlas.provenance import generator

logger = structlog.get_logger()

ProgressSink = Callable[[ProgressEvent], None]

# The backend paints here; only a PNG that generated and took its metadata is
# moved onto ``fantasy.png``, so a failed bake never leaves job.json describing
# a half-written or foreign canvas.
FANTASY_PART = "fantasy.part.png"


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


def without_bake_commands(commands: list[GdalCommand]) -> list[GdalCommand]:
    """*commands* less a chunk bake's blend and crop: the ones reading or writing the painted canvas
    (``fantasy.tif``), its seam blend, the crop's temp file or the blend's ``.blend-*`` scratch dir.

    The control and seam stages never touch those (seams read the neighbours' published crops), so what is
    left is what the controls recorded, plus labels.
    """
    from argus_atlas.chunks import BLEND_TIF, CHUNK_TIF

    names = {FANTASY_TIF, BLEND_TIF, f".{CHUNK_TIF}.part"}

    def from_a_bake(command: GdalCommand) -> bool:
        return any(Path(arg).name in names or Path(arg).parent.name.startswith(".blend-") for arg in command.argv)

    return [c for c in commands if not from_a_bake(c)]


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
    if sidecar.recipe is not None and params.recipe != sidecar.recipe.recipe.name:
        # Painted from other params (`bake`, POST /bake/stream): not that recipe's chunk any more, so
        # neither job.json, the embedded payload nor find_neighbours may claim it is.
        sidecar.recipe = None
    # A new bake replaces the timings of the last one; the control stages stay. So do its commands: a chunk's
    # blend and crop are appended below, so the last bake's are dropped first.
    timings = {k: v for k, v in sidecar.timings.items() if k in CONTROL_STAGES}
    sidecar.commands = without_bake_commands(sidecar.commands)
    clock = [time.perf_counter()]

    def lap(stage: str) -> None:
        now = time.perf_counter()
        timings[stage] = round(now - clock[0], 3)
        clock[0] = now

    def emit(kind: str, message: str) -> None:
        if progress is not None:
            progress(ProgressEvent(job_id=job_id, type=kind, message=message))  # type: ignore[arg-type]

    painted = recorded = False
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
        part = job_dir / FANTASY_PART
        part.unlink(missing_ok=True)
        output = backend.generate(
            BakeRequest(job_id=job_id, grid=sidecar.grid, params=params, controls=controls),
            part,
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
        embed_png(part, payload)  # metadata only; pixels untouched, the gate sees the same image
        image_sha = sha256_file(part)
        os.replace(part, png)
        painted = True  # from here job.json's old bake no longer describes fantasy.png
        image_ref = FileRef(path=png.name, sha256=image_sha)
        lap("generate")

        emit("georeference", "stamping the sidecar geotransform and checking alignment")
        tif = job_dir / FANTASY_TIF
        try:
            stamp(png, tif, sidecar.grid, description=payload_json(payload))
        except AlignmentError as exc:
            lap("georeference")
            sidecar.timings = timings
            if sidecar.chunk is not None:
                sidecar.chunk = sidecar.chunk.model_copy(update={"image": None, "blend": None})
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
                completed_at=utc_now_iso(),
                generator=generator(),
            )
            sidecar.tiles = None
            save_sidecar(job_dir, sidecar)
            recorded = True
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
            completed_at=utc_now_iso(),
            generator=generator(),
        )
        sidecar.tiles = None  # a new bake invalidates any earlier archive
        lap("georeference")
        sidecar.timings = timings
        if sidecar.chunk is not None:
            sidecar.chunk = sidecar.chunk.model_copy(update={"image": None, "blend": None})
        save_sidecar(job_dir, sidecar)
        recorded = True
        if sidecar.chunk is not None:
            from argus_atlas.chunks import blend_chunk, crop_chunk

            try:
                if sidecar.chunk.neighbours:
                    emit(
                        "georeference",
                        f"matching chunk {sidecar.chunk.tile} to {len(sidecar.chunk.neighbours)} neighbour(s)",
                    )
                blend, blend_commands = blend_chunk(job_dir, sidecar, description=payload_json(payload))
                sidecar.chunk = sidecar.chunk.model_copy(update={"blend": blend})
                sidecar.commands = [*sidecar.commands, *blend_commands]
                if blend is not None:
                    lap("blend")
                emit("georeference", f"cropping the {sidecar.chunk.margin} px margin off chunk {sidecar.chunk.tile}")
                image, command = crop_chunk(job_dir, sidecar)
            except Exception as exc:
                # The canvas passed its gate but the tile did not: say so in job.json, not only in the stream.
                lap("crop")
                sidecar.timings = timings
                detail = str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}"
                sidecar.bake = sidecar.bake.model_copy(update={"error": f"crop failed: {detail}"})
                save_sidecar(job_dir, sidecar)
                raise
            sidecar.chunk = sidecar.chunk.model_copy(update={"image": image})
            sidecar.commands = [*sidecar.commands, command]
            lap("crop")
            sidecar.timings = timings
            save_sidecar(job_dir, sidecar)
        emit("done", f"{tif.name} aligned to the canvas; run tiles to cut the archive")
        logger.info("bake.done", job_id=job_id)
        return sidecar
    except Exception as exc:
        if painted and not recorded:
            _invalidate(job_dir, sidecar, timings)
        if not painted:
            (job_dir / FANTASY_PART).unlink(missing_ok=True)  # a half-painted canvas is nobody's
        message = str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}"
        emit("error", message)
        logger.warning("bake.failed", job_id=job_id, error=message)
        raise


def _invalidate(job_dir: Path, sidecar: JobSidecar, timings: dict[str, float]) -> None:
    """``fantasy.png`` was replaced but the bake failed before it was recorded:
    drop the old bake/tiles claims (and a reset recipe) instead of leaving them
    pointing at a canvas they no longer describe."""
    sidecar.bake = None
    sidecar.tiles = None
    sidecar.timings = timings
    if sidecar.chunk is not None:
        sidecar.chunk = sidecar.chunk.model_copy(update={"image": None, "blend": None})
    try:
        save_sidecar(job_dir, sidecar)
    except Exception as exc:  # noqa: BLE001 - the original failure is the one to report
        logger.warning("bake.invalidate_failed", job_id=sidecar.job_id, error=str(exc))
