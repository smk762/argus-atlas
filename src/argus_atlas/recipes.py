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
import importlib.resources
import json
from pathlib import Path

from argus_atlas.backends.workflow import (
    EXAMPLE_TEMPLATE_SEAM,
    EXAMPLE_TEMPLATE_SEAM_NOLORA,
    PACKAGED_TEMPLATES,
)
from argus_atlas.hashing import sha256_bytes
from argus_atlas.jobs import is_safe_id
from argus_atlas.models import AtlasError, BakeParams, Recipe, RecipeRecord, ZFactor
from argus_atlas.presets import get_preset, resolve_params

RECIPE_SUFFIX = ".json"


# Relief fields at these values are left out of the digest: a recipe pinned to the pre-0.3.0
# unscaled relief ("z_factor": 1) keeps the digest its chunks were baked under.
_LEGACY_RELIEF = {"z_factor": 1.0, "vertical_exaggeration": 1.0}


def recipe_digest(recipe: Recipe) -> str:
    """SHA256 of the recipe's canonical JSON (sorted keys, defaults filled in).

    The relief fields count like any other, so chunks painted over differently scaled
    hillshades are never neighbours, done chunks or one mosaic. The default ``auto``
    z factor is in the canonical JSON; at their legacy values they are omitted."""
    fields = recipe.model_dump(mode="json")
    for key, legacy in _LEGACY_RELIEF.items():
        if fields.get(key) == legacy:
            del fields[key]
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def with_relief(recipe: Recipe, z_factor: ZFactor | None = None, vertical_exaggeration: float | None = None) -> Recipe:
    """*recipe* with its relief overridden where given (``chunk``/``chunks`` flags); the digest follows."""
    update: dict[str, object] = {}
    if z_factor is not None:
        update["z_factor"] = z_factor
    if vertical_exaggeration is not None:
        update["vertical_exaggeration"] = vertical_exaggeration
    if not update:
        return recipe
    try:
        return Recipe.model_validate({**recipe.model_dump(), **update})
    except ValueError as exc:
        raise AtlasError(f"recipe {recipe.name!r}: {exc}") from exc


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
    """The workflow template to bake with: a packaged template's installed file, or a file next to the recipe.

    A packaged template comes back as an absolute path, so a file of the same name in the
    working directory can never stand in for it.
    """
    name = recipe.workflow or (EXAMPLE_TEMPLATE_SEAM if recipe.lora else EXAMPLE_TEMPLATE_SEAM_NOLORA)
    if name in PACKAGED_TEMPLATES:
        return Path(str(importlib.resources.files("argus_atlas").joinpath("templates", name)))
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
