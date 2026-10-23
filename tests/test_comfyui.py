from __future__ import annotations

import io
from pathlib import Path

import pytest

from argus_atlas.backends.base import BackendError, BakeRequest
from argus_atlas.backends.comfyui import ComfyUIBackend
from argus_atlas.backends.http import encode_multipart
from argus_atlas.backends.workflow import example_template
from argus_atlas.models import BakeParams, Grid
from argus_atlas.png import png_size, write_png

GRID = Grid(width=256, height=192, ulx=1114000.0, uly=5640000.0, pixel_width=70.0, pixel_height=-70.0)
PARAMS = BakeParams(checkpoint="sd_xl_base_1.0.safetensors", lora="fantasy_map.safetensors", seed=3)


class FakeTransport:
    """Scripted ComfyUI: records calls, returns a PNG of *size* from /view."""

    def __init__(self, size: tuple[int, int] = (256, 192), node_errors: dict | None = None) -> None:
        self.size = size
        self.node_errors = node_errors or {}
        self.posts: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, dict[str, str]]] = []
        self.history_polls = 0

    def post_json(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        assert path == "/prompt"
        return {"prompt_id": "p-1", "node_errors": self.node_errors}

    def post_file(self, path: str, *, field: str, filename: str, data: bytes, fields: dict[str, str]) -> dict:
        assert path == "/upload/image" and field == "image"
        assert png_size(data)  # a real PNG went up
        self.uploads.append((filename, fields))
        return {"name": filename, "subfolder": fields["subfolder"], "type": "input"}

    def get_json(self, path: str) -> dict:
        if path == "/system_stats":
            return {"system": {"comfyui_version": "0.3.99"}}
        if path == "/queue":
            return {"queue_running": [[1, "p-1", {}, {}, []]], "queue_pending": []}
        if path.startswith("/history/"):
            self.history_polls += 1
            if self.history_polls < 2:
                return {}  # still running
            return {
                "p-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "12": {"images": [{"filename": "argus-atlas_00001_.png", "subfolder": "", "type": "output"}]}
                    },
                }
            }
        raise AssertionError(path)

    def get_bytes(self, path: str) -> bytes:
        assert path.startswith("/view?")
        buf = Path(self._tmp) / "out.png"
        write_png(buf, *self.size)
        return buf.read_bytes()


@pytest.fixture
def controls(tmp_path: Path) -> dict[str, Path]:
    return {
        "hillshade": write_png(tmp_path / "hillshade.png", GRID.width, GRID.height),
        "slope": write_png(tmp_path / "slope.png", GRID.width, GRID.height),
    }


def _backend(transport: FakeTransport, tmp_path: Path) -> ComfyUIBackend:
    transport._tmp = str(tmp_path)  # where the fake renders its /view PNG
    return ComfyUIBackend(example_template(), "http://comfy:8188", transport=transport, poll_interval=0.0)


def test_generate_uploads_only_referenced_controls_and_writes_png(tmp_path: Path, controls: dict[str, Path]) -> None:
    transport = FakeTransport()
    backend = _backend(transport, tmp_path)
    messages: list[str] = []
    out = backend.generate(
        BakeRequest(job_id="aoi-1", grid=GRID, params=PARAMS, controls=controls),
        tmp_path / "fantasy.png",
        messages.append,
    )
    # The starter template references $hillshade only -> one upload, named per job.
    assert [name for name, _ in transport.uploads] == ["aoi-1-hillshade.png"]
    assert transport.uploads[0][1]["subfolder"] == "argus-atlas"
    graph = transport.posts[0][1]["prompt"]
    load = next(n for n in graph.values() if n["class_type"] == "LoadImage")
    assert load["inputs"]["image"] == "argus-atlas/aoi-1-hillshade.png"
    sampler = next(n for n in graph.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["seed"] == 3
    assert png_size(tmp_path / "fantasy.png") == (256, 192)
    assert out.engine_version == "0.3.99" and out.workflow_sha256 and out.detail["prompt_id"] == "p-1"
    assert any("queued" in m for m in messages) and any("received 256x192" in m for m in messages)


def test_wrong_size_image_is_refused_but_kept(tmp_path: Path, controls: dict[str, Path]) -> None:
    backend = _backend(FakeTransport(size=(256, 191)), tmp_path)
    with pytest.raises(BackendError, match="256x191 image, canvas is 256x192"):
        backend.generate(BakeRequest(job_id="aoi-1", grid=GRID, params=PARAMS, controls=controls), tmp_path / "f.png")
    assert (tmp_path / "f.png").is_file()


def test_node_errors_are_surfaced(tmp_path: Path, controls: dict[str, Path]) -> None:
    backend = _backend(FakeTransport(node_errors={"8": {"errors": ["missing controlnet"]}}), tmp_path)
    with pytest.raises(BackendError, match="rejected the workflow"):
        backend.generate(BakeRequest(job_id="aoi-1", grid=GRID, params=PARAMS, controls=controls), tmp_path / "f.png")


def test_missing_control_for_placeholder(tmp_path: Path) -> None:
    backend = _backend(FakeTransport(), tmp_path)
    with pytest.raises(BackendError, match=r"\$hillshade.*no hillshade control"):
        backend.generate(BakeRequest(job_id="aoi-1", grid=GRID, params=PARAMS, controls={}), tmp_path / "f.png")


def test_probe_reports_version_or_unreachable(tmp_path: Path) -> None:
    status = _backend(FakeTransport(), tmp_path).probe()
    assert status.reachable and status.version == "0.3.99"

    class Down(FakeTransport):
        def get_json(self, path: str) -> dict:
            raise BackendError("connection refused")

    down = _backend(Down(), tmp_path).probe()
    assert not down.reachable and "refused" in (down.detail or "")


def test_multipart_encoding_is_well_formed() -> None:
    body, boundary = encode_multipart(field="image", filename="a.png", data=b"PNGDATA", fields={"overwrite": "true"})
    text = io.BytesIO(body).read()
    assert text.startswith(f"--{boundary}\r\n".encode()) and text.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="overwrite"\r\n\r\ntrue\r\n' in text
    assert b'name="image"; filename="a.png"\r\nContent-Type: image/png\r\n\r\nPNGDATA\r\n' in text


def test_vanished_prompt_fails_fast(tmp_path: Path, controls: dict[str, Path]) -> None:
    """ComfyUI restarted mid-bake: /history stays empty and /queue no longer lists the prompt."""

    class Vanished(FakeTransport):
        def get_json(self, path: str) -> dict:
            if path == "/queue":
                return {"queue_running": [], "queue_pending": []}
            if path.startswith("/history/"):
                self.history_polls += 1
                return {}
            return super().get_json(path)

    transport = Vanished()
    transport._tmp = str(tmp_path)
    backend = ComfyUIBackend(
        example_template(), "http://comfy:8188", transport=transport, poll_interval=0.0, vanish_after=0.0
    )
    with pytest.raises(BackendError, match="neither the queue nor the history"):
        backend.generate(BakeRequest("j", GRID, PARAMS, controls), tmp_path / "out.png")
    assert transport.history_polls < 5  # failed on the first queue check, not at the bake timeout


def test_pending_prompt_keeps_polling(tmp_path: Path, controls: dict[str, Path]) -> None:
    """Still queued (ComfyUI loading a checkpoint) is not 'vanished': polling continues to success."""
    transport = FakeTransport()
    transport._tmp = str(tmp_path)
    backend = ComfyUIBackend(
        example_template(), "http://comfy:8188", transport=transport, poll_interval=0.0, vanish_after=0.0
    )
    backend.generate(BakeRequest("j", GRID, PARAMS, controls), tmp_path / "out.png")
    assert (tmp_path / "out.png").is_file()
