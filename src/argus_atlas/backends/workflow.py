"""Render a parametric ComfyUI workflow template for one bake.

A template is a ComfyUI **API-format** graph — ``{node_id: {"class_type": str,
"inputs": {name: value}}}`` — whose placeholder *string* values are replaced
with typed values. Only a value that is exactly a placeholder is replaced (no
substring interpolation), so ints/floats stay typed and node links
(``[id, slot]``) are untouched. ControlNet choice and weight live in the
template, not in Python conditionals.

Placeholders, argus-proof style:

* always needed — ``$checkpoint`` ``$positive`` ``$negative`` ``$seed``
  ``$steps`` ``$cfg`` ``$denoise`` ``$width`` ``$height`` ``$hillshade``;
* LoRA — ``$lora`` and ``$lora_weight`` together, required iff a LoRA is given;
* optional controls — ``$slope`` ``$land`` (uploaded only when the template uses them).

A needed placeholder the template lacks, or a LoRA slot without a LoRA, is a
:class:`BackendError` that names the placeholder.
"""

from __future__ import annotations

import copy
import importlib.resources
import json
from pathlib import Path

from argus_atlas.backends.base import BackendError
from argus_atlas.hashing import sha256_bytes

EXAMPLE_TEMPLATE = "comfyui_sdxl_img2img_control.json"

REQUIRED_PLACEHOLDERS: tuple[str, ...] = (
    "$checkpoint",
    "$positive",
    "$negative",
    "$seed",
    "$steps",
    "$cfg",
    "$denoise",
    "$width",
    "$height",
    "$hillshade",
)
LORA_PLACEHOLDERS: tuple[str, ...] = ("$lora", "$lora_weight")
CONTROL_PLACEHOLDERS: dict[str, str] = {"hillshade": "$hillshade", "slope": "$slope", "land": "$land"}


def load_template(path: Path) -> dict:
    """Load and lightly validate a ComfyUI API-format workflow template."""
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendError(f"cannot read workflow template {path}: {exc}") from exc
    if not isinstance(template, dict) or not template:
        raise BackendError(f"workflow template {path} is not a non-empty ComfyUI API graph")
    return template


def example_template() -> dict:
    """The packaged starter graph (SDXL img2img + one ControlNet + one LoRA)."""
    resource = importlib.resources.files("argus_atlas").joinpath("templates", EXAMPLE_TEMPLATE)
    return json.loads(resource.read_text(encoding="utf-8"))


def template_digest(template: dict) -> str:
    """A stable SHA256 of the template, recorded in the bake record."""
    return sha256_bytes(json.dumps(template, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def placeholders_used(template: dict) -> set[str]:
    """Every ``$placeholder`` string that appears as an input value."""
    used: set[str] = set()
    for node in template.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            used.update(v for v in inputs.values() if isinstance(v, str) and v.startswith("$"))
    return used


def render_workflow(template: dict, values: dict[str, object]) -> dict:
    """A concrete graph: every placeholder in *values* substituted (typed).

    ``values`` maps ``"$name"`` to its value; ``None`` means "not supplied".
    Raises :class:`BackendError` when a required placeholder is absent from the
    template, when the template needs a value that was not supplied, or when a
    LoRA is supplied but the template has no slot for it (it would be silently
    dropped while the record claimed it was used).
    """
    used = placeholders_used(template)

    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in used]
    if missing:
        raise BackendError(f"workflow template is missing required placeholder(s): {', '.join(missing)}")

    lora_given = values.get("$lora") is not None
    lora_slots = [p for p in LORA_PLACEHOLDERS if p in used]
    if lora_given and len(lora_slots) != len(LORA_PLACEHOLDERS):
        absent = [p for p in LORA_PLACEHOLDERS if p not in used]
        raise BackendError(
            f"a LoRA was supplied but the workflow template lacks {', '.join(absent)} — it would be silently ignored"
        )
    if not lora_given and lora_slots:
        raise BackendError(
            f"workflow template uses {', '.join(lora_slots)} but no LoRA was supplied (--lora), "
            "or point ATLAS_WORKFLOW at a template without a LoRA slot"
        )

    unsupplied = sorted(p for p in used if p in values and values[p] is None)
    if unsupplied:
        raise BackendError(f"workflow template needs {', '.join(unsupplied)} but the bake has no value for it")

    rendered = copy.deepcopy(template)
    for node in rendered.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key, val in inputs.items():
            if isinstance(val, str) and val.startswith("$") and val in values:
                inputs[key] = values[val]
    return rendered
