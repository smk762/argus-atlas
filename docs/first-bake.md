# First real bake — runbook for the GPU host (morpheus)

State on 17 Oct 2026, written on the laptop before moving development to the GPU host, then amended the same day after the first bake ran there. Everything below was checked against the live ComfyUI; re-check anything that looks stale. **What actually happened** at the end records the commands that produced `garda-1`.

## What is already there

| Thing | Where | Notes |
|---|---|---|
| ComfyUI 0.19.3 | `http://192.168.1.39:8188`, container `voluptas_comfyui` (`ghcr.io/radiatingreverberations/comfyui-extensions:v0.19.3`) on morpheus | RTX 3090, 24 GB. Started with `--listen`, so it is reachable from the LAN and from other containers on the host. **A watcher container (`voluptas_comfyui_cleanup`) stops it after 30 min idle and clears ComfyUI's `/output` and `/temp` every 15 min**: `docker start voluptas_comfyui` before a bake if `doctor` says unreachable, and never rely on ComfyUI's output dir — atlas's copy in the job dir is the one that survives. |
| Model tree | `/srv/imogen/models` (main), `/srv/imogen/fast/models-hot` (extra tree, `extra_model_paths.yaml`) | Owned by `smk`. 39 GB free on `/`. Mounted into ComfyUI as `/comfyui/models` and `/comfyui/extra_models/models-hot`. |
| SDXL checkpoints | `checkpoints/sdxl/...` | e.g. `sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors`, `sdxl/epicrealismXL_pureFix/...`. **Juggernaut lives in the hot tree** (`/srv/imogen/fast/models-hot/checkpoints/sdxl/...`), not under `/srv/imogen/models` — ComfyUI merges both, atlas does not. No `sd_xl_base_1.0` (a 6.9 GB copy exists on the laptop in `argus-seed/models/` if wanted). |
| ControlNets | `controlnet/openpose-sdxl/`, `controlnet/controlnet-union-sdxl/` | The union promax model was downloaded on 17 Oct (2.5 GB); ComfyUI listed it without a restart. |
| LoRAs | many, none for cartography | Use the packaged no-LoRA template for the first bake. |
| Nodes | all eleven the templates use are present | `ControlNetApplyAdvanced` also accepts an optional `vae` input; the templates don't use it. |

argus-atlas pins the checkpoint and LoRA by SHA256, so `ATLAS_MODELS_DIR` must see the **same files ComfyUI loads**. The resolver takes one root, so point it at the tree that holds the checkpoint you name: `/srv/imogen/fast/models-hot` for Juggernaut, `/srv/imogen/models` for the checkpoints that live there. Model names are given exactly as ComfyUI lists them, relative to `checkpoints/` or `loras/` (the resolver tries the name as a path under the root, then under those subdirs).

## Downloads (the operator's job; atlas never downloads)

**As `smk` on morpheus there is no passwordless sudo**: `/srv/argus` cannot be created and `gdal-bin` cannot be apt-installed. Use `/home/smk/argus` wherever this page says `/srv/argus`, and run every atlas command from the image (see *Or from the image*). `/srv/imogen/models` is owned by `smk`, so the ControlNet goes where the page says.

1. **A depth-capable SDXL ControlNet.** Recommended: xinsir's controlnet-union SDXL (promax variant, ~2.5 GB, handles depth, canny and tile):

   ```bash
   mkdir -p /srv/imogen/models/controlnet/controlnet-union-sdxl
   cd /srv/imogen/models/controlnet/controlnet-union-sdxl
   curl -L -o diffusion_pytorch_model_promax.safetensors \
     https://huggingface.co/xinsir/controlnet-union-sdxl-1.0/resolve/main/diffusion_pytorch_model_promax.safetensors
   ```

   ComfyUI then lists it as `controlnet-union-sdxl/diffusion_pytorch_model_promax.safetensors`. Lighter alternative: `diffusers/controlnet-depth-sdxl-1.0` (`diffusion_pytorch_model.fp16.safetensors`, ~1.2 GB).

2. **A DEM.** Copernicus GLO-30 is public on AWS, no login. The N45 E010 tile covers Lake Garda and the Alps — strong relief plus a shoreline, which is what makes "it lines up" visible:

   ```bash
   mkdir -p /srv/argus/dem && cd /srv/argus/dem
   curl -LO https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N45_00_E010_00_DEM/Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif
   ```

   Suggested AOI: `--bbox 10.55,45.40,10.95,45.90`. At the default 1024 canvas this plans **576x1024 px at 77.8 m/px, native zoom 11, archive zooms 9–11** (the box is taller than wide in Web Mercator, so the long side is the height).

3. **Optional: a land polygon** (`--land`) for the land mask. Not needed for the first bake. Any OGR-readable file works (GeoJSON, GPKG). Natural Earth 10m land is a quick source if wanted.

## Point the template at the ControlNet

Both packaged templates name `diffusers_xl_depth_full.safetensors`, which does not exist on morpheus. Copy one and edit the `ControlNetLoader` node:

```bash
python -c "import argus_atlas.backends.workflow as w, json; print(json.dumps(w.example_template(w.EXAMPLE_TEMPLATE_NOLORA), indent=2))" > /srv/argus/atlas-nolora.json
sed -i 's|diffusers_xl_depth_full.safetensors|controlnet-union-sdxl/diffusion_pytorch_model_promax.safetensors|' /srv/argus/atlas-nolora.json
```

Then `--workflow /srv/argus/atlas-nolora.json` (or `ATLAS_WORKFLOW=`). The `strength` (0.65) and `end_percent` (0.8) in that node are the ControlNet knobs; they live in the template on purpose.

`chunk` reads neither `--workflow` nor `ATLAS_WORKFLOW` (here they name non-seam templates); it bakes with the recipe's `workflow`. Patch the seam template the same way and name the copy in the recipe, as a path relative to the recipe file:

```bash
python -c "import argus_atlas.backends.workflow as w, json; print(json.dumps(w.example_template(w.EXAMPLE_TEMPLATE_SEAM_NOLORA), indent=2))" > /srv/argus/recipes/atlas-seam-nolora.json
sed -i 's|diffusers_xl_depth_full.safetensors|controlnet-union-sdxl/diffusion_pytorch_model_promax.safetensors|' /srv/argus/recipes/atlas-seam-nolora.json
# then in /srv/argus/recipes/antique.json (mounted as ATLAS_RECIPES_DIR): "workflow": "atlas-seam-nolora.json"
```

## Run it (CLI, no studio)

```bash
git clone https://github.com/smk762/argus-atlas && cd argus-atlas
make install            # uv venv + [dev,server,cli,tiles]; needs gdal-bin + python3-gdal on the host
uv run argus-atlas doctor   # or run everything from the image if GDAL isn't installed there (see below)

# 1. Controls (GDAL only)
uv run argus-atlas controls \
  --dem /srv/argus/dem/Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif \
  --bbox 10.55,45.40,10.95,45.90 \
  --out /srv/argus/atlas-jobs/garda-1
# open /srv/argus/atlas-jobs/garda-1/hillshade.png and check the relief before spending a bake

# 2. Bake (ComfyUI on the same host)
ATLAS_ALLOW_BAKE=1 COMFYUI_BASE_URL=http://127.0.0.1:8188 uv run argus-atlas bake /srv/argus/atlas-jobs/garda-1 \
  --checkpoint sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors \
  --models-dir /srv/imogen/fast/models-hot \
  --workflow /srv/argus/atlas-nolora.json \
  --seed 7
# the output must pass the alignment gate: fantasy.tif only appears if size, SRS and corners match job.json

# 3. Tiles
uv run argus-atlas tiles /srv/argus/atlas-jobs/garda-1
uv run argus-atlas inspect /srv/argus/atlas-jobs/garda-1
```

Variant bakes without redoing controls: add `--out /srv/argus/atlas-jobs/garda-1-s9 --seed 9` (controls are copied).

### Or from the image

```bash
docker build -t argus-atlas:dev .
docker run --rm -it \
  -e ATLAS_ALLOW_BAKE=1 -e COMFYUI_BASE_URL=http://host.docker.internal:8188 \
  --add-host host.docker.internal:host-gateway \
  -v /srv/argus/dem:/data/dem:ro -v /srv/imogen/fast/models-hot:/data/models:ro -v /srv/argus/out:/data/out \
  -v /srv/argus/atlas-nolora.json:/data/atlas-nolora.json:ro \
  argus-atlas:dev bash
# inside: argus-atlas controls --dem /data/dem/<tile>.tif --bbox ... --out /data/out/atlas/garda-1 ; argus-atlas bake ... --workflow /data/atlas-nolora.json
```

For studio: `COMFYUI_BASE_URL=http://host.docker.internal:8188`, `DEM_DIR=/srv/argus/dem`, `MODELS_DIR=/srv/imogen/models`, `ATLAS_ALLOW_BAKE=1` in the studio `.env`, then `docker compose -f compose.yaml -f compose.build.yaml --profile atlas up --build` with `../argus-atlas` checked out (the GHCR image exists only after the first tag). Studio PR: smk762/argus-studio#89.

## What to look at afterwards

- `fantasy.tif` exists and `inspect` shows `aligned=True` with the checkpoint SHA256.
- Drape `fantasy.pmtiles` on a basemap (MapLibre + pmtiles protocol) and slide opacity: coastline and ridges must line up. That is the hero asset and the moment the handoff's resume line becomes true.
- If relief is washed out, raise ControlNet `strength` or lower `--denoise`; if the output ignores the prompt, do the opposite. The hillshade `-z` factor in `gdal_ops.hillshade_argv` is the knob if Web Mercator stretch at 45° flattens the shading.

## What actually happened (17 Oct 2026, morpheus)

First bake ran end to end from the image; nothing in the code needed changing. Paths differ from the plan above because of the no-sudo note: DEM in `/home/smk/argus/dem`, jobs in `/home/smk/argus/atlas-jobs`, template at `/home/smk/argus/atlas-nolora.json`. Host venv (`make install`) works but `doctor` reports GDAL missing, so it is only useful for rendering the template and running non-GDAL tests.

```bash
docker build -t argus-atlas:dev --build-arg EXTRAS="server,cli,tiles,dev" .

# controls
docker run --rm -u $(id -u):$(id -g) \
  -v /home/smk/argus/dem:/data/dem:ro -v /home/smk/argus/atlas-jobs:/data/out/atlas \
  argus-atlas:dev argus-atlas controls \
  --dem /data/dem/Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif --bbox 10.55,45.40,10.95,45.90 \
  --out /data/out/atlas/garda-1

# bake
docker run --rm -u $(id -u):$(id -g) \
  -e ATLAS_ALLOW_BAKE=1 -e COMFYUI_BASE_URL=http://host.docker.internal:8188 \
  --add-host host.docker.internal:host-gateway \
  -v /home/smk/argus/dem:/data/dem:ro -v /srv/imogen/fast/models-hot:/data/models:ro \
  -v /home/smk/argus/atlas-jobs:/data/out/atlas \
  -v /home/smk/argus/atlas-nolora.json:/data/atlas-nolora.json:ro \
  argus-atlas:dev argus-atlas bake /data/out/atlas/garda-1 \
  --checkpoint sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors \
  --models-dir /data/models --workflow /data/atlas-nolora.json --seed 7

# tiles + inspect
docker run --rm -u $(id -u):$(id -g) -v /home/smk/argus/atlas-jobs:/data/out/atlas \
  argus-atlas:dev sh -c "argus-atlas tiles /data/out/atlas/garda-1 && argus-atlas inspect /data/out/atlas/garda-1"
```

Result: `garda-1` is 576x1024 at 77.758 m/px, native zoom 11; `inspect` shows `aligned=True`, checkpoint SHA256 `dd08fa32…`, `fantasy.pmtiles` with 23 tiles at zoom 9–11. The `-u $(id -u)` keeps the job dir owned by `smk`; without it the container writes as root.

Of the 537 s on ComfyUI's queue, the 30 sampling steps took 5 s (6.7 it/s); the rest was loading the checkpoint and ControlNet (`--cache-none`, and the host had ~3 GB RAM free).

Visually (no LoRA, union ControlNet at strength 0.65, denoise 0.55, seed 7): the lake and the Monte Baldo / Alpine ridgelines follow the hillshade, so the geometry is there. A compass rose and small illegible labels appeared despite the negative prompt, and the plain south is read as a drawn plain rather than left blank. Those are prompt/LoRA matters, not alignment ones. The drape-on-basemap check still needs a browser; it is not done yet.

## Next: a cartography LoRA

No public map LoRA exists on Hugging Face (the hub search finds two, one with seven downloads); Civitai has several SDXL ones ("Fantasy Map", "LargeFantasyCityMap", "OgMapTerrainPatch"). **Civitai is region-blocked from morpheus**, so fetch elsewhere and rsync:

```bash
# on a machine that can reach civitai.com; --list first to see name/base/licence/trigger words
python3 -I scripts/fetch_civitai_lora.py --list 382959
CIVITAI_TOKEN=... python3 -I scripts/fetch_civitai_lora.py --out ./models 382959
rsync -av ./models/loras/ morpheus:/srv/imogen/fast/models-hot/loras/
```

Put LoRAs in the **same tree as the checkpoint** (`models-hot`): the resolver takes one `--models-dir`, so a Juggernaut bake cannot hash a LoRA that lives under `/srv/imogen/models`. The LoRA-capable template with the union ControlNet is already rendered at `/home/smk/argus/atlas-lora.json`; bake with `--workflow /data/atlas-lora.json --lora sdxl/cartography/<slug>/<file>.safetensors --lora-weight 0.8` and put the LoRA's trigger word (from the `.civitai.json` sidecar) in `--prompt`.

### LoRA variants (17 Oct, later the same day)

Three Civitai LoRAs were fetched on wombat and rsynced into `models-hot/loras/sdxl/cartography/` (Fantasy Map "Heavy", LargeFantasyCityMap, Og VTT Terrain Patch). Fantasy Map is the only region-map style; the other two are top-down city/battle-map LoRAs. Two variants of `garda-1` at the same seed, via `--out`, so controls were copied rather than recomputed:

```bash
# same docker run as the bake above, plus -v /home/smk/argus/atlas-lora.json:/data/atlas-lora.json:ro
argus-atlas bake /data/out/atlas/garda-1 --out /data/out/atlas/garda-1-fm08 \
  --checkpoint sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors \
  --lora "sdxl/cartography/Fantasy_Map/fantasy map-heavy.safetensors" --lora-weight 0.8 \
  --models-dir /data/models --workflow /data/atlas-lora.json --seed 7
# and --out garda-1-fm05 --lora-weight 0.5
```

Both aligned, both tiled (23 tiles, z9–11); the LoRA SHA256 in `job.json` matches the `.civitai.json` sidecar. With ComfyUI warm the two bakes plus tiling took 62 s in total, so the 537 s of the first bake was a cold page cache, not a per-bake cost.

What the three look like at seed 7 (judge them draped in the viewer, not as PNGs):

- **no LoRA**: cleanest relief, lake reads as a flat basin, stray compass rose and label scribbles.
- **Fantasy Map @ 0.8**: strongest "illustrated atlas" look (villages, trees, figures, a big "N"), but the shoreline softens and the north arm of the lake is partly painted as land — the LoRA is fighting the ControlNet.
- **Fantasy Map @ 0.5**: keeps the 0.8 style with the shoreline and ridges as crisp as the no-LoRA bake. Best starting point.

In all three the lake is the same parchment tone as the plain, because nothing tells the model where water is. The next knob is the land mask: `controls --land <Natural Earth or OSM water polygons>` gives the template a `$land` control to drive water colour.

### Style and checkpoint sweeps (17 Oct)

All at seed 7 on the `garda-1` controls via `--out`, so the geometry is identical and only the painting changes. Contact sheets: [`img/garda-1-styles.jpg`](img/garda-1-styles.jpg), [`img/garda-1-checkpoints.jpg`](img/garda-1-checkpoints.jpg). With ComfyUI warm every bake + tiles took 20–45 s, including cold checkpoint loads from `models-hot`.

**Seven prompts, Juggernaut, no LoRA, denoise 0.55** (`garda-1-imhof`, `-engraving`, `-portolan`, `-tolkien`, `-watercolor`, `-isometric`, `-contour`): the prompt changes palette and texture, not structure — at 0.55 the hillshade dominates. Imhof (muted greens, pale blue cast) is the most atlas-like; Tolkien gives a clean monochrome pen-and-ink; engraving is the base look with fewer stray symbols; portolan picks up red rhumb lines and a compass rose but is messy; watercolor has the strongest palette and invents streams in the plain; isometric reads as a 3D render; contour is only a blue tint, no contour lines.

**Seven other SDXL checkpoints, default prompt** (`garda-1-ck-*`; `illustrious/` is a diffusers folder that `CheckpointLoaderSimple` cannot load): the realism checkpoints (aureanuance, bigLove, cyberrealistic, epicrealism, eventHorizon, realityBound) all give the same monochrome hillshade-on-paper look. `makeArtNotGoon_v8` is the outlier — warm parchment, compass rose, trees, varied linework, the most "fantasy map" of the set — with eventHorizon second. Checkpoint matters less than prompt, which matters less than denoise.

In every bake the lake stays the parchment tone and the Verona-area lowland gets a radial "star fort": the model has no idea where water or towns are. Land mask next.

**Denoise 0.55 / 0.7 / 0.85 for Imhof, Tolkien and watercolor** ([`img/garda-1-denoise.jpg`](img/garda-1-denoise.jpg), jobs `-d7` / `-d85`): this is the knob that matters. At 0.85 Imhof paints the lake teal and the lowland green on its own — the model infers water from the flat basin — and Tolkien becomes a real pen-and-ink with profile mountains, hatched hills and forests on cream paper. Ridges and the shoreline hold through 0.85 for both. Watercolor at 0.85 invents a river across the southern lake, so at that denoise the ControlNet (strength 0.65, end 0.8) no longer pins the flat areas; raise `strength`/`end_percent` in the template or add the land mask before going that high. The alignment gate is a grid check and passes all of these; feature fidelity is a visual check in the viewer.

**Themed styles** ([`img/garda-1-themes.jpg`](img/garda-1-themes.jpg); sci-fi, cyberpunk, theme park, 16-bit pixel, Mars at denoise 0.55 and 0.7, with `parchment, paper, sepia, antique` added to the negative) and **Minecraft** ([`img/garda-1-minecraft.jpg`](img/garda-1-minecraft.jpg); voxel terrain and in-game map item at 0.7 and 0.85): at 0.7+ these land fully — cyberpunk draws a neon street grid along the valleys, pixel and both Minecraft looks paint the lake as blue water with grass, stone and snow bands by elevation, theme park fills the lowland with rides and lawns, Mars turns the basin to dust. The ControlNet keeps the shoreline and ridges in all of them. Minecraft voxel at 0.85 is the best single demo image so far. Every one is a `garda-1-<style>-d<denoise>` job in the viewer.

**Gotcha: the watcher can restart ComfyUI mid-bake.** `garda-1-mars-d7` queued at 09:41:44; the cleanup cycle at 09:52 found ComfyUI "down", it came back at 09:52:06, and the in-flight prompt was gone. atlas kept polling `/history/<id>` (empty) until its 1200 s timeout. Rerun fixes it; the backend should fail fast when the prompt is in neither the queue nor the history.

## OSM layers and the composite (17 Oct, `garda-2`)

The lake never read as water because nothing told the model where water was. `argus-atlas osm` now fetches OSM features for the bbox at the job's zoom (here 11: motorways, trunks and primaries; no footpaths) and `controls --layer` burns them; the known layers are painted over the hillshade into `composite.png`, which the packaged `comfyui_sdxl_img2img_composite_nolora.json` template feeds to the img2img init while the ControlNet keeps the hillshade.

```bash
RUN="docker run --rm -u $(id -u):$(id -g) -v /home/smk/argus/dem:/data/dem:ro -v /home/smk/argus/osm:/data/osm -v /home/smk/argus/atlas-jobs:/data/out/atlas argus-atlas:dev"
$RUN argus-atlas osm --bbox 10.55,45.40,10.95,45.90 --zoom 11 --layers water,rivers,roads,rail,forest,urban --out /data/osm/garda
#   55 MB extract in about 60 s from overpass-api.de; <layer>.geojson + osm.json (query, osm_base timestamp, digests)
$RUN argus-atlas controls --dem /data/dem/Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif --bbox 10.55,45.40,10.95,45.90 \
  --out /data/out/atlas/garda-2 \
  --layer water=/data/osm/garda/water.geojson --layer rivers=/data/osm/garda/rivers.geojson \
  --layer roads=/data/osm/garda/roads.geojson --layer rail=/data/osm/garda/rail.geojson \
  --layer forest=/data/osm/garda/forest.geojson --layer urban=/data/osm/garda/urban.geojson
# render + patch the composite template exactly like the others — the bare packaged name still
# carries the placeholder ControlNet and ComfyUI rejects it with "value_not_in_list" on node 8:
python -c "import argus_atlas.backends.workflow as w, json; print(json.dumps(w.example_template(w.EXAMPLE_TEMPLATE_COMPOSITE), indent=2))" > /home/smk/argus/atlas-composite.json
sed -i 's|diffusers_xl_depth_full.safetensors|controlnet-union-sdxl/diffusion_pytorch_model_promax.safetensors|' /home/smk/argus/atlas-composite.json
# then bake as before with -v /home/smk/argus/atlas-composite.json:/data/atlas-composite.json:ro and --workflow /data/atlas-composite.json
```

Two things bit on the way and are now fixed in code and tests:

- **`gdal_rasterize` never reprojects.** Its `-a_srs` only labels the output, so the original `--land` path burnt nothing from a 4326 polygon (the test never looked at pixel values). Every vector now goes through `ogr2ogr -t_srs EPSG:3857 -clipdst` first; masks are exported without the NoData/transparency flag and the composite is built with `--hideNoData`.
- **Overpass output order matters to GDAL's OSM driver.** `out body; >; out skel qt;` prints ways before their nodes (by quadtile) and the driver, reading sequentially, drops every way: a 54 MB extract became six empty layers and an all-grey composite. The query ends with `(._;>;);out body;` (nodes, ways, relations by id) and the split works. Check `ogrinfo -so <layer>.geojson` shows a non-zero feature count before burning.

### What the composite changes ([`img/garda-2-composite.jpg`](img/garda-2-composite.jpg))

Same seed, same prompts, same denoise as the `garda-1` bakes next to them; only the init image differs. With the composite:

- the lake is water in every style, with the OSM shoreline, and the Adige is a river rather than a valley texture;
- forests sit where OSM has them (Tolkien draws them as tree clusters, Imhof as green relief), so the plain south of the lake stops being invented;
- roads are traced — cyberpunk turns the road network into its neon grid, which is exactly the right behaviour for that style;
- ridges and the shoreline still match the DEM, because the ControlNet still reads the hillshade.

Antique at 0.55 is already a usable map; 0.7 gives the stronger illustrated look with the same geometry. Six bakes with tiles took about four minutes with ComfyUI warm. All `garda-2-*` jobs are in the viewer.

## Western Australia series and the golden presets (23 Oct)

Four AOIs on a VRT mosaic of six Copernicus GLO-30 tiles (`/home/smk/argus/dem/wa_glo30.vrt`), each with five OSM layers at its native zoom and `--sea-level 0` (OSM has no ocean polygons; GLO-30 puts the sea at 0 m, so the `sea` mask comes from the DEM), then the seven preset candidates at seed 7 through the composite template. 28 bakes with tiles in 18 minutes; Overpass was called once per AOI with a 90 s pause between calls.

| AOI | bbox | canvas | note |
|---|---|---|---|
| `wa-rockingham` | 115.55,-32.45,115.90,-32.10 | 896x1024, 45 m/px, z12 | Garden Island, Cockburn Sound, Rockingham/Kwinana urban fabric |
| `wa-rottnest` | 115.42,-32.05,115.58,-31.95 | 1024x768, 17 m/px, z13 | an island: sea mask does all the work |
| `wa-albany` | 117.75,-35.15,118.05,-34.90 | 1024x1024, 33 m/px, z12 | King George Sound, Princess Royal Harbour, Torndirrup |
| `wa-stirling` | 117.75,-34.50,118.35,-34.25 | 1024x576, 65 m/px, z11 | Bluff Knoll relief; the park's rectangular bush boundary is real OSM data |

Sheets: [`img/wa-rockingham.jpg`](img/wa-rockingham.jpg), [`img/wa-rottnest.jpg`](img/wa-rottnest.jpg), [`img/wa-albany.jpg`](img/wa-albany.jpg), [`img/wa-stirling.jpg`](img/wa-stirling.jpg). Coastlines, islands and harbours sit where the mask put them in every preset.

**Golden six** (`recommended` in `argus_atlas.presets`, the menu `flow.html?lock=1` shows): `antique`, `imhof`, `tolkien`, `watercolor`, `minecraft`, `pixel` — each held up on all four AOIs. `cyberpunk` is kept but not recommended: spectacular over Rockingham/Kwinana, flat on Rottnest and the Stirling bush because there is no road grid to turn neon. Two tunings came out of the sheets: "no compass rose" in the positive prompt does nothing (antique and tolkien grew roses on Rockingham), so compass rose and cartouche are in those presets' negatives now (antique also gets sea monster and ship; on tolkien those two flattened the inked sea, so it keeps only the rose terms) — verified on `wa-rockingham-*-v2`; and the sea mask is what makes islands and sounds read as water in every style.

Every output of the series carries its request: `exiftool`/Pillow read the PNG's ImageDescription, `gdalinfo` shows `TIFFTAG_IMAGEDESCRIPTION` on the GeoTIFF, and the PMTiles metadata has `argus_bake` — checked on `wa-albany-tolkien`. Rank by preset, seed or AOI from the files alone.

## Albany 3x3 chunk series (25 Oct)

Nine z12 chunks around Albany (`12/3388..3390/2473..2475`, 1024 px + 128 px margin, native z14, ~24 km across) baked with `argus-atlas chunk` row by row from the `antique` recipe (Juggernaut, union ControlNet via a patched seam template, OSM water/rivers/roads/forest/urban at zoom 13, `--sea-level 0`). Each chunk took 45–55 s end to end (generate 23–40 s; verify 4–13 s because each container re-hashes the checkpoint).

Seams measured on the mosaic as the mean RGB step across a chunk edge divided by the step between adjacent pixels inside a chunk (1.0 = invisible), same seeds in every pass:

| pass | seam step | interior step | ratio |
|---|---|---|---|
| no neighbours (`--neighbours` an empty path) | 11.33 | 1.81 | 6.3 |
| neighbours pasted, hard mask, no blend | 8.60 | 1.93 | 4.5 |
| + soft mask, tone, full-res residual | 3.60 | 1.94 | 1.9 (streaks along coasts) |
| + residual low-passed only | 6.90 | 1.90 | 3.6 (no streaks, line back) |
| + two-scale residual (shipped defaults) | 3.76 | 1.91 | 2.0 (no streaks, faint line) |

Rivers and coastlines continue across every seam once neighbours are pasted; the blend removes the colour step in open water and most of the line. What is left is model variance between chunks: two neighbours can still be painted in a slightly different hand (paper-like vs warm relief), which no pixel blend fixes. Jobs: `/home/smk/argus/atlas-jobs/albany-3x3*`.

Two things only a real bake caught: `gdal_proximity`/`gdal_fillnodata` write scratch files into the working directory (`/app` in the image is not writable by `-u $(id -u)`; the tests run as root), and a stacked VRT has no colour interpretation, so `gdalbuildvrt` refused to mosaic blended crops with the first, unblended one.

## Review the output

`examples/viewer.html` drapes the job on a basemap. On morpheus the API runs from the image and the page from a static server; both are reachable on the LAN:

```bash
docker run -d --name atlas-api -u $(id -u):$(id -g) -p 8105:8105 -e ATLAS_CORS_ORIGINS='*' \
  -v /home/smk/argus/atlas-jobs:/data/out/atlas:ro argus-atlas:dev argus-atlas serve --port 8105 --cors
python3 -m http.server 8106 --bind 0.0.0.0 --directory examples
# http://192.168.1.39:8106/viewer.html?server=http://192.168.1.39:8105&job=garda-1
```

`examples/flow.html` (same static server, `http://192.168.1.39:8106/flow.html?server=http://192.168.1.39:8105`) runs the whole flow from the browser. For that the API container needs the inputs mounted and both opt-ins on — this is how `atlas-api` runs since 23 Oct:

```bash
docker run -d --name atlas-api -u $(id -u):$(id -g) -p 8105:8105 \
  -e ATLAS_ALLOW_BAKE=1 -e ATLAS_ALLOW_OSM=1 -e ATLAS_CORS_ORIGINS='*' \
  -e COMFYUI_BASE_URL=http://host.docker.internal:8188 -e ATLAS_WORKFLOW=/data/atlas-composite.json \
  --add-host host.docker.internal:host-gateway \
  -v /home/smk/argus/dem:/data/dem -v /srv/imogen/fast/models-hot:/data/models:ro \
  -v /home/smk/argus/atlas-jobs:/data/out/atlas -v /home/smk/argus/atlas-composite.json:/data/atlas-composite.json:ro \
  argus-atlas:dev argus-atlas serve --port 8105 --cors
```

The DEM dir is mounted read-write because `POST /osm` writes its extracts under `/data/dem/osm/<job>/`. The workflow is the server-wide template: the page cannot pick one per bake.

Proven headless on 23 Oct ([`img/flow-sirmione.jpg`](img/flow-sirmione.jpg)): a 12 x 9 km AOI around Sirmione, OSM fetch 40 s, controls 1 s (512x512 at 32.6 m/px, zoom 12, eight controls), bake + tiles + drape by 78 s with ComfyUI warm, no page errors. The painted peninsula sits on the basemap's.

`garda-1` was checked this way on 17 Oct: the painted lake sits on the real Lake Garda outline and the Monte Baldo ridge lines up. ComfyUI also keeps the raw PNG in its own output dir (`/home/smk/voluptas/output/argus-atlas_00001_.png`, same pixels as `fantasy.png`, no georeferencing). The bake took about 540 s on the queue, nearly all of it loading the 6.6 GB checkpoint and the 2.5 GB ControlNet; ComfyUI runs with `--cache-none`, so every bake pays that again.

## Release

PyPI trusted publishing and the `pypi` / `testpypi` GitHub environments are configured (no secrets needed). `git tag v0.1.0a1 && git push --tags` runs build, install test, TestPyPI, PyPI and the GHCR image.
