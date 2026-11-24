"""Errors raised below the backends as well as by them. Imports nothing from :mod:`argus_atlas.backends`."""

from __future__ import annotations

from argus_atlas.models import AtlasError


class BackendError(AtlasError):
    """A generation failure: engine unreachable, bad workflow, missing model, wrong-size output."""
