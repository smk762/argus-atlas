from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_atlas import metadata
from argus_atlas.models import AtlasError, BakeParams, ModelRef
from argus_atlas.png import png_size, write_png
from argus_atlas.presets import PRESETS, get_preset, resolve_params
from tests.test_server import _seed_job


def _payload(tmp_path: Path) -> dict:
    sidecar = _seed_job(tmp_path / "jobs", "meta")
    params = BakeParams(checkpoint="ck.safetensors", seed=7, preset="antique", prompt="x")
    return metadata.bake_payload(
        sidecar,
        params,
        checkpoint=ModelRef(name="ck.safetensors", sha256="a" * 64),
        lora=None,
        backend="fake",
        engine_version="0",
        workflow_sha256=None,
    )


def test_png_payload_roundtrip_keeps_pixels(tmp_path: Path) -> None:
    png = write_png(tmp_path / "f.png", 64, 32)
    before = png.read_bytes()
    payload = _payload(tmp_path)
    metadata.embed_png(png, payload)
    after = png.read_bytes()
    assert png_size(png) == (64, 32)
    assert b"eXIf" in after and b"tEXt" in after and after.endswith(before[-12:])  # IEND intact
    idat = lambda data: [body for kind, body in metadata._chunks(data) if kind == b"IDAT"]  # noqa: E731
    assert idat(after) == idat(before)  # pixel data byte-identical
    read = metadata.read_png_payload(png)
    assert read == json.loads(metadata.payload_json(payload))
    assert read["params"]["preset"] == "antique" and read["job_id"] == "meta"
    # embedding twice replaces, not appends
    metadata.embed_png(png, payload)
    assert after.count(b"eXIf") == png.read_bytes().count(b"eXIf") == 1


def test_exif_blob_parses_back() -> None:
    blob = metadata.build_exif('{"a":1}')
    assert blob.startswith(b"II*\x00")
    assert metadata.parse_exif_description(blob) == '{"a":1}'
    assert metadata.parse_exif_description(b"garbage") is None


def test_exif_readable_by_pillow_if_present(tmp_path: Path) -> None:
    pillow = pytest.importorskip("PIL.Image")
    png = write_png(tmp_path / "p.png", 16, 16)
    metadata.embed_png(png, {"k": "v"})
    exif = pillow.open(png).getexif()
    assert json.loads(exif[0x010E]) == {"k": "v"}


def test_presets_resolve_and_override() -> None:
    assert len(PRESETS) >= 6 and all(p.prompt and 0 < p.denoise <= 1 for p in PRESETS.values())
    assert sum(p.recommended for p in PRESETS.values()) == 6  # the golden six
    assert all("no compass rose" not in p.prompt for p in PRESETS.values())  # negations go in the negative
    knobs = resolve_params("tolkien")
    assert knobs["preset"] == "tolkien" and knobs["denoise"] == get_preset("tolkien").denoise
    assert knobs["prompt"] == get_preset("tolkien").prompt
    knobs = resolve_params("tolkien", denoise=0.4, prompt="custom")
    assert knobs["denoise"] == 0.4 and knobs["prompt"] == "custom" and knobs["preset"] == "tolkien"
    assert resolve_params(None)["preset"] is None
    with pytest.raises(AtlasError, match="unknown preset"):
        resolve_params("sepia-dreams")


def test_exif_goes_before_the_image_data(tmp_path: Path) -> None:
    png = write_png(tmp_path / "f.png", 8, 8)
    metadata.embed_png(png, {"k": "v"})
    kinds = [kind for kind, _ in metadata._chunks(png.read_bytes())]
    assert kinds.index(b"eXIf") < kinds.index(b"IDAT") and kinds.index(b"tEXt") < kinds.index(b"IDAT")
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("text", ["", "ab", "abc", "abcd", "abcdefgh"])
def test_short_ascii_values_are_inline(text: str) -> None:
    import struct

    blob = metadata.build_exif(text, software="sw")
    assert metadata.parse_exif_description(blob) == text
    # IFD0 entry 0 is ImageDescription, entry 1 Software ("sw\0" is 3 bytes: inline, NUL-padded)
    tag, kind, count = struct.unpack("<HHI", blob[22:30])
    assert (tag, kind, count, blob[30:34]) == (0x0131, 2, 3, b"sw\x00\x00")
    tag, _, count = struct.unpack("<HHI", blob[10:18])
    assert tag == 0x010E and count == len(text) + 1
    if count <= 4:
        assert blob[18:22] == (text.encode() + b"\x00").ljust(4, b"\x00")


def test_truncated_ifd_is_none_not_an_error() -> None:
    blob = metadata.build_exif('{"a":1}')
    head = blob[:8] + b"\xff\xff" + blob[10:20]  # claims 65535 entries, holds one
    assert metadata.parse_exif_description(head) is None


def test_a_truncated_png_chunk_is_an_error_and_nothing_follows_iend(tmp_path: Path) -> None:
    png = write_png(tmp_path / "f.png", 8, 8)
    metadata.embed_png(png, {"a": 1})
    whole = png.read_bytes()
    cut = tmp_path / "cut.png"
    cut.write_bytes(whole[: whole.index(b"tEXt") + 10])  # the chunk says more bytes than the file holds
    with pytest.raises(AtlasError, match="truncated PNG chunk"):
        metadata.read_png_payload(cut)
    trailing = tmp_path / "trailing.png"
    trailing.write_bytes(whole + b"\x00\x00\x00\x02junkxx")
    assert [kind for kind, _ in metadata._chunks(trailing.read_bytes())][-1] == b"IEND"
    assert metadata.read_png_payload(trailing) == {"a": 1}


@pytest.mark.parametrize("description", ["Untitled", "42", "[1, 2]", '"text"', "{broken"])
def test_another_tools_exif_description_is_not_a_payload(tmp_path: Path, description: str) -> None:
    png = write_png(tmp_path / "f.png", 8, 8)
    data = png.read_bytes()
    exif = metadata._chunk(b"eXIf", metadata.build_exif(description, software="SomeEditor 1.0"))
    png.write_bytes(data[:33] + exif + data[33:])  # after IHDR (8 + 25 bytes)
    assert metadata.read_png_payload(png) is None


def test_the_payload_is_json_all_the_way_down(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    assert json.loads(metadata.payload_json(payload)) == payload
    assert payload["checkpoint"] == {"name": "ck.safetensors", "sha256": "a" * 64} and payload["lora"] is None


@pytest.mark.parametrize(("description", "software"), [('{"a":12}', "argus"), ('{"a":1}', "sw-1"), ("x" * 9, "y" * 9)])
def test_exif_offsets_are_word_aligned(description: str, software: str) -> None:
    import struct

    blob = metadata.build_exif(description, software=software)
    (ifd0,) = struct.unpack("<I", blob[4:8])
    entries = [struct.unpack("<HHII", blob[ifd0 + 2 + 12 * i : ifd0 + 14 + 12 * i]) for i in range(3)]
    exif_ifd = next(value for tag, _, _, value in entries if tag == 0x8769)
    offsets = [value for _, _, count, value in entries if count > 4] + [exif_ifd]
    _, _, count, comment = struct.unpack("<HHII", blob[exif_ifd + 2 : exif_ifd + 14])
    assert all(off % 2 == 0 for off in [ifd0, *offsets, comment]), offsets
    assert [c for t, _, c, _ in entries if t == 0x010E] == [len(description) + 1]  # counts stay unpadded
    assert metadata.parse_exif_description(blob) == description
    assert blob[comment : comment + count].endswith(description.encode())
