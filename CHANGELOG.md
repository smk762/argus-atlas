# Changelog

All notable changes to `argus-atlas` are recorded here. The format follows Keep a Changelog, and versions come from git tags via `hatch-vcs`.

## [Unreleased]

## [0.5.0rc1] - 2026-11-23

### Changed

- **The docs opsec gate no longer names the suite's machines in its own source**: `tests/test_rules.py` reads the private names only from `OPSEC_DENYLIST` (the environment, the Actions secret of the same name, or an untracked `.opsec-denylist` file), matched as whole words, case-insensitively. Unconfigured, the name check is skipped with one notice; the IP, path and email checks always run.
- **argus-cortex 0.8 → 0.14** (#78), pinned `>=0.14,<0.15`. Hashing and timestamps use cortex's `sha256_hex` and `utc_now_iso`.
- **Tooling synced with argus-pkg-template `44d4288`**: the Suite check also runs on `dev`, CI and Suite workflows default to read-only `contents`, and `tests/test_workflows.py` holds every workflow Python to `requires-python` and checks the GitHub Release job. The template's cortex-dispatch image rebuild is not adopted: the image installs from `uv.lock`, so a rebuild would not pick up a new cortex.

## [0.4.1] - 2026-11-20

### Changed

- **Chunk tile addressing has its own light module, `argus_atlas.addressing`** (#69): `parse_tile`, `chunk_grids`, `chunk_bbox`, `canvas_bbox`, `neighbour_tiles`, `chunk_job_id`, `chunk_native_zoom`, `validate_chunk` and the chunk size/margin constants. It imports only `mercator` and `models`, so argus-realms' deterministic rules and the Pyodide arena no longer load GDAL wrappers, the bake stack, `urllib.request` or `http.client` (0.4.0's `chunks` did, via `controls → attribution → dem`). `chunks` re-exports all of it, so existing imports keep working; `test_addressing` holds the module to its import list.
- opentelemetry-api 1.45.1 → 1.45.2 (#71).

## [0.4.0] - 2026-11-19

### Added

- The README's Realms entry links Realms' row in the suite's [What to run for which UI](https://docs.dragonhound.dev/setup/which-services/#realms) matrix.
- **GPU leases through the argus-iris broker** (#54). Every bake holds a `batch` lease on `comfyui` (kind `bake`) from argus-cortex's coordinator, configured host-wide by `ARGUS_GPU_*`; unset is no gating, as before. The prompt goes to the ComfyUI the grant names. `chunks` holds one lease across the block, checks `should_yield()` before each chunk, and re-queues on the same node when the broker asks. A refused or timed-out lease is a `BackendError` that says so. `gpu-tenant.yaml` declares the kind and ships in the image at `/app/gpu-tenant.yaml`; compose passes the `ARGUS_GPU_*` variables through. argus-cortex now comes with its `[remote,yaml]` extras (httpx, PyYAML).
- **Checks for the rules that had none** (#55), in `tests/test_rules.py`: no GDAL wheel or rasterio in `uv.lock`/pyproject; an opsec gate over `docs/`, README, CHANGELOG and `.env.example` (private IPs, home paths, emails, the suite's host names, and an `OPSEC_DENYLIST` secret in CI); every API request field and path param is listed, and every file-naming field is held to its root; no route builds labels and a labelled job's bake gets no labels; a geocache source must call `allow_reuse`; model weights are mounted read-only and never copied into the image, and compose never runs as root; only a final release moves `:latest`. A CI `main-not-ahead` job fails a push to `main` that holds anything `dev` lacks. CLAUDE.md now has a "Must never happen" table naming the check behind each rule, and its module walk-through is cut to what the code doesn't say.
- **Every published tile set carries its attribution** (#61). `job.json` records each input's source when the controls are built (`sources`, sidecar schema 0.5). A DEM counts as Copernicus GLO-30 when the provenance from `argus-atlas dem` vouches for its bytes, and a layer counts as OpenStreetMap when the `osm.json` beside it lists it. `tiles` writes the credit into the PMTiles `attribution` metadata (shown by MapLibre), `argus_sources` and `tiles.attribution`. `mosaic` merges the chunks' sources into the archive, the manifest (`attribution`, `sources`) and the merged labels file. A source atlas can't recognise is recorded without attribution, and `tiles` and `mosaic` warn about it. `inspect` lists the sources.
- **`ATLAS_GPU_PRIORITY`** sets the class of atlas's GPU leases (`batch` by default). `background` queues a long re-bake behind every other tenant, so it runs only on capacity nobody else wants, when the argus-iris broker enforces admission. An unknown class is a `BackendError`, which `doctor` reports. Compose passes it through with `ARGUS_GPU_LEASE_TIMEOUT_S`, which lets a queued lease wait for hours instead of the 300 s default.

### Changed

- **Job, recipe and mosaic ids are checked by argus-cortex's `fs.safe_name`** (argus-cortex 0.8, ai-code-auditor#56): the same rule as before (start alphanumeric, then letters, digits, `.`, `_` or `-`), now matched against the whole string and capped at 128 characters. argus-cortex is pinned `>=0.8,<0.9`.
- **The build commit check uses `fullmatch`** (argus-cortex's `fullmatch` codemod, ai-code-auditor#56), so `provenance` accepts a commit only when the whole string is a 7–40 character hex sha. With `match`, `$` also matched before a trailing newline, so a `_build_info.COMMIT` ending in `\n` was recorded as the commit.
- **`bake --out` refuses a directory that already holds files, or the job itself** (ai-code-auditor nightly). `copy_job` used to merge into an existing job, dropping its bake/tiles record while its outputs stayed. It now copies into a hidden staging dir and renames it into place.
- **`load_recipe` reads a file path only with `allow_path=True`** (the CLI passes it). A name is confined to `ATLAS_RECIPES_DIR`, and a recipe that fails validation is reported by field and message, never with its contents.
- **OSM filters accept plain tokens only** (letters, digits, `_`, `:`, `-`), and the OGR `-where` quotes the key as an identifier (`"building" IS NOT NULL`). **The Overpass URL must be `http(s)://`**, redirects are refused, and error messages no longer quote the response body (it goes to the log).
- **DEM cell size in metres is computed per axis**: for a geographic DEM only the longitude spacing shrinks with latitude, and a projected DEM's linear unit (feet, US survey feet) is converted. This feeds the resampling choice, so a few mid- and high-latitude DEMs now pick a different resampling.

### Fixed

- **Audit findings.** Failures that escaped their error type now stay inside it: a model that cannot be hashed, an `http.client` error or an unreadable HTTP error body from the engine are `BackendError`s; a NaN resolution or an extent-less bbox is an `AtlasError`; packing no tiles is a `TilesError`; a mosaic over jobs with no chunk record or no tile archive is a `MosaicError`; a truncated PNG chunk is an `AtlasError`, and an `eXIf` description another tool wrote reads as no payload. A neighbour whose `job.json` names a crop outside its job directory is refused. `Grid` refuses non-square pixels. A failed PMTiles pack keeps the archive already there (it is written beside it and moved over once complete). `gdalinfo` exiting non-zero is no GDAL version; the DEM fill reaches `gdalwarp` exactly instead of rounded to six digits; `slope_argv` refuses a `z_factor` that is not positive. Multipart field names and filenames are escaped. The hash cache holds one entry per path and at most 256 paths. `LayerPreset.tags_for` is empty below `min_zoom`; an `osm_base` cut off by the header read is no timestamp; an infinite `scalerank` drops the label; a malformed `commands` in `labels.json` reads as none. `scripts/fetch_civitai_lora.py` takes only a bare id or a model page URL (a download link holds a version id) and lists base models when a version has none.
- **Chunk tone matching works with numpy 2.** The tone-strip `gdal_calc` expressions marked cells outside the strip with `-1` in `where(cond, A, -1)`; under numpy 2's promotion rules (NEP 50) that result stays `uint8`, so `-1` became 255 and those cells were averaged in as white, shrinking the tone offset (CI on Ubuntu 26.04: GDAL 3.12.2, numpy 2.3). The marker is now `-1.0`.
- **A failed bake no longer leaves `job.json` describing another canvas.** The backend paints `fantasy.part.png`, which is moved onto `fantasy.png` only once it is complete and carries its metadata; a failure after that drops the old bake/tiles record instead of keeping it. Every failure, not only an `AtlasError`, ends `run_bake` and `POST /bake/stream` with an `error` frame, and a crashed worker no longer aborts the stream mid-body.
- **A chunk re-run that fails with any exception keeps its earlier crop published** (unless the canvas was already repainted). A seam re-run deletes the previous run's footprint, distance and mask rasters first, so footprints of neighbours that are gone no longer persist.
- **Mosaic:** a tile shared by a chunk and a chunk that starts at a finer zoom comes from the overview, so chunk edges show no blank strip; non-point label features are skipped instead of crashing the merge.
- **ComfyUI:** a prompt that finishes between the `/history` and `/queue` reads is no longer reported as vanished.
- **PNG metadata:** `eXIf`/`tEXt` go before the first `IDAT`, short EXIF ASCII values are stored inline, a truncated IFD reads as no payload, and the rewrite is atomic.
- Stale PAM `.aux.xml` files are removed before and after stamping and cropping, so they cannot override a new geotransform. `labels` writes its files only after the grid re-check passes. `gdalinfo` timeouts are a `GdalError`. The model resolver refuses absolute names and `..`, and still follows a symlink the operator put under the models root (a split model tree). Layer names are matched in full. An AAIGrid `NODATA_value` reads as no land. The sea mask excludes the warp's NoData value by name.
- `scripts/fetch_civitai_lora.py` sends the token only as Civitai's `token=` parameter (never as a header that follows the redirect to the CDN) and refuses a remote file name that is not a plain file name.
- **The image reports the release's version** (#50). `.dockerignore` left the tracked `.github/` and `data/*/.gitkeep` out of the build context while keeping `.git`, so hatch-vcs saw a dirty tree and versioned the image `X.Y.(Z+1).dev0+g<sha>.d<date>` even on a release tag. Nothing tracked is ignored any more, the release passes the tag to the build as `VERSION` (set as `SETUPTOOLS_SCM_PRETEND_VERSION` only while argus-atlas's own wheel is built), and the release fails if the pushed image's `/health.version` is not the tag.
- Release review: `bake --out` creates the copied job with the umask's mode, not `0700`; a failed bake removes its half-painted `fantasy.part.png`; any exception in a chunk's blend, crop or tiles is recorded in `job.json` and ends the stream with an `error` frame; `scripts/fetch_civitai_lora.py` no longer prints the `token=` query in its HTML-page error.

## [0.3.1] - 2026-11-13

### Added

- `job.json` and its bake record carry `generator`: the argus-atlas package version and the full git commit that wrote them (#34). The controls and the bake each record their own, because a job can be baked by a later build than the one that built its controls. The bake payload embedded in every output adds `atlas_commit`, the mosaic manifest adds `generator`, `GET /health` adds `commit`, and `inspect` prints it. Sidecar schema `atlas_version` 0.4 (additive; older jobs read with `generator: null`). This lets a viewer such as Realms check that its imported docs and its tiles came from the same atlas build.
- `hatch_build.py` records the build's commit in `argus_atlas/_build_info.py` for wheels, the sdist and the image. hatch-vcs leaves `__commit_id__` empty, and a tagged release's version carries no sha. A source checkout reports its live `git rev-parse HEAD` instead.
- `POST /purge/{id}` deletes one job and everything under its directory; `POST /purge` deletes every job that is not busy and lists the ones it `skipped` (#29). Both are off unless `ATLAS_ALLOW_DELETE=1` (compose falls back to `DEVMODE`), need the body `{"confirm": "purge"}`, and are advertised as `delete_allowed` on `GET /health`. A job is deleted only if its id passes the strict charset, its directory is a real directory directly under the jobs dir (not a symlink) and it holds a `job.json`; an unreadable `job.json` can still be deleted.
- `docs/cli.md`: every command and option, generated from the Typer app by `make docs`. CI's `docs` job fails when it no longer matches the CLI.
- `docs/chunks-and-mosaic.md` (chunks, seams, recipes, blocks, the mosaic, place names: how the Realms map was made) and `docs/alignment-gate.md` (the grid-first contract, each check, and every step that gates again). The Realms "How it was made" panel links to these pages.

### Changed

- The server tracks which jobs a route is writing (`POST /controls`, `POST /bake/stream`, `POST /tiles`) or deleting. A purge of a busy job is a 409, and so is a second bake or tiles run of a job that is already busy.
- README: the chunk, mosaic and labels sections are shortened to a summary that links the new pages, and a docs index is under the status line. The HTTP API table adds `GET /presets`, `GET /inputs`, `POST /osm` and the `POST /controls`, `/bake/stream` and `/tiles` bodies, and gives every preview kind. The env table adds `ATLAS_ALLOW_OSM`, `ATLAS_OVERPASS_URL` and `ATLAS_RECIPES_DIR`. The preset count is corrected (seven: six recommended plus cyberpunk). "When it fails" quotes the messages the code actually prints. `serve --host`/`--allow-bake` and `chunks --dem-source`/`--max-tiles` are documented.

## [0.3.0] - 2026-11-13

### Added

- Relief scaling for `controls`, `chunk`, `chunks` and `POST /controls`. `--z-factor auto` is the default: it multiplies heights by 1/cos(latitude) at the canvas centre before `gdaldem hillshade` (`-z`) and `gdaldem slope` (`-s 1/z`). This undoes Web Mercator's horizontal stretch, so relief far from the equator is no longer flattened (×1.33 at 41°, ×2.37 at 65°). `--vertical-exaggeration` / `--ve` multiplies on top for cartographic exaggeration. `--z-factor off` (or `1`, or any fixed number) turns the correction off. `job.json` records a `relief` record (mode, latitude, base factor, exaggeration, effective z; additive, sidecar schema `atlas_version` 0.3). See `docs/relief.md`.
- Recipes gain `z_factor` (default `"auto"`) and `vertical_exaggeration` (default 1), and both are in the recipe digest. **Chunks baked before this release do not match a recipe that leaves them at their defaults**, so `chunks` re-bakes them. To extend an existing block with its old relief, add `"z_factor": 1` to the recipe: z 1 with no exaggeration keeps the old digest. `chunk` refuses a neighbour with other relief, and `mosaic` refuses a style dir that mixes reliefs.

### Changed

- Docs cleanup: `docs/first-bake.md` is replaced by `docs/your-first-bake.md`, a first bake on a GPU host written against the `.env` placeholders, and `docs/styles.md`, what the sweeps showed about denoise, prompts, checkpoints, the composite, the presets and chunk seams. `.env.example` and `scripts/fetch_civitai_lora.py` use placeholders for host paths.
- README: the hero is the Cook Strait chunk mosaic the Realms demo shows, in two styles; "Live demo" links [Realms](https://realms.dragonhound.dev) and [Argus Studio](https://studio.dragonhound.dev/atlas/); every image and doc link is an absolute GitHub URL so the PyPI page renders them; the links to agent and planning files are gone.

## [0.3.0a1] - 2026-10-31

### Added

- `labels`: place names as a vector overlay on the drape (#14). It reads a local places file (Natural Earth populated places, ranked by `SCALERANK`; an OSM `.osm`/`.pbf` or `places` extract, ranked by `place` class), keeps the named points inside the box that survive at the zoom, and writes EPSG:4326 GeoJSON with `name`, `rank` and `kind`, plus `labels.json` with the source SHA256, cutoff and ogr2ogr argv. `--job` labels a job's canvas (a chunk's tile) at its native zoom and records `labels` in `job.json` (sidecar schema `atlas_version` 0.3, additive). Names never enter the model, the GeoTIFF or the PMTiles.
- `GET /jobs/{id}/labels` serves a job's labels; `JobDigest.has_labels`. There is no route that builds them.
- `osm --layers places`: named `place=*` nodes, scale-aware like the other layers, as a source for `labels`.
- `examples/viewer.html` draws a job's labels (or a local `labels.geojson`) above the raster in the basemap's font, with toggles for them and for the basemap's own labels. `examples/sample_places.geojson` labels the sample DEM.
- `compose.yaml`: run the CLI (`docker compose run --rm atlas <command>`), the API (`up -d serve`, bound to 127.0.0.1 with a healthcheck) and the test suite (`--profile test`) from the image, as your uid, with host paths, flags and the ComfyUI URL from `.env`. `.env.example` gains the compose host settings (`UID`/`GID`, `ATLAS_*_HOST` mounts, `ATLAS_BIND`, `ATLAS_IMAGE`) plus `ATLAS_ALLOW_OSM`, `ATLAS_DEM_SOURCE` and `ATLAS_OVERPASS_URL`. ComfyUI is not managed.
- `dem`: fetch a Copernicus GLO-30 DEM for a bbox (#15). It names the 1x1° tiles the bbox touches, reads only the needed windows of the COGs on the public AWS bucket with `gdalwarp` over `/vsicurl/`, and writes one Float32 GeoTIFF on the tiles' own lattice, snapped inside the bbox, that `controls --dem` accepts. A tile the source lacks is an error that names it; `--allow-missing` fills absent (all-ocean) tiles with 0 m. `<out>.json` records the source, tile keys, requested and written bounds, output SHA256 and the gdalwarp argv, and the command prints the Copernicus attribution. `--source` / `ATLAS_DEM_SOURCE` selects a mirror or a local directory. CLI only: the server has no DEM route.
- `opencaching.allow_reuse`: the Geocaching Australia licence gate (CC BY-NC-SA 2.5). Geocaching.com is rejected. A commercial reuse, or a paid app, is refused before any listings are fetched.
- `dem --cover` snaps the window outward to the GLO-30 lattice, so the GeoTIFF contains the whole bbox instead of stopping up to a pixel short (#19). It fetches the neighbouring tile when the grown window reaches into it. `dem --tile z/x/y [--size --margin]` fetches exactly a chunk's canvas, margin included, with the same maths as `chunk` (`chunks.canvas_bbox`), and implies `--cover`. `--max-tiles` raises the 16-tile budget for blocks of chunks. `<out>.json` records `snap`.
- `chunks`: plan and bake a block of chunks (#18). `--tile z/x/y --rings N` or `--bbox … --zoom z` picks the chunks. They are baked in seam order (north to south, west to east), with one `dem --cover` fetch and one OSM fetch for the block's canvas box, kept in `--inputs` (default `<out>/.block`) and reused on re-runs. `--land` skips chunks with no land in their tile. Chunks already baked with the same recipe, size and margin are skipped, so a re-run resumes after a failure. `--labels` fetches OSM places with the block and labels every chunk. `--plan` prints the plan and stops. `--controls-only` skips finished chunks too, so it never unpublishes one. `--bbox` is capped at the 1089 chunks of `--rings 16`. The library pieces are in `argus_atlas.blocks` (`block`, `land_tiles`, `plan_block`, `run_block`). `chunks._published` is now `published_chunk`.
- `mosaic`: combine a style's chunk archives into one XYZ directory or PMTiles archive (#17). Chunk tiles are copied byte for byte, and a tile two archives disagree on is an error. Overviews down to `--overview-min` (default 5) come from `gdal2tiles` over a `gdalbuildvrt -addalpha` mosaic of the chunk crops. The chunks' `labels.geojson` are merged without duplicates. `mosaic.json` records each source chunk's job id and archive SHA256. MBTiles output stays with #3.
- Release: a `v*` tag now ends with a GitHub Release (#13), created after the PyPI publish succeeds, with this version's CHANGELOG section as notes and the wheel and sdist attached. Tags with `a<n>`, `b<n>`, `rc` or `-` are pre-releases. Re-running the workflow edits the existing Release instead of failing.

### Changed

- Releases: feature work lands on `dev`, and a release is a `dev` → `main` PR with `vX.Y.ZrcN` candidates tagged on its head (README → Develop). CI runs on both branches.
- `uv.lock` is committed. `make install`, CI and the image install with `uv sync --locked` instead of `uv pip install`, so they all get the same resolved versions, and a `pyproject.toml` change without `make lock` fails. The release smoke test still installs the built wheel.

### Fixed

- Image tags: release tags are PEP 440, which the semver tag type rejects, and `:latest` was hard-coded. The image now gets its exact version tag on every release, `:X.Y` only on finals, and `:latest` only on finals, never on an alpha or a release candidate.
- The Docker build context leaves out `.env`, `data/`, `jobs/` and `output/`, so local DEMs and exported bakes are no longer sent to the builder or copied into the image.

## [0.2.0a1] - 2026-10-25

### Added

- `controls --resampling` (default auto): gdalwarp uses `cubicspline` when the canvas is at least twice as fine as the DEM, so small AOIs on a 30 m DEM no longer inherit a cell staircase in the hillshade; `bilinear` otherwise. Also on `POST /controls`.
- `chunk`: bake one XYZ tile end to end (controls, seams, bake, crop, tiles) from a style recipe. The canvas is the tile at 256/512/1024 px plus a margin; `ChunkRecord` in `job.json` holds the tile, size, margin, the tile's own grid, the neighbours used and the crop. `--controls-only` stops before the GPU.
- Seam-aware bakes: already-baked chunks of the same recipe are pasted into the margin of a `seam_init` control, and a `seam_mask` control marks them as kept. The new packaged templates `comfyui_sdxl_chunk_seam.json` and `comfyui_sdxl_chunk_seam_nolora.json` denoise only the masked area (`SetLatentNoiseMask`). A neighbour whose crop no longer matches its sidecar, or that has another chunk size, is an error, and so is a chunk of the recipe that overlaps the one being baked (the same tile in another job, or another zoom over the same ground). Only chunks baked from the recipe count: re-baking a chunk job with `bake` or `POST /bake/stream` drops its recipe. A re-run that fails before its bake is recorded keeps the job's earlier crop and archive published.
- Seam matching: the seam mask fades over `--mask-feather` px (default 32) on the neighbours' side, so the model may repaint the kept pixels next to the seam instead of meeting a hard latent edge. After the bake, `blend_chunk` shifts the canvas's RGB tone by the difference between the neighbours' strip along the seam and the painted strip facing it (capped at 24 levels, `--tone/--no-tone`), then spreads the remaining difference on the neighbours' side into the tile with `gdal_fillnodata` (capped at 48 levels) at two scales: averaged over 16 px blocks and faded over `--feather` px (default 64) for the colour, and at full resolution faded over 8 px for the line the VAE leaves at the seam, so the neighbours' coastlines and rivers are not dragged into the tile as streaks. The matched canvas is labelled RGB so it mosaics with unblended chunks. `fantasy.tif` stays as painted; the crop is cut from the gated `fantasy_blend.tif`, and `ChunkRecord.blend` records the offset, feather and digest. Needs `gdal_proximity.py` and `gdal_fillnodata.py` (python3-gdal).
- Crop gate: the bake of a chunk job is cropped to the tile (`fantasy_chunk.tif`) and gated against the tile's grid; `tiles` and `POST /tiles` tile the crop from the chunk's own zoom up to native, and drop gdal2tiles' sub-half-pixel slivers on the tile edges so neighbouring archives never overlap.
- Style recipes (`argus_atlas.recipes`, `examples/recipes/antique.json`): a JSON file per style with preset, checkpoint, LoRA, workflow and a `tile` or `fixed` seed scheme. `--recipe <name>` resolves under `ATLAS_RECIPES_DIR`. The recipe and its digest are recorded in `job.json`, its name in the bake params and the embedded payload (with the chunk tile and recipe digest).
- Stage timings: `job.json` records seconds for controls, seams, verify, generate, georeference, crop and tiles; `inspect` prints them. Sidecar schema `atlas_version` 0.2 (additive: `chunk`, `recipe`, `timings`).

### Fixed

- `GET /inputs` lists GDAL virtual mosaics (`.vrt`) as DEMs.
- Chunks in the first or last XYZ column, and at zoom 0–1, get a valid PMTiles bbox: `mercator_to_bbox` clamps to the Web Mercator world.
- `tiles --min-zoom` and `POST /tiles` refuse a zoom coarser than a chunk's own, which would spill into its neighbours.
- A failed crop is recorded as `bake.error` with its timing; tiling an uncropped chunk exits 2 / HTTP 409 (`NotTileableError`).
- The seam templates take the recipe's sampler and scheduler (`$sampler`, `$scheduler`) instead of hard-coding the ones they recorded.
- A packaged workflow named by a recipe resolves to the installed file, so a same-named file in the working directory no longer replaces it.
- `chunk` validates its inputs before anything runs: a bad tile, recipe, DEM, layer or size exits 2 whatever the job dir already holds; failures after that exit 1.
- `parse_tile` accepts ASCII digits only and caps the zoom before computing the grid size.
- Neighbour lookup skips unreadable job dirs and counts a symlinked job once.
- Pasting neighbours no longer uses `gdalwarp -srcband/-dstband`, which need GDAL 3.7.

### Changed

- Image: uv 0.13.5, a hash-pinned pip 26.2.2 and certifi 2026.10.19+; the system CA bundle drops the emSign ECC Root CA - G3 and Comodo AAA roots, as certifi does.

## [0.1.0a1] - 2026-10-23

### Added

- `controls`: warp a DEM to EPSG:3857, clip to an AOI, fit the canvas to a long side of at most 1536 with sides a multiple of 64, and write hillshade, slope and optional land-mask rasters plus 8-bit PNG model inputs.
- Job sidecar `job.json` as the georeferencing authority: geotransform, CRS, canvas size, SHA256 of the DEM, control PNGs, checkpoint and LoRA, and the exact GDAL argv that ran.
- Alignment gate: refuse to write a GeoTIFF when the painted output's size, SRS or corner coordinates do not match the sidecar.
- `tiles`: cut an XYZ pyramid with `gdal2tiles.py` for a zoom range that never exceeds the canvas's native Web Mercator zoom, bounded to the AOI, and pack it with the Python `pmtiles` library.
- ComfyUI backend with a `$placeholder` workflow template (`comfyui_sdxl_img2img_control.json`); a missing placeholder is an error that names it.
- `bake`, gated behind `ATLAS_ALLOW_BAKE=1`, with exit codes 1 runtime, 2 input, 3 baking disabled.
- `doctor` and `inspect` commands.
- FastAPI server on port 8105 with read-only default, NDJSON bake stream, and DEM and land names confined to `ATLAS_DEM_DIR`.
- Dockerfile on `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4`; standalone CI that installs `gdal-bin` and runs the suite against real GDAL on a synthetic DEM.
- README with value propositions and onboarding paths, launch checklist (`TODO.md`), PyPI discovery metadata.
- `examples/sample_dem.tif`: a 350 KB Copernicus GLO-30 clip of southern Lake Garda so the quick start runs unedited.
- `examples/viewer.html`: a static MapLibre page that drapes a job's `fantasy.pmtiles` on an OpenFreeMap basemap with an opacity slider and a hillshade comparison layer, loading from the API (`/jobs`, range requests on `/archive`) or a local `.pmtiles` file.
- `scripts/fetch_civitai_lora.py`: operator helper that fetches SDXL LoRAs from Civitai into a `loras/` tree with SHA256 verification, resumable `.part` downloads and a `.civitai.json` sidecar.
- `osm`: fetch OpenStreetMap features for a bbox from Overpass into per-layer GeoJSON (water, rivers, roads, rail, forest, urban, buildings, glaciers), with scale-aware tag rules per zoom, the raw extract kept and an `osm.json` manifest recording query, timestamp and digests.
- `controls --layer name=path` (repeatable): burn any OGR vector file as a 0/255 mask control on the canvas grid; known layers are also painted over the hillshade into `composite.png` (water blue, forest green, roads dark …). `POST /controls` takes `layers` as names under `ATLAS_DEM_DIR`; `GET /jobs/{id}/preview/{kind}` serves any control.
- `examples/flow.html`: the whole flow from a browser against the API — AOI from the map, `POST /osm`, `POST /controls`, the `POST /bake/stream` NDJSON frames live, `POST /tiles`, then the drape. `GET /inputs` lists the DEMs, vector files and OSM extracts under `ATLAS_DEM_DIR`; `POST /osm` (opt-in, `ATLAS_ALLOW_OSM=1`) fetches layers into `ATLAS_DEM_DIR/osm/<name>/`; `GET /health` reports `osm_allowed`.
- Style presets (`argus_atlas.presets`): six named prompt/denoise/cfg bundles chosen from the sweeps; `bake --preset`, `argus-atlas presets`, `GET /presets`, `preset` on `POST /bake/stream` (explicit knobs win), and a preset menu in `examples/flow.html` with `?lock=1` hiding free text for a moderation-free demo.
- Every output carries its bake payload (prompt, negative, seed, steps, cfg, denoise, preset, checkpoint and LoRA SHA256, workflow digest, job id, bbox, grid): `fantasy.png` gets an `eXIf` chunk (ImageDescription, UserComment) and a `tEXt` chunk keyed `argus-atlas`, `fantasy.tif` gets `TIFFTAG_IMAGEDESCRIPTION`/`TIFFTAG_SOFTWARE`, and `fantasy.pmtiles` metadata gets `argus_bake`. Pixels are untouched; the sidecar digest covers the file with its metadata.
- `controls --sea-level <m>`: a `sea` mask of DEM cells at or below a level (Copernicus GLO-30 puts the sea at 0), painted as water in the composite — OSM has no ocean polygons.
- Workflow templates may use any control as a `$<name>` placeholder; the packaged `comfyui_sdxl_img2img_composite_nolora.json` uses `$composite` as the img2img init and `$hillshade` for the ControlNet.
- ComfyUI backend fails fast when a queued prompt disappears from both `/queue` and `/history` (ComfyUI restarted mid-bake) instead of waiting out the bake timeout.
- `docs/first-bake.md`: the GPU-host runbook, amended with the first real bake (Lake Garda, Copernicus GLO-30), LoRA, prompt, checkpoint and denoise sweeps, and contact sheets under `docs/img/`.

### Fixed

- The land mask burnt nothing: `gdal_rasterize -a_srs` labels but never reprojects, so a polygon in EPSG:4326 missed the EPSG:3857 canvas entirely. Vectors are now reprojected and clipped with `ogr2ogr` first, masks no longer carry a NoData/transparency flag, and the test checks pixel counts.
