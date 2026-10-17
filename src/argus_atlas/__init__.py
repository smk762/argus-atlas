"""argus-atlas — Bake georeferenced fantasy-map overlay tiles from real terrain: GDAL controls from a DEM, restyled through a cartography LoRA, stamped back to a GeoTIFF and PMTiles archive"""

from __future__ import annotations

try:
    # Written by hatch-vcs at build time (see pyproject [tool.hatch.build.hooks.vcs]).
    from argus_atlas._version import __version__
except ImportError:  # running from a source checkout that hasn't been built
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("argus-atlas")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
