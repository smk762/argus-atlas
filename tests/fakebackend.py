"""A deterministic bake backend double: writes a PNG of the canvas size (or
deliberately off by *offset* pixels, to exercise the alignment gate)."""

from __future__ import annotations

from pathlib import Path

from argus_atlas.backends.base import BackendError, BackendStatus, BakeOutput, BakeRequest, StatusSink
from argus_atlas.png import write_png


class FakeBackend:
    name = "fake"

    def __init__(self, *, offset: tuple[int, int] = (0, 0), fail: str | None = None) -> None:
        self.offset = offset
        self.fail = fail
        self.requests: list[BakeRequest] = []

    def probe(self) -> BackendStatus:
        return BackendStatus(name=self.name, reachable=True, version="0.0")

    def generate(self, request: BakeRequest, out_png: Path, progress: StatusSink | None = None) -> BakeOutput:
        self.requests.append(request)
        if self.fail:
            raise BackendError(self.fail)
        width = request.grid.width + self.offset[0]
        height = request.grid.height + self.offset[1]
        write_png(out_png, width, height)
        if progress is not None:
            progress(f"fake painted {width}x{height}")
        return BakeOutput(engine_version="0.0", workflow_sha256="0" * 64)
