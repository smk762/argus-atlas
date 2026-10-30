# Data sources for atlas layers and DEMs

Anything OGR can read works as a `controls --layer name=path` input, in any CRS; anything GDAL can read works as `--dem`. This page is the shortlist of where to get such files, with what each one is good for in atlas. Compiled from a thread by Mashford Mahute (X, 18 Oct 2026) plus the sources already in use here.

## In use

| Source | What | Used as |
|---|---|---|
| **Copernicus GLO-30** on AWS, `https://copernicus-dem-30m.s3.amazonaws.com/` | 30 m global DSM, 1x1° COG tiles, no login; sea at exactly 0 m | `--dem` (`docs/first-bake.md`); `--sea-level 0` derives the `sea` mask from it. Attribution required (see `examples/README.md`). |
| **OpenStreetMap via Overpass**, `https://overpass-api.de/api/interpreter` | Live OSM features for a bbox | `argus-atlas osm` / `POST /osm`: water, rivers, roads, rail, forest, urban, buildings, glaciers. Rate-limited: one call per AOI, pause between calls, reuse extracts. No ocean polygons (coastline is a line), hence the DEM sea mask. |
| **OpenFreeMap**, `https://tiles.openfreemap.org/styles/liberty` | Vector basemap, no key | The viewer and flow pages' basemap. |

## OSM without Overpass

| Source | What | For atlas |
|---|---|---|
| **Geofabrik**, `https://download.geofabrik.de` | OSM extracts by continent/country/region, PBF and shapefile, refreshed daily | Big or repeated AOIs: download a region `.osm.pbf` once and point `osm.extract_layers()` / `ogr2ogr` at it instead of hitting Overpass. GDAL's OSM driver reads PBF directly, and `labels --source region.osm.pbf` reads its `place=*` nodes. |
| **OSM** itself, `https://openstreetmap.org` | The data and its ODbL licence | Attribution for every layer that came from it. |

## Water, land and coast

| Source | What | For atlas |
|---|---|---|
| **HydroSHEDS**, `https://hydrosheds.org/products` | Rivers (HydroRIVERS), lakes (HydroLAKES), basins, with Strahler order and discharge; shapefile | Global `rivers`/`water` layers with scale filtering by stream order, more consistent than OSM in sparsely mapped areas. |
| **Natural Earth**, `https://naturalearthdata.com` | 1:10m/50m/110m land, ocean, lakes, rivers, coastline, boundaries, places; shapefile/GeoJSON, public domain | A `land` polygon for coasts (`--land`) at zooms up to ~9; too coarse for the 1024 px canvases used so far. Also `glaciers`, `urban` areas, boundaries. Populated places (`ne_10m_populated_places`) are a `labels --source`, ranked by `SCALERANK`. |
| **USGS EarthExplorer**, `https://earthexplorer.usgs.gov` | SRTM 1-arc-second, 3DEP (USA, down to 1 m), hydrography, boundaries; login | Alternative DEMs; 3DEP for US AOIs finer than 30 m. |
| **Koordinates**, `https://koordinates.com` | Marketplace of government open data: LiDAR DEMs (NZ, parts of AU), coastlines, cadastre, imagery | Fine DEMs for Australian/NZ AOIs (e.g. WA 1–5 m LiDAR where published) and local coastline/water layers. |

## Land cover and forest

| Source | What | For atlas |
|---|---|---|
| **Global Forest Watch**, `https://globalforestwatch.org` | Tree cover, loss/gain (Hansen), rasters and vectors | A `forest` layer where OSM landuse is thin; vectorise or threshold the raster first. |
| **European Environment Agency**, `https://eea.europa.eu` | CORINE land cover (Europe), Natura 2000, elevation | `forest`/`urban`/`water` classes for European AOIs from one consistent product. |
| **FAO GeoNetwork**, `https://fao.org/geonetwork` | Agriculture, soils, land cover, fisheries | Land-cover classes for non-European AOIs. |
| **WRI Open Data**, `https://wri.org/data` | Environment and natural resources (Aqueduct water risk, forests, land) | Thematic layers for themed presets. |
| **UNEP Environmental Data Explorer**, `https://uneplive.unep.org` | Environmental indicators and spatial datasets | Thematic layers. |

## Boundaries, population, infrastructure

| Source | What | For atlas |
|---|---|---|
| **GADM**, `https://gadm.org` | Administrative boundaries, levels 0–5, shapefile/GeoJSON/GPKG; free for non-commercial use | A `borders` layer (burnt as a line mask; add it to `LINE_LAYERS` or give it a palette entry) for political-map presets. |
| **DIVA-GIS**, `https://diva-gis.org` | Country packages: boundaries, roads, rail, water, population, climate | One-stop country layers where OSM is thin. |
| **HDX**, `https://data.humdata.org` | Humanitarian datasets: population, settlements, infrastructure, boundaries | Settlement points/polygons for an `urban` layer in sparsely mapped regions. |
| **NASA SEDAC**, `https://sedac.ciesin.columbia.edu` | Gridded population (GPW), infrastructure, environment | Population rasters as an `urban` proxy. |
| **Africa GeoPortal**, `https://africageoportal.com` | Boundaries, land cover, infrastructure across Africa | Regional layers for African AOIs. |

## Notes

- Licences differ: OSM is ODbL (attribution + share-alike for derived databases), Natural Earth is public domain, Copernicus DEM and GADM have their own terms. `osm.json` and `job.json` record provenance; keep the attribution with any published tileset.
- A new layer name outside `COMPOSITE_PALETTE` is burnt as a mask and reachable in a template as `$<name>`; to paint it in the composite add a colour to the palette in `controls.py`.
