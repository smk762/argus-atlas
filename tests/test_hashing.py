from __future__ import annotations

import os
from pathlib import Path

import pytest

from argus_atlas import hashing
from argus_atlas.hashing import sha256_bytes, sha256_cached


def test_the_cache_holds_one_entry_per_path_and_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "_cache", {})
    model = tmp_path / "model.safetensors"
    for n in range(5):  # the same path regenerated: a new size and mtime each time
        model.write_bytes(b"x" * (n + 1))
        os.utime(model, ns=(n * 10**9, n * 10**9))
        assert sha256_cached(model) == sha256_bytes(b"x" * (n + 1))
    assert len(hashing._cache) == 1

    monkeypatch.setattr(hashing, "CACHE_MAX", 3)
    for n in range(6):
        (tmp_path / f"{n}.bin").write_bytes(bytes([n]))
        sha256_cached(tmp_path / f"{n}.bin")
    assert len(hashing._cache) == 3 and str((tmp_path / "5.bin").resolve()) in hashing._cache


def test_a_cached_digest_is_not_rehashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "_cache", {})
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    first = sha256_cached(path)
    monkeypatch.setattr(hashing, "sha256_file", lambda _path: pytest.fail("hashed twice"))
    assert sha256_cached(path) == first == sha256_bytes(b"abc")
