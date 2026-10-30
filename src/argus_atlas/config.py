"""Environment configuration — the ``ATLAS_*`` knobs and the path roots.

Every caller-supplied path that reaches the HTTP API is resolved under one of
these roots (:func:`resolve_under_root`), the same idea as argus-proof's export
root: a relative path is joined under the root, an absolute path must already
lie inside it, and anything that escapes is refused.
"""

from __future__ import annotations

import os
from pathlib import Path

from argus_atlas.models import AtlasError

ENV_JOBS_DIR = "ATLAS_JOBS_DIR"
ENV_DEM_DIR = "ATLAS_DEM_DIR"
ENV_ALLOW_BAKE = "ATLAS_ALLOW_BAKE"
ENV_BACKEND = "ATLAS_BACKEND"
ENV_COMFYUI_URL = "COMFYUI_BASE_URL"
ENV_MODELS_DIR = "ATLAS_MODELS_DIR"
ENV_WORKFLOW = "ATLAS_WORKFLOW"
ENV_CORS_ORIGINS = "ATLAS_CORS_ORIGINS"
ENV_ALLOW_OSM = "ATLAS_ALLOW_OSM"
ENV_OVERPASS_URL = "ATLAS_OVERPASS_URL"
ENV_RECIPES_DIR = "ATLAS_RECIPES_DIR"
ENV_DEM_SOURCE = "ATLAS_DEM_SOURCE"

DEFAULT_JOBS_DIR = "/data/out/atlas"
DEFAULT_DEM_DIR = "/data/dem"
DEFAULT_MODELS_DIR = "/data/models"
DEFAULT_RECIPES_DIR = "/data/recipes"
DEFAULT_BACKEND = "comfyui"
DEFAULT_COMFYUI_URL = "http://host.docker.internal:8188"
DEFAULT_CORS_ORIGINS = "http://localhost:3000"
DEFAULT_PORT = 8105

# Same spellings argus_cortex.server.env_flag accepts. Duplicated (four strings)
# rather than imported because cortex's server module needs starlette, which a
# CLI-only install does not have.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_path(name: str, default: str, override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    return Path(os.environ.get(name) or default).expanduser()


def jobs_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Where job sidecars, controls, GeoTIFFs and PMTiles live."""
    return _env_path(ENV_JOBS_DIR, DEFAULT_JOBS_DIR, override)


def dem_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """The only root under which the API may name a DEM (or land polygon)."""
    return _env_path(ENV_DEM_DIR, DEFAULT_DEM_DIR, override)


def models_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Checkpoints and LoRAs (ComfyUI layout: ``checkpoints/``, ``loras/``), read-only."""
    return _env_path(ENV_MODELS_DIR, DEFAULT_MODELS_DIR, override)


def recipes_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Where ``--recipe <name>`` finds ``<name>.json`` (a path to a recipe file works anywhere)."""
    return _env_path(ENV_RECIPES_DIR, DEFAULT_RECIPES_DIR, override)


def backend_name(override: str | None = None) -> str:
    return override or os.environ.get(ENV_BACKEND) or DEFAULT_BACKEND


def comfyui_url(override: str | None = None) -> str:
    return override or os.environ.get(ENV_COMFYUI_URL) or DEFAULT_COMFYUI_URL


def workflow_path(override: str | os.PathLike[str] | None = None) -> Path | None:
    """An operator workflow template, or ``None`` for the packaged starter."""
    if override is not None:
        return Path(override).expanduser()
    value = os.environ.get(ENV_WORKFLOW)
    return Path(value).expanduser() if value else None


def cors_origins(override: list[str] | None = None) -> list[str]:
    if override is not None:
        return override
    raw = os.environ.get(ENV_CORS_ORIGINS) or DEFAULT_CORS_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def bake_allowed(override: bool | None = None) -> bool:
    """Live baking is OFF unless ``ATLAS_ALLOW_BAKE`` is truthy (``1``/``true``/
    ``yes``/``on``) — the demo-safe posture forge and proof share. A set-but-
    unrecognised value counts as off."""
    if override is not None:
        return override
    return os.environ.get(ENV_ALLOW_BAKE, "").strip().lower() in _TRUTHY


def osm_allowed(override: bool | None = None) -> bool:
    """``POST /osm`` (the server fetching from Overpass) is OFF unless ``ATLAS_ALLOW_OSM``
    is truthy — same opt-in posture as baking; the CLI ``osm`` command needs no flag."""
    if override is not None:
        return override
    return os.environ.get(ENV_ALLOW_OSM, "").strip().lower() in _TRUTHY


def overpass_url(override: str | None = None) -> str:
    from argus_atlas.osm import DEFAULT_OVERPASS_URL

    return override or os.environ.get(ENV_OVERPASS_URL) or DEFAULT_OVERPASS_URL


def dem_source(override: str | None = None) -> str:
    """Where ``argus-atlas dem`` finds Copernicus GLO-30 tiles: the public bucket, a mirror, or a local dir."""
    from argus_atlas.dem import DEFAULT_DEM_SOURCE

    return override or os.environ.get(ENV_DEM_SOURCE) or DEFAULT_DEM_SOURCE


def resolve_under_root(root: Path, candidate: str, label: str = "path") -> Path:
    """Resolve *candidate* under *root*, refusing anything that escapes it.

    Relative paths are joined under the root; absolute paths are accepted only
    when they already lie inside it. Symlinks are resolved before the check, so
    a link out of the root is refused too.
    """
    if not candidate or "\x00" in candidate:
        raise AtlasError(f"invalid {label}")
    root_resolved = root.expanduser().resolve()
    given = Path(candidate).expanduser()
    full = (given if given.is_absolute() else root_resolved / given).resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise AtlasError(f"{label} {candidate!r} escapes the configured root {root}")
    return full
