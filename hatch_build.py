"""Build hook: record the git commit a build was made from in ``argus_atlas/_build_info.py``.

hatch-vcs writes ``_version.py`` without the commit (its hook never passes the SCM state on, so
``__commit_id__`` is always ``None``), and a tagged release's version has no ``+g<sha>`` either.
Sidecars record the commit (``JobSidecar.generator``) so a viewer can check its docs and its tiles
came from the same atlas build. Outside a git checkout (a wheel built from the sdist) the file the
sdist carries is kept; with neither, the commit is ``None``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILD_INFO = "src/argus_atlas/_build_info.py"


def git_commit(root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None


class BuildInfoHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        path = Path(self.root) / BUILD_INFO
        commit = git_commit(self.root)
        if commit is not None or not path.is_file():
            path.write_text(
                "# Written by hatch_build.py at build time; don't change, don't track in version control.\n"
                f"COMMIT: str | None = {commit!r}\n",
                encoding="utf-8",
            )
        build_data["artifacts"].append(f"/{BUILD_INFO}")
