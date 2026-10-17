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

### Not yet

- No real AOI has been through the alignment gate; nothing is published to PyPI.
