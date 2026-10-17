"""ComfyUI bake backend — drives a ComfyUI already running on the host GPU.

Per bake: upload the control PNGs the template references (``/upload/image``),
render the workflow template with the operator's knobs, submit it
(``/prompt``), poll ``/history`` until it finishes, download the saved image
(``/view``) and write it as the job's PNG — refusing an image that is not
exactly the canvas size. No weights live in this package; ComfyUI loads the
checkpoint and LoRA by name from its own model dirs.
"""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

import structlog

from argus_atlas.backends.base import BackendError, BackendStatus, BakeOutput, BakeRequest, StatusSink
from argus_atlas.backends.http import Transport, UrllibTransport
from argus_atlas.backends.workflow import (
    CONTROL_PLACEHOLDERS,
    placeholders_used,
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
    ) -> None:
        # request_timeout is per HTTP call; `timeout` bounds the whole bake —
        # ComfyUI can stall for minutes while a checkpoint first loads.
        self.template = workflow_template
        self.base_url = base_url
        self.transport = transport or UrllibTransport(base_url, timeout=request_timeout)
        self.client_id = client_id
        self.poll_interval = poll_interval
        self.timeout = timeout

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

    def generate(self, request: BakeRequest, out_png: Path, progress: StatusSink | None = None) -> BakeOutput:
        def say(message: str) -> None:
            if progress is not None:
                progress(message)

        used = placeholders_used(self.template)
        uploaded: dict[str, str] = {}
        for kind, placeholder in CONTROL_PLACEHOLDERS.items():
            if placeholder not in used:
                continue
            path = request.controls.get(kind)
            if path is None or not path.is_file():
                raise BackendError(f"workflow template uses {placeholder} but the job has no {kind} control")
            uploaded[placeholder] = self._upload(path, f"{request.job_id}-{kind}.png")
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

        resp = self.transport.post_json("/prompt", {"prompt": graph, "client_id": self.client_id})
        if resp.get("node_errors"):
            raise BackendError(f"ComfyUI rejected the workflow: {resp['node_errors']}")
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            raise BackendError(f"ComfyUI returned no prompt_id: {resp}")
        say(f"queued prompt {prompt_id} ({p.steps} steps, denoise {p.denoise})")

        entry = self._await_history(str(prompt_id))
        data = self._first_image(entry, str(prompt_id))
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

        version = self._version_from(self._safe_stats())
        return BakeOutput(
            engine_version=version, workflow_sha256=template_digest(self.template), detail={"prompt_id": prompt_id}
        )

    def _safe_stats(self) -> dict:
        try:
            return self.transport.get_json("/system_stats")
        except BackendError:
            return {}

    def _upload(self, path: Path, filename: str) -> str:
        resp = self.transport.post_file(
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

    def _await_history(self, prompt_id: str) -> dict:
        """Poll ``/history/{prompt_id}`` until completion, error, or the bake timeout."""
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                history = self.transport.get_json(f"/history/{urllib.parse.quote(prompt_id)}")
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
            if time.monotonic() >= deadline:
                raise BackendError(f"ComfyUI bake did not finish within {self.timeout:.0f}s")
            time.sleep(self.poll_interval)

    def _first_image(self, entry: dict, prompt_id: str) -> bytes:
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
                return self.transport.get_bytes(f"/view?{query}")
        raise BackendError(f"ComfyUI produced no saved image for prompt {prompt_id}")
