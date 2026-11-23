"""Streaming SHA256 of files — the identity the job sidecar pins.

The DEM, every control PNG, the checkpoint and LoRA, the painted PNG and the
GeoTIFF are all recorded by content hash, never by name alone. Hashing a
multi-GB checkpoint is slow, so :func:`sha256_cached` memoizes by
``(path, size, mtime)`` for the process (same scheme as argus-proof), one
entry per path and at most ``CACHE_MAX`` paths.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB

CACHE_MAX = 256  # paths remembered; the least recently used goes first

_cache: dict[str, tuple[int, int, str]] = {}  # resolved path -> (size, mtime_ns, digest)


def sha256_file(path: Path) -> str:
    """Lowercase-hex SHA256 of *path* (no caching)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_cached(path: Path) -> str:
    """SHA256 of *path*, memoized by ``(path, size, mtime)`` for the process (a changed file replaces its entry)."""
    resolved = path.resolve()
    st = resolved.stat()
    key = str(resolved)
    held = _cache.pop(key, None)
    if held is None or held[:2] != (st.st_size, st.st_mtime_ns):
        held = (st.st_size, st.st_mtime_ns, sha256_file(resolved))
    _cache[key] = held  # re-inserted last: the dict's order is the LRU order
    while len(_cache) > CACHE_MAX:
        del _cache[next(iter(_cache))]
    return held[2]
