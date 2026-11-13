# The alignment gate

A diffusion model takes pixels in and gives pixels out. It has no notion of coordinates, so nothing it returns can be trusted to say where on Earth it belongs. `argus-atlas` never asks it to. The grid is fixed before the model runs, and the painted image is checked against that grid afterwards. The check is the **alignment gate**.

## The grid comes first

`argus-atlas controls` (or `chunk`) plans the canvas in Web Mercator (EPSG:3857). The long side is at most 1536 px and both sides are multiples of 64; a chunk's grid comes from its XYZ tile. The plan is written to `job.json` as the upper-left corner, pixel size, width and height, before any control raster exists. The hillshade, slope, masks and composite are all warped and rasterised onto that grid, so the model's inputs line up with it pixel for pixel.

The control PNGs the model sees carry no georeferencing of their own. Their SHA256s are in `job.json` and are checked again before a bake, so a control edited after the fact is refused, not silently painted.

## Stamp, then check

After the bake, `fantasy.png` is stamped with the sidecar's geotransform (`gdal_translate -a_srs EPSG:3857 -a_ullr …`) into a temporary GeoTIFF. Then `gdalinfo -json` reads that file back, and all of these must hold:

- **Size:** width and height equal the canvas in `job.json`. A wrong-size PNG is refused before a GeoTIFF is even attempted.
- **CRS:** EPSG:3857.
- **Corners:** the upper-left and lower-right corners match the sidecar within 1e-3 m.

Only then is the temporary file renamed to `fantasy.tif`.

The painted image is never resampled to make it fit. A model that returns 1023 px for a 1024 px canvas fails the gate; it is not stretched into place. Pixels decide colour only; the sidecar decides where the pixels are.

## When it fails

- `fantasy.png` is kept, so you can see what the model did.
- No `fantasy.tif` is written, and no tiles are cut.
- `job.json` records the bake with `aligned: false` and the reason, such as `fantasy.png is 1023x1024, canvas is 1024x1024`.
- The CLI exits non-zero. The bake stream ends with an `error` frame. `POST /tiles` answers 409.

## Every later step gates again

The gate is not a one-off. Every step that writes georeferenced output re-runs it against the grid it is meant to sit on:

| Step | Gated file | Grid |
|---|---|---|
| `bake` | `fantasy.tif` | the job's canvas |
| `chunk`, seam blend (only with baked neighbours, and `--feather` above 0 or `--tone`) | `fantasy_blend.tif` | the chunk's canvas, margin included |
| `chunk`, crop | `fantasy_chunk.tif` | the chunk's own XYZ tile, margin removed |
| `tiles` / `POST /tiles` | the GeoTIFF being tiled | the canvas, or the chunk's tile |

So a chunk passes the gate at least twice: once with its margin and once as the bare tile. That is why the coastlines in a mosaic run on across the seams, and why they sit on the real ones when the layer is draped over a basemap.

## Tiles never invent detail

Tiles are cut only from a gated GeoTIFF, and only down to the canvas's native zoom (the Web Mercator zoom nearest its ground resolution, clipped to 0–14). Asking for a finer zoom is an error. A tile outside the canvas is also an error, not a warning. A chunk is tiled only inside its own XYZ tile and only from its own zoom, so neighbouring chunks' archives never overlap. See [`chunks-and-mosaic.md`](https://github.com/smk762/argus-atlas/blob/main/docs/chunks-and-mosaic.md).

## Provenance travels with the pixels

The gate covers position. `job.json` and the embedded metadata cover what was painted:

- the SHA256 of the source DEM, of each control, of the checkpoint and LoRA, and of the workflow;
- the exact GDAL argv of every command that ran;
- the full bake request, embedded in `fantasy.png` (EXIF and a text chunk), `fantasy.tif` (`TIFFTAG_IMAGEDESCRIPTION`) and the PMTiles metadata.

The payload is embedded in `fantasy.png` before its digest is taken and before it is stamped, so metadata never changes pixels, and the gate checks the same image the model returned.

`argus-atlas inspect <job>` prints all of it.
