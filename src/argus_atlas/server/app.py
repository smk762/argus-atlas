"""FastAPI micro-server for argus-atlas (peer of proof on :8104; this is :8105).

Routes:

    GET  /health                       {status, service, version, read_only, gdal}
    GET  /doctor                       gdal version + tools, tiler, pmtiles packer, backend reachability, bake flag
    POST /controls                     {dem, bbox, land?, long_side?, resolution?, job_id?} -> {job_id, sidecar}
    POST /bake/stream                  NDJSON progress for one existing job; 403 unless ATLAS_ALLOW_BAKE=1
    POST /tiles                        {job_id} -> TilesRecord (after the alignment gate has passed)
    GET  /jobs                         {jobs: [JobDigest]}
    GET  /jobs/{id}                    {sidecar, status}
    GET  /jobs/{id}/preview/{kind}     PNG; kind is hillshade, slope, land or fantasy (ids only, no paths)
    GET  /jobs/{id}/archive            the PMTiles bytes

Paths in request bodies (the DEM, a land polygon) are resolved under
``$ATLAS_DEM_DIR`` and refused if they escape it; jobs are addressed by id and
every id is validated against a strict charset, so no client-supplied path is
ever joined under the jobs root.

Live baking is OFF unless ``$ATLAS_ALLOW_BAKE`` is truthy — the demo-safe
posture forge and proof share. The guard is the shared
:class:`argus_cortex.server.WriteGuard` with a predicate that refuses only the
``/bake`` routes, so ``POST /controls`` and ``POST /tiles`` (CPU + GDAL only)
stay available on a GPU-less host and stored jobs remain readable.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError("Server requires: pip install argus-atlas[server]") from exc

from argus_cortex.server import WriteGuard
from pydantic import BaseModel, Field

from argus_atlas import __version__, config, gdal_ops
from argus_atlas.backends.base import BackendError, BakeBackend
from argus_atlas.jobs import JobStore, is_safe_id, new_job_id
from argus_atlas.models import (
    DEFAULT_LORA_WEIGHT,
    AtlasError,
    BakeParams,
    BBox,
    JobDigest,
    JobSidecar,
    TilesRecord,
)

_BAKE_REFUSAL = "live baking is disabled on this host (set ATLAS_ALLOW_BAKE=1 to enable POST /bake/stream)"
_OSM_REFUSAL = "fetching OSM from the server is disabled on this host (set ATLAS_ALLOW_OSM=1 to enable POST /osm)"
INPUT_DEM_SUFFIXES = (".tif", ".tiff", ".vrt")
INPUT_VECTOR_SUFFIXES = (".geojson", ".gpkg", ".shp", ".fgb")


def _make_refuse(allow_bake: bool, allow_osm: bool):  # noqa: ANN202 - argus_cortex Refuse
    """WriteGuard predicate: refuse the bake and/or osm routes that are switched off; other writes pass."""

    def refuse(scope, headers) -> str | None:  # noqa: ANN001 - starlette Scope / Headers
        path = str(scope.get("path", ""))
        if not allow_bake and path.startswith("/bake"):
            return _BAKE_REFUSAL
        if not allow_osm and path.startswith("/osm"):
            return _OSM_REFUSAL
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
    backend_factory=None,  # noqa: ANN001 - Callable[[], BakeBackend]; None = backend_from_env
    osm_fetcher=None,  # noqa: ANN001 - Callable[[str, str], bytes]; None = Overpass over HTTP (tests inject one)
) -> FastAPI:
    """Build the app. Omitted arguments fall back to the ``ATLAS_*`` environment.

    ``allow_bake=None`` reads ``$ATLAS_ALLOW_BAKE``; unset means read-only for
    baking and ``GET /health`` reports ``read_only: true``.
    """
    allow_bake = config.bake_allowed(allow_bake)
    allow_osm = config.osm_allowed(allow_osm)
    jobs_root = config.jobs_dir(jobs_dir)
    dem_root = config.dem_dir(dem_dir)
    models_root = config.models_dir(models_dir)
    store = JobStore(jobs_root)

    app = FastAPI(
        title="Argus Atlas",
        description="Georeferenced fantasy-map overlays from real terrain: DEM in, PMTiles out.",
        version=__version__,
    )

    if not (allow_bake and allow_osm):
        # Registered before CORS so CORSMiddleware stays outermost and a refused
        # cross-origin bake still carries CORS headers. Pure ASGI: never buffers
        # the NDJSON stream when baking IS allowed (the predicate passes it through).
        app.add_middleware(WriteGuard, refuse=_make_refuse(allow_bake, allow_osm))

    if cors:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins(cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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

    @app.get("/health")
    async def health() -> dict:
        """Liveness + capability probe. ``read_only`` is ``True`` when live baking
        is disabled; ``gdal`` is the GDAL version or ``null`` when it is missing."""
        return {
            "status": "ok",
            "service": "argus-atlas",
            "version": __version__,
            "read_only": not allow_bake,
            "osm_allowed": allow_osm,
            "gdal": gdal_ops.gdal_version(),
        }

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
        job_dir = store.dir(sidecar.job_id)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def emit(frame: dict | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, frame)

        def work() -> None:
            try:
                run_bake(
                    job_dir,
                    params,
                    backend,
                    models_root=models_root,
                    progress=lambda ev: emit(ev.model_dump()),
                )
            except AtlasError:
                pass  # run_bake already emitted the error frame
            finally:
                emit(None)

        async def stream():
            worker = loop.run_in_executor(None, work)
            try:
                while True:
                    frame = await queue.get()
                    if frame is None:
                        break
                    yield json.dumps(frame) + "\n"
            finally:
                await worker

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/tiles")
    def tiles(request: TilesRequest) -> TilesRecord:
        """Cut the PMTiles archive for a baked job. Re-runs the alignment gate first."""
        from argus_atlas.georef import FANTASY_TIF, AlignmentError, verify_alignment
        from argus_atlas.tiles import FANTASY_PMTILES, bake_metadata, build_tiles

        sidecar = _load(request.job_id)
        job_dir = store.dir(sidecar.job_id)
        if sidecar.bake is None or not sidecar.bake.aligned or sidecar.bake.geotiff is None:
            raise HTTPException(status_code=409, detail=f"job {sidecar.job_id!r} has no aligned GeoTIFF to tile")
        tif = job_dir / FANTASY_TIF
        try:
            gdal_ops.require_gdal()
            verify_alignment(tif, sidecar.grid)
            zmin, zmax = sidecar.zoom_range
            zoom = (
                request.min_zoom if request.min_zoom is not None else zmin,
                request.max_zoom if request.max_zoom is not None else zmax,
            )
            record = build_tiles(
                tif,
                job_dir / FANTASY_PMTILES,
                sidecar.grid,
                zoom_range=zoom,
                native_zoom=sidecar.native_zoom,
                name=sidecar.job_id,
                work_dir=job_dir,
                extra_metadata=bake_metadata(sidecar),
            )
        except AlignmentError as exc:
            raise HTTPException(status_code=409, detail=f"alignment gate failed: {exc}") from exc
        except gdal_ops.GdalError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AtlasError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sidecar.tiles = record
        store.save(sidecar)
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

    return app
