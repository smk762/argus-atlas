"""Job directories: ``<jobs root>/<job_id>/job.json`` plus its artifacts.

Job ids are validated against a strict filename charset so an id can never be
a path (no traversal surface, as in argus-proof); the HTTP API addresses every
artifact by ``(job_id, kind)`` and never resolves a client-supplied path.
"""

from __future__ import annotations

import contextlib
import fcntl
import secrets
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

from argus_cortex.fs import PathRefused, safe_name

from argus_atlas.models import COMPOSITE_KIND, SIDECAR_NAME, AtlasError, JobDigest, JobSidecar

# Fixed preview names; any other kind is looked up in the job's controls.
PREVIEW_FILES: dict[str, str] = {"fantasy": "fantasy.png"}
# Outputs of a bake/tiles run — everything else in a job dir is a control artifact copy_job carries over.
BAKE_OUTPUT_PREFIX = "fantasy"
# Timings of the stages copy_job carries over with the controls.
CONTROL_STAGES: frozenset[str] = frozenset({"controls", "seams"})
# flock'd around every job.json write, and by a read-modify-write that must not lose another run's save.
SIDECAR_LOCK = ".job.lock"


def is_safe_id(value: str) -> bool:
    """One path segment: starts alphanumeric, then letters, digits, ``.``, ``_`` or ``-`` (argus-cortex's
    :func:`~argus_cortex.fs.safe_name`, which matches the whole string, so a trailing newline is refused)."""
    try:
        safe_name(value)
    except PathRefused:
        return False
    return True


def require_safe_id(value: str, label: str = "job id") -> str:
    if not is_safe_id(value):
        raise AtlasError(f"invalid {label} {value!r}: use letters, digits, '.', '_' or '-' only")
    return value


def new_job_id() -> str:
    return f"atlas-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


@contextlib.contextmanager
def sidecar_lock(job_dir: Path) -> Iterator[None]:
    """Hold the job's ``job.json`` lock. Not re-entrant: inside it, save with ``locked=True``."""
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / SIDECAR_LOCK).open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save_sidecar(job_dir: Path, sidecar: JobSidecar, *, locked: bool = False) -> Path:
    """Write ``job.json`` atomically under :func:`sidecar_lock` (*locked*: the caller already holds it)."""
    if not locked:
        with sidecar_lock(job_dir):
            return save_sidecar(job_dir, sidecar, locked=True)
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / SIDECAR_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_sidecar(job_dir: Path) -> JobSidecar:
    """Read ``job.json``; :class:`FileNotFoundError` if absent, :class:`AtlasError` if invalid."""
    path = job_dir / SIDECAR_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {SIDECAR_NAME} in {job_dir}")
    try:
        return JobSidecar.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AtlasError(f"invalid {path}: {exc}") from exc


def digest(sidecar: JobSidecar) -> JobDigest:
    return JobDigest(
        job_id=sidecar.job_id,
        created_at=sidecar.created_at,
        status=sidecar.status,
        bbox=sidecar.bbox,
        width=sidecar.grid.width,
        height=sidecar.grid.height,
        resolution=sidecar.resolution,
        native_zoom=sidecar.native_zoom,
        has_land=sidecar.has_land,
        layers=sidecar.layers,
        has_composite=COMPOSITE_KIND in sidecar.controls,
        has_archive=sidecar.tiles is not None,
        has_labels=sidecar.labels is not None,
        chunk=sidecar.chunk.tile if sidecar.chunk else None,
        recipe=sidecar.recipe.recipe.name if sidecar.recipe else None,
    )


def copy_job(src: Path, dst: Path) -> JobSidecar:
    """Copy the controls of job *src* into a new job dir *dst* (bake variants
    without regenerating controls). The copy gets *dst*'s name as its id and no
    bake/tiles record."""
    sidecar = load_sidecar(src)
    job_id = require_safe_id(dst.name)
    if dst.resolve() == src.resolve():
        raise AtlasError(f"cannot copy job {src.name} onto itself")
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        # Never merge into another job: its bake/tiles record would be dropped while its outputs stay.
        raise AtlasError(f"{dst} already exists — pick a new name, or bake that job directly")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Copy into a hidden sibling and rename it into place, so a half-finished copy never looks like a job.
    # mkdir, not mkdtemp: mkdtemp makes it 0700, and the renamed job must keep the umask's mode.
    staging = dst.parent / f".{dst.name}.{secrets.token_hex(6)}.part"
    staging.mkdir()
    try:
        for source in src.iterdir():
            if (
                source.is_file()
                and source.name not in (SIDECAR_NAME, SIDECAR_LOCK)
                and not source.name.startswith(BAKE_OUTPUT_PREFIX)
            ):
                shutil.copy2(source, staging / source.name)
        fresh = _fresh_copy(sidecar, job_id)
        save_sidecar(staging, fresh)
        if dst.exists():
            dst.rmdir()  # empty (checked above); rename needs it gone
        staging.rename(dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return fresh


def _fresh_copy(sidecar: JobSidecar, job_id: str) -> JobSidecar:
    return sidecar.model_copy(
        update={
            "job_id": job_id,
            "bake": None,
            "tiles": None,
            "chunk": sidecar.chunk.model_copy(update={"image": None}) if sidecar.chunk else None,
            "timings": {k: v for k, v in sidecar.timings.items() if k in CONTROL_STAGES},
        }
    )


class JobStore:
    """All jobs under one root; ids only, never paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def dir(self, job_id: str) -> Path:
        return self.root / require_safe_id(job_id)

    def exists(self, job_id: str) -> bool:
        return (self.dir(job_id) / SIDECAR_NAME).is_file()

    def load(self, job_id: str) -> JobSidecar:
        return load_sidecar(self.dir(job_id))

    def save(self, sidecar: JobSidecar) -> Path:
        return save_sidecar(self.dir(sidecar.job_id), sidecar)

    def list(self) -> list[JobDigest]:
        """Digests of every readable job, newest first. Unreadable dirs are skipped."""
        if not self.root.is_dir():
            return []
        rows: list[JobDigest] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not is_safe_id(child.name):
                continue
            try:
                rows.append(digest(load_sidecar(child)))
            except (FileNotFoundError, AtlasError, OSError):
                continue
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return rows

    def preview_path(self, job_id: str, kind: str) -> Path | None:
        """The PNG for *kind* in job *job_id*, or ``None`` if it does not exist."""
        name = PREVIEW_FILES.get(kind)
        if name is None:
            ref = self.load(job_id).controls.get(kind)
            if ref is None:
                raise AtlasError(f"unknown preview kind {kind!r}: not a control of job {job_id!r}")
            name = Path(ref.path).name
        path = self.dir(job_id) / name
        return path if path.is_file() else None

    def archive_path(self, job_id: str) -> Path | None:
        sidecar = self.load(job_id)
        if sidecar.tiles is None:
            return None
        path = self.dir(job_id) / Path(sidecar.tiles.archive.path).name
        return path if path.is_file() else None

    def labels_path(self, job_id: str) -> Path | None:
        sidecar = self.load(job_id)
        if sidecar.labels is None:
            return None
        path = self.dir(job_id) / Path(sidecar.labels.file.path).name
        return path if path.is_file() else None
