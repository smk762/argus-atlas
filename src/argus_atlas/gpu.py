"""GPU leases from the suite's broker (argus-iris), through argus-cortex's coordinator.

Atlas's GPU work is the ComfyUI prompt that paints one canvas. It is ``batch`` and preemptible between tiles:

* a single bake (``bake``, ``chunk``, ``POST /bake/stream``) holds one lease on ``comfyui`` for its ``generate``;
* a block (``chunks``) holds one lease across its chunks. Before each chunk is painted :class:`TileLeases` checks
  ``should_yield()``; when the broker wants the GPU back it releases the lease and queues again, **pinned to the node
  the block started on**, so every chunk of one recipe is painted by the same ComfyUI and its models.

The lease covers the ComfyUI round trip only: controls, the gate, the crop and the tiles are GDAL work and hold no GPU.
When the grant names a ComfyUI (``base_url``), the prompt goes there; ``None`` means atlas's own ``COMFYUI_BASE_URL``.

The coordinator comes from the host-wide ``ARGUS_GPU_*`` environment (:func:`argus_cortex.gpu.coordinator_from_env`):
unset is ``none``, no gating, which is how atlas ran before. ``broker`` falls back to ``ARGUS_GPU_BROKER_FALLBACK``
(``lease`` by default) when iris cannot be reached.

:data:`BAKE` is the figure declared in ``gpu-tenant.yaml``; ``tests/test_gpu.py`` keeps the two in step. The broker
uses its learned p95 per kind when that is larger. A refused or timed-out lease, or a broker error, is a
:class:`~argus_atlas.backends.base.BackendError` that says so (:func:`lease_failure`). Nothing retries it.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import get_args

from argus_cortex.gpu import GpuCoordinator, GpuLease, GpuLeaseRefused, GpuLeaseTimeout, coordinator_from_env
from argus_cortex.gpu_broker import BrokerError
from argus_cortex.gpu_wire import GpuConstraints, PriorityClass

from argus_atlas.backends.base import BackendError

TENANT = "argus-atlas"
TARGET_COMFYUI = "comfyui"
DEFAULT_PRIORITY: PriorityClass = "batch"
# The class atlas's leases ask for, when set: `background` queues a long re-bake behind everything else, so it runs
# only on capacity nobody else wants (with the broker in `admission: enforce`; in `observe` every lease is granted).
ENV_PRIORITY = "ATLAS_GPU_PRIORITY"

LEASE_ERRORS: tuple[type[Exception], ...] = (GpuLeaseRefused, GpuLeaseTimeout, BrokerError)


@dataclass(frozen=True)
class Footprint:
    kind: str
    target: str
    vram_mb: int  # the whole footprint: weights plus working set
    model_mb: int  # the weights that stay loaded after the lease
    preemptible: bool = True  # a block yields between tiles


# SDXL checkpoint + SDXL ControlNet (+ an optional LoRA) in ComfyUI, sampling a canvas of up to 1536 px.
BAKE = Footprint("bake", TARGET_COMFYUI, vram_mb=12000, model_mb=9000)


def default_coordinator() -> GpuCoordinator:
    """The coordinator ``ARGUS_GPU_*`` names; a :class:`BackendError` (which ``doctor`` reports) if it is unknown."""
    try:
        return coordinator_from_env()
    except ValueError as exc:
        raise BackendError(str(exc)) from exc


def priority_from_env() -> PriorityClass:
    """``ATLAS_GPU_PRIORITY`` (a cortex priority class), else :data:`DEFAULT_PRIORITY`; a :class:`BackendError` for
    anything else, so a typo never silently bakes at the wrong class."""
    raw = os.environ.get(ENV_PRIORITY, "").strip().lower()
    if not raw:
        return DEFAULT_PRIORITY
    classes = get_args(PriorityClass)
    if raw not in classes:
        raise BackendError(f"{ENV_PRIORITY}={raw!r} is not a priority class: {', '.join(classes)}")
    return raw  # type: ignore[return-value]


def lease_failure(exc: Exception) -> str:
    """The error message for a lease that was not granted."""
    if isinstance(exc, GpuLeaseRefused):
        return f"the GPU broker refused the lease: {exc}"
    if isinstance(exc, GpuLeaseTimeout):
        return f"no GPU lease in time: {exc}"
    return f"the GPU broker could not be used: {exc}"


class TileLeases:
    """One lease across a run's tiles, given back and re-queued when the broker asks.

    Use as a context manager and call :meth:`current` before each tile: it returns the held lease, taking it on the
    first call (so nothing is held while the first tile's controls are built), and releasing it and queueing for a
    fresh one when ``should_yield()`` is set. A re-acquired lease is constrained to the node of the first grant (when
    the broker named one). Lease errors propagate as the coordinator raised them (:data:`LEASE_ERRORS`).
    """

    def __init__(
        self,
        coordinator: GpuCoordinator,
        footprint: Footprint = BAKE,
        *,
        caller: str,
        priority: PriorityClass = DEFAULT_PRIORITY,
    ) -> None:
        self.coordinator = coordinator
        self.footprint = footprint
        self.caller = caller
        self.priority = priority
        self.yields = 0
        self._node: str | None = None
        self._stack: contextlib.ExitStack | None = None
        self._lease: GpuLease | None = None

    def __enter__(self) -> TileLeases:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        # Hand the exception to the lease, so the broker records a failed run as failed, not ok.
        stack, self._stack, self._lease = self._stack, None, None
        if stack is not None:
            stack.__exit__(exc_type, exc, tb)  # type: ignore[arg-type]

    @property
    def node(self) -> str | None:
        """The node the first grant named; every later lease is pinned to it."""
        return self._node

    def current(self) -> GpuLease:
        """The lease to paint the next tile under."""
        if self._lease is None or self._lease.should_yield():
            if self._lease is not None:
                self.yields += 1
            self._release()
            self._acquire()
        assert self._lease is not None
        return self._lease

    def _acquire(self) -> None:
        stack = contextlib.ExitStack()
        constraints = GpuConstraints(node=self._node) if self._node else None
        lease = stack.enter_context(
            self.coordinator.lease(
                caller=self.caller,
                min_vram_mb=self.footprint.vram_mb,
                model_mb=self.footprint.model_mb,
                kind=self.footprint.kind,
                target=self.footprint.target,
                priority=self.priority,
                preemptible=self.footprint.preemptible,
                constraints=constraints,
            )
        )
        self._stack, self._lease = stack, lease
        if self._node is None and lease.node:
            self._node = lease.node

    def _release(self) -> None:
        stack, self._stack, self._lease = self._stack, None, None
        if stack is not None:
            stack.close()
