"""argus-atlas CLI — ``doctor``, ``osm``, ``controls``, ``bake``, ``tiles``, ``chunk``, ``labels``, ``inspect``, ``serve``.

Everything works without the HTTP server or studio: ``controls`` needs only
GDAL, ``bake`` needs a reachable ComfyUI and ``ATLAS_ALLOW_BAKE=1``, ``tiles``
needs GDAL plus the ``tiles`` extra. ``serve`` is the thin wrapper studio uses.
"""

from __future__ import annotations

import json
import logging
import os
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
    DEFAULT_LORA_WEIGHT,
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


def _parse_layers(items: list[str]) -> dict[str, Path]:
    """``--layer name=path`` options -> ``{name: path}``."""
    layers: dict[str, Path] = {}
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise AtlasError(f"--layer expects name=path, got {item!r}")
        layers[name] = Path(path)
    return layers


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
        "gdal": {
            "version": gdal_ops.gdal_version(),
            "tools": gdal_ops.gdal_tools(),
            "tiler": gdal_ops.find_tiler(),
            "calc": gdal_ops.find_calc(),
        },
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
        typer.echo(
            f"  calc            {g['calc'] or 'missing (gdal_calc.py: python3-gdal / gdal-python-tools; no composite)'}"
        )
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
    land: Path | None = Option(None, "--land", help="Land polygon file (any OGR format): the layer named 'land'"),
    layer: list[str] = Option(
        [],
        "--layer",
        help="name=path: burn any OGR vector file as a 0/255 mask control (repeatable); "
        "water/rivers/roads/rail/forest/urban/buildings/glaciers also paint the composite",
    ),
    sea_level: float | None = Option(
        None, "--sea-level", help="Metres: add a 'sea' mask of DEM cells at or below this (0 for Copernicus coasts)"
    ),
    composite: bool = Option(True, "--composite/--no-composite", help="Paint composite.png from the known layers"),
    resampling: str | None = Option(
        None,
        "--resampling",
        help="gdalwarp method (bilinear|cubic|cubicspline|lanczos|average); default: auto by upsampling ratio",
    ),
    long_side: int | None = Option(
        None, "--long-side", help="Canvas long side in px (default 1024, cap 1536, multiple of 64)"
    ),
    resolution: float | None = Option(None, "--resolution", help="Explicit metres per pixel instead of --long-side"),
) -> None:
    """Warp the DEM to EPSG:3857, write hillshade/slope(/layers/composite) controls and job.json. No GPU."""
    from argus_atlas.controls import build_controls

    try:
        layers = _parse_layers(layer)
        aoi = BBox.parse(bbox)
        sidecar = build_controls(
            dem,
            aoi,
            out,
            land=land,
            layers=layers,
            sea_level=sea_level,
            composite=composite,
            resampling=resampling,
            long_side=long_side,
            resolution=resolution,
        )
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
def osm(
    bbox: str = Option(..., "--bbox", help="AOI as west,south,east,north in EPSG:4326 degrees"),
    out: Path = Option(..., "--out", "-o", help="Directory for extract.osm, <layer>.geojson and osm.json"),
    layers: str = Option("water,roads,forest,urban", "--layers", help="Comma-separated OSM layer presets"),
    zoom: int = Option(11, "--zoom", "-z", min=0, max=19, help="Target zoom (the job's native zoom) for scale rules"),
    overpass_url: str = Option(None, "--overpass-url", help="Overpass endpoint (default $ATLAS_OVERPASS_URL)"),
) -> None:
    """Fetch OSM features for an AOI from Overpass into per-layer GeoJSON for `controls --layer`. Needs the network."""
    from argus_atlas.osm import DEFAULT_OVERPASS_URL, LAYER_PRESETS, fetch_osm

    names = [n.strip() for n in layers.split(",") if n.strip()]
    unknown = [n for n in names if n not in LAYER_PRESETS]
    if unknown:
        _fail(f"unknown OSM layer(s) {', '.join(unknown)}; choose from {', '.join(LAYER_PRESETS)}", EXIT_INPUT)
    url = overpass_url or os.environ.get("ATLAS_OVERPASS_URL") or DEFAULT_OVERPASS_URL
    try:
        manifest = fetch_osm(BBox.parse(bbox), out, names, zoom, url=url)
    except AtlasError as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    typer.echo(f"{out}: {manifest['extract']['bytes'] / 1e6:.1f} MB extract, osm_base {manifest['osm_base']}")
    for name, ref in manifest["layers"].items():
        typer.echo(f"  --layer {name}={out / ref['path']}")
    for name in manifest["skipped"]:
        typer.echo(f"  {name}: skipped (empty at zoom {zoom})")


@app.command()
def presets(json_out: bool = Option(False, "--json", help="Machine-readable")) -> None:
    """List the style presets `bake --preset` accepts."""
    from argus_atlas.presets import PRESETS

    if json_out:
        typer.echo(json.dumps({n: p.as_dict() for n, p in PRESETS.items()}, indent=2))
        return
    for p in sorted(PRESETS.values(), key=lambda x: not x.recommended):
        star = "*" if p.recommended else " "
        typer.echo(
            f"{star}{p.name:<10} {p.title:<18} denoise {p.denoise:<5} cfg {p.cfg:<4} steps {p.steps:<3} {p.blurb}"
        )
        typer.echo(f"{'':<11} {p.prompt}")


@app.command()
def bake(
    job: Path = Argument(..., help="Job directory written by `controls`"),
    checkpoint: str = Option(..., "--checkpoint", "-c", help="Base checkpoint, named as ComfyUI loads it"),
    lora: str | None = Option(None, "--lora", help="Cartography LoRA, named as ComfyUI loads it"),
    lora_weight: float = Option(DEFAULT_LORA_WEIGHT, "--lora-weight"),
    out: Path | None = Option(None, "--out", "-o", help="Bake into this job dir instead (controls are copied there)"),
    preset: str | None = Option(
        None, "--preset", help="Style preset (see `argus-atlas presets`); explicit flags override"
    ),
    prompt: str | None = Option(None, "--prompt"),
    negative: str | None = Option(None, "--negative"),
    seed: int | None = Option(None, "--seed", help="Random if omitted"),
    steps: int | None = Option(None, "--steps", min=1),
    cfg: float | None = Option(None, "--cfg"),
    denoise: float | None = Option(None, "--denoise", min=0.0, max=1.0, help="img2img strength"),
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
        from argus_atlas.presets import resolve_params

        params = BakeParams(
            checkpoint=checkpoint,
            lora=lora,
            lora_weight=lora_weight,
            seed=seed if seed is not None else secrets.randbelow(2**32),
            **resolve_params(preset, prompt=prompt, negative=negative, denoise=denoise, cfg=cfg, steps=steps),
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
    """Cut XYZ tiles from the aligned GeoTIFF (native-2..native; a chunk's own zoom..native) and pack them as PMTiles."""
    from argus_atlas.georef import AlignmentError
    from argus_atlas.jobs import load_sidecar
    from argus_atlas.tiles import FANTASY_PMTILES, NotTileableError, tile_job

    job_dir = _job_dir_of(source)
    try:
        sidecar = load_sidecar(job_dir)
    except (FileNotFoundError, AtlasError) as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    if sidecar.bake is None or not sidecar.bake.aligned:
        _fail(f"{sidecar.job_id} has no aligned GeoTIFF — bake it first (or its alignment gate failed)", EXIT_INPUT)
    try:
        record = tile_job(job_dir, sidecar, out=out, min_zoom=min_zoom, max_zoom=max_zoom)
    except AlignmentError as exc:
        _fail(f"alignment gate failed, no tiles written: {exc}", 1)
        return
    except NotTileableError as exc:  # e.g. a chunk whose crop failed: an input state, like an unbaked job
        _fail(str(exc), EXIT_INPUT)
        return
    except AtlasError as exc:
        _fail(str(exc), 1)
        return
    typer.echo(
        f"{sidecar.job_id}: {record.n_tiles} tiles, zoom {record.min_zoom}-{record.max_zoom} -> {out or job_dir / FANTASY_PMTILES}"
    )


@app.command()
def chunk(
    tile: str = Option(..., "--tile", "-t", help="XYZ tile z/x/y to bake as one chunk"),
    recipe: str = Option(..., "--recipe", "-r", help="Recipe file, or a name under $ATLAS_RECIPES_DIR"),
    dem: Path = Option(..., "--dem", help="Source DEM (GeoTIFF or VRT) covering the tile and its margin"),
    out: Path | None = Option(None, "--out", "-o", help="Job directory (default $ATLAS_JOBS_DIR/<recipe>-<z>-<x>-<y>)"),
    neighbours: Path | None = Option(
        None, "--neighbours", help="Where the recipe's other chunk jobs live (default: the parent of --out)"
    ),
    size: int = Option(1024, "--size", help="Tile side in px: 256, 512 or 1024 (native zoom = z + log2(size/256))"),
    margin: int = Option(128, "--margin", help="Px of neighbour context on every side, a multiple of 32"),
    mask_feather: int = Option(
        32, "--mask-feather", help="Px the seam mask fades over on the neighbours' side, up to the margin (0 = hard)"
    ),
    feather: int = Option(
        64, "--feather", help="Px into the tile the remaining seam difference fades over, up to half the tile (0 = off)"
    ),
    tone: bool = Option(True, "--tone/--no-tone", help="Shift the chunk's colours to match its neighbours"),
    land: Path | None = Option(None, "--land", help="Land polygon file (any OGR format): the layer named 'land'"),
    layer: list[str] = Option([], "--layer", help="name=path vector layer, as for `controls` (repeatable)"),
    sea_level: float | None = Option(
        None, "--sea-level", help="Metres: add a 'sea' mask of DEM cells at or below this"
    ),
    composite: bool = Option(True, "--composite/--no-composite", help="Paint composite.png from the known layers"),
    resampling: str | None = Option(None, "--resampling", help="gdalwarp method; default: auto by upsampling ratio"),
    controls_only: bool = Option(
        False, "--controls-only", help="Stop after the controls and seam controls (no GPU, no ATLAS_ALLOW_BAKE)"
    ),
    models_dir: Path | None = Option(None, "--models-dir", help="Default $ATLAS_MODELS_DIR"),
    comfyui_url: str | None = Option(None, "--comfyui-url", help="Default $COMFYUI_BASE_URL"),
) -> None:
    """One chunk end to end: controls -> seams from baked neighbours -> bake -> blend -> crop -> tiles, by a recipe."""
    if not controls_only and not config.bake_allowed():
        _fail(f"refusing to bake: set {config.ENV_ALLOW_BAKE}=1 (or pass --controls-only)", EXIT_REFUSED)

    from argus_atlas import gdal_ops
    from argus_atlas.chunks import chunk_job_id, parse_tile, run_chunk, validate_chunk, validate_seams
    from argus_atlas.controls import check_inputs
    from argus_atlas.jobs import require_safe_id
    from argus_atlas.recipes import load_recipe, recipe_record, recipe_workflow

    # The inputs first, before anything is written: a mistake in them is EXIT_INPUT whatever the job
    # dir already holds, and anything run_chunk raises after that is a runtime failure.
    try:
        z, x, y = parse_tile(tile)
        validate_chunk(z, size, margin)
        validate_seams(size, margin, mask_feather=mask_feather, feather=feather)
        loaded, recipe_path = load_recipe(recipe, recipes_root=config.recipes_dir())
        workflow = None if controls_only else recipe_workflow(loaded, recipe_path)
        layers = _parse_layers(layer)
        check_inputs(dem, land, layers)
        if resampling is not None and resampling not in gdal_ops.RESAMPLING_METHODS:
            raise AtlasError(f"unknown resampling {resampling!r}; choose from {', '.join(gdal_ops.RESAMPLING_METHODS)}")
        job_dir = out if out is not None else config.jobs_dir() / chunk_job_id(loaded.name, z, x, y)
        require_safe_id(job_dir.name)
    except AtlasError as exc:
        _fail(f"chunk failed: {exc}", EXIT_INPUT)
        return
    try:
        backend = None
        if workflow is not None:
            from argus_atlas.backends import backend_from_env

            backend = backend_from_env(workflow=workflow, base_url=comfyui_url)
        sidecar = run_chunk(
            dem,
            tile,
            recipe_record(loaded),
            job_dir,
            backend=backend,
            models_root=config.models_dir(models_dir),
            neighbours_root=neighbours,
            land=land,
            layers=layers,
            sea_level=sea_level,
            composite=composite,
            resampling=resampling,
            size=size,
            margin=margin,
            mask_feather=mask_feather,
            feather=feather,
            tone=tone,
            controls_only=controls_only,
            progress=lambda ev: typer.echo(f"[{ev.type}] {ev.message}", err=ev.type == "error"),
        )
    except FileNotFoundError as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    except AtlasError as exc:  # BackendError and GdalError included
        _fail(f"chunk failed: {exc}", 1)
        return
    timing = ", ".join(f"{k} {v:.1f}s" for k, v in sidecar.timings.items())
    typer.echo(f"{sidecar.job_id}: chunk {tile} [{sidecar.status}] in {job_dir} ({timing})")


@app.command()
def labels(
    source: Path = Option(
        ...,
        "--source",
        "-s",
        help="Places file: Natural Earth populated places, an OSM .osm/.pbf, or an osm places layer",
    ),
    job: Path | None = Option(
        None, "--job", help="Job dir: label its canvas at its native zoom into labels.geojson, recorded in job.json"
    ),
    bbox: str | None = Option(None, "--bbox", help="Without --job: west,south,east,north in EPSG:4326 degrees"),
    zoom: int | None = Option(None, "--zoom", "-z", min=0, max=19, help="Rank cutoff zoom (default: the job's native)"),
    out: Path | None = Option(None, "--out", "-o", help="Without --job: the .geojson to write (labels.json beside it)"),
    source_layer: str | None = Option(None, "--source-layer", help="Layer of a multi-layer source (default: first)"),
) -> None:
    """Place names for the drape: rank and clip a local places file to GeoJSON. The model never sees them."""
    from argus_atlas.labels import build_labels, label_job, manifest_path

    try:
        if job is not None:
            if bbox is not None or out is not None:
                raise AtlasError("--job takes its bbox and output from the job; drop --bbox/--out")
            job_dir = _job_dir_of(job)
            record = label_job(job_dir, source, zoom=zoom, layer=source_layer)
            target, count, cutoff = job_dir / record.file.path, record.count, record.cutoff
        else:
            if bbox is None or zoom is None or out is None:
                raise AtlasError("without --job, pass --bbox, --zoom and --out")
            manifest = build_labels(source, BBox.parse(bbox), zoom, out, layer=source_layer)
            target, count, cutoff = out, manifest["count"], manifest["cutoff"]
    except FileNotFoundError as exc:
        _fail(str(exc), EXIT_INPUT)
        return
    except AtlasError as exc:  # GdalError included
        _fail(f"labels failed: {exc}", EXIT_INPUT)
        return
    typer.echo(f"{count} names ({cutoff}) -> {target}")
    typer.echo(f"  manifest  {manifest_path(target)}")


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
    if sidecar.chunk:
        c = sidecar.chunk
        near = ", ".join(n.tile for n in c.neighbours) or "none"
        typer.echo(f"  chunk:    {c.tile}, {c.size} px + {c.margin} px margin, neighbours: {near}")
        typer.echo(f"            crop: {c.image.path if c.image else 'not yet'}")
        if c.blend:
            typer.echo(f"            blend: tone offset {c.blend.offset}, seam feather {c.blend.feather} px")
    if sidecar.recipe:
        typer.echo(f"  recipe:   {sidecar.recipe.recipe.name} ({sidecar.recipe.sha256[:12]}…)")
    if sidecar.labels:
        lb = sidecar.labels
        typer.echo(
            f"  labels:   {lb.file.path} — {lb.count} names at zoom {lb.zoom} ({lb.cutoff}) from {lb.source.path}"
        )
    if sidecar.tiles:
        t = sidecar.tiles
        typer.echo(f"  tiles:    {t.archive.path} — {t.n_tiles} tiles, zoom {t.min_zoom}-{t.max_zoom} ({t.packer})")
    if sidecar.timings:
        typer.echo("  timings:  " + ", ".join(f"{k} {v:.2f}s" for k, v in sidecar.timings.items()))


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
