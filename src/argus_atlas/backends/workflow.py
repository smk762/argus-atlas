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
* optional controls — ``$slope``, ``$composite``, ``$land`` or any ``$<layer>`` the
  job has (uploaded only when the template uses them); a ``$name`` the job cannot
  supply is an error.

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
# Same graph without the LoraLoader, for a first bake before a cartography LoRA exists.
EXAMPLE_TEMPLATE_NOLORA = "comfyui_sdxl_img2img_control_nolora.json"
# No LoRA, and the img2img init image is the painted composite (water, forest,
# roads …) while the ControlNet still reads the hillshade. Needs a job with layers.
EXAMPLE_TEMPLATE_COMPOSITE = "comfyui_sdxl_img2img_composite_nolora.json"
PACKAGED_TEMPLATES: tuple[str, ...] = (EXAMPLE_TEMPLATE, EXAMPLE_TEMPLATE_NOLORA, EXAMPLE_TEMPLATE_COMPOSITE)

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
# Every non-control placeholder a template may use; anything else that starts
# with "$" must be a control of the job (``$hillshade``, ``$slope``, ``$composite``, ``$<layer>``).
VALUE_PLACEHOLDERS: frozenset[str] = frozenset(
    {*REQUIRED_PLACEHOLDERS, *LORA_PLACEHOLDERS, "$sampler", "$scheduler"} - {"$hillshade"}
)


def control_placeholder(kind: str) -> str:
    return f"${kind}"


def control_placeholders_used(template: dict) -> dict[str, str]:
    """``kind -> placeholder`` for every placeholder in *template* that names a control."""
    return {p[1:]: p for p in placeholders_used(template) if p not in VALUE_PLACEHOLDERS}


def load_template(path: Path) -> dict:
    """Load and lightly validate a ComfyUI API-format workflow template."""
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendError(f"cannot read workflow template {path}: {exc}") from exc
    if not isinstance(template, dict) or not template:
        raise BackendError(f"workflow template {path} is not a non-empty ComfyUI API graph")
    return template


def example_template(name: str = EXAMPLE_TEMPLATE) -> dict:
    """A packaged starter graph: SDXL img2img + one ControlNet, with (default)
    or without (``EXAMPLE_TEMPLATE_NOLORA``) a LoRA slot."""
    if name not in PACKAGED_TEMPLATES:
        raise BackendError(f"no packaged workflow template {name!r}; choose one of {', '.join(PACKAGED_TEMPLATES)}")
    resource = importlib.resources.files("argus_atlas").joinpath("templates", name)
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
