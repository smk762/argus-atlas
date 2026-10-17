"""The pluggable generation backend contract.

A backend takes one :class:`BakeRequest` — the canvas grid, the control PNGs and
the operator's knobs — and writes exactly one PNG of the canvas size. It knows
nothing about georeferencing: the sidecar fixes the geotransform before the
bake and :mod:`argus_atlas.georef` checks it after. ComfyUI ships first; the
interface is a :class:`~typing.Protocol` so another engine is a config change.
"""

from __future__ import annotations

import glob
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from argus_atlas.hashing import sha256_cached
from argus_atlas.models import AtlasError, BakeParams, Grid, ModelRef


class BackendError(AtlasError):
    """A generation failure: engine unreachable, bad workflow, missing model, wrong-size output."""


@dataclass
class BackendStatus:
    """What ``doctor`` / ``GET /doctor`` report about the configured engine."""

    name: str
    reachable: bool
    version: str | None = None
    detail: str | None = None


@dataclass
class BakeRequest:
    """One bake: the canvas, the controls on disk, and the generation knobs."""

    job_id: str
    grid: Grid
    params: BakeParams
    controls: dict[str, Path]  # kind -> PNG path (hillshade, slope, optional land)


@dataclass
class BakeOutput:
    """What the engine reports back once the PNG is on disk."""

    engine_version: str
    workflow_sha256: str | None = None
    detail: dict = field(default_factory=dict)


# Human-readable progress lines a backend may emit while it works.
StatusSink = Callable[[str], None]


@runtime_checkable
class BakeBackend(Protocol):
    """A generation engine: ``probe`` for doctor, ``generate`` for one canvas."""

    name: str

    def probe(self) -> BackendStatus: ...

    def generate(self, request: BakeRequest, out_png: Path, progress: StatusSink | None = None) -> BakeOutput:
        """Paint *request* and write the result to *out_png* (exactly the canvas size)."""
        ...


# Resolve a model name (as the engine loads it) to a local file for hashing.
ModelResolver = Callable[[str], Path]

# ComfyUI's model layout: the subdirs a bare filename is looked up under first.
_MODEL_SUBDIRS = ("checkpoints", "loras")


def make_dir_resolver(root: Path) -> ModelResolver:
    """A :data:`ModelResolver` over *root*: the name as a relative path, then
    under ``checkpoints/`` / ``loras/``, then a recursive search by basename."""
    root = root.expanduser()

    def resolve(name: str) -> Path:
        direct = root / name
        if direct.is_file():
            return direct
        for sub in _MODEL_SUBDIRS:
            candidate = root / sub / name
            if candidate.is_file():
                return candidate
        basename = glob.escape(Path(name).name)
        for candidate in sorted(root.rglob(basename)) if root.is_dir() else []:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"no model named {name!r} under {root}")

    return resolve


def hash_model(resolve: ModelResolver, name: str) -> ModelRef:
    """Pin *name* by SHA256 via *resolve*, or raise :class:`BackendError`."""
    try:
        path = resolve(name)
    except Exception as exc:  # the resolver signals "not found" however it likes
        raise BackendError(f"cannot hash model {name!r}: {exc}") from exc
    return ModelRef(name=name, sha256=sha256_cached(path))
