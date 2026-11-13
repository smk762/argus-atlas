# argus-atlas CLI reference

Bake a georeferenced fantasy-map overlay from a DEM: GDAL controls -&gt; ComfyUI -&gt; GeoTIFF -&gt; PMTiles.

**Usage**:

```console
$ argus-atlas [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `-v, --verbose`: Show info/debug logs
* `--install-completion`: Install completion for the current shell.
* `--show-completion`: Show completion for the current shell, to copy it or customize the installation.
* `--help`: Show this message and exit.

**Commands**:

* `doctor`: Report GDAL, the tiler, the PMTiles...
* `controls`: Warp the DEM to EPSG:3857, write...
* `dem`: Clip Copernicus GLO-30 to an AOI into one...
* `osm`: Fetch OSM features for an AOI from...
* `presets`: List the style presets `bake --preset`...
* `bake`: Paint the controls through ComfyUI and...
* `tiles`: Cut XYZ tiles from the aligned GeoTIFF...
* `chunk`: One chunk end to end: controls -&gt; seams...
* `chunks`: A block of chunks: one DEM and OSM fetch,...
* `mosaic`: Lay every chunk of a style into one tile...
* `labels`: Place names for the drape: rank and clip a...
* `inspect`: Summarise a job&#x27;s sidecar: grid, controls,...
* `serve`: Start the argus-atlas micro-server...

## `argus-atlas doctor`

Report GDAL, the tiler, the PMTiles packer, the backend and the bake flag. Exits 1 if GDAL is missing.

**Usage**:

```console
$ argus-atlas doctor [OPTIONS]
```

**Options**:

* `--json`: Machine-readable report
* `--help`: Show this message and exit.

## `argus-atlas controls`

Warp the DEM to EPSG:3857, write hillshade/slope(/layers/composite) controls and job.json. No GPU.

**Usage**:

```console
$ argus-atlas controls [OPTIONS]
```

**Options**:

* `--dem <path>`: Source DEM GeoTIFF (local path)  [required]
* `--bbox <str>`: AOI as west,south,east,north in EPSG:4326 degrees  [required]
* `-o, --out <path>`: Job directory to create (its name is the job id)  [required]
* `--land <path>`: Land polygon file (any OGR format): the layer named &#x27;land&#x27;
* `--layer <str>`: name=path: burn any OGR vector file as a 0/255 mask control (repeatable); water/rivers/roads/rail/forest/urban/buildings/glaciers also paint the composite
* `--sea-level <float>`: Metres: add a &#x27;sea&#x27; mask of DEM cells at or below this (0 for Copernicus coasts)
* `--composite / --no-composite`: Paint composite.png from the known layers  [default: composite]
* `--resampling <str>`: gdalwarp method (bilinear|cubic|cubicspline|lanczos|average); default: auto by upsampling ratio
* `--long-side <int>`: Canvas long side in px (default 1024, cap 1536, multiple of 64)
* `--resolution <float>`: Explicit metres per pixel instead of --long-side
* `--z-factor <str>`: Height scale for hillshade and slope: &#x27;auto&#x27; corrects Web Mercator&#x27;s 1/cos(latitude) stretch at the canvas centre; a number is used as is (&#x27;off&#x27; = 1, the uncorrected relief before 0.3.0)  [default: auto]
* `--vertical-exaggeration, --ve <float range>`: Vertical exaggeration on top of --z-factor: final z = z factor x this  [default: 1.0; x&gt;=0.0]
* `--help`: Show this message and exit.

## `argus-atlas dem`

Clip Copernicus GLO-30 to an AOI into one GeoTIFF for `controls --dem`. Needs the network (or a mirror).

**Usage**:

```console
$ argus-atlas dem [OPTIONS]
```

**Options**:

* `--bbox <str>`: AOI as west,south,east,north in EPSG:4326 degrees
* `-t, --tile <str>`: Instead of --bbox: the canvas of chunk z/x/y (with --size/--margin); implies --cover
* `--size <int>`: With --tile: the chunk&#x27;s tile side in px, as for `chunk` (1024)
* `--margin <int>`: With --tile: the chunk&#x27;s margin in px, as for `chunk` (128)
* `-o, --out <path>`: GeoTIFF to write (provenance in &lt;out&gt;.json beside it)  [required]
* `--source <str>`: GLO-30 bucket URL, mirror or local dir of &lt;key&gt;/&lt;key&gt;.tif (default $ATLAS_DEM_SOURCE)
* `--allow-missing`: Fill tiles the source lacks (all-ocean ones) with 0 m instead of failing
* `--cover`: Snap the window outward so the output contains the whole bbox (default: inside it)
* `--max-tiles <int range>`: Refuse an AOI touching more GLO-30 tiles than this  [default: 16; x&gt;=1]
* `--help`: Show this message and exit.

## `argus-atlas osm`

Fetch OSM features for an AOI from Overpass into per-layer GeoJSON for `controls --layer`. Needs the network.

**Usage**:

```console
$ argus-atlas osm [OPTIONS]
```

**Options**:

* `--bbox <str>`: AOI as west,south,east,north in EPSG:4326 degrees  [required]
* `-o, --out <path>`: Directory for extract.osm, &lt;layer&gt;.geojson and osm.json  [required]
* `--layers <str>`: Comma-separated OSM layer presets  [default: water,roads,forest,urban]
* `-z, --zoom <int range>`: Target zoom (the job&#x27;s native zoom) for scale rules  [default: 11; 0&lt;=x&lt;=19]
* `--overpass-url <str>`: Overpass endpoint (default $ATLAS_OVERPASS_URL)
* `--help`: Show this message and exit.

## `argus-atlas presets`

List the style presets `bake --preset` accepts.

**Usage**:

```console
$ argus-atlas presets [OPTIONS]
```

**Options**:

* `--json`: Machine-readable
* `--help`: Show this message and exit.

## `argus-atlas bake`

Paint the controls through ComfyUI and stamp the result to fantasy.tif. Refuses unless ATLAS_ALLOW_BAKE=1.

**Usage**:

```console
$ argus-atlas bake [OPTIONS] {job}
```

**Arguments**:

* `job`: Job directory written by `controls`  [required]

**Options**:

* `-c, --checkpoint <str>`: Base checkpoint, named as ComfyUI loads it  [required]
* `--lora <str>`: Cartography LoRA, named as ComfyUI loads it
* `--lora-weight <float>`: [default: 0.8]
* `-o, --out <path>`: Bake into this job dir instead (controls are copied there)
* `--preset <str>`: Style preset (see `argus-atlas presets`); explicit flags override
* `--prompt <str>`
* `--negative <str>`
* `--seed <int>`: Random if omitted
* `--steps <int range>`: [x&gt;=1]
* `--cfg <float>`
* `--denoise <float range>`: img2img strength  [0.0&lt;=x&lt;=1.0]
* `--models-dir <path>`: Where checkpoints/LoRAs are hashed from (default $ATLAS_MODELS_DIR)
* `--workflow <path>`: ComfyUI API-format template (default $ATLAS_WORKFLOW or packaged)
* `--comfyui-url <str>`: Default $COMFYUI_BASE_URL
* `--help`: Show this message and exit.

## `argus-atlas tiles`

Cut XYZ tiles from the aligned GeoTIFF (native-2..native; a chunk&#x27;s own zoom..native) and pack them as PMTiles.

**Usage**:

```console
$ argus-atlas tiles [OPTIONS] {source}
```

**Arguments**:

* `source`: A job dir, or its fantasy.tif  [required]

**Options**:

* `-o, --out <path>`: PMTiles path (default &lt;job&gt;/fantasy.pmtiles)
* `--min-zoom <int range>`: [x&gt;=0]
* `--max-zoom <int range>`: Never finer than the canvas&#x27;s native zoom  [x&gt;=0]
* `--help`: Show this message and exit.

## `argus-atlas chunk`

One chunk end to end: controls -&gt; seams from baked neighbours -&gt; bake -&gt; blend -&gt; crop -&gt; tiles, by a recipe.

**Usage**:

```console
$ argus-atlas chunk [OPTIONS]
```

**Options**:

* `-t, --tile <str>`: XYZ tile z/x/y to bake as one chunk  [required]
* `-r, --recipe <str>`: Recipe file, or a name under $ATLAS_RECIPES_DIR  [required]
* `--dem <path>`: Source DEM (GeoTIFF or VRT) covering the tile and its margin  [required]
* `-o, --out <path>`: Job directory (default $ATLAS_JOBS_DIR/&lt;recipe&gt;-&lt;z&gt;-&lt;x&gt;-&lt;y&gt;)
* `--neighbours <path>`: Where the recipe&#x27;s other chunk jobs live (default: the parent of --out)
* `--size <int>`: Tile side in px: 256, 512 or 1024 (native zoom = z + log2(size/256))  [default: 1024]
* `--margin <int>`: Px of neighbour context on every side, a multiple of 32  [default: 128]
* `--mask-feather <int>`: Px the seam mask fades over on the neighbours&#x27; side, up to the margin (0 = hard)  [default: 32]
* `--feather <int>`: Px into the tile the remaining seam difference fades over, up to half the tile (0 = off)  [default: 64]
* `--tone / --no-tone`: Shift the chunk&#x27;s colours to match its neighbours  [default: tone]
* `--land <path>`: Land polygon file (any OGR format): the layer named &#x27;land&#x27;
* `--layer <str>`: name=path vector layer, as for `controls` (repeatable)
* `--sea-level <float>`: Metres: add a &#x27;sea&#x27; mask of DEM cells at or below this
* `--composite / --no-composite`: Paint composite.png from the known layers  [default: composite]
* `--resampling <str>`: gdalwarp method; default: auto by upsampling ratio
* `--controls-only`: Stop after the controls and seam controls (no GPU, no ATLAS_ALLOW_BAKE)
* `--models-dir <path>`: Default $ATLAS_MODELS_DIR
* `--comfyui-url <str>`: Default $COMFYUI_BASE_URL
* `--z-factor <str>`: Height scale for hillshade and slope: &#x27;auto&#x27; corrects Web Mercator&#x27;s 1/cos(latitude) stretch at the canvas centre; a number is used as is (&#x27;off&#x27; = 1, the uncorrected relief before 0.3.0); default: the recipe&#x27;s (auto)
* `--vertical-exaggeration, --ve <float range>`: Vertical exaggeration on top of --z-factor: final z = z factor x this; default: the recipe&#x27;s (1)  [x&gt;=0.0]
* `--help`: Show this message and exit.

## `argus-atlas chunks`

A block of chunks: one DEM and OSM fetch, seam order, sea and finished chunks skipped. Re-run to resume.

**Usage**:

```console
$ argus-atlas chunks [OPTIONS]
```

**Options**:

* `-t, --tile <str>`: Centre tile z/x/y of the block (with --rings)
* `--rings <int range>`: With --tile: rings of chunks around it (1 = 3x3, 3 = 7x7)  [default: 0; x&gt;=0]
* `--bbox <str>`: Instead of --tile: every chunk at --zoom touching this box
* `-z, --zoom <int range>`: With --bbox: the chunk zoom  [x&gt;=0]
* `-r, --recipe <str>`: Recipe file, or a name under $ATLAS_RECIPES_DIR  [required]
* `-o, --out <path>`: Style dir holding the chunk jobs (default $ATLAS_JOBS_DIR/&lt;recipe&gt;)
* `--inputs <path>`: Where the block&#x27;s DEM and OSM are fetched and reused (default &lt;out&gt;/.block)
* `--dem <path>`: Use this DEM (GeoTIFF/VRT covering the block) instead of fetching
* `--dem-source <str>`: GLO-30 source for the fetch (default $ATLAS_DEM_SOURCE)
* `--allow-missing`: Fill absent (all-ocean) GLO-30 tiles with 0 m
* `--max-tiles <int range>`: GLO-30 tile budget for the block&#x27;s DEM fetch  [default: 16; x&gt;=1]
* `--osm-layers <str>`: OSM layer presets fetched once for the block and burnt as controls (&#x27;&#x27; = none)  [default: water,rivers,roads,forest,urban]
* `--layer <str>`: Extra name=path vector layer, as for `chunk` (repeatable)
* `--land <path>`: Land polygon file: skip chunks with no land in their tile (not burnt as a control)
* `--sea-level <float>`: Metres: add a &#x27;sea&#x27; mask, as for `chunk`
* `--labels`: Fetch OSM places with the block and label every chunk
* `--size <int>`: Tile side in px: 256, 512 or 1024  [default: 1024]
* `--margin <int>`: Px of neighbour context on every side, a multiple of 32  [default: 128]
* `--plan`: Print the plan (order, sea chunks, done chunks) and stop
* `--controls-only`: Stop each chunk after its controls (no GPU, no ATLAS_ALLOW_BAKE)
* `--models-dir <path>`: Default $ATLAS_MODELS_DIR
* `--comfyui-url <str>`: Default $COMFYUI_BASE_URL
* `--z-factor <str>`: Height scale for hillshade and slope: &#x27;auto&#x27; corrects Web Mercator&#x27;s 1/cos(latitude) stretch at the canvas centre; a number is used as is (&#x27;off&#x27; = 1, the uncorrected relief before 0.3.0); default: the recipe&#x27;s (auto)
* `--vertical-exaggeration, --ve <float range>`: Vertical exaggeration on top of --z-factor: final z = z factor x this; default: the recipe&#x27;s (1)  [x&gt;=0.0]
* `--help`: Show this message and exit.

## `argus-atlas mosaic`

Lay every chunk of a style into one tile set, with overview zooms and merged labels.

**Usage**:

```console
$ argus-atlas mosaic [OPTIONS] {style_dir}
```

**Arguments**:

* `style_dir`: Style dir holding the chunk jobs of one recipe  [required]

**Options**:

* `-o, --out <path>`: XYZ directory, or a .pmtiles file  [required]
* `--overview-min <int range>`: Coarsest overview zoom built below the chunk zoom  [default: 5; 0&lt;=x&lt;=14]
* `--help`: Show this message and exit.

## `argus-atlas labels`

Place names for the drape: rank and clip a local places file to GeoJSON. The model never sees them.

**Usage**:

```console
$ argus-atlas labels [OPTIONS]
```

**Options**:

* `-s, --source <path>`: Places file: Natural Earth populated places, an OSM .osm/.pbf, or an osm places layer  [required]
* `--job <path>`: Job dir: label its canvas at its native zoom into labels.geojson, recorded in job.json
* `--bbox <str>`: Without --job: west,south,east,north in EPSG:4326 degrees
* `-z, --zoom <int range>`: Rank cutoff zoom (default: the job&#x27;s native)  [0&lt;=x&lt;=19]
* `-o, --out <path>`: Without --job: the .geojson to write (labels.json beside it)
* `--source-layer <str>`: Layer of a multi-layer source (default: first)
* `--help`: Show this message and exit.

## `argus-atlas inspect`

Summarise a job&#x27;s sidecar: grid, controls, bake and tiles state.

**Usage**:

```console
$ argus-atlas inspect [OPTIONS] {job}
```

**Arguments**:

* `job`: A job dir (or a file inside it)  [required]

**Options**:

* `--json`: Print the sidecar as JSON
* `--help`: Show this message and exit.

## `argus-atlas serve`

Start the argus-atlas micro-server (FastAPI) on :8105.

**Usage**:

```console
$ argus-atlas serve [OPTIONS]
```

**Options**:

* `-p, --port <int>`: [default: 8105]
* `--host <str>`: [default: 0.0.0.0]
* `--cors`: Enable CORS for $ATLAS_CORS_ORIGINS (default http://localhost:3000)
* `--allow-bake / --no-allow-bake`: Enable POST /bake/stream (default: $ATLAS_ALLOW_BAKE)
* `--help`: Show this message and exit.
