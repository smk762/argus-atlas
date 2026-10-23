"""Named style presets: the knobs of a bake that were found to produce good maps.

A preset bundles prompt, negative prompt, denoise, cfg and steps under a short
name so a demo can offer a fixed menu instead of free-text prompts (no
moderation surface), and so every output can say which preset made it (the
name travels in :class:`~argus_atlas.models.BakeParams` and into the image
metadata). Explicit values given alongside a preset win over the preset's.

The six marked ``recommended`` held up on every AOI of the Lake Garda and
Western Australia sweeps of October 2026 (see ``docs/first-bake.md``); the rest
are kept for the right terrain. All assume the composite init image (water,
forest, roads from OSM) and an SDXL checkpoint, no LoRA.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from argus_atlas.models import DEFAULT_CFG, DEFAULT_NEGATIVE, DEFAULT_STEPS, AtlasError

_NEG_PAPER = DEFAULT_NEGATIVE + ", parchment, paper, sepia, antique"


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    prompt: str
    negative: str = DEFAULT_NEGATIVE
    denoise: float = 0.7
    cfg: float = DEFAULT_CFG
    steps: int = DEFAULT_STEPS
    blurb: str = ""
    # The golden six hold up on every AOI tried; a non-recommended preset needs the right terrain.
    recommended: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "antique",
            "Antique atlas",
            "antique fantasy map, ink coastlines, parchment, illustrated relief, blue water, no text, no labels",
            negative=DEFAULT_NEGATIVE + ", compass rose, cartouche, sea monster, ship",
            denoise=0.6,
            blurb="Parchment and ink; the safest all-rounder.",
        ),
        Preset(
            "imhof",
            "Swiss relief",
            "Swiss topographic map, Eduard Imhof style relief shading, soft aerial perspective, "
            "pale blue water, muted greens and ochres, fine contour lines, no text, no labels",
            denoise=0.7,
            blurb="Painterly relief shading in the Imhof manner.",
        ),
        Preset(
            "tolkien",
            "Pen and ink",
            "Tolkien style hand-drawn fantasy map, pen and ink, mountains drawn in profile, hatched hills, "
            "forests of tiny trees, blue-grey water, aged paper, no text, no labels",
            # only the rose: "ship, sea monster" in the negative also flattened the inked sea texture
            negative=DEFAULT_NEGATIVE + ", compass rose, cartouche",
            denoise=0.7,
            blurb="Monochrome hatching with mountains in profile.",
        ),
        Preset(
            "watercolor",
            "Watercolour plate",
            "watercolour atlas plate, soft washes, blue water, green valleys, brown mountains, "
            "hand-painted relief, paper texture, no text, no labels",
            denoise=0.7,
            blurb="Loose washes; strongest palette.",
        ),
        Preset(
            "cyberpunk",
            "Neon grid",
            "cyberpunk night city map, neon magenta and cyan glow on black, luminous street grid, "
            "circuit-board terrain, dark water, rain-slick, no text, no labels",
            negative=_NEG_PAPER,
            denoise=0.7,
            blurb="Roads become the neon grid; needs towns in the AOI, flat on islands and bush.",
            recommended=False,
        ),
        Preset(
            "minecraft",
            "Voxel world",
            "Minecraft voxel terrain seen from above, blocky cube landscape, grass blocks, stone and gravel "
            "mountains, sand beaches, blue water blocks, 16x16 pixel textures, bright flat lighting, "
            "no text, no labels",
            negative=_NEG_PAPER + ", smooth, realistic",
            denoise=0.85,
            blurb="Blocky terrain with water, grass, stone and snow by elevation.",
        ),
        Preset(
            "pixel",
            "16-bit overworld",
            "16-bit pixel art RPG overworld map, tile-based, blue water, green plains, brown mountains, "
            "sandy coast, retro game, no text, no labels",
            negative=_NEG_PAPER,
            denoise=0.7,
            blurb="Retro tile look.",
        ),
    )
}


def get_preset(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise AtlasError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}") from exc


def resolve_params(
    preset: str | None,
    *,
    prompt: str | None = None,
    negative: str | None = None,
    denoise: float | None = None,
    cfg: float | None = None,
    steps: int | None = None,
) -> dict:
    """Prompt/negative/denoise/cfg/steps for a bake: explicit values, else the
    preset's, else the package defaults. ``preset`` is echoed for the record."""
    from argus_atlas.models import DEFAULT_DENOISE, DEFAULT_PROMPT

    base = get_preset(preset).as_dict() if preset else {}
    return {
        "preset": preset,
        "prompt": prompt if prompt is not None else base.get("prompt", DEFAULT_PROMPT),
        "negative": negative if negative is not None else base.get("negative", DEFAULT_NEGATIVE),
        "denoise": denoise if denoise is not None else base.get("denoise", DEFAULT_DENOISE),
        "cfg": cfg if cfg is not None else base.get("cfg", DEFAULT_CFG),
        "steps": steps if steps is not None else base.get("steps", DEFAULT_STEPS),
    }
