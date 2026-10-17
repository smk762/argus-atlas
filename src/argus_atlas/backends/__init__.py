"""Generation backends: ``get_backend(name)`` selects by config, not code."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from argus_atlas import config
from argus_atlas.backends.base import (
    BackendError,
    BackendStatus,
    BakeBackend,
    BakeOutput,
    BakeRequest,
    ModelResolver,
    StatusSink,
    hash_model,
    make_dir_resolver,
)

__all__ = [
    "BackendError",
    "BackendStatus",
    "BakeBackend",
    "BakeOutput",
    "BakeRequest",
    "ModelResolver",
    "StatusSink",
    "backend_from_env",
    "get_backend",
    "hash_model",
    "make_dir_resolver",
]


def _comfyui(workflow: Path | None = None, base_url: str | None = None, **kwargs: object) -> BakeBackend:
    from argus_atlas.backends.comfyui import ComfyUIBackend
    from argus_atlas.backends.workflow import example_template, load_template

    path = config.workflow_path(workflow)
    template = load_template(path) if path is not None else example_template()
    return ComfyUIBackend(template, config.comfyui_url(base_url), **kwargs)  # type: ignore[arg-type]


_BACKENDS: dict[str, Callable[..., BakeBackend]] = {"comfyui": _comfyui}


def get_backend(name: str, **kwargs: object) -> BakeBackend:
    """Instantiate the backend registered as *name* (``comfyui``)."""
    try:
        factory = _BACKENDS[name]
    except KeyError as exc:
        raise BackendError(f"unknown backend {name!r}; available: {', '.join(sorted(_BACKENDS))}") from exc
    return factory(**kwargs)


def backend_from_env(workflow: Path | None = None, base_url: str | None = None) -> BakeBackend:
    """The backend ``$ATLAS_BACKEND`` names, configured from the environment."""
    return get_backend(config.backend_name(), workflow=workflow, base_url=base_url)
