"""FastAPI micro-server for argus-atlas (peer of proof on :8104; this is :8105).

Routes:

    GET  /health                       cortex's ServiceHealth plus {commit, osm_allowed, delete_allowed, gdal}
    GET  /doctor                       gdal version + tools, tiler, pmtiles packer, backend reachability, bake flag
    POST /controls                     {dem, bbox, land?, long_side?, resolution?, job_id?} -> {job_id, sidecar}
    POST /bake/stream                  NDJSON progress for one existing job; 403 unless ATLAS_ALLOW_BAKE=1
    POST /tiles                        {job_id} -> TilesRecord (after the alignment gate has passed)
    GET  /jobs                         {jobs: [JobDigest]}
    GET  /jobs/{id}                    {sidecar, status}
    GET  /jobs/{id}/preview/{kind}     PNG; kind is hillshade, slope, land or fantasy (ids only, no paths)
    GET  /jobs/{id}/archive            the PMTiles bytes
    GET  /jobs/{id}/labels             the place-name GeoJSON `argus-atlas labels --job` wrote (built by the CLI only)
    POST /purge/{id}                   {"confirm": "purge"} -> delete one job dir; 403 unless ATLAS_ALLOW_DELETE=1
    POST /purge                        {"confirm": "purge"} -> delete every job dir that is not busy; same flag

Paths in request bodies (the DEM, a land polygon) are resolved under
``$ATLAS_DEM_DIR`` and refused if they escape it; jobs are addressed by id and
every id is validated against a strict charset, so no client-supplied path is
ever joined under the jobs root.

Live baking is OFF unless ``$ATLAS_ALLOW_BAKE`` is truthy — the demo-safe
posture forge and proof share. The guard is the shared
:class:`argus_cortex.server.WriteGuard` with a predicate that refuses only the
``/bake`` routes, so ``POST /controls`` and ``POST /tiles`` (CPU + GDAL only)
stay available on a GPU-less host and stored jobs remain readable. Deleting
jobs (``/purge``) is a dev-mode tool, off unless ``$ATLAS_ALLOW_DELETE`` is
truthy, and refuses a job that a bake, tiles run or controls run is writing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import FileResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError("Server requires: pip install argus-atlas[server]") from exc

import structlog
from argus_cortex.server import WriteGuard
from argus_cortex.service import DependencyCheck, install_health
from pydantic import BaseModel, Field, field_validator

from argus_atlas import __version__, config, gdal_ops, gpu
from argus_atlas.backends.base import BackendError, BakeBackend
from argus_atlas.jobs import JobStore, is_safe_id, new_job_id
from argus_atlas.models import (
    ATLAS_VERSION,
    DEFAULT_LORA_WEIGHT,
    SIDECAR_NAME,
    Z_FACTOR_AUTO,
    AtlasError,
    BakeParams,
    BBox,
    JobDigest,
    JobSidecar,
    ProgressEvent,
    TilesRecord,
    ZFactor,
    check_z_factor,
)
from argus_atlas.provenance import generator

logger = structlog.get_logger()

_BAKE_REFUSAL = "live baking is disabled on this host (set ATLAS_ALLOW_BAKE=1 to enable POST /bake/stream)"
_OSM_REFUSAL = "fetching OSM from the server is disabled on this host (set ATLAS_ALLOW_OSM=1 to enable POST /osm)"
_DELETE_REFUSAL = "deleting jobs is disabled on this host (set ATLAS_ALLOW_DELETE=1 to enable POST /purge)"
PURGE_CONFIRM = {"confirm": "purge"}
_GDAL_TTL_S = 30.0
INPUT_DEM_SUFFIXES = (".tif", ".tiff", ".vrt")
INPUT_VECTOR_SUFFIXES = (".geojson", ".gpkg", ".shp", ".fgb")


def _make_refuse(allow_bake: bool, allow_osm: bool, allow_delete: bool = False):  # noqa: ANN202 - argus_cortex Refuse
    """WriteGuard predicate: refuse the bake, osm and/or purge routes that are switched off; other writes pass."""

    def refuse(scope, headers) -> str | None:  # noqa: ANN001 - starlette Scope / Headers
        path = str(scope.get("path", ""))
        if not allow_bake and path.startswith("/bake"):
            return _BAKE_REFUSAL
        if not allow_osm and path.startswith("/osm"):
            return _OSM_REFUSAL
        if not allow_delete and path.startswith("/purge"):
            return _DELETE_REFUSAL
        return None

    return refuse


def _require_id(value: str) -> str:
    if not is_safe_id(value):
        raise HTTPException(status_code=400, detail=f"invalid job id {value!r}")
    return value


class ControlsRequest(BaseModel):
    """``POST /controls``: a DEM and land file are *names under ATLAS_DEM_DIR*, never arbitrary paths."""

    dem: str
    bbox: list[float] | str
    land: str | None = None
    # layer name -> vector file *name under ATLAS_DEM_DIR* (e.g. from `argus-atlas osm`)
    layers: dict[str, str] = Field(default_factory=dict)
    sea_level: float | None = None
    composite: bool = True
    resampling: str | None = None
    long_side: int | None = Field(default=None, gt=0)
    resolution: float | None = Field(default=None, gt=0)
    job_id: str | None = None
    # "auto" corrects Web Mercator's stretch at the canvas centre; a number is used as is (1 = uncorrected).
    z_factor: ZFactor = Z_FACTOR_AUTO
    vertical_exaggeration: float = Field(default=1.0, gt=0, allow_inf_nan=False)

    @field_validator("z_factor")
    @classmethod
    def _z_factor(cls, value: ZFactor) -> ZFactor:
        return check_z_factor(value)


class BakeStreamRequest(BaseModel):
    """``POST /bake/stream``. ``preset`` fills prompt/negative/denoise/cfg/steps (see ``GET /presets``);
    any of those given explicitly wins over the preset."""

    job_id: str
    checkpoint: str
    lora: str | None = None
    lora_weight: float = DEFAULT_LORA_WEIGHT
    preset: str | None = None
    prompt: str | None = None
    negative: str | None = None
    seed: int | None = Field(default=None, ge=0)
    steps: int | None = Field(default=None, gt=0)
    cfg: float | None = Field(default=None, gt=0)
    denoise: float | None = Field(default=None, gt=0, le=1)


class OsmRequest(BaseModel):
    """``POST /osm``: fetch OSM layers for a bbox into ``<ATLAS_DEM_DIR>/osm/<name>/`` so
    ``POST /controls`` can name them. Opt-in via ``ATLAS_ALLOW_OSM=1``."""

    name: str
    bbox: list[float] | str
    zoom: int = Field(default=11, ge=0, le=19)
    layers: list[str] = Field(default_factory=lambda: ["water", "rivers", "roads", "forest", "urban"])


class TilesRequest(BaseModel):
    job_id: str
    min_zoom: int | None = Field(default=None, ge=0)
    max_zoom: int | None = Field(default=None, ge=0)


def create_app(
    cors: bool = False,
    cors_origins: list[str] | None = None,
    jobs_dir: str | Path | None = None,
    dem_dir: str | Path | None = None,
    models_dir: str | Path | None = None,
    allow_bake: bool | None = None,
    allow_osm: bool | None = None,
    allow_delete: bool | None = None,
    backend_factory=None,  # noqa: ANN001 - Callable[[], BakeBackend]; None = backend_from_env
    osm_fetcher=None,  # noqa: ANN001 - Callable[[str, str], bytes]; None = Overpass over HTTP (tests inject one)
    gpu_coordinator=None,  # noqa: ANN001 - GpuCoordinator registered at startup; None = the ARGUS_GPU_* environment
) -> FastAPI:
    """Build the app. Omitted arguments fall back to the ``ATLAS_*`` environment.

    ``allow_bake=None`` reads ``$ATLAS_ALLOW_BAKE``; unset means read-only for
    baking and ``GET /health`` reports ``read_only: true``.
    """
    allow_bake = config.bake_allowed(allow_bake)
    allow_osm = config.osm_allowed(allow_osm)
    allow_delete = config.delete_allowed(allow_delete)
    jobs_root = config.jobs_dir(jobs_dir)
    dem_root = config.dem_dir(dem_dir)
    models_root = config.models_dir(models_dir)
    store = JobStore(jobs_root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Registers atlas's tenant manifest with the GPU broker (argus-iris) now rather than at the first bake, and
        # signs off at shutdown. Nothing without ARGUS_GPU_BROKER_URL; never blocks startup on the broker.
        async with gpu.keep_registered(gpu_coordinator):
            yield

    app = FastAPI(
        title="Argus Atlas",
        description="Georeferenced fantasy-map overlays from real terrain: DEM in, PMTiles out.",
        version=__version__,
        lifespan=lifespan,
    )

    if not (allow_bake and allow_osm and allow_delete):
        # Registered before CORS so CORSMiddleware stays outermost and a refused
        # cross-origin bake still carries CORS headers. Pure ASGI: never buffers
        # the NDJSON stream when baking IS allowed (the predicate passes it through).
        app.add_middleware(WriteGuard, refuse=_make_refuse(allow_bake, allow_osm, allow_delete))

    if cors:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins(cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Job ids some route is writing (controls, bake, tiles) or deleting. A purge refuses a busy job and
    # claims the one it deletes, so a bake cannot start in a directory that is being removed.
    busy: set[str] = set()
    busy_lock = threading.Lock()

    def _claim(job_id: str) -> None:
        with busy_lock:
            if job_id in busy:
                raise HTTPException(
                    status_code=409, detail=f"job {job_id!r} is busy (a bake, tiles or purge is running)"
                )
            busy.add(job_id)

    def _release(job_id: str) -> None:
        with busy_lock:
            busy.discard(job_id)

    @contextmanager
    def _claimed(job_id: str) -> Iterator[None]:
        _claim(job_id)
        try:
            yield
        finally:
            _release(job_id)

    def _job_dir_to_delete(job_id: str) -> Path:
        """The job's directory, confined to the jobs root: a safe id, a real directory (not a symlink)
        whose resolved parent is the root, holding a ``job.json`` (read or not, so a broken job can go)."""
        _require_id(job_id)
        path = store.dir(job_id)
        if path.is_symlink() or not (path / SIDECAR_NAME).is_file():
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        if path.resolve().parent != jobs_root.resolve():
            raise HTTPException(status_code=400, detail=f"job {job_id!r} is outside the jobs dir")
        return path

    def _backend() -> BakeBackend:
        if backend_factory is not None:
            return backend_factory()
        from argus_atlas.backends import backend_from_env

        return backend_from_env()

    def _load(job_id: str) -> JobSidecar:
        _require_id(job_id)
        try:
            return store.load(job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}") from exc
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _resolve_input(name: str, label: str) -> Path:
        try:
            path = config.resolve_under_root(dem_root, name, label)
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{label} {name!r} not found under {dem_root}")
        return path

    # `gdalinfo --version` is a subprocess: asked at most once per _GDAL_TTL_S, so /health only reads state.
    gdal_seen: list = [0.0, None, False]  # monotonic time, version, asked yet
    gdal_lock = threading.Lock()

    def _gdal() -> str | None:
        with gdal_lock:
            if not gdal_seen[2] or time.monotonic() - gdal_seen[0] > _GDAL_TTL_S:
                gdal_seen[:] = [time.monotonic(), gdal_ops.gdal_version(), True]
            return gdal_seen[1]

    def _check_gdal() -> DependencyCheck:
        version = _gdal()
        return DependencyCheck(ok=version is not None, detail=version or "gdalinfo is missing or broken")

    def _check_comfyui() -> DependencyCheck:
        # The version only: /health is unauthenticated, so never the ComfyUI address (GET /doctor has the detail).
        status = _backend().probe()
        return DependencyCheck(ok=status.reachable, detail=status.version if status.reachable else "unreachable")

    def _health_extra() -> dict:
        return {
            "commit": generator().commit,
            "osm_allowed": allow_osm,
            "delete_allowed": allow_delete,
            "gdal": _gdal(),
        }

    # Liveness + capability probe, in cortex's ServiceHealth shape (argus-cortex 0.16) with atlas's own fields beside
    # it. `read_only` is True when live baking is disabled; `gdal` is the GDAL version or null when it is missing,
    # which also makes `status` "degraded" (still HTTP 200). The backend is checked only where baking is allowed and
    # never degrades the service: with a GPU broker the grant may name another ComfyUI. `?checks=0` skips both.
    health_checks = {"gdal": _check_gdal}
    if allow_bake:
        health_checks["comfyui"] = _check_comfyui
    install_health(
        app,
        service="argus-atlas",
        version=__version__,
        wire={"atlas_version": ATLAS_VERSION},
        checks=health_checks,
        informational=["comfyui"] if allow_bake else [],
        read_only=not allow_bake,
        extra=_health_extra,
    )

    @app.get("/presets")
    async def presets() -> dict:
        """The style presets ``POST /bake/stream`` accepts by name — the demo's fixed menu."""
        from argus_atlas.presets import PRESETS

        return {"presets": [p.as_dict() for p in PRESETS.values()]}

    @app.get("/inputs")
    async def inputs() -> dict:
        """What ``POST /controls`` may name: DEMs and vector files under ``ATLAS_DEM_DIR``
        (relative names, three levels deep) and the OSM extracts ``POST /osm`` / the CLI wrote."""
        dems: list[str] = []
        vectors: list[str] = []
        extracts: list[dict] = []
        if dem_root.is_dir():
            # nosemgrep: handler-scans-all - the endpoint lists the operator's input dir; it is small and the answer is the whole listing
            for path in sorted(dem_root.rglob("*")):
                rel = path.relative_to(dem_root)
                if len(rel.parts) > 3 or not path.is_file():
                    continue
                if path.suffix.lower() in INPUT_DEM_SUFFIXES:
                    dems.append(rel.as_posix())
                elif path.suffix.lower() in INPUT_VECTOR_SUFFIXES:
                    vectors.append(rel.as_posix())
                elif path.name == "osm.json":
                    try:
                        manifest = json.loads(path.read_text(encoding="utf-8"))
                        extracts.append(
                            {
                                "name": rel.parent.as_posix(),
                                "bbox": manifest.get("bbox"),
                                "zoom": manifest.get("zoom"),
                                "osm_base": manifest.get("osm_base"),
                                "layers": {
                                    layer: (rel.parent / ref["path"]).as_posix()
                                    for layer, ref in manifest.get("layers", {}).items()
                                },
                            }
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
        return {"root": str(dem_root), "dems": dems, "vectors": vectors, "osm": extracts}

    @app.post("/osm")
    def osm(request: OsmRequest) -> dict:
        """Fetch OSM layers for a bbox into ``<ATLAS_DEM_DIR>/osm/<name>/``; the response names
        each layer the way ``POST /controls`` wants it. Refused unless ``ATLAS_ALLOW_OSM=1``."""
        from argus_atlas.osm import LAYER_PRESETS, fetch_osm

        _require_id(request.name)
        unknown = [n for n in request.layers if n not in LAYER_PRESETS]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown OSM layer(s) {unknown}; choose from {sorted(LAYER_PRESETS)}"
            )
        try:
            bbox = BBox.parse(request.bbox) if isinstance(request.bbox, str) else BBox.from_list(request.bbox)
            gdal_ops.require_gdal()
        except gdal_ops.GdalError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out_dir = dem_root / "osm" / request.name
        try:
            manifest = fetch_osm(
                bbox, out_dir, request.layers, request.zoom, url=config.overpass_url(), fetcher=osm_fetcher
            )
        except gdal_ops.GdalError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AtlasError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        rel = out_dir.relative_to(dem_root)
        return {
            "name": request.name,
            "layers": {layer: (rel / ref["path"]).as_posix() for layer, ref in manifest["layers"].items()},
            "skipped": manifest["skipped"],
            "osm_base": manifest["osm_base"],
            "extract_bytes": manifest["extract"]["bytes"],
        }

    @app.get("/doctor")
    def doctor() -> dict:
        """Everything a bake needs, probed: GDAL + tiler, PMTiles packer, backend, flag, roots.
        Sync ``def`` so the subprocess/HTTP probes run in FastAPI's threadpool."""
        from argus_atlas.tiles import packer_status

        try:
            status = _backend().probe()
            backend = {
                "name": status.name,
                "reachable": status.reachable,
                "version": status.version,
                "detail": status.detail,
            }
        except (BackendError, AtlasError) as exc:
            backend = {"name": config.backend_name(), "reachable": False, "version": None, "detail": str(exc)}
        return {
            "gdal": {
                "version": gdal_ops.gdal_version(),
                "tools": gdal_ops.gdal_tools(),
                "tiler": gdal_ops.find_tiler(),
            },
            "pmtiles": packer_status(),
            "backend": backend,
            "bake_allowed": allow_bake,
            "jobs_dir": str(jobs_root),
            "dem_dir": str(dem_root),
            "models_dir": str(models_root),
            "workflow": str(config.workflow_path() or "packaged template"),
        }

    @app.post("/controls")
    def controls(request: ControlsRequest) -> dict:
        """Plan the canvas and write the GDAL controls for a new job (no GPU).
        Path checks run before anything else so an escaping path is a clean 400."""
        from argus_atlas.controls import build_controls

        dem = _resolve_input(request.dem, "dem")
        land = _resolve_input(request.land, "land") if request.land else None
        layers = {name: _resolve_input(path, f"layer {name}") for name, path in request.layers.items()}
        try:
            bbox = BBox.parse(request.bbox) if isinstance(request.bbox, str) else BBox.from_list(request.bbox)
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job_id = request.job_id or new_job_id()
        _require_id(job_id)
        if store.exists(job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id!r} already exists")
        try:
            gdal_ops.require_gdal()
        except gdal_ops.GdalError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            with _claimed(job_id):
                # Again under the claim: another request may have created it since the check above.
                if store.exists(job_id):
                    raise HTTPException(status_code=409, detail=f"job {job_id!r} already exists")
                sidecar = build_controls(
                    dem,
                    bbox,
                    store.dir(job_id),
                    land=land,
                    layers=layers,
                    sea_level=request.sea_level,
                    composite=request.composite,
                    resampling=request.resampling,
                    long_side=request.long_side,
                    resolution=request.resolution,
                    job_id=job_id,
                    z_factor=request.z_factor,
                    vertical_exaggeration=request.vertical_exaggeration,
                )
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job_id": sidecar.job_id, "status": sidecar.status, "sidecar": sidecar.model_dump(mode="json")}

    @app.post("/bake/stream")
    async def bake_stream(request: BakeStreamRequest) -> StreamingResponse:
        """Bake one existing job, streaming ``ProgressEvent`` frames as NDJSON:
        ``controls`` / ``generate`` / ``georeference`` / ``done`` or ``error``."""
        from argus_atlas.bake import run_bake
        from argus_atlas.presets import resolve_params

        sidecar = _load(request.job_id)
        try:
            knobs = resolve_params(
                request.preset,
                prompt=request.prompt,
                negative=request.negative,
                denoise=request.denoise,
                cfg=request.cfg,
                steps=request.steps,
            )
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = BakeParams(
            checkpoint=request.checkpoint,
            lora=request.lora,
            lora_weight=request.lora_weight,
            seed=request.seed if request.seed is not None else secrets.randbelow(2**32),
            **knobs,
        )
        try:
            backend = _backend()
        except (BackendError, AtlasError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        _claim(sidecar.job_id)
        try:  # re-read under the claim: a purge may have removed the job since the read above
            sidecar = _load(sidecar.job_id)
        except BaseException:
            _release(sidecar.job_id)
            raise
        job_dir = store.dir(sidecar.job_id)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def emit(frame: dict | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, frame)

        terminal = [False]  # whether a done/error frame has gone out

        def progress(ev: ProgressEvent) -> None:
            terminal[0] = terminal[0] or ev.type in ("done", "error")
            emit(ev.model_dump())

        def work() -> None:
            try:
                run_bake(job_dir, params, backend, models_root=models_root, progress=progress)
            except Exception as exc:  # noqa: BLE001 - every failure ends the stream with an error frame
                if not terminal[0]:
                    message = str(exc) if isinstance(exc, AtlasError) else f"{type(exc).__name__}: {exc}"
                    progress(ProgressEvent(job_id=sidecar.job_id, type="error", message=message))
                logger.warning("server.bake_failed", job_id=sidecar.job_id, error=str(exc))
            finally:
                _release(sidecar.job_id)
                emit(None)

        # Started here, not in the generator, so the claim is released even if the body is never streamed.
        worker = loop.run_in_executor(None, work)

        async def stream():
            try:
                while True:
                    frame = await queue.get()
                    if frame is None:
                        break
                    yield json.dumps(frame) + "\n"
            finally:
                try:
                    await worker  # work() swallows its own failures; never re-raise into a started body
                except Exception as exc:  # noqa: BLE001
                    logger.warning("server.bake_worker_failed", error=str(exc))

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/tiles")
    def tiles(request: TilesRequest) -> TilesRecord:
        """Cut the PMTiles archive for a baked job. Re-runs the alignment gate first."""
        from argus_atlas.georef import AlignmentError
        from argus_atlas.tiles import NotTileableError, tile_job

        _require_id(request.job_id)
        try:
            with _claimed(request.job_id):  # claimed before the read, so a purge cannot slip in between
                sidecar = _load(request.job_id)
                job_dir = store.dir(sidecar.job_id)
                if sidecar.bake is None or not sidecar.bake.aligned or sidecar.bake.geotiff is None:
                    detail = f"job {sidecar.job_id!r} has no aligned GeoTIFF to tile"
                    raise HTTPException(status_code=409, detail=detail)
                record = tile_job(job_dir, sidecar, min_zoom=request.min_zoom, max_zoom=request.max_zoom)
        except AlignmentError as exc:
            raise HTTPException(status_code=409, detail=f"alignment gate failed: {exc}") from exc
        except NotTileableError as exc:  # e.g. a chunk whose crop failed: not tileable yet, like an unbaked job
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except gdal_ops.GdalError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return record

    @app.get("/jobs")
    async def list_jobs() -> dict[str, list[JobDigest]]:
        return {"jobs": store.list()}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        sidecar = _load(job_id)
        return {"job_id": sidecar.job_id, "status": sidecar.status, "sidecar": sidecar.model_dump(mode="json")}

    @app.get("/jobs/{job_id}/preview/{kind}")
    async def preview(job_id: str, kind: str) -> FileResponse:
        """A control or the painted image as PNG — ids and kinds only, never paths."""
        _require_id(job_id)
        if not store.exists(job_id):
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        try:
            path = store.preview_path(job_id, kind)
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if path is None:
            raise HTTPException(status_code=404, detail=f"job {job_id!r} has no {kind} image")
        return FileResponse(path, media_type="image/png")

    @app.get("/jobs/{job_id}/archive")
    async def archive(job_id: str) -> FileResponse:
        sidecar = _load(job_id)
        path = store.archive_path(sidecar.job_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"job {job_id!r} has no PMTiles archive yet")
        return FileResponse(path, media_type="application/vnd.pmtiles", filename=path.name)

    @app.get("/jobs/{job_id}/labels")
    async def labels(job_id: str) -> FileResponse:
        """Place names for the viewer to draw over the raster. No route builds them: that is the CLI."""
        sidecar = _load(job_id)
        path = store.labels_path(sidecar.job_id)
        if path is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id!r} has no labels (run `argus-atlas labels --job`)"
            )
        return FileResponse(path, media_type="application/geo+json")

    async def _require_confirm(request: Request) -> None:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if body != PURGE_CONFIRM:
            raise HTTPException(status_code=400, detail='the body must be {"confirm": "purge"}')

    @app.post("/purge/{job_id}")
    async def purge_job(job_id: str, request: Request) -> dict:
        """Delete one job and everything under its directory. Off unless ``ATLAS_ALLOW_DELETE=1``;
        409 while the job is busy. A broken ``job.json`` does not stop it: that is a leftover too."""
        await _require_confirm(request)
        path = _job_dir_to_delete(job_id)
        with _claimed(job_id):
            await run_in_threadpool(shutil.rmtree, path)
        return {"deleted": [job_id]}

    @app.post("/purge")
    async def purge_all(request: Request) -> dict:
        """Delete every job under the jobs dir, skipping (and listing) the busy ones. Only directories
        with a safe id and a ``job.json`` count as jobs; anything else in the root is left alone."""
        await _require_confirm(request)
        deleted: list[str] = []
        skipped: list[str] = []
        if jobs_root.is_dir():
            # nosemgrep: handler-scans-all - purge-all: visiting every job is the operation
            for child in sorted(jobs_root.iterdir()):
                if not is_safe_id(child.name) or child.is_symlink() or not (child / SIDECAR_NAME).is_file():
                    continue
                try:
                    _claim(child.name)
                except HTTPException:
                    skipped.append(child.name)
                    continue
                try:
                    await run_in_threadpool(shutil.rmtree, child)
                    deleted.append(child.name)
                finally:
                    _release(child.name)
        return {"deleted": deleted, "skipped": skipped}

    return app
