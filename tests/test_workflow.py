from __future__ import annotations

from pathlib import Path

import pytest

from argus_atlas.backends.base import BackendError
from argus_atlas.backends.workflow import (
    REQUIRED_PLACEHOLDERS,
    example_template,
    placeholders_used,
    render_workflow,
    template_digest,
)

VALUES: dict[str, object] = {
    "$checkpoint": "sd_xl_base_1.0.safetensors",
    "$lora": "fantasy_map.safetensors",
    "$lora_weight": 0.8,
    "$positive": "antique fantasy map",
    "$negative": "text",
    "$seed": 7,
    "$steps": 30,
    "$cfg": 6.0,
    "$denoise": 0.55,
    "$width": 1024,
    "$height": 768,
    "$hillshade": "argus-atlas/aoi-1-hillshade.png",
    "$slope": None,
    "$land": None,
}


def test_example_template_uses_every_required_placeholder() -> None:
    used = placeholders_used(example_template())
    assert set(REQUIRED_PLACEHOLDERS) <= used
    assert {"$lora", "$lora_weight"} <= used


def test_render_substitutes_typed_values_and_leaves_links() -> None:
    graph = render_workflow(example_template(), VALUES)
    sampler = next(n for n in graph.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["seed"] == 7 and sampler["inputs"]["denoise"] == 0.55
    assert sampler["inputs"]["model"] == ["2", 0], "node links are untouched"
    scale = next(n for n in graph.values() if n["class_type"] == "ImageScale")
    assert (scale["inputs"]["width"], scale["inputs"]["height"]) == (1024, 768)
    load = next(n for n in graph.values() if n["class_type"] == "LoadImage")
    assert load["inputs"]["image"] == "argus-atlas/aoi-1-hillshade.png"
    assert "$" not in str(graph), "no placeholder survives rendering"
    assert example_template() != graph, "the template is not mutated"


def test_missing_required_placeholder_is_named() -> None:
    template = example_template()
    load = next(n for n in template.values() if n["class_type"] == "LoadImage")
    load["inputs"]["image"] = "hardcoded.png"
    with pytest.raises(BackendError, match=r"\$hillshade"):
        render_workflow(template, VALUES)


def test_lora_slot_without_lora_is_refused() -> None:
    values = {**VALUES, "$lora": None}
    with pytest.raises(BackendError, match=r"\$lora.*no LoRA was supplied"):
        render_workflow(example_template(), values)


def test_lora_without_slot_is_refused() -> None:
    template = {k: v for k, v in example_template().items() if v["class_type"] != "LoraLoader"}
    # Rewire the graph past the removed node so it is still a plausible template.
    for node in template.values():
        for key, val in node["inputs"].items():
            if val == ["2", 0]:
                node["inputs"][key] = ["1", 0]
            elif val == ["2", 1]:
                node["inputs"][key] = ["1", 1]
    with pytest.raises(BackendError, match=r"lacks \$lora, \$lora_weight"):
        render_workflow(template, VALUES)
    graph = render_workflow(template, {**VALUES, "$lora": None})
    assert "LoraLoader" not in {n["class_type"] for n in graph.values()}


def test_template_needing_an_unsupplied_control_is_refused() -> None:
    template = example_template()
    template["99"] = {"class_type": "LoadImage", "inputs": {"image": "$slope", "upload": "image"}}
    with pytest.raises(BackendError, match=r"\$slope"):
        render_workflow(template, VALUES)
    graph = render_workflow(template, {**VALUES, "$slope": "argus-atlas/aoi-1-slope.png"})
    assert graph["99"]["inputs"]["image"] == "argus-atlas/aoi-1-slope.png"


def test_digest_is_stable() -> None:
    assert template_digest(example_template()) == template_digest(example_template())
    assert len(template_digest(example_template())) == 64


def test_nolora_template_bakes_without_a_lora(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_atlas.backends import get_backend
    from argus_atlas.backends.workflow import EXAMPLE_TEMPLATE_NOLORA

    template = example_template(EXAMPLE_TEMPLATE_NOLORA)
    assert "LoraLoader" not in {n["class_type"] for n in template.values()}
    assert set(REQUIRED_PLACEHOLDERS) <= placeholders_used(template)
    graph = render_workflow(template, {**VALUES, "$lora": None})
    sampler = next(n for n in graph.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["model"] == ["1", 0]
    with pytest.raises(BackendError, match="no packaged workflow template"):
        example_template("nope.json")
    # A bare packaged name is accepted where a path is expected (--workflow / ATLAS_WORKFLOW).
    backend = get_backend("comfyui", workflow=Path(EXAMPLE_TEMPLATE_NOLORA), base_url="http://comfy:8188")
    assert "LoraLoader" not in {n["class_type"] for n in backend.template.values()}  # type: ignore[attr-defined]


def test_any_control_can_be_a_placeholder() -> None:
    from argus_atlas.backends.workflow import control_placeholders_used, example_template

    template = example_template("comfyui_sdxl_img2img_composite_nolora.json")
    assert control_placeholders_used(template) == {"hillshade": "$hillshade", "composite": "$composite"}
    template["99"] = {"class_type": "LoadImage", "inputs": {"image": "$roads", "upload": "image"}}
    assert control_placeholders_used(template)["roads"] == "$roads"
    # value placeholders are never mistaken for controls
    assert "seed" not in control_placeholders_used(template)
