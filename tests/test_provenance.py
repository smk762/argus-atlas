from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from argus_atlas import __version__, provenance
from argus_atlas.models import GeneratorRecord, JobSidecar

REPO = Path(provenance.__file__).resolve().parents[2]


def _git_head() -> str | None:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        return None
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def test_commit_from_version() -> None:
    assert provenance.commit_from_version("0.3.1.dev3+gf552f5dd1.d20261113") == "f552f5dd1"
    assert provenance.commit_from_version("0.3.1.dev3+gf552f5dd1") == "f552f5dd1"
    assert provenance.commit_from_version("0.3.1") is None
    assert provenance.commit_from_version("0.1.dev0+d20261017") is None


def test_generator_is_this_build() -> None:
    gen = provenance.generator()
    assert gen.package == "argus-atlas" and gen.version == __version__
    head = _git_head()
    if head is not None:  # a source checkout: the live commit, not the stale build-time one
        assert gen.commit == head


def test_built_commit_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(provenance, "_checkout_commit", lambda: None)
    monkeypatch.setitem(sys.modules, "argus_atlas._build_info", types.SimpleNamespace(COMMIT=sha))
    provenance.generator.cache_clear()
    try:
        assert provenance.generator().commit == sha
        monkeypatch.setitem(sys.modules, "argus_atlas._build_info", types.SimpleNamespace(COMMIT="not a sha"))
        provenance.generator.cache_clear()
        assert provenance.generator().commit == provenance.commit_from_version(__version__)
    finally:
        provenance.generator.cache_clear()


def test_hatch_hook_reads_git(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO))
    try:
        import hatch_build
    except ImportError:
        pytest.skip("hatchling not installed")
    finally:
        sys.path.remove(str(REPO))
    head = _git_head()
    assert hatch_build.git_commit(str(REPO)) == head
    assert hatch_build.git_commit(str(tmp_path)) is None  # not a checkout: None, never an error


def test_pre_04_sidecar_has_no_generator() -> None:
    raw = {
        "atlas_version": "0.3",
        "job_id": "old",
        "created_at": "2026-10-26T02:20:31+00:00",
        "dem": {"path": "/data/dem/dem.tif", "sha256": "a" * 64},
        "bbox": {"west": 10.0, "south": 45.0, "east": 10.1, "north": 45.05},
        "grid": {"width": 64, "height": 64, "ulx": 0.0, "uly": 0.0, "pixel_width": 1.0, "pixel_height": -1.0},
        "resolution": 1.0,
        "native_zoom": 11,
        "zoom_range": [9, 11],
        "controls": {},
    }
    assert JobSidecar.model_validate(raw).generator is None
    assert GeneratorRecord(version="0.4.0").commit is None
