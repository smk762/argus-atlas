"""argus-atlas CLI — ``doctor``, ``controls``, ``bake``, ``tiles``, ``inspect``, ``serve``.

Everything works without the HTTP server or studio: ``controls`` needs only
GDAL, ``bake`` needs a reachable ComfyUI and ``ATLAS_ALLOW_BAKE=1``, ``tiles``
needs GDAL plus the ``tiles`` extra. ``serve`` is the thin wrapper studio uses.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
from pathlib import Path

import structlog

try:
    import typer
    from typer import Argument, Option
except ImportError as _exc:  # pragma: no cover
    print("CLI requires: pip install argus-atlas[cli]", file=sys.stderr)
    raise SystemExit(1) from _exc

from argus_atlas import config
from argus_atlas.models import (
    DEFAULT_CFG,
    DEFAULT_DENOISE,
    DEFAULT_LORA_WEIGHT,
    DEFAULT_NEGATIVE,
    DEFAULT_PROMPT,
    DEFAULT_STEPS,
    AtlasError,
    BakeParams,
    BBox,
)

app = typer.Typer(
    name="argus-atlas",
    help="Bake a georeferenced fantasy-map overlay from a DEM: GDAL controls -> ComfyUI -> GeoTIFF -> PMTiles.",
    no_args_is_help=True,
)

EXIT_INPUT = 2
EXIT_REFUSED = 3


@app.callback()
def _cli(verbose: bool = Option(False, "--verbose", "-v", help="Show info/debug logs")) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))


def _fail(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _job_dir_of(path: Path) -> Path:
    """Accept a job dir or a file inside it (``jobs/aoi-1/fantasy.tif``)."""
    return path if path.is_dir() else path.parent


@app.command()
def doctor(json_out: bool = Option(False, "--json", help="Machine-readable report")) -> None:
    """Report GDAL, the tiler, the PMTiles packer, the backend and the bake flag. Exits 1 if GDAL is missing."""
    from argus_atlas import gdal_ops
    from argus_atlas.backends import backend_from_env
    from argus_atlas.backends.base import BackendError
    from argus_atlas.tiles import packer_status

    try:
        status = backend_from_env().probe()
        backend = {
            "name": status.name,
            "reachable": status.reachable,
            "version": status.version,
            "detail": status.detail,
        }
    except (BackendError, AtlasError) as exc:
        backend = {"name": config.backend_name(), "reachable": False, "version": None, "detail": str(exc)}
    report = {
        "gdal": {"version": gdal_ops.gdal_version(), "tools": gdal_ops.gdal_tools(), "tiler": gdal_ops.find_tiler()},
        "pmtiles": packer_status(),
        "backend": backend,
        "bake_allowed": config.bake_allowed(),
        "jobs_dir": str(config.jobs_dir()),
        "dem_dir": str(config.dem_dir()),
        "models_dir": str(config.models_dir()),
        "workflow": str(config.workflow_path() or "packaged template"),
    }
    if json_out:
        typer.echo(json.dumps(report, indent=2))
    else:
        g = report["gdal"]
        typer.echo(f"gdal:        {g['version'] or 'MISSING — install gdal-bin (apt) / gdal (dnf)'}")
        for name, path in g["tools"].items():
            typer.echo(f"  {name:<16}{path or 'missing'}")
        typer.echo(f"  tiler           {g['tiler'] or 'missing (gdal2tiles.py: python3-gdal / gdal-python-tools)'}")
        pm = report["pmtiles"]
        typer.echo(
            f"pmtiles:     library={pm['library'] or 'missing (pip install argus-atlas[tiles])'} cli={pm['cli'] or 'absent'}"
        )
        b = report["backend"]
        state = f"reachable, version {b['version']}" if b["reachable"] else f"unreachable ({b['detail']})"
        typer.echo(f"backend:     {b['name']} — {state}")
        typer.echo(
            f"bake:        {'ALLOWED (ATLAS_ALLOW_BAKE)' if report['bake_allowed'] else 'disabled (set ATLAS_ALLOW_BAKE=1)'}"
        )
        typer.echo(f"jobs dir:    {report['jobs_dir']}")
        typer.echo(f"dem dir:     {report['dem_dir']}")
        typer.echo(f"models dir:  {report['models_dir']}")
        typer.echo(f"workflow:    {report['workflow']}")
    if report["gdal"]["version"] is None:
        _fail("GDAL is not installed; argus-atlas cannot run without it", 1)


@app.command()
def controls(
    dem: Path = Option(..., "--dem", help="Source DEM GeoTIFF (local path)"),
    bbox: str = Option(..., "--bbox", help="AOI as west,south,east,north in EPSG:4326 degrees"),
    out: Path = Option(..., "--out", "-o", help="Job directory to create (its name is the job id)"),
    land: Path | None = Option(None, "--land", help="Land polygon file (any OGR format) for an optional land mask"),
    long_side: int | None = Option(
        None, "--long-side", help="Canvas long side in px (default 1024, cap 1536, multiple of 64)"
    ),
    resolution: float | None = Option(None, "--resolution", help="Explicit metres per pixel instead of --long-side"),
) -> None:
    """Warp the DEM to EPSG:3857, write hillshade/slope(/land) controls and job.json. No GPU."""
    from argus_atlas.controls import build_controls

    try:
        aoi = BBox.parse(bbox)
        sidecar = build_controls(dem, aoi, out, land=land, long_side=long_side, resolution=resolution)
    except AtlasError as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    g = sidecar.grid
    typer.echo(
        f"{sidecar.job_id}: {g.width}x{g.height} px at {g.resolution:.3f} m/px, native zoom {sidecar.native_zoom}"
    )
    for kind, ref in sidecar.controls.items():
        typer.echo(f"  {kind:<10}{out / ref.path}  {ref.sha256[:12]}…")
    typer.echo(f"  sidecar   {out / 'job.json'}")


@app.command()
def bake(
    job: Path = Argument(..., help="Job directory written by `controls`"),
    checkpoint: str = Option(..., "--checkpoint", "-c", help="Base checkpoint, named as ComfyUI loads it"),
    lora: str | None = Option(None, "--lora", help="Cartography LoRA, named as ComfyUI loads it"),
    lora_weight: float = Option(DEFAULT_LORA_WEIGHT, "--lora-weight"),
    out: Path | None = Option(None, "--out", "-o", help="Bake into this job dir instead (controls are copied there)"),
    prompt: str = Option(DEFAULT_PROMPT, "--prompt"),
    negative: str = Option(DEFAULT_NEGATIVE, "--negative"),
    seed: int | None = Option(None, "--seed", help="Random if omitted"),
    steps: int = Option(DEFAULT_STEPS, "--steps", min=1),
    cfg: float = Option(DEFAULT_CFG, "--cfg"),
    denoise: float = Option(DEFAULT_DENOISE, "--denoise", min=0.0, max=1.0, help="img2img strength"),
    models_dir: Path | None = Option(
        None, "--models-dir", help="Where checkpoints/LoRAs are hashed from (default $ATLAS_MODELS_DIR)"
    ),
    workflow: Path | None = Option(
        None, "--workflow", help="ComfyUI API-format template (default $ATLAS_WORKFLOW or packaged)"
    ),
    comfyui_url: str | None = Option(None, "--comfyui-url", help="Default $COMFYUI_BASE_URL"),
) -> None:
    """Paint the controls through ComfyUI and stamp the result to fantasy.tif. Refuses unless ATLAS_ALLOW_BAKE=1."""
    if not config.bake_allowed():
        _fail(f"refusing to bake: set {config.ENV_ALLOW_BAKE}=1 to enable live generation on this host", EXIT_REFUSED)

    from argus_atlas.backends import backend_from_env
    from argus_atlas.backends.base import BackendError
    from argus_atlas.bake import run_bake
    from argus_atlas.jobs import copy_job

    job_dir = _job_dir_of(job)
    try:
        if out is not None and out.resolve() != job_dir.resolve():
            copy_job(job_dir, out)
            job_dir = out
        params = BakeParams(
            checkpoint=checkpoint,
            lora=lora,
            lora_weight=lora_weight,
            prompt=prompt,
            negative=negative,
            seed=seed if seed is not None else secrets.randbelow(2**32),
            steps=steps,
            cfg=cfg,
            denoise=denoise,
        )
        backend = backend_from_env(workflow=workflow, base_url=comfyui_url)
        sidecar = run_bake(
            job_dir,
            params,
            backend,
            models_root=config.models_dir(models_dir),
            progress=lambda ev: typer.echo(f"[{ev.type}] {ev.message}", err=ev.type == "error"),
        )
    except FileNotFoundError as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    except (BackendError, AtlasError) as exc:
        _fail(f"bake failed: {exc}", 1)
        return
    assert sidecar.bake is not None
    typer.echo(f"{sidecar.job_id}: seed {params.seed} -> {job_dir / sidecar.bake.geotiff.path}")  # type: ignore[union-attr]


@app.command()
def tiles(
    source: Path = Argument(..., help="A job dir, or its fantasy.tif"),
    out: Path | None = Option(None, "--out", "-o", help="PMTiles path (default <job>/fantasy.pmtiles)"),
    min_zoom: int | None = Option(None, "--min-zoom", min=0),
    max_zoom: int | None = Option(None, "--max-zoom", min=0, help="Never finer than the canvas's native zoom"),
) -> None:
    """Cut XYZ tiles from the aligned GeoTIFF (native-2..native) and pack them as PMTiles."""
    from argus_atlas import gdal_ops
    from argus_atlas.georef import FANTASY_TIF, AlignmentError, verify_alignment
    from argus_atlas.jobs import load_sidecar, save_sidecar
    from argus_atlas.tiles import FANTASY_PMTILES, build_tiles

    job_dir = _job_dir_of(source)
    try:
        sidecar = load_sidecar(job_dir)
    except (FileNotFoundError, AtlasError) as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    if sidecar.bake is None or not sidecar.bake.aligned:
        _fail(f"{sidecar.job_id} has no aligned GeoTIFF — bake it first (or its alignment gate failed)", EXIT_INPUT)
    tif = job_dir / FANTASY_TIF
    zmin, zmax = sidecar.zoom_range
    zoom = (min_zoom if min_zoom is not None else zmin, max_zoom if max_zoom is not None else zmax)
    try:
        gdal_ops.require_gdal()
        verify_alignment(tif, sidecar.grid)  # the gate again, right before tiles are written
        record = build_tiles(
            tif,
            out or job_dir / FANTASY_PMTILES,
            sidecar.grid,
            zoom_range=zoom,
            native_zoom=sidecar.native_zoom,
            name=sidecar.job_id,
            work_dir=job_dir,
        )
    except AlignmentError as exc:
        _fail(f"alignment gate failed, no tiles written: {exc}", 1)
        return
    except AtlasError as exc:
        _fail(str(exc), 1)
        return
    sidecar.tiles = record
    save_sidecar(job_dir, sidecar)
    typer.echo(
        f"{sidecar.job_id}: {record.n_tiles} tiles, zoom {record.min_zoom}-{record.max_zoom} -> {out or job_dir / FANTASY_PMTILES}"
    )


@app.command()
def inspect(
    job: Path = Argument(..., help="A job dir (or a file inside it)"),
    json_out: bool = Option(False, "--json", help="Print the sidecar as JSON"),
) -> None:
    """Summarise a job's sidecar: grid, controls, bake and tiles state."""
    from argus_atlas.jobs import load_sidecar

    try:
        sidecar = load_sidecar(_job_dir_of(job))
    except (FileNotFoundError, AtlasError) as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    if json_out:
        typer.echo(sidecar.model_dump_json(indent=2))
        return
    g = sidecar.grid
    typer.echo(f"{sidecar.job_id} [{sidecar.status}] atlas {sidecar.atlas_version}, created {sidecar.created_at}")
    typer.echo(f"  dem:      {sidecar.dem.path} ({sidecar.dem.sha256[:12]}…)")
    typer.echo(f"  bbox:     {sidecar.bbox.as_text()}")
    typer.echo(f"  grid:     {g.width}x{g.height} px, {g.resolution:.3f} m/px, ul ({g.ulx:.3f}, {g.uly:.3f}) {g.crs}")
    typer.echo(f"  zoom:     native {sidecar.native_zoom}, archive {sidecar.zoom_range[0]}-{sidecar.zoom_range[1]}")
    typer.echo(f"  controls: {', '.join(sidecar.controls) or 'none'}  ({len(sidecar.commands)} GDAL commands recorded)")
    if sidecar.bake:
        b = sidecar.bake
        lora = f" + {b.lora.name} @ {b.params.lora_weight}" if b.lora else ""
        typer.echo(
            f"  bake:     {b.backend} {b.engine_version}, {b.checkpoint.name}{lora}, seed {b.params.seed}, denoise {b.params.denoise}"
        )
        typer.echo(f"            aligned={b.aligned}" + (f" error={b.error}" if b.error else ""))
    if sidecar.tiles:
        t = sidecar.tiles
        typer.echo(f"  tiles:    {t.archive.path} — {t.n_tiles} tiles, zoom {t.min_zoom}-{t.max_zoom} ({t.packer})")


@app.command()
def serve(
    port: int = Option(config.DEFAULT_PORT, "--port", "-p"),
    host: str = Option("0.0.0.0", "--host"),
    cors: bool = Option(False, "--cors", help="Enable CORS for $ATLAS_CORS_ORIGINS (default http://localhost:3000)"),
    allow_bake: bool | None = Option(
        None, "--allow-bake/--no-allow-bake", help="Enable POST /bake/stream (default: $ATLAS_ALLOW_BAKE)"
    ),
) -> None:
    """Start the argus-atlas micro-server (FastAPI) on :8105."""
    try:
        import uvicorn
    except ImportError as _exc:  # pragma: no cover
        typer.echo("Server requires: pip install argus-atlas[server]", err=True)
        raise typer.Exit(1) from _exc

    from argus_atlas.server import create_app

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    uvicorn.run(create_app(cors=cors, allow_bake=allow_bake), host=host, port=port)


if __name__ == "__main__":
    app()
