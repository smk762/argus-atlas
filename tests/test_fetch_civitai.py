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


@pytest.mark.parametrize(
    ("newurl", "keeps"),
    [
        ("https://civitai.com/api/download/models/1?x=1", True),
        ("https://civitai.com:443/other", True),
        ("http://civitai.com/api/download/models/1", False),  # downgrade: cleartext token
        ("https://civitai.com:8443/api/download/models/1", False),
        ("https://cdn.example.net/file", False),
    ],
)
def test_redirect_keeps_the_token_only_on_the_same_https_origin(newurl: str, keeps: bool) -> None:
    import email.message
    import urllib.request

    req = urllib.request.Request("https://civitai.com/api/download/models/1", headers={"Authorization": "Bearer t"})
    new = script._SameHostAuthRedirect().redirect_request(req, None, 302, "Found", email.message.Message(), newurl)
    assert new is not None and new.has_header("Authorization") is keeps


def _model(mid: int) -> dict:
    files = [{"name": f"m{mid}.safetensors", "downloadUrl": f"https://x/{mid}", "hashes": {}}]
    version = {"id": mid * 10, "name": "v1", "baseModel": "SDXL 1.0", "files": files}
    return {"id": mid, "name": f"model {mid}", "type": "LORA", "modelVersions": [version]}


def test_a_failed_download_skips_to_the_next_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    def download(url: str, dest: Path, token: object, expected: object) -> str:
        if url.endswith("/1"):
            raise SystemExit("download failed: HTTP 403 Forbidden")
        dest.write_bytes(b"lora")
        return "ab" * 32

    monkeypatch.setattr(script, "fetch_json", lambda url, token: _model(int(url.rsplit("/", 1)[1])))
    monkeypatch.setattr(script, "download", download)
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "1", "2"])
    with pytest.raises(SystemExit, match="1 download"):
        script.main()
    assert list(tmp_path.rglob("*.civitai.json")) == [tmp_path / "loras/sdxl/cartography/model_2/m2.civitai.json"]
    assert "HTTP 403" in capsys.readouterr().err


def test_a_metadata_network_error_continues_with_the_next_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys
    import urllib.error

    def fetch(url: str, token: object) -> dict:
        mid = int(url.rsplit("/", 1)[1])
        if mid == 1:
            raise urllib.error.URLError("dns failed")
        return _model(mid)

    def download(url: str, dest: Path, token: object, expected: object) -> str:
        dest.write_bytes(b"lora")
        return "ab" * 32

    monkeypatch.setattr(script, "fetch_json", fetch)
    monkeypatch.setattr(script, "download", download)
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "1", "2"])
    with pytest.raises(SystemExit, match="1 download"):
        script.main()
    assert (tmp_path / "loras/sdxl/cartography/model_2/m2.civitai.json").is_file()
    assert "dns failed" in capsys.readouterr().err


def test_a_failed_metadata_lookup_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import email.message
    import sys
    import urllib.error

    def fetch(url: str, token: object) -> dict:
        raise urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", email.message.Message(), None)

    monkeypatch.setattr(script, "fetch_json", fetch)
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "9"])
    with pytest.raises(SystemExit, match="1 download"):
        script.main()
    err = capsys.readouterr().err
    assert "HTTP 451" in err and "region-blocked" in err
    assert not list(tmp_path.rglob("*.safetensors"))


def test_a_model_with_no_matching_version_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    model = {"id": 1, "name": "m", "modelVersions": [{"id": 2, "baseModel": "Pony", "files": []}]}
    monkeypatch.setattr(script, "fetch_json", lambda url, token: model)
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "1"])
    with pytest.raises(SystemExit, match="1 download"):
        script.main()


def test_a_version_without_safetensors_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    model = _model(4)
    model["modelVersions"][0]["files"] = [{"name": "weights.pt"}]
    monkeypatch.setattr(script, "fetch_json", lambda url, token: model)
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "4"])
    with pytest.raises(SystemExit, match="1 download"):
        script.main()


def test_an_existing_file_is_hashed_without_reading_it_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import sys

    model = _model(3)
    body = b"x" * 3_000_000
    model["modelVersions"][0]["files"][0]["hashes"] = {"SHA256": hashlib.sha256(body).hexdigest()}
    dest = tmp_path / "loras/sdxl/cartography/model_3/m3.safetensors"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(body)
    monkeypatch.setattr(script, "fetch_json", lambda url, token: model)
    monkeypatch.setattr(script, "download", lambda *a, **k: pytest.fail("already present"))
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read the whole file"))
    monkeypatch.setattr(sys, "argv", ["fetch", "--out", str(tmp_path), "3"])
    script.main()
