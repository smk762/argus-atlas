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

Controls: opacity slider, "painted map" / "hillshade control" / "place names" / "basemap labels" toggles, hold space to peek at the basemap. A job with labels (`argus-atlas labels --job`) gets its names drawn on top from `GET /jobs/{id}/labels`; without a server, pick a `labels.geojson` under the local-file section.

## `sample_dem.tif`

A 577x577-cell, 350 KB clip of Copernicus GLO-30 around the south end of Lake Garda (10.60–10.76 E, 45.44–45.60 N: Sirmione, the lake, the southern tip of Monte Baldo) so the README quick start runs with no edits:

```bash
argus-atlas controls --dem examples/sample_dem.tif --bbox 10.60,45.44,10.76,45.60 --out jobs/sample
```

`sample_places.geojson` is eight hand-placed towns and villages around it in OSM spelling (`name`, `place`; approximate positions). Six fall inside the canvas; Desenzano and Verona are outside, so they show the clipping:

```bash
argus-atlas labels --source examples/sample_places.geojson --job jobs/sample
```

Attribution, as the Copernicus DEM licence requires: produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
