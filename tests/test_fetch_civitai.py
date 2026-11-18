from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_civitai_lora.py"
_spec = importlib.util.spec_from_file_location("fetch_civitai_lora", _SCRIPT)
assert _spec is not None and _spec.loader is not None
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


@pytest.mark.parametrize(
    "arg",
    [
        "382959",
        "https://civitai.com/models/382959",
        "https://civitai.com/models/382959/some-slug?modelVersionId=427436",
        "civitai.com/models/382959/",
    ],
)
def test_model_id_accepts_an_id_or_a_model_page(arg: str) -> None:
    assert script.model_id(arg) == 382959


@pytest.mark.parametrize(
    "arg",
    [
        "123abc",
        "12/foo",
        "",
        "-5",
        "https://civitai.com/api/download/models/427436",  # a version id, not a model id
        "https://civitai.com/api/v1/models/382959",
        "https://civitai.com/models/abc",
        "/models/382959",
    ],
)
def test_model_id_refuses_anything_else(arg: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        script.model_id(arg)


def test_describe_lists_base_models_when_a_version_has_none() -> None:
    model = {
        "id": 1,
        "name": "m",
        "modelVersions": [{"id": 1, "baseModel": "SDXL 1.0"}, {"id": 2}, {"id": 3, "baseModel": "Pony"}, {"id": 4}],
    }
    assert "available base models: ['Pony', 'SDXL 1.0']" in script.describe(model, None)
