"""GPU leases (argus-iris, #54): what atlas leases, where the prompt goes, when a block yields, what a refusal does."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from argus_cortex.gpu import GpuLease, GpuLeaseRefused, GpuLeaseTimeout, NullCoordinator
from argus_cortex.gpu_broker import BrokerError
from argus_cortex.gpu_wire import load_tenant_manifest

from argus_atlas import gpu
from argus_atlas.backends.base import BackendError, BakeRequest
from argus_atlas.backends.comfyui import ComfyUIBackend
from argus_atlas.backends.workflow import example_template
from argus_atlas.png import write_png
from tests.conftest import requires_gdal
from tests.fakebackend import FakeBackend
from tests.test_comfyui import GRID, PARAMS, FakeTransport

MANIFEST = Path(__file__).resolve().parent.parent / "gpu-tenant.yaml"


class Recording:
    """A coordinator that grants at once and records each lease and its outcome.

    *grants* are the leases handed out in order (the last repeats); *raises* fails every lease.
    """

    name = "broker"

    def __init__(self, grants: list[GpuLease] | None = None, raises: Exception | None = None) -> None:
        self.grants = grants or [GpuLease(via="broker")]
        self.raises = raises
        self.calls: list[dict] = []
        self.exits: list[type[BaseException] | None] = []
        self.held = False

    @contextlib.contextmanager
    def lease(self, **kw):
        assert not self.held, "leases must not nest"
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises
        grant = self.grants[min(len(self.calls), len(self.grants)) - 1]
        self.held = True
        try:
            yield grant
        except BaseException as exc:
            self.exits.append(type(exc))
            raise
        else:
            self.exits.append(None)
        finally:
            self.held = False

    def alease(self, **kw):  # pragma: no cover - atlas leases from threads only
        raise NotImplementedError


@pytest.fixture
def controls(tmp_path: Path) -> dict[str, Path]:
    return {"hillshade": write_png(tmp_path / "hillshade.png", GRID.width, GRID.height)}


def _backend(tmp_path: Path, coordinator, transport: FakeTransport | None = None) -> ComfyUIBackend:  # noqa: ANN001
    transport = transport or FakeTransport()
    transport._tmp = str(tmp_path)
    return ComfyUIBackend(
        example_template(), "http://comfy:8188", transport=transport, poll_interval=0.0, gpu_coordinator=coordinator
    )


def _request(controls: dict[str, Path], job_id: str = "aoi-1") -> BakeRequest:
    return BakeRequest(job_id=job_id, grid=GRID, params=PARAMS, controls=controls)


# --- the manifest and the figures ----------------------------------------------------------------------------


def test_the_manifest_validates_and_matches_the_lease_figures() -> None:
    manifest = load_tenant_manifest(MANIFEST)
    assert manifest.tenant == gpu.TENANT
    assert manifest.residents == ()
    assert [k.kind for k in manifest.kinds] == [gpu.BAKE.kind]
    kind = manifest.kind_for(gpu.BAKE.kind)
    assert kind is not None
    assert (kind.target, kind.vram_mb, kind.model_mb) == (gpu.BAKE.target, gpu.BAKE.vram_mb, gpu.BAKE.model_mb)
    assert kind.priority == gpu.DEFAULT_PRIORITY == "batch"
    assert kind.preemptible is gpu.BAKE.preemptible is True


def test_no_coordinator_configured_means_no_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGUS_GPU_COORDINATOR", raising=False)
    assert isinstance(gpu.default_coordinator(), NullCoordinator)


def test_an_unknown_coordinator_is_a_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "sometimes")
    with pytest.raises(BackendError, match="unknown GPU coordinator"):
        gpu.default_coordinator()


# --- one bake ------------------------------------------------------------------------------------------------


def test_a_bake_holds_one_batch_lease_on_comfyui(tmp_path: Path, controls: dict[str, Path]) -> None:
    coordinator = Recording()
    _backend(tmp_path, coordinator).generate(_request(controls), tmp_path / "fantasy.png")
    [call] = coordinator.calls
    assert call["caller"] == "aoi-1"
    assert (call["kind"], call["target"], call["priority"], call["preemptible"]) == ("bake", "comfyui", "batch", True)
    assert (call["min_vram_mb"], call["model_mb"]) == (gpu.BAKE.vram_mb, gpu.BAKE.model_mb)
    assert call["constraints"] is None
    assert coordinator.exits == [None]


def test_a_missing_control_fails_before_any_lease(tmp_path: Path) -> None:
    coordinator = Recording()
    with pytest.raises(BackendError, match=r"\$hillshade"):
        _backend(tmp_path, coordinator).generate(_request({}), tmp_path / "fantasy.png")
    assert coordinator.calls == []


def test_a_failed_bake_is_reported_to_the_broker_as_failed(tmp_path: Path, controls: dict[str, Path]) -> None:
    coordinator = Recording()
    with pytest.raises(BackendError, match="rejected the workflow"):
        _backend(tmp_path, coordinator, FakeTransport(node_errors={"3": "bad"})).generate(
            _request(controls), tmp_path / "fantasy.png"
        )
    assert coordinator.exits == [BackendError]


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (GpuLeaseRefused("never fits"), "the GPU broker refused the lease: never fits"),
        (GpuLeaseTimeout("queue"), "no GPU lease in time: queue"),
        (BrokerError("401"), "the GPU broker could not be used: 401"),
    ],
)
def test_a_lease_failure_is_a_backend_error_that_says_so(
    tmp_path: Path, controls: dict[str, Path], exc: Exception, message: str
) -> None:
    transport = FakeTransport()
    with pytest.raises(BackendError, match=message):
        _backend(tmp_path, Recording(raises=exc), transport).generate(_request(controls), tmp_path / "fantasy.png")
    assert transport.posts == [] and transport.uploads == []


def test_the_prompt_goes_to_the_comfyui_the_grant_names(
    tmp_path: Path, controls: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    placed, configured = FakeTransport(), FakeTransport()
    placed._tmp = str(tmp_path)
    backend = _backend(tmp_path, Recording([GpuLease(via="broker", base_url="http://dragon:8188/")]), configured)
    seen: list[str | None] = []

    def transport_for(base_url):  # noqa: ANN001, ANN202
        seen.append(base_url)
        return placed

    monkeypatch.setattr(backend, "_transport_for", transport_for)
    out = backend.generate(_request(controls), tmp_path / "fantasy.png")
    assert seen == ["http://dragon:8188/"]
    assert len(placed.posts) == 1 and configured.posts == [] and configured.uploads == []
    assert out.detail["gpu_via"] == "broker"


def test_a_grant_without_a_url_uses_the_configured_comfyui(tmp_path: Path) -> None:
    configured = FakeTransport()
    backend = _backend(tmp_path, Recording(), configured)
    assert backend._transport_for(None) is configured
    assert backend._transport_for("http://dragon:8188") is backend._transport_for("http://dragon:8188/")
    with pytest.raises(BackendError, match="unusable ComfyUI URL"):
        backend._transport_for("file:///etc")


# --- a block -------------------------------------------------------------------------------------------------


def test_a_session_takes_no_lease_until_a_tile_is_painted(tmp_path: Path) -> None:
    coordinator = Recording()
    backend = _backend(tmp_path, coordinator)
    with backend.gpu_session("chunks:antique"):
        pass
    assert coordinator.calls == []


def test_a_block_holds_one_lease_and_requeues_on_the_same_node_when_asked(
    tmp_path: Path, controls: dict[str, Path]
) -> None:
    first = GpuLease(via="broker", node="dragon")
    second = GpuLease(via="broker", node="dragon")
    coordinator = Recording([first, second])
    backend = _backend(tmp_path, coordinator)
    with backend.gpu_session("chunks:antique") as leases:
        for i in range(3):
            backend._transport_for(None).history_polls = 0  # type: ignore[attr-defined]
            backend.generate(_request(controls, f"chunk-{i}"), tmp_path / f"{i}.png")
            if i == 0:
                first._yield.set()  # the broker asks for the GPU back while chunk 0 paints
    assert len(coordinator.calls) == 2  # given back once, between chunks 0 and 1
    assert [c["caller"] for c in coordinator.calls] == ["chunks:antique"] * 2
    assert coordinator.calls[0]["constraints"] is None
    assert coordinator.calls[1]["constraints"].node == "dragon"  # pinned: one recipe, one ComfyUI
    assert leases.yields == 1 and coordinator.exits == [None, None]


def test_a_failure_inside_a_block_is_reported_as_failed(tmp_path: Path, controls: dict[str, Path]) -> None:
    coordinator = Recording()
    backend = _backend(tmp_path, coordinator, FakeTransport(node_errors={"3": "bad"}))
    with pytest.raises(BackendError), backend.gpu_session("chunks:antique"):
        backend.generate(_request(controls), tmp_path / "fantasy.png")
    assert coordinator.exits == [BackendError]


def test_sessions_do_not_nest(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Recording())
    with backend.gpu_session("a"), pytest.raises(BackendError, match="already open"), backend.gpu_session("b"):
        pass


class SessionBackend(FakeBackend):
    """A fake that records the GPU sessions run_block opens and paints under the session's lease."""

    def __init__(self, coordinator: Recording) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.sessions: list[str] = []
        self.leases: gpu.TileLeases | None = None

    @contextlib.contextmanager
    def gpu_session(self, caller: str):
        self.sessions.append(caller)
        with gpu.TileLeases(self.coordinator, caller=caller) as leases:
            self.leases = leases
            yield leases
            self.leases = None

    def generate(self, request, out_png, progress=None):  # noqa: ANN001, ANN201
        assert self.leases is not None, "painted outside the block's session"
        self.leases.current()
        return super().generate(request, out_png, progress)


@requires_gdal
def test_run_block_paints_every_chunk_under_one_session(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    from argus_atlas.blocks import plan_block, run_block
    from tests.test_blocks import MARGIN, RECIPE, SIZE, X0, Y0, Z

    tiles = [(Z, X0, Y0), (Z, X0 + 1, Y0)]
    coordinator = Recording()
    backend = SessionBackend(coordinator)
    style = tmp_path / "antique"
    controls_plan = plan_block(tiles, RECIPE, style, size=SIZE, margin=MARGIN)
    run_block(controls_plan, RECIPE, style, dem=dem, backend=backend, controls_only=True)
    assert backend.sessions == [] and coordinator.calls == []  # controls hold no GPU

    plan = plan_block(tiles, RECIPE, style, size=SIZE, margin=MARGIN)
    baked = run_block(plan, RECIPE, style, dem=dem, backend=backend, models_root=models_dir)
    assert len(baked) == 2 and len(backend.requests) == 2
    assert backend.sessions == ["chunks:antique"]
    assert len(coordinator.calls) == 1 and coordinator.exits == [None]
