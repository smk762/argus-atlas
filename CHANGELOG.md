# Changelog

All notable changes to `argus-atlas` are recorded here. The format follows Keep a Changelog, and versions come from git tags via `hatch-vcs`.

## [Unreleased]

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
- `controls --resampling` (default auto): gdalwarp uses `cubicspline` when the canvas is at least twice as fine as the DEM, so small AOIs on a 30 m DEM no longer inherit a cell staircase in the hillshade; `bilinear` otherwise. Also on `POST /controls`.
- `controls --sea-level <m>`: a `sea` mask of DEM cells at or below a level (Copernicus GLO-30 puts the sea at 0), painted as water in the composite — OSM has no ocean polygons.
- Workflow templates may use any control as a `$<name>` placeholder; the packaged `comfyui_sdxl_img2img_composite_nolora.json` uses `$composite` as the img2img init and `$hillshade` for the ControlNet.
- ComfyUI backend fails fast when a queued prompt disappears from both `/queue` and `/history` (ComfyUI restarted mid-bake) instead of waiting out the bake timeout.
- `chunk`: bake one XYZ tile end to end (controls, seams, bake, crop, tiles) from a style recipe. The canvas is the tile at 256/512/1024 px plus a margin; `ChunkRecord` in `job.json` holds the tile, size, margin, the tile's own grid, the neighbours used and the crop. `--controls-only` stops before the GPU.
- Seam-aware bakes: already-baked chunks of the same recipe are pasted into the margin of a `seam_init` control, and a `seam_mask` control marks them as kept. The new packaged templates `comfyui_sdxl_chunk_seam.json` and `comfyui_sdxl_chunk_seam_nolora.json` denoise only the masked area (`SetLatentNoiseMask`). A neighbour whose crop no longer matches its sidecar, or that has another chunk size, is an error, and so is a chunk of the recipe that overlaps the one being baked (the same tile in another job, or another zoom over the same ground). Only chunks baked from the recipe count: re-baking a chunk job with `bake` or `POST /bake/stream` drops its recipe. A re-run that fails before its bake is recorded keeps the job's earlier crop and archive published.
- Crop gate: the bake of a chunk job is cropped to the tile (`fantasy_chunk.tif`) and gated against the tile's grid; `tiles` and `POST /tiles` tile the crop from the chunk's own zoom up to native, and drop gdal2tiles' sub-half-pixel slivers on the tile edges so neighbouring archives never overlap.
- Style recipes (`argus_atlas.recipes`, `examples/recipes/antique.json`): a JSON file per style with preset, checkpoint, LoRA, workflow and a `tile` or `fixed` seed scheme. `--recipe <name>` resolves under `ATLAS_RECIPES_DIR`. The recipe and its digest are recorded in `job.json`, its name in the bake params and the embedded payload (with the chunk tile and recipe digest).
- Stage timings: `job.json` records seconds for controls, seams, verify, generate, georeference, crop and tiles; `inspect` prints them. Sidecar schema `atlas_version` 0.2 (additive: `chunk`, `recipe`, `timings`).
- `docs/first-bake.md`: the morpheus runbook, amended with the first real bake (Lake Garda, Copernicus GLO-30), LoRA, prompt, checkpoint and denoise sweeps, and contact sheets under `docs/img/`.

### Fixed

- The land mask burnt nothing: `gdal_rasterize -a_srs` labels but never reprojects, so a polygon in EPSG:4326 missed the EPSG:3857 canvas entirely. Vectors are now reprojected and clipped with `ogr2ogr` first, masks no longer carry a NoData/transparency flag, and the test checks pixel counts.

### Not yet

- Nothing is published to PyPI. The first real AOI (Lake Garda) passed the alignment gate on 17 Oct 2026; see `docs/first-bake.md`.
