from __future__ import annotations

import argus_atlas


def test_version_is_exposed() -> None:
    assert isinstance(argus_atlas.__version__, str)
    assert argus_atlas.__version__
