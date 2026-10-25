from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_atlas.backends import backend_from_env
from argus_atlas.backends.workflow import (
    EXAMPLE_TEMPLATE_SEAM,
    EXAMPLE_TEMPLATE_SEAM_NOLORA,
    REQUIRED_PLACEHOLDERS,
    example_template,
    placeholders_used,
)
from argus_atlas.models import AtlasError, Recipe, SeedScheme
from argus_atlas.presets import get_preset
from argus_atlas.recipes import (
    load_recipe,
    recipe_digest,
    recipe_params,
    recipe_record,
    recipe_seed,
    recipe_workflow,
)


def _write(path: Path, **fields: object) -> Path:
    path.write_text(json.dumps({"name": "antique", "checkpoint": "juggernaut.safetensors", **fields}), "utf-8")
    return path


def test_load_by_path_and_by_name(tmp_path: Path) -> None:
    path = _write(tmp_path / "antique.json", preset="antique")
    recipe, source = load_recipe(path)
    assert recipe.name == "antique" and source == path
    by_name, source = load_recipe("antique", recipes_root=tmp_path)
    assert by_name == recipe and source == path
    with pytest.raises(AtlasError, match="no recipe named"):
        load_recipe("missing", recipes_root=tmp_path)
    with pytest.raises(AtlasError, match="not found"):
        load_recipe("../antique", recipes_root=tmp_path)


def test_bad_recipes_fail_on_load(tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="unknown preset"):
        load_recipe(_write(tmp_path / "a.json", preset="nope"))
    with pytest.raises(AtlasError, match="invalid recipe"):
        load_recipe(_write(tmp_path / "b.json", colour="red"))  # extra fields are refused, not ignored
    with pytest.raises(AtlasError, match="invalid recipe"):
        load_recipe(_write(tmp_path / "c.json", name="has space"))


def test_seed_per_tile_is_stable_and_distinct() -> None:
    recipe = Recipe(name="antique", checkpoint="c")
    assert recipe_seed(recipe, 10, 1, 2) == recipe_seed(recipe, 10, 1, 2)
    assert recipe_seed(recipe, 10, 1, 2) != recipe_seed(recipe, 10, 2, 1)
    other = Recipe(name="neon", checkpoint="c")
    assert recipe_seed(recipe, 10, 1, 2) != recipe_seed(other, 10, 1, 2)
    fixed = Recipe(name="antique", checkpoint="c", seed=SeedScheme(scheme="fixed", base=42))
    assert recipe_seed(fixed, 10, 1, 2) == recipe_seed(fixed, 3, 0, 0) == 42


def test_params_take_recipe_then_preset_then_defaults() -> None:
    preset = get_preset("tolkien")
    params = recipe_params(Recipe(name="ink", preset="tolkien", checkpoint="c", cfg=4.5), seed=9)
    assert params.prompt == preset.prompt and params.denoise == preset.denoise
    assert params.cfg == 4.5 and params.seed == 9
    assert params.recipe == "ink" and params.preset == "tolkien"


def test_workflow_defaults_follow_the_lora(tmp_path: Path) -> None:
    nolora = recipe_workflow(Recipe(name="a", checkpoint="c"))
    assert nolora.is_absolute() and nolora.is_file() and nolora.name == EXAMPLE_TEMPLATE_SEAM_NOLORA
    assert recipe_workflow(Recipe(name="a", checkpoint="c", lora="map.safetensors")).name == EXAMPLE_TEMPLATE_SEAM
    graph = tmp_path / "graphs" / "mine.json"
    graph.parent.mkdir()
    graph.write_text("{}", "utf-8")
    recipe_file = tmp_path / "a.json"
    assert recipe_workflow(Recipe(name="a", checkpoint="c", workflow="graphs/mine.json"), recipe_file) == graph
    with pytest.raises(AtlasError, match="not found"):
        recipe_workflow(Recipe(name="a", checkpoint="c", workflow="nope.json"), recipe_file)


def test_digest_covers_defaults() -> None:
    a = Recipe(name="antique", checkpoint="c")
    assert recipe_digest(a) == recipe_digest(Recipe.model_validate_json(a.model_dump_json()))
    assert recipe_digest(a) != recipe_digest(a.model_copy(update={"steps": 31}))
    assert recipe_record(a).recipe == a


@pytest.mark.parametrize("name", [EXAMPLE_TEMPLATE_SEAM, EXAMPLE_TEMPLATE_SEAM_NOLORA])
def test_seam_templates_mask_the_latent(name: str) -> None:
    graph = example_template(name)
    used = placeholders_used(graph)
    assert set(REQUIRED_PLACEHOLDERS) <= used and {"$seam_init", "$seam_mask"} <= used
    assert ("$lora" in used) == (name == EXAMPLE_TEMPLATE_SEAM)
    sampler = next(n for n in graph.values() if n["class_type"] == "KSampler")
    mask_node = graph[sampler["inputs"]["latent_image"][0]]
    assert mask_node["class_type"] == "SetLatentNoiseMask"
    knobs = (sampler["inputs"]["sampler_name"], sampler["inputs"]["scheduler"])
    assert knobs == ("$sampler", "$scheduler"), "the recipe's sampler and scheduler reach the graph"


def test_a_file_in_the_cwd_cannot_replace_the_packaged_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / EXAMPLE_TEMPLATE_SEAM_NOLORA).write_text('{"1": {"class_type": "Stub", "inputs": {}}}', "utf-8")
    monkeypatch.chdir(tmp_path)
    backend = backend_from_env(workflow=recipe_workflow(Recipe(name="a", checkpoint="c")))
    assert backend.template == example_template(EXAMPLE_TEMPLATE_SEAM_NOLORA)  # type: ignore[attr-defined]
