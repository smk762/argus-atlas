# CLAUDE.md — argus-atlas

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

The stage after `argus-proof`: take a scored cartography LoRA and **bake one georeferenced fantasy-map overlay from real terrain**. GDAL does the geodata work; the diffusion model only paints pixels whose geotransform was fixed *before* generation and is checked *after* it.

```
argus-quarry -> argus-curator -> argus-lens -> argus-forge -> trainer -> argus-proof -> argus-atlas
  acquire        curate           caption       configs                   evaluate       bake a map
```

v0 bakes **one** Web Mercator canvas (long side <= 1536 px, both sides multiples of 64) from a caller-supplied DEM + AOI, or one **chunk**: an XYZ tile plus a margin, continuing the chunks of the same recipe already baked around it. No worldwide pyramid (`mosaic` joins one style's chunks, nothing more), no DEM download on the server (the `dem` CLI command fetches Copernicus GLO-30), no game (claims, PostGIS, Leaflet, GeoServer are out of scope; the game demo is its own repo and wraps `argus-atlas chunk`). Studio stays a thin shell; no bake logic lives there.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/main/SUITE.md

## Must never happen

Each rule names the check that fails when it is broken. A rule marked **UNENFORCED** has a linked issue; give it a check rather than more prose. A rule about code shape (a banned import or API) can go in argus-ci's suite-rules catalogue (smk762/argus-ci#16) instead of a test here.

| Never | Enforced by |
|---|---|
| The model's output defines the geotransform, or a `fantasy.tif` exists before `verify_alignment` passes | `test_georef::test_one_pixel_off_fails_gate_without_geotiff`, `test_bake::test_off_by_one_fails_gate_keeps_png_records_failure` |
| Tiles above native zoom | `test_cli::test_controls_inspect_and_tiles_round_trip` |
| A chunk tiles coarser than its own zoom (it would spill into its neighbours) | `test_chunks::test_tiles_never_start_coarser_than_the_chunk` |
| The blend touches `fantasy.tif` | `test_chunks::test_blend_closes_the_seam_and_keeps_the_painted_canvas` |
| A tampered control is painted | `test_bake::test_tampered_control_is_refused` |
| An output image without its request embedded | `test_bake::test_bake_embeds_payload_in_png_and_geotiff` |
| Neighbours or one mosaic mixing relief (`relief_key`) | `test_relief::test_chunks_of_other_relief_are_not_neighbours_or_one_mosaic` |
| A mosaic mixing two recipes, or overwriting a chunk tile another archive disagrees on | `test_mosaic::test_mosaic_refuses_a_second_recipe_a_changed_archive_and_a_file_out`, `::test_merge_refuses_a_tile_two_archives_disagree_on` |
| Bake, server OSM fetch or purge without its opt-in flag | `test_cli::test_bake_refuses_without_allow_flag`, `test_server::test_bake_stream_403_when_disabled_but_jobs_readable`, `::test_osm_403_when_disabled_and_inputs_lists_dem_dir`, `::test_purge_403_when_disabled` |
| Purge outside the jobs root, or of a job that is baking | `test_server::test_purge_stays_inside_the_jobs_dir`, `::test_purge_refuses_a_job_that_is_baking` |
| A route takes a filesystem path | `test_rules::test_no_route_accepts_a_field_nobody_has_reviewed_for_paths` (every body field and path param is listed), `::test_every_file_naming_field_is_held_to_its_root`, `test_server::test_controls_path_escape_is_400` |
| A server route for `dem` | `test_dem::test_server_has_no_dem_route` |
| A route builds labels, or place names reach the model | `test_rules::test_no_route_builds_labels`, `::test_place_names_never_reach_the_model` |
| GDAL from pip, or rasterio as a dependency | `test_rules::test_no_gdal_wheel_or_rasterio_is_a_dependency` (uv.lock and pyproject) |
| Machine names, host paths, LAN addresses or emails in `docs/`, README, CHANGELOG or `.env.example` (Studio and Realms import `docs/`) | `test_rules::test_published_docs_pass_the_opsec_gate`, plus the CI secret `OPSEC_DENYLIST` for private names |
| A geocache source read without `opencaching.allow_reuse` | `test_rules::test_every_geocache_source_goes_through_the_licence_gate` |
| Weights copied into the image, or compose writing the checkout as root | `test_rules::test_weights_are_mounted_read_only_and_never_copied_into_the_image`, `::test_compose_never_runs_as_root_on_the_mounts`. A hand-typed `docker run` as root can't be checked: don't (it leaves root-owned `__pycache__`). |
| A failed bake reported to the GPU broker as `ok`, or a yielded block re-queued on another node | `test_gpu::test_a_failed_bake_is_reported_to_the_broker_as_failed`, `::test_a_block_holds_one_lease_and_requeues_on_the_same_node_when_asked` |
| Published tiles without the Copernicus and OSM (ODbL) attribution | `test_attribution::test_tiles_and_mosaic_carry_copernicus_and_osm_attribution`, `::test_a_dem_of_unknown_origin_is_published_with_a_warning` |
| `docs/cli.md` out of step with the CLI | CI `docs` job (`make docs-check`) |
| A dependency change without `make lock` | CI `uv sync --locked` |
| A release tag without its own CHANGELOG heading | `release.yml` `github-release` job |
| An rc or alpha tag moves `:latest` | `test_rules::test_only_a_final_release_moves_latest` |
| A push to `main` that didn't come through `dev`, or `main` holding anything `dev` lacks | branch protection on `main`; CI `main-not-ahead` job |

## Layout

The module docstrings describe each module. What they don't say:

- `models.py`: **`JobSidecar` (`job.json`) is the API contract and the georeferencing authority**: the grid, every SHA256, the exact GDAL argv arrays, and the bake, tiles, chunk, recipe, labels and timings records. `ATLAS_VERSION` is its schema version.
- `provenance.py`: the commit in `generator()` is the live `git rev-parse HEAD` in a checkout, else `_build_info.COMMIT` (written by the root `hatch_build.py` hook, because hatch-vcs leaves `__commit_id__` empty), else the `+g<sha>` of a dev version. Realms compares it with its docs record, so a wheel or image must carry it.
- `gpu.py` + `gpu-tenant.yaml`: argus-iris leases. A bake holds a `batch` lease on `comfyui`; `chunks` holds one per block and yields between chunks. The figures in the two files are kept in step by `test_gpu`.
- `server/app.py`: FastAPI on **:8105** (peers: lens :8100 … proof :8104). `examples/flow.html` drives the whole flow through the API; `examples/viewer.html` only reviews.
- `templates/`: the starter ComfyUI graphs. ControlNet name and weight are hard-coded there on purpose. `--workflow <bare packaged name>` selects a packaged template; any other value is a path.
- `docs/` is imported by Studio and Realms at `dev` HEAD. `docs/cli.md` is generated (`make docs`). `docs/your-first-bake.md` uses `.env` placeholders (`<GPU_HOST>`, `$ATLAS_*_HOST`). The v0 product spec isn't committed; **What this is** and the README summarise it.
- `tests/` builds a tiny Float32 DEM with GDAL (`conftest.dem`) and paints with a `FakeBackend`. CI has no GPU and no ComfyUI.

## Commands

`make help` lists the targets. GDAL-backed tests skip when `gdalinfo` is absent; CI sets `ATLAS_TEST_REQUIRE_GDAL=1` so they fail instead. With no GDAL on the dev box, run the suite in the image against the checkout, as your uid:

```bash
docker compose --profile test run --rm test
```

`compose.yaml` also runs the CLI (`docker compose run --rm atlas <command>`, container paths under `/data`) and the API (`docker compose up -d serve`), with host paths, UID/GID and flags from `.env`. It never manages ComfyUI; argus-studio's compose stays the deployment.

## Conventions & gotchas

- **The grid comes first.** `plan_grid` plans it and `job.json` records it before generation; the GeoTIFF is checked against it. Don't read georeferencing back from the PNG, and don't resample the painted image to "make it fit".
- **Chunks tile only their crop.** A chunk's `zoom_range` is `(z, native)`. `tile_job` tiles `fantasy_chunk.tif` against `chunk.inner` and drops gdal2tiles' sub-half-pixel edge slivers (`CHUNK_SLIVER_PX`); the margin only conditions the model. Non-chunk jobs keep the strict outside-the-canvas `TilesError`.
- **Opt-in flags.** `ATLAS_ALLOW_BAKE`, `ATLAS_ALLOW_OSM` and `ATLAS_ALLOW_DELETE` gate `/bake*`, `/osm*` and `/purge*` through the shared `argus_cortex.server.WriteGuard`. `POST /controls` and `POST /tiles` stay allowed because they only need GDAL. Purge refuses a job in the server's busy set with 409 and deletes only a non-symlink dir directly under the jobs root that holds a `job.json`. Keep `/health.read_only == not allow_bake`, `/health.osm_allowed == allow_osm` and `/health.delete_allowed == allow_delete`.
- **Paths from clients are names under a root.** `dem`, `land` and `layers` resolve under `ATLAS_DEM_DIR` (`resolve_under_root`); model names resolve under the models root; job artifacts are addressed by `(job_id, kind)`. A new request field fails `test_rules` until it is reviewed.
- **Record, don't assume.** Every GDAL command goes into `sidecar.commands` as an argv array, and every model is pinned by SHA256 (`hash_model`). The bake payload is embedded in `fantasy.png` *before* its digest is taken and before stamping, so metadata never changes pixels and the gate sees the same image.
- **Placeholders are exact-match strings.** `render_workflow` substitutes only values that *are* a `$name`; ints and floats stay typed. Required: `$checkpoint $positive $negative $seed $steps $cfg $denoise $width $height $hillshade`. `$lora` and `$lora_weight` must both be present iff a LoRA is supplied. Any other `$name` is a control of the job (`$slope`, `$composite`, `$land`, `$<layer>`), uploaded only when used; a `$name` the job lacks is a `BackendError` that names it.
- **Relief is scaled before shading.** `resolve_relief` gives `z = base × vertical_exaggeration`, where `base` is 1/cos(canvas-centre latitude) for `z_factor="auto"` (the default) or the fixed factor. Hillshade gets `-z z` and slope gets `-s 1/z`; at z = 1 the argv is unchanged. `JobSidecar.relief` records it, and `None` means a pre-0.3.0 job at z 1. Recipe `z_factor`/`vertical_exaggeration` are in the digest, which omits them at the legacy 1/1.
- **gdal_rasterize never reprojects.** Its `-a_srs` only labels the output, so every vector goes through `reproject_argv` (ogr2ogr `-t_srs EPSG:3857 -clipdst`) first. It also marks 0 as NoData; masks are exported with `-a_nodata none` and the composite is built with `--hideNoData`.
- **Branches and tags.** Branch from `origin/dev`; feature PRs go to `dev`, and `main` only takes `dev` → `main` release PRs. Don't merge `main` back into `dev`. Release candidates are tagged `vX.Y.ZrcN` on the PR head and finals `vX.Y.Z` on `main` (README → Develop).
- Versioning is git-tag-derived (`hatch-vcs`); `_version.py` is generated. `structlog` logging, Pydantic v2, ruff line-length 120. Scaffolded from `argus-pkg-template` (`copier update` pulls tooling changes), but **CI is standalone**: it apt-installs `gdal-bin python3-gdal`, which the shared argus-ci workflow has no hook for.
- The image base is `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4` (Ubuntu 24.04, Python 3.12, `gdal2tiles.py` included), not `python:*-slim`.
