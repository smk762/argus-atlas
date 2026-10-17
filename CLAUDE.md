# CLAUDE.md — argus-atlas

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

The stage after `argus-proof`: take a scored cartography LoRA and **bake one georeferenced fantasy-map overlay from real terrain**. GDAL does the geodata work; the diffusion model only paints pixels whose geotransform was fixed *before* generation and is checked *after* it.

```
argus-quarry -> argus-curator -> argus-lens -> argus-forge -> trainer -> argus-proof -> argus-atlas
  acquire        curate           caption       configs                   evaluate       bake a map
```

v0 bakes **one** Web Mercator canvas (long side <= 1536 px, both sides multiples of 64) from a caller-supplied DEM + AOI. No worldwide pyramid, no seam blending, no DEM download, no game (claims, PostGIS, Leaflet, GeoServer are out of scope). Studio stays a thin shell; no bake logic lives there.

## Layout

`src/argus_atlas/`:

- `models.py` — Pydantic v2 wire types + defaults. **`JobSidecar` (`job.json`) is the API contract and the georeferencing authority**: CRS, grid (`ulx/uly/pixel size`), DEM + control + model SHA256s, the exact GDAL argv arrays, bake and tiles records. `ATLAS_VERSION` is its schema version.
- `mercator.py` — dependency-free EPSG:3857 maths (lon/lat <-> metres, zoom resolution, native zoom, XYZ tile bounds).
- `config.py` — `ATLAS_*` env knobs, `bake_allowed()`, and `resolve_under_root()` (the path fence the API uses).
- `gdal_ops.py` — argv builders + one subprocess runner for `gdalwarp` / `gdaldem` / `gdal_rasterize` / `gdal_translate` / `gdalinfo` / `gdal2tiles.py`. **GDAL is a system package, never pip; rasterio is not a dependency.**
- `controls.py` — `plan_grid()` (canvas sizing; fails with the largest extent that fits when an explicit resolution overflows the cap) and `build_controls()` (warp, hillshade, slope, optional land mask, PNG export, grid verification, sidecar).
- `georef.py` — `stamp()` + the **alignment gate** (`verify_alignment`: size, EPSG:3857, corners within 1e-3 m). A failed gate keeps the PNG and never leaves a `fantasy.tif`.
- `tiles.py` — `gdal2tiles.py --xyz` for `native-2..native` only, packed with the Python `pmtiles` library.
- `bake.py` — `run_bake()`: verify controls -> pin models -> backend -> stamp/gate -> record. Emits `controls`/`generate`/`georeference`/`done|error` frames.
- `jobs.py` — `JobStore`, strict job-id charset, sidecar IO, `copy_job()` for baking variants.
- `backends/` — `base.py` (`BakeBackend` protocol, model resolver/hasher), `workflow.py` (`$placeholder` renderer), `http.py` (urllib transport incl. multipart upload), `comfyui.py` (upload controls -> `/prompt` -> poll `/history` -> `/view`). Registry in `__init__.py` (`get_backend`, `backend_from_env`).
- `server/app.py` — FastAPI on **:8105** (peer of lens :8100 … proof :8104). `cli.py` — Typer (`doctor`, `controls`, `bake`, `tiles`, `inspect`, `serve`).
- `templates/comfyui_sdxl_img2img_control.json` — the starter graph the operator replaces (ControlNet name/weight are hard-coded there on purpose). `..._nolora.json` is the same graph without the LoraLoader for a bake before a cartography LoRA exists; `--workflow <bare packaged name>` selects a packaged template, any other value is a path.
- `docs/first-bake.md` — the runbook for the first real bake on the GPU host (morpheus): what ComfyUI has, what to download, exact commands. The v0 product spec (settled decisions: one canvas, cap 1536, ComfyUI first, no game, bake opt-in, MIT/hatch-vcs) is summarised in **What this is** above and in the README; it is not committed.

`tests/` builds a tiny Float32 DEM with GDAL (`conftest.dem`) and a `FakeBackend` that writes a PNG of the canvas size (or one pixel off). No GPU, no ComfyUI in CI.

## Commands

```bash
make install   # uv venv + editable install with [dev,server,cli,tiles]
make test      # pytest; GDAL-backed tests SKIP when gdalinfo is absent (ATLAS_TEST_REQUIRE_GDAL=1 makes them fail instead — CI sets it)
make lint      # ruff 0.15.16 check + format --check
make format
```

No GDAL on the dev box? Run the suite in the image:

```bash
docker build -t argus-atlas:test --build-arg EXTRAS="server,cli,tiles,dev" .
docker run --rm -e ATLAS_TEST_REQUIRE_GDAL=1 argus-atlas:test pytest -q -p no:cacheprovider
```

## Conventions & gotchas

- **Never let the model's output define the geotransform.** The grid is planned in `plan_grid`, written into `job.json` before generation, and the GeoTIFF is checked against it. Don't read georeferencing back from the PNG, don't resample the painted image to "make it fit", and don't write `fantasy.tif` before `verify_alignment` passes.
- **Zoom never exceeds native.** `native_zoom(resolution)` is clipped to 0–14; `build_tiles` refuses `max_zoom > native`. Tiles outside the canvas are a `TilesError`, not a warning.
- **Live baking is opt-in** (`ATLAS_ALLOW_BAKE=1`), the demo-safe posture forge and proof share (argus-halo#7). The server gate is the shared `argus_cortex.server.WriteGuard` with a predicate that refuses only `/bake*` — `POST /controls` and `POST /tiles` stay allowed because they only need GDAL. Keep `/health.read_only == not allow_bake`.
- **Paths from clients are names under a root.** The API resolves `dem`/`land` under `ATLAS_DEM_DIR` (`resolve_under_root`), and every job artifact is addressed by `(job_id, kind)`; job ids match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. Don't add a route that takes a filesystem path.
- **Record, don't assume.** Every GDAL command goes into `sidecar.commands` as an argv array; every model is pinned by SHA256 via `hash_model`; the control PNGs are re-hashed before a bake (`verify_controls`) so a tampered control can't be painted silently.
- **Placeholders are exact-match strings.** `render_workflow` substitutes only values that *are* a `$name`; ints/floats stay typed. Required: `$checkpoint $positive $negative $seed $steps $cfg $denoise $width $height $hillshade`. `$lora`/`$lora_weight` must both be present iff a LoRA is supplied. `$slope`/`$land` are optional and uploaded only when used.
- **Cross-session note (2026-10-17):** README.md, TODO.md, CHANGELOG.md and examples/ were authored by a second agent session; pyproject.toml carries its PyPI discovery metadata on top of the suite template. Dependencies/extras are owned here.
- Versioning is git-tag-derived (`hatch-vcs`); `_version.py` is generated. `structlog` logging, Pydantic v2, ruff line-length 120. Scaffolded from `argus-pkg-template` — `copier update` pulls tooling changes, but **CI is standalone** (`.github/workflows/ci.yml` apt-installs `gdal-bin python3-gdal`; the shared argus-ci workflow has no system-package hook).
- Image base is `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4` (Ubuntu 24.04, Python 3.12, `gdal2tiles.py` included), not `python:*-slim`. Weights are mounted, never copied.
