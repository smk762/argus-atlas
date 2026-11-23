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


def test_finish_between_history_and_queue_reads_is_success(tmp_path: Path, controls: dict[str, Path]) -> None:
    """The prompt leaves /queue for /history between the two reads: a re-read of /history finds it."""

    class Racy(FakeTransport):
        def get_json(self, path: str) -> dict:
            if path == "/queue":
                return {"queue_running": [], "queue_pending": []}  # already done by the time /queue is read
            return super().get_json(path)

    transport = Racy()
    transport._tmp = str(tmp_path)
    backend = ComfyUIBackend(
        example_template(), "http://comfy:8188", transport=transport, poll_interval=0.0, vanish_after=0.0
    )
    backend.generate(BakeRequest("j", GRID, PARAMS, controls), tmp_path / "out.png")
    assert (tmp_path / "out.png").is_file()


def test_multipart_names_cannot_break_out_of_their_header() -> None:
    body, _ = encode_multipart(
        field='im"age', filename='a".png\r\nContent-Type: text/html', data=b"X", fields={'k"\r\nX-Evil: 1': "v"}
    )
    assert b"X-Evil" in body and b"\r\nX-Evil" not in body, "the field name stays inside its quotes"
    assert b'name="k%22%0D%0AX-Evil: 1"\r\n\r\nv\r\n' in body
    assert b'name="im%22age"; filename="a%22.png%0D%0AContent-Type: text/html"\r\nContent-Type: image/png\r\n' in body
    assert body.count(b"\r\nContent-Type:") == 1


def test_transport_failures_are_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client
    import urllib.error
    import urllib.request

    from argus_atlas.backends.http import UrllibTransport

    transport = UrllibTransport("http://comfy.invalid:8188")

    def raising(exc: BaseException):  # noqa: ANN202
        def urlopen(*_args: object, **_kwargs: object) -> None:
            raise exc

        return urlopen

    # The engine went away mid-response: an http.client error, neither URLError nor OSError.
    for exc in (http.client.IncompleteRead(b"par"), http.client.BadStatusLine("")):
        monkeypatch.setattr(urllib.request, "urlopen", raising(exc))
        with pytest.raises(BackendError, match="request to http://comfy.invalid:8188/history failed"):
            transport.get_bytes("/history")

    class Unreadable(io.BytesIO):
        def read(self, *_args: object) -> bytes:
            raise ConnectionResetError("reset while reading the error body")

    error = urllib.error.HTTPError("http://comfy.invalid:8188/prompt", 502, "Bad Gateway", {}, Unreadable())  # type: ignore[arg-type]
    monkeypatch.setattr(urllib.request, "urlopen", raising(error))
    with pytest.raises(BackendError, match="returned HTTP 502"):
        transport.post_json("/prompt", {})


def test_a_model_that_cannot_be_read_is_a_backend_error(tmp_path: Path) -> None:
    from argus_atlas.backends.base import hash_model

    with pytest.raises(BackendError, match="cannot hash model 'gone.safetensors'"):
        hash_model(lambda name: tmp_path / name, "gone.safetensors")
    (tmp_path / "dir.safetensors").mkdir()
    with pytest.raises(BackendError, match="cannot hash model 'dir.safetensors'"):
        hash_model(lambda name: tmp_path / name, "dir.safetensors")


# --- where ComfyUI is: atlas's variable, the suite-wide one, the default ----------------------------------------


def test_comfyui_url_order_is_override_own_variable_suite_variable_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_atlas import config

    assert config.comfyui_url() == config.DEFAULT_COMFYUI_URL
    monkeypatch.setenv("ARGUS_COMFYUI_URL", "http://suite.invalid:8188/")
    assert config.comfyui_url() == "http://suite.invalid:8188"
    monkeypatch.setenv("COMFYUI_BASE_URL", "http://own.invalid:8188")
    assert config.comfyui_url() == "http://own.invalid:8188"  # atlas's own name still wins
    assert config.comfyui_url("http://flag.invalid:8188") == "http://flag.invalid:8188"
    monkeypatch.setenv("COMFYUI_BASE_URL", "")  # what compose's ${COMFYUI_BASE_URL:-} passes: unset
    assert config.comfyui_url() == "http://suite.invalid:8188"


def test_comfyui_url_comes_from_the_suite_file_when_the_environment_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from argus_atlas import config

    suite = tmp_path / "suite.env"
    suite.write_text("ARGUS_COMFYUI_URL=http://file.invalid:8188\n", encoding="utf-8")
    suite.chmod(0o600)
    monkeypatch.setenv("ARGUS_ENV_FILE", str(suite))
    assert config.comfyui_url() == "http://file.invalid:8188"
    monkeypatch.setenv("COMFYUI_BASE_URL", "http://own.invalid:8188")
    assert config.comfyui_url() == "http://own.invalid:8188"


@pytest.mark.parametrize("value", ["gpu-box:8188", "/comfy", "http://h.invalid:8188/?token=made-up"])
def test_a_malformed_comfyui_url_is_refused_by_name_not_replaced_by_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from argus_atlas import config
    from argus_atlas.backends import backend_from_env
    from argus_atlas.models import AtlasError

    monkeypatch.setenv("COMFYUI_BASE_URL", value)
    with pytest.raises(AtlasError) as refused:
        config.comfyui_url()
    assert "COMFYUI_BASE_URL" in str(refused.value) and value not in str(refused.value)
    with pytest.raises(AtlasError):  # the CLI and GET /doctor report it as a configuration error
        backend_from_env()


def test_comfyui_turned_off_is_an_error_not_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_atlas import config
    from argus_atlas.models import AtlasError

    monkeypatch.setenv("ARGUS_COMFYUI_URL", "off")
    with pytest.raises(AtlasError, match="turned off"):
        config.comfyui_url()
