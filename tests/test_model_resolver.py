from __future__ import annotations

from pathlib import Path

import pytest

from argus_atlas.backends.base import make_dir_resolver


@pytest.mark.parametrize("name", [".", "./", "loras/."])
def test_a_name_with_no_basename_is_file_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    (tmp_path / "loras").mkdir()
    real_rglob = Path.rglob

    def rglob(self: Path, pattern: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not pattern:  # what Python 3.13 does; 3.12 silently matches the directory itself
            raise ValueError(f"Unacceptable pattern: {pattern!r}")
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", rglob)
    with pytest.raises(FileNotFoundError):
        make_dir_resolver(tmp_path)(name)
