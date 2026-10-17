"""Job directories: ``<jobs root>/<job_id>/job.json`` plus its artifacts.

Job ids are validated against a strict filename charset so an id can never be
a path (no traversal surface, as in argus-proof); the HTTP API addresses every
artifact by ``(job_id, kind)`` and never resolves a client-supplied path.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from argus_atlas.models import SIDECAR_NAME, AtlasError, JobDigest, JobSidecar

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# preview kind -> filename inside the job dir
PREVIEW_FILES: dict[str, str] = {
    "hillshade": "hillshade.png",
    "slope": "slope.png",
    "land": "land.png",
    "fantasy": "fantasy.png",
}
# Files that constitute a reusable controls job (copied by copy_job).
CONTROL_ARTIFACTS: tuple[str, ...] = (
    SIDECAR_NAME,
    "dem_3857.tif",
    "hillshade.tif",
    "hillshade.png",
    "slope.tif",
    "slope.png",
    "land_mask.tif",
    "land.png",
)


def is_safe_id(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and _SAFE_ID.match(value) is not None


def require_safe_id(value: str, label: str = "job id") -> str:
    if not is_safe_id(value):
        raise AtlasError(f"invalid {label} {value!r}: use letters, digits, '.', '_' or '-' only")
    return value


def new_job_id() -> str:
    return f"atlas-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def save_sidecar(job_dir: Path, sidecar: JobSidecar) -> Path:
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
        has_archive=sidecar.tiles is not None,
    )


def copy_job(src: Path, dst: Path) -> JobSidecar:
    """Copy the controls of job *src* into a new job dir *dst* (bake variants
    without regenerating controls). The copy gets *dst*'s name as its id and no
    bake/tiles record."""
    sidecar = load_sidecar(src)
    job_id = require_safe_id(dst.name)
    dst.mkdir(parents=True, exist_ok=True)
    for name in CONTROL_ARTIFACTS:
        source = src / name
        if source.is_file() and name != SIDECAR_NAME:
            shutil.copy2(source, dst / name)
    fresh = sidecar.model_copy(update={"job_id": job_id, "bake": None, "tiles": None})
    save_sidecar(dst, fresh)
    return fresh


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
            raise AtlasError(f"unknown preview kind {kind!r}; choose one of {', '.join(PREVIEW_FILES)}")
        path = self.dir(job_id) / name
        return path if path.is_file() else None

    def archive_path(self, job_id: str) -> Path | None:
        sidecar = self.load(job_id)
        if sidecar.tiles is None:
            return None
        path = self.dir(job_id) / Path(sidecar.tiles.archive.path).name
        return path if path.is_file() else None
