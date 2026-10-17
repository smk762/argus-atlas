"""Streaming SHA256 of files — the identity the job sidecar pins.

The DEM, every control PNG, the checkpoint and LoRA, the painted PNG and the
GeoTIFF are all recorded by content hash, never by name alone. Hashing a
multi-GB checkpoint is slow, so :func:`sha256_cached` memoizes by
``(path, size, mtime)`` for the process (same scheme as argus-proof).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB

_cache: dict[tuple[str, int, int], str] = {}


def sha256_file(path: Path) -> str:
    """Lowercase-hex SHA256 of *path* (no caching)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_cached(path: Path) -> str:
    """SHA256 of *path*, memoized by ``(path, size, mtime)`` for the process."""
    resolved = path.resolve()
    st = resolved.stat()
    key = (str(resolved), st.st_size, st.st_mtime_ns)
    if key not in _cache:
        _cache[key] = sha256_file(resolved)
    return _cache[key]
