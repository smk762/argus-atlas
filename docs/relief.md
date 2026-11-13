# Relief: the Mercator correction and vertical exaggeration

## Why relief flattens away from the equator

`controls` warps the DEM onto the Web Mercator (EPSG:3857) canvas, then runs `gdaldem hillshade` and `gdaldem slope` on it. Web Mercator keeps shapes but stretches horizontal distances by 1/cos(latitude). On the canvas a hill is that much wider while its height in metres stays the same, so a slope computed there is too shallow:

| Latitude | Stretch | A true 30° slope reads as |
|---|---|---|
| 0° (equator) | ×1.00 | 30° |
| 41°S (Cook Strait) | ×1.33 | 23.5° |
| 45°N (Lake Garda) | ×1.41 | 22.2° |
| 65°N (Iceland) | ×2.37 | 13.7° |

Before 0.3.0, a bake near the poles got a much flatter hillshade, and so a flatter painting, than the same terrain at the equator.

## What atlas does about it

Since 0.3.0 the heights are multiplied by the stretch at the canvas centre before shading (`--z-factor auto`, the default). That restores the true slope, so the same mountain range is shaded the same way in Iceland or in Réunion. It is a per-canvas constant: across a 1024 px chunk at z10 the stretch changes by well under 1 %.

On top of that, `--vertical-exaggeration` (`--ve`) is the cartographer's knob. Relief maps commonly exaggerate gentle terrain 1.5–3× so that low hills read at all:

```
z = mercator correction × vertical exaggeration
```

The hillshade gets `gdaldem hillshade -z <z>`. The slope gets `gdaldem slope -s <1/z>`, because `slope` has no `-z` and dividing horizontal distances by z gives the same result.

| Flag | Meaning |
|---|---|
| `--z-factor auto` (default) | correct for Web Mercator at the canvas centre |
| `--z-factor off` (= `1`) | no correction: the relief of 0.3.0a1 and earlier |
| `--z-factor 2.5` | use this factor as is, no correction |
| `--vertical-exaggeration 1.5` | multiply whichever of the above by 1.5 (default 1) |

They are on `controls`, `chunk` and `chunks`, and on `POST /controls` as `z_factor` (`"auto"` or a number) and `vertical_exaggeration`.

## Where it is recorded

- `job.json` has a `relief` record: `mode` (`auto` or `fixed`), the canvas-centre `latitude`, the `base` factor (the correction, or the fixed value), `vertical_exaggeration`, and the effective `z_factor`. The `gdaldem` argv in `commands` carries the same numbers. A job from before 0.3.0 has no `relief`, which means z = 1.
- A style recipe has `z_factor` (default `"auto"`) and `vertical_exaggeration` (default 1). On `chunk` and `chunks`, the flags override the recipe's values. Both fields are part of the recipe digest, so a recipe with other relief is a different recipe: its chunks are not "done" for this one, and a re-run re-bakes them.
- Chunks painted over differently scaled hillshades must not be stitched together. `chunk` refuses a neighbour with other relief instead of pasting it into the seam margin, and `mosaic` refuses a style dir that mixes reliefs. Under `auto`, chunks match when their exaggeration matches, because the correction follows each chunk's own latitude.

## Old blocks

A style dir baked before 0.3.0 has no correction. To extend it seamlessly, pin the old relief in its recipe with `"z_factor": 1`, or pass `--z-factor off`. A recipe at z 1 with no exaggeration keeps the digest its chunks were baked under, so they still count as done and as neighbours. To get the corrected relief, bake into a new style dir.
