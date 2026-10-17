# First real bake — runbook for the GPU host (morpheus)

State on 17 Oct 2026, written on the laptop before moving development to the GPU host. Everything below was checked against the live ComfyUI; re-check anything that looks stale.

## What is already there

| Thing | Where | Notes |
|---|---|---|
| ComfyUI 0.19.3 | `http://192.168.1.39:8188`, container `voluptas_comfyui` (`ghcr.io/radiatingreverberations/comfyui-extensions:v0.19.3`) on morpheus | RTX 3090, 24 GB. Started with `--listen`, so it is reachable from the LAN and from other containers on the host. |
| Model tree | `/srv/imogen/models` (main), `/srv/imogen/fast/models-hot` (extra tree, `extra_model_paths.yaml`) | Owned by `smk`. 39 GB free on `/`. Mounted into ComfyUI as `/comfyui/models` and `/comfyui/extra_models/models-hot`. |
| SDXL checkpoints | `checkpoints/sdxl/...` | e.g. `sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors`, `sdxl/epicrealismXL_pureFix/...`. No `sd_xl_base_1.0` (a 6.9 GB copy exists on the laptop in `argus-seed/models/` if wanted). |
| ControlNets | `controlnet/openpose-sdxl/` only | **No depth / canny / tile model yet — see below.** |
| LoRAs | many, none for cartography | Use the packaged no-LoRA template for the first bake. |
| Nodes | all eleven the templates use are present | `ControlNetApplyAdvanced` also accepts an optional `vae` input; the templates don't use it. |

argus-atlas pins the checkpoint and LoRA by SHA256, so `ATLAS_MODELS_DIR` must see the **same files ComfyUI loads**. Run atlas on morpheus with `/srv/imogen/models` mounted (or `--models-dir /srv/imogen/models` for the CLI). Model names are given exactly as ComfyUI lists them, relative to `checkpoints/` or `loras/` (the resolver tries the name as a path under the root, then under those subdirs).

## Downloads (the operator's job; atlas never downloads)

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

   Suggested AOI: `--bbox 10.55,45.40,10.95,45.90` (about 40 m/px at the default 1024 canvas, native zoom 12).

3. **Optional: a land polygon** (`--land`) for the land mask. Not needed for the first bake. Any OGR-readable file works (GeoJSON, GPKG). Natural Earth 10m land is a quick source if wanted.

## Point the template at the ControlNet

Both packaged templates name `diffusers_xl_depth_full.safetensors`, which does not exist on morpheus. Copy one and edit the `ControlNetLoader` node:

```bash
python -c "import argus_atlas.backends.workflow as w, json; print(json.dumps(w.example_template(w.EXAMPLE_TEMPLATE_NOLORA), indent=2))" > /srv/argus/atlas-nolora.json
sed -i 's|diffusers_xl_depth_full.safetensors|controlnet-union-sdxl/diffusion_pytorch_model_promax.safetensors|' /srv/argus/atlas-nolora.json
```

Then `--workflow /srv/argus/atlas-nolora.json` (or `ATLAS_WORKFLOW=`). The `strength` (0.65) and `end_percent` (0.8) in that node are the ControlNet knobs; they live in the template on purpose.

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
  --models-dir /srv/imogen/models \
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
  -v /srv/argus/dem:/data/dem:ro -v /srv/imogen/models:/data/models:ro -v /srv/argus/out:/data/out \
  -v /srv/argus/atlas-nolora.json:/data/atlas-nolora.json:ro \
  argus-atlas:dev bash
# inside: argus-atlas controls --dem /data/dem/<tile>.tif --bbox ... --out /data/out/atlas/garda-1 ; argus-atlas bake ... --workflow /data/atlas-nolora.json
```

For studio: `COMFYUI_BASE_URL=http://host.docker.internal:8188`, `DEM_DIR=/srv/argus/dem`, `MODELS_DIR=/srv/imogen/models`, `ATLAS_ALLOW_BAKE=1` in the studio `.env`, then `docker compose -f compose.yaml -f compose.build.yaml --profile atlas up --build` with `../argus-atlas` checked out (the GHCR image exists only after the first tag). Studio PR: smk762/argus-studio#89.

## What to look at afterwards

- `fantasy.tif` exists and `inspect` shows `aligned=True` with the checkpoint SHA256.
- Drape `fantasy.pmtiles` on a basemap (MapLibre + pmtiles protocol) and slide opacity: coastline and ridges must line up. That is the hero asset and the moment the handoff's resume line becomes true.
- If relief is washed out, raise ControlNet `strength` or lower `--denoise`; if the output ignores the prompt, do the opposite. The hillshade `-z` factor in `gdal_ops.hillshade_argv` is the knob if Web Mercator stretch at 45° flattens the shading.

## Release

PyPI trusted publishing and the `pypi` / `testpypi` GitHub environments are configured (no secrets needed). `git tag v0.1.0a1 && git push --tags` runs build, install test, TestPyPI, PyPI and the GHCR image.
