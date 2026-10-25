"""Style recipes: one JSON file per style, so every chunk of a style is painted the same way.

A recipe (:class:`~argus_atlas.models.Recipe`) pins the preset, checkpoint,
LoRA, workflow template, prompt knobs and a seed scheme. The ``tile`` scheme
derives each chunk's seed from ``(recipe name, base, z/x/y)``, so re-baking a
chunk reproduces it and no two chunks share noise. The whole recipe and the
digest of its canonical JSON go into ``job.json`` (:class:`RecipeRecord`) and
its name into the bake params, so every output says which style made it.

``--recipe`` takes a path to a recipe file, or a bare name looked up as
``<ATLAS_RECIPES_DIR>/<name>.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from argus_atlas.backends.workflow import (
    EXAMPLE_TEMPLATE_SEAM,
    EXAMPLE_TEMPLATE_SEAM_NOLORA,
    PACKAGED_TEMPLATES,
)
from argus_atlas.hashing import sha256_bytes
from argus_atlas.jobs import is_safe_id
from argus_atlas.models import AtlasError, BakeParams, Recipe, RecipeRecord
from argus_atlas.presets import get_preset, resolve_params

RECIPE_SUFFIX = ".json"


def recipe_digest(recipe: Recipe) -> str:
    """SHA256 of the recipe's canonical JSON (sorted keys, defaults filled in)."""
    canonical = json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def recipe_record(recipe: Recipe) -> RecipeRecord:
    return RecipeRecord(recipe=recipe, sha256=recipe_digest(recipe))


def load_recipe(ref: str | Path, *, recipes_root: Path | None = None) -> tuple[Recipe, Path]:
    """The recipe at *ref* (a file) or named *ref* under *recipes_root*, and the file it came from."""
    path = Path(ref).expanduser()
    if not path.is_file():
        name = str(ref)
        if recipes_root is None or not is_safe_id(name) or name.endswith(RECIPE_SUFFIX):
            raise AtlasError(f"recipe file not found: {ref}")
        path = recipes_root.expanduser() / f"{name}{RECIPE_SUFFIX}"
        if not path.is_file():
            raise AtlasError(f"no recipe named {name!r} (looked for {path})")
    try:
        recipe = Recipe.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AtlasError(f"invalid recipe {path}: {exc}") from exc
    if recipe.preset is not None:
        get_preset(recipe.preset)  # an unknown preset fails here, not mid-bake
    return recipe, path


def recipe_workflow(recipe: Recipe, recipe_path: Path | None = None) -> Path:
    """The workflow template to bake with: a packaged name (as a bare path) or a file next to the recipe."""
    name = recipe.workflow or (EXAMPLE_TEMPLATE_SEAM if recipe.lora else EXAMPLE_TEMPLATE_SEAM_NOLORA)
    if name in PACKAGED_TEMPLATES:
        return Path(name)
    path = Path(name).expanduser()
    if not path.is_absolute() and recipe_path is not None:
        path = recipe_path.parent / path
    if not path.is_file():
        raise AtlasError(f"recipe {recipe.name}: workflow template not found: {path}")
    return path.resolve()


def recipe_seed(recipe: Recipe, z: int, x: int, y: int) -> int:
    """The chunk's seed: *base* for ``fixed``, else a 32-bit hash of recipe, base and tile."""
    if recipe.seed.scheme == "fixed":
        return recipe.seed.base
    key = f"{recipe.name}:{recipe.seed.base}:{z}/{x}/{y}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "big")


def recipe_params(recipe: Recipe, *, seed: int) -> BakeParams:
    """The bake knobs of *recipe*: its own values, else its preset's, else the package defaults."""
    return BakeParams(
        checkpoint=recipe.checkpoint,
        lora=recipe.lora,
        lora_weight=recipe.lora_weight,
        seed=seed,
        sampler=recipe.sampler,
        scheduler=recipe.scheduler,
        recipe=recipe.name,
        **resolve_params(
            recipe.preset,
            prompt=recipe.prompt,
            negative=recipe.negative,
            denoise=recipe.denoise,
            cfg=recipe.cfg,
            steps=recipe.steps,
        ),
    )
