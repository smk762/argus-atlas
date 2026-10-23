# Examples

## `flow.html` — the whole flow from a browser

Drives a running `argus-atlas serve` step by step: pan the map and take the view as the AOI, fetch OSM layers (`POST /osm`, needs `ATLAS_ALLOW_OSM=1`), run controls with a DEM and layers from the server's DEM dir (`GET /inputs`), bake with the NDJSON progress frames streamed into the page (`POST /bake/stream`, needs `ATLAS_ALLOW_BAKE=1`), cut tiles and drape the result. Nothing is computed in the page; every step is the same API call the CLI makes. Pick an existing job from the dropdown to resume at any step. Proven end to end on 23 Oct 2026 (`docs/img/flow-sirmione.jpg`).

```bash
ATLAS_ALLOW_BAKE=1 ATLAS_ALLOW_OSM=1 ATLAS_CORS_ORIGINS='*' ATLAS_DEM_DIR=/data/dem ATLAS_WORKFLOW=/data/atlas-composite.json argus-atlas serve --cors
xdg-open examples/flow.html   # ?server=http://host:8105&job=<id> preloads a job
```

## `viewer.html` — drape a bake on a real basemap

A single static page (MapLibre GL + PMTiles, no build step) that lays `fantasy.pmtiles` over an OpenFreeMap basemap with an opacity slider, so you can check that coastlines and ridges line up. It is the review tool for a bake and the seed of the Phase 3 demo page.

Two ways to load a job:

- **From a running atlas server** (job picker, hillshade comparison layer). Start the server with CORS open for a `file://` page, then open the HTML in a browser:

  ```bash
  ATLAS_CORS_ORIGINS='*' argus-atlas serve --cors            # or the image, with the jobs dir mounted
  xdg-open examples/viewer.html                              # then point "Atlas server" at http://<host>:8105
  ```

  `?server=http://host:8105&job=garda-1` in the URL preloads a job.

- **From a local `fantasy.pmtiles`** via the file picker (no server; the file is range-read in the browser).

Controls: opacity slider, "painted map" / "hillshade control" toggles, hold space to peek at the basemap.

## `sample_dem.tif` (placeholder)

Before the first PyPI release, put a tiny public-domain DEM here (SRTM or similar, well under 2 MB) so the README quick start runs with no edits. See `TODO.md`, Phase 1.
