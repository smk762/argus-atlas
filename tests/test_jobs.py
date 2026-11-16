from __future__ import annotations

import pytest

from argus_atlas.jobs import is_safe_id, new_job_id, require_safe_id
from argus_atlas.models import AtlasError


@pytest.mark.parametrize("good", ["site1", "a.b_c-d", "2026-10-31"])
def test_safe_ids(good: str) -> None:
    assert is_safe_id(good)
    assert require_safe_id(good) == good


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "-x", ".hidden", "a b", "a\x00", "site1\n", "x" * 129])
def test_unsafe_ids(bad: str) -> None:
    assert not is_safe_id(bad)
    with pytest.raises(AtlasError):
        require_safe_id(bad)


def test_new_job_ids_are_safe() -> None:
    assert is_safe_id(new_job_id())
