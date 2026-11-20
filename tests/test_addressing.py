from __future__ import annotations

import json
import subprocess
import sys

from argus_atlas import addressing, chunks

# What a caller of the tile addressing must never pay for: a network client, GDAL, or the bake stack.
FORBIDDEN = (
    "urllib.request",
    "http.client",
    "osgeo",
    "structlog",
    "argus_atlas.chunks",
    "argus_atlas.controls",
    "argus_atlas.dem",
    "argus_atlas.gdal_ops",
    "argus_atlas.jobs",
    "argus_atlas.attribution",
)


def test_addressing_imports_no_network_client_gdal_or_bake_stack() -> None:
    """argus-realms' deterministic rules and the Pyodide arena import this module (smk762/argus-atlas#69)."""
    probe = "import json, sys; import argus_atlas.addressing; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True).stdout
    loaded = set(json.loads(out))
    assert not loaded & set(FORBIDDEN)
    assert {m for m in loaded if m.startswith("argus_atlas.")} <= {
        "argus_atlas._version",
        "argus_atlas.addressing",
        "argus_atlas.mercator",
        "argus_atlas.models",
    }


def test_chunks_reexports_the_addressing() -> None:
    for name in (
        "CHUNK_SIZES",
        "DEFAULT_CHUNK_SIZE",
        "DEFAULT_MARGIN",
        "MARGIN_MULTIPLE",
        "TILE_PX",
        "canvas_bbox",
        "chunk_bbox",
        "chunk_grids",
        "chunk_job_id",
        "chunk_native_zoom",
        "neighbour_tiles",
        "parse_tile",
        "validate_chunk",
    ):
        assert getattr(chunks, name) is getattr(addressing, name)
