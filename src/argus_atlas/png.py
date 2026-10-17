"""Minimal PNG helpers (stdlib only): read a canvas size, write a test image.

The alignment gate needs the model output's pixel size *before* a GeoTIFF is
written, and the fake backends used in tests need to produce a real PNG — both
without pulling Pillow into the runtime dependencies.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from argus_atlas.models import AtlasError

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(source: Path | bytes) -> tuple[int, int]:
    """``(width, height)`` from a PNG header, or :class:`AtlasError` if it isn't one."""
    head = source if isinstance(source, bytes) else _read_head(source)
    if len(head) < 24 or head[:8] != _SIGNATURE or head[12:16] != b"IHDR":
        raise AtlasError(f"not a PNG: {source if isinstance(source, Path) else '<bytes>'}")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _read_head(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(24)
    except OSError as exc:
        raise AtlasError(f"cannot read {path}: {exc}") from exc


def write_png(path: Path, width: int, height: int, *, rgb: tuple[int, int, int] | None = None) -> Path:
    """Write an 8-bit RGB PNG of ``width x height``: a smooth gradient, or a flat
    colour when *rgb* is given. Pure Python, so only for small test canvases."""

    def _chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        if rgb is not None:
            rows.extend(bytes(rgb) * width)
        else:
            gy = (y * 255) // max(1, height - 1)
            for x in range(width):
                gx = (x * 255) // max(1, width - 1)
                rows.extend((gx, gy, (gx + gy) // 2))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + _chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path
