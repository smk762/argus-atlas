"""ComfyUI bake backend — drives a ComfyUI already running on the host GPU.

Per bake: upload the control PNGs the template references (``/upload/image``),
render the workflow template with the operator's knobs, submit it
(``/prompt``), poll ``/history`` until it finishes, download the saved image
(``/view``) and write it as the job's PNG — refusing an image that is not
exactly the canvas size. No weights live in this package; ComfyUI loads the
checkpoint and LoRA by name from its own model dirs.

Each ``generate`` holds a GPU lease on ``comfyui`` (:mod:`argus_atlas.gpu`) for
that round trip, or runs under the block's lease when :meth:`ComfyUIBackend.gpu_session`
is open. When the grant names a ComfyUI ``base_url``, the prompt goes there.
"""

from __future__ import annotations

import contextlib
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import structlog
from argus_cortex.gpu import GpuCoordinator

from argus_atlas import gpu
from argus_atlas.backends.base import BackendError, BackendStatus, BakeOutput, BakeRequest, StatusSink
from argus_atlas.backends.http import Transport, UrllibTransport
from argus_atlas.backends.workflow import (
    control_placeholders_used,
    render_workflow,
    template_digest,
)
from argus_atlas.png import png_size

logger = structlog.get_logger()

BACKEND_NAME = "comfyui"
UPLOAD_SUBFOLDER = "argus-atlas"


class ComfyUIBackend:
    """Generate one canvas through a running ComfyUI instance."""

    name = BACKEND_NAME

    def __init__(
        self,
        workflow_template: dict,
        base_url: str,
        *,
        transport: Transport | None = None,
        client_id: str = "argus-atlas",
        poll_interval: float = 1.0,
        timeout: float = 1200.0,
        request_timeout: float = 180.0,
        vanish_after: float = 30.0,
        gpu_coordinator: GpuCoordinator | None = None,
        priority: gpu.PriorityClass | None = None,
    ) -> None:
        # request_timeout is per HTTP call; `timeout` bounds the whole bake —
        # ComfyUI can stall for minutes while a checkpoint first loads.
        # vanish_after: once the prompt has been absent from /history for this
        # long, /queue is consulted; a prompt in neither place is gone for good
        # (ComfyUI restarted or its queue was cleared), so fail then, not at `timeout`.
        self.template = workflow_template
        self.base_url = base_url
        self.transport = transport or UrllibTransport(base_url, timeout=request_timeout)
        self.request_timeout = request_timeout
        self.client_id = client_id
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.vanish_after = vanish_after
        # Default: the host-wide ARGUS_GPU_* environment; unset is no gating.
        self.gpu = gpu_coordinator if gpu_coordinator is not None else gpu.default_coordinator()
        self.priority = priority or gpu.priority_from_env()  # ATLAS_GPU_PRIORITY, else batch
        self._session: gpu.TileLeases | None = None
        self._placed: dict[str, Transport] = {}

    # -- doctor ------------------------------------------------------------

    def probe(self) -> BackendStatus:
        try:
            stats = self.transport.get_json("/system_stats")
        except BackendError as exc:
            return BackendStatus(name=self.name, reachable=False, detail=str(exc))
        return BackendStatus(name=self.name, reachable=True, version=self._version_from(stats), detail=self.base_url)

    @staticmethod
    def _version_from(stats: dict) -> str:
        system = stats.get("system", {}) if isinstance(stats, dict) else {}
        return str(system.get("comfyui_version") or "unknown")

    # -- bake --------------------------------------------------------------

    @contextlib.contextmanager
    def gpu_session(self, caller: str) -> Iterator[gpu.TileLeases]:
        """Hold one GPU lease across several ``generate`` calls (a block's chunks).

        Each ``generate`` inside it paints under :meth:`~argus_atlas.gpu.TileLeases.current`, so a
        yield the broker asked for is honoured before the next tile and re-queued on the same node.
        """
        if self._session is not None:
            raise BackendError("a GPU session is already open on this backend")
        with gpu.TileLeases(self.gpu, gpu.BAKE, caller=caller, priority=self.priority) as leases:
            self._session = leases
            try:
                yield leases
            finally:
                self._session = None

    def generate(self, request: BakeRequest, out_png: Path, progress: StatusSink | None = None) -> BakeOutput:
        def say(message: str) -> None:
            if progress is not None:
                progress(message)

        controls = self._controls_for(request)  # a job that can't be painted fails before it queues for a GPU
        try:
            if self._session is not None:
                return self._paint(self._session, request, controls, out_png, say)
            with gpu.TileLeases(self.gpu, gpu.BAKE, caller=request.job_id, priority=self.priority) as leases:
                return self._paint(leases, request, controls, out_png, say)
        except gpu.LEASE_ERRORS as exc:
            raise BackendError(gpu.lease_failure(exc)) from exc

    def _controls_for(self, request: BakeRequest) -> dict[str, tuple[str, Path]]:
        """``{placeholder: (kind, path)}`` for every control the template uses, or a :class:`BackendError`."""
        controls: dict[str, tuple[str, Path]] = {}
        for kind, placeholder in sorted(control_placeholders_used(self.template).items()):
            path = request.controls.get(kind)
            if path is None or not path.is_file():
                have = ", ".join(sorted(request.controls)) or "none"
                raise BackendError(
                    f"workflow template uses {placeholder} but the job has no {kind} control (it has: {have})"
                )
            controls[placeholder] = (kind, path)
        return controls

    def _transport_for(self, base_url: str | None) -> Transport:
        """The configured transport, or one for the ComfyUI a broker grant names."""
        if not base_url:
            return self.transport
        url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BackendError(f"the GPU broker granted an unusable ComfyUI URL: {base_url!r}")
        if url not in self._placed:
            self._placed[url] = UrllibTransport(url, timeout=self.request_timeout)
        return self._placed[url]

    def _paint(
        self,
        leases: gpu.TileLeases,
        request: BakeRequest,
        controls: dict[str, tuple[str, Path]],
        out_png: Path,
        say: StatusSink,
    ) -> BakeOutput:
        lease = leases.current()
        transport = self._transport_for(lease.base_url)
        if lease.via != "local":
            say(f"GPU lease via {lease.via}" + (f" on {lease.node}" if lease.node else ""))

        uploaded: dict[str, str] = {}
        for placeholder, (kind, path) in controls.items():
            uploaded[placeholder] = self._upload(transport, path, f"{request.job_id}-{kind}.png")
            say(f"uploaded {kind} control to ComfyUI")

        p = request.params
        values: dict[str, object] = {
            "$checkpoint": p.checkpoint,
            "$lora": p.lora,
            "$lora_weight": p.lora_weight,
            "$positive": p.prompt,
            "$negative": p.negative,
            "$seed": p.seed,
            "$steps": p.steps,
            "$cfg": p.cfg,
            "$denoise": p.denoise,
            "$width": request.grid.width,
            "$height": request.grid.height,
            "$sampler": p.sampler,
            "$scheduler": p.scheduler,
            **uploaded,
        }
        graph = render_workflow(self.template, values)

        resp = transport.post_json("/prompt", {"prompt": graph, "client_id": self.client_id})
        if resp.get("node_errors"):
            raise BackendError(f"ComfyUI rejected the workflow: {resp['node_errors']}")
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            raise BackendError(f"ComfyUI returned no prompt_id: {resp}")
        say(f"queued prompt {prompt_id} ({p.steps} steps, denoise {p.denoise})")

        entry = self._await_history(transport, str(prompt_id))
        data = self._first_image(transport, entry, str(prompt_id))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(data)
        size = png_size(data)
        if size != (request.grid.width, request.grid.height):
            # Keep the PNG for inspection; the caller's gate will refuse it too.
            raise BackendError(
                f"ComfyUI returned a {size[0]}x{size[1]} image, canvas is {request.grid.width}x{request.grid.height} "
                f"(kept at {out_png})"
            )
        say(f"received {size[0]}x{size[1]} image")

        version = self._version_from(self._safe_stats(transport))
        detail = {"prompt_id": prompt_id, "gpu_via": lease.via, "gpu_node": lease.node}
        return BakeOutput(engine_version=version, workflow_sha256=template_digest(self.template), detail=detail)

    @staticmethod
    def _safe_stats(transport: Transport) -> dict:
        try:
            return transport.get_json("/system_stats")
        except BackendError:
            return {}

    @staticmethod
    def _upload(transport: Transport, path: Path, filename: str) -> str:
        resp = transport.post_file(
            "/upload/image",
            field="image",
            filename=filename,
            data=path.read_bytes(),
            fields={"overwrite": "true", "type": "input", "subfolder": UPLOAD_SUBFOLDER},
        )
        name = resp.get("name")
        if not name:
            raise BackendError(f"ComfyUI upload of {path.name} returned no name: {resp}")
        subfolder = resp.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else str(name)

    def _await_history(self, transport: Transport, prompt_id: str) -> dict:
        """Poll ``/history/{prompt_id}`` until completion, error, the prompt vanishing, or the bake timeout."""
        deadline = time.monotonic() + self.timeout
        next_queue_check = time.monotonic() + self.vanish_after
        # ComfyUI may finish between our /history read and the /queue read that
        # misses it, so a queue miss only fails after one more /history read.
        vanished = False
        while True:
            try:
                history = transport.get_json(f"/history/{urllib.parse.quote(prompt_id)}")
            except BackendError as exc:
                if time.monotonic() >= deadline:
                    raise BackendError(f"ComfyUI bake did not finish within {self.timeout:.0f}s: {exc}") from exc
                logger.debug("comfyui.poll_retry", prompt_id=prompt_id, error=str(exc))
                time.sleep(self.poll_interval)
                continue
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise BackendError(f"ComfyUI run failed: {status}")
                if status.get("completed") is True or status.get("status_str") == "success" or entry.get("outputs"):
                    return entry
            if vanished:
                raise BackendError(
                    f"ComfyUI prompt {prompt_id} is in neither the queue nor the history — "
                    "ComfyUI restarted or its queue was cleared mid-bake; rerun"
                )
            now = time.monotonic()
            if now >= deadline:
                raise BackendError(f"ComfyUI bake did not finish within {self.timeout:.0f}s")
            if now >= next_queue_check:
                next_queue_check = now + self.vanish_after
                if not self._queued(transport, prompt_id):
                    vanished = True
                    continue  # re-read /history now, before deciding it is gone
            time.sleep(self.poll_interval)

    @staticmethod
    def _queued(transport: Transport, prompt_id: str) -> bool:
        """Whether ``/queue`` still lists *prompt_id* (running or pending); ``True`` if the queue can't be read."""
        try:
            queue = transport.get_json("/queue")
        except BackendError as exc:
            logger.debug("comfyui.queue_unreadable", prompt_id=prompt_id, error=str(exc))
            return True
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key) or []:
                # each item is [number, prompt_id, prompt, extra_data, outputs_to_execute]
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    @staticmethod
    def _first_image(transport: Transport, entry: dict, prompt_id: str) -> bytes:
        for node_output in entry.get("outputs", {}).values():
            for ref in node_output.get("images", []):
                if ref.get("type") == "temp":
                    continue  # previews; keep SaveImage outputs only
                query = urllib.parse.urlencode(
                    {
                        "filename": ref.get("filename", ""),
                        "subfolder": ref.get("subfolder", ""),
                        "type": ref.get("type", "output"),
                    }
                )
                return transport.get_bytes(f"/view?{query}")
        raise BackendError(f"ComfyUI produced no saved image for prompt {prompt_id}")
