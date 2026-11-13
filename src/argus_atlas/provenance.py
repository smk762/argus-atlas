"""Which argus-atlas build wrote a job: package version + git commit (``JobSidecar.generator``).

The commit comes from, in order:

1. ``git rev-parse HEAD`` when the package is imported from a git checkout (an editable install,
   where the build-time record goes stale as soon as you commit);
2. ``_build_info.COMMIT``, written by ``hatch_build.py`` when the wheel was built;
3. the ``+g<sha>`` local part of a dev version (a short sha, better than nothing);

else ``None``. Realms checks this against its docs record, so a released wheel or image must carry it.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from argus_atlas import __version__
from argus_atlas.models import GeneratorRecord

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_LOCAL_SHA = re.compile(r"\+g([0-9a-f]{7,40})(?:\.|$)")


def _checkout_commit() -> str | None:
    root = Path(__file__).resolve().parents[2]  # src/argus_atlas/provenance.py -> checkout root
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if _SHA.match(sha) else None


def _built_commit() -> str | None:
    try:
        from argus_atlas._build_info import COMMIT  # type: ignore[import-not-found]
    except ImportError:
        return None
    return COMMIT if isinstance(COMMIT, str) and _SHA.match(COMMIT) else None


def commit_from_version(version: str) -> str | None:
    match = _LOCAL_SHA.search(version)
    return match.group(1) if match else None


@lru_cache(maxsize=1)
def generator() -> GeneratorRecord:
    commit = _checkout_commit() or _built_commit() or commit_from_version(__version__)
    return GeneratorRecord(version=__version__, commit=commit)
