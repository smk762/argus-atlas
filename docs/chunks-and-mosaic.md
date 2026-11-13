# Chunks, mosaics and place names

How the [Realms demo](https://realms.dragonhound.dev) map was made. Cook Strait is not one canvas: it is a block of 1024 px XYZ chunks. Each chunk is painted with its neighbours in view, cropped to its tile, gated twice, and mosaicked with the others into one tile set. Place names are drawn on top as a separate vector layer. This page covers each step: one chunk, a block of chunks, the mosaic, and the labels. The georeferencing checks behind them are in [`alignment-gate.md`](https://github.com/smk762/argus-atlas/blob/main/docs/alignment-gate.md). Every command and option is in [`cli.md`](https://github.com/smk762/argus-atlas/blob/main/docs/cli.md).

## One chunk

To grow a map piece by piece, bake it one XYZ tile at a time. `argus-atlas chunk --tile z/x/y --recipe <style> --dem <dem>` runs controls, seams, bake, crop and tiles in one go:

- **The grid comes from the tile id.** A chunk is painted at 256, 512 or 1024 px, so its native zoom is z, z+1 or z+2 and every tile in its pyramid is a whole XYZ tile. All chunks of one size share one pixel lattice.
- **Seams continue the neighbours.** The canvas is the tile plus a margin (default 128 px, a multiple of 32). Chunks already baked from the same recipe next to it (under `--neighbours`, by default the parent of the job dir) are pasted into the margin of a `seam_init` control. A `seam_mask` control marks those pixels as kept. The packaged `comfyui_sdxl_chunk_seam*.json` templates denoise only the unmasked area, so the model paints up to the tile edge while it sees the neighbour across it. A chunk job re-baked with `bake` or `POST /bake/stream` no longer counts as the recipe's, and a chunk of the recipe that would overlap one already baked (the same tile in another job dir, or another zoom over the same ground) is refused.
- **Match, then crop.** The mask fades over 32 px on the neighbours' side (`--mask-feather`), so there is no hard latent edge. After the bake, the chunk's colours are shifted to match the neighbours along the seam (`--tone`), and what difference is left at the seam is faded out over 64 px into the tile (`--feather`). The painted `fantasy.tif` is kept as it came from the model; the crop comes from `fantasy_blend.tif`.
- **Crop, then gate again.** After the alignment gate the margin is cut off (`fantasy_chunk.tif`) and the crop is gated against the tile's own grid. Only the crop is tiled, from zoom z to native, so the archives of neighbouring chunks never overlap.
- **A recipe is a style you can repeat.** It is a JSON file (see [`examples/recipes/antique.json`](https://github.com/smk762/argus-atlas/blob/main/examples/recipes/antique.json)) naming a preset, checkpoint, optional LoRA, workflow and a seed scheme. With `tile` seeds, each chunk's seed is a hash of the recipe name, a base and z/x/y, so baking a chunk again gives the same image. `--recipe <name>` looks up `$ATLAS_RECIPES_DIR/<name>.json`. The whole recipe and its digest go into `job.json`.
- **The workflow comes from the recipe.** `chunk` bakes with the recipe's `workflow` (unset: the packaged seam template matching its LoRA), never with `--workflow` or `ATLAS_WORKFLOW`, which name non-seam templates. Like the other packaged templates, the seam templates name the placeholder ControlNet `diffusers_xl_depth_full.safetensors`: on a host without it, copy one, edit its `ControlNetLoader` and set the recipe's `workflow` to the copy (a path relative to the recipe file).
- **Every stage is timed.** `job.json` records wall-clock seconds for controls, seams, verify, generate, georeference, crop and tiles, so a bake queue can be sized from real runs.

`--controls-only` stops after the seam controls and needs no GPU.

## A block of chunks

`argus-atlas chunks` bakes many chunks at once and handles the ordering for you:

```bash
argus-atlas chunks --tile 10/1009/641 --rings 3 --recipe antique --out maps/antique \
  --sea-level 0 --allow-missing --land land.gpkg --labels --plan   # drop --plan to bake
```

- **One fetch for the block.** The lon/lat box covering every chunk's canvas, margins included, is fetched once: the DEM with `dem --cover`, and the OSM control layers (`--osm-layers`, by default `water,rivers,roads,forest,urban`) at the chunks' native zoom. With `--labels` the fetch includes `places`, and every chunk is labelled afterwards. Both go to `--inputs` (by default `<out>/.block`) and are reused while their manifests cover the block (a smaller block inside a fetched one fetches nothing; a new layer refetches the same box with the old layers kept). Point a second recipe's run at the same `--inputs` and nothing is fetched twice. `--dem` uses your own DEM instead.
- **Seam order.** Chunks are baked north to south, then west to east, so each one finds its west and north neighbours already baked.
- **Sea skipping.** With `--land`, chunks whose own tile has no land under the polygon are skipped. `--land` is only a filter here; to burn it as a control too, pass `--layer land=<file>`.
- **Resume.** A chunk that already has this recipe's gated crop and archive, at the same size and margin, is skipped. The first failure stops the run, because later chunks would seam against a gap. Re-run the same command to continue.
- **The DEM budget.** The block's DEM fetch reads GLO-30 from `--dem-source` (or `ATLAS_DEM_SOURCE`: the public bucket, a mirror URL or a local directory) and is refused if it touches more than `--max-tiles` GLO-30 tiles (default 16). Raise it for a wide block.
- `--plan` prints the order, the sea chunks and the finished chunks without fetching or baking. `--bbox … --zoom z` picks the chunks by area instead of `--tile`/`--rings`.

## One tile set for the region

Each chunk has its own `fantasy.pmtiles`. `argus-atlas mosaic <style-dir> --out <dir | file.pmtiles>` combines every baked chunk of a style into one tile set a viewer can load:

- Chunk tiles are copied unchanged. A tile that two archives both carry with different pixels is an error naming both jobs.
- Overview zooms below the chunk zoom, down to `--overview-min` (default 5), are cut by `gdal2tiles` from a VRT of the chunks' gated crops, so the painted map stays visible when you zoom out. Gaps between chunks (skipped sea) stay transparent.
- Every chunk's `labels.geojson` is merged into one, with a name at the same point kept once. It goes to `labels.geojson` in the directory, or `<stem>.labels.geojson` beside a `.pmtiles`.
- `mosaic.json` (or `<stem>.json`) records the zoom range, bounds, tile count, each source chunk's job id, tile and archive SHA256, and the GDAL argv. Writing to a directory replaces an earlier mosaic there, and refuses a directory that holds anything else.

## Place names

The prompts forbid text, because diffusion paints illegible lettering and breaks tile seams, so the bake has no names. `argus-atlas labels` adds them as a vector overlay the viewer draws on top, in a real font. Names never reach the model, `fantasy.tif` or the PMTiles.

```bash
# Label a job: the box is its canvas (a chunk's tile), the cutoff its native zoom.
argus-atlas labels --source ne_10m_populated_places.shp --job jobs/sample
# Or any box, for use outside a job.
argus-atlas labels --source places.osm.pbf --bbox 10.60,45.44,10.76,45.60 --zoom 13 --out labels.geojson
```

The source is a file you already have: Natural Earth populated places (ranked by `SCALERANK` <= zoom − 2), a Geofabrik `.osm.pbf` or `.osm` (by `place` class: cities from zoom 0, towns from 8, villages from 11, suburbs and hamlets from 13, the rest from 15), or the `places` layer of `argus-atlas osm`. Points outside the box or below the cutoff are dropped. The output has `name`, `rank` and `kind` per point, in EPSG:4326. `labels.json` beside it records the source and its SHA256, the cutoff and the ogr2ogr argv; with `--job`, `job.json` gains a `labels` record and `GET /jobs/{id}/labels` serves the file. No route builds labels. `examples/viewer.html` draws them over the raster, so they stay put while the opacity slider fades the bake. [`examples/sample_places.geojson`](https://github.com/smk762/argus-atlas/blob/main/examples/sample_places.geojson) labels the sample DEM.
