"""The bake payload every output image carries, and how it is written.

Ranking a hundred bakes later needs the prompt, model, seed and preset *in the
file*, not in a sidecar that may have been separated from it. So after the
backend writes ``fantasy.png`` the payload (a compact JSON document) goes into
the PNG twice — an ``eXIf`` chunk (``ImageDescription`` + ``UserComment``,
readable by exiftool / Pillow's ``getexif``) and a ``tEXt`` chunk keyed
``argus-atlas`` for tools that only read text chunks — then into the GeoTIFF as
``TIFFTAG_IMAGEDESCRIPTION`` when it is stamped, and into the PMTiles
archive's metadata when it is tiled. Pixels are never touched, so the alignment
gate sees the same image; the sidecar records the digest of the file *with*
the metadata.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from argus_atlas import __version__
from argus_atlas.models import AtlasError, BakeParams, JobSidecar, ModelRef

TEXT_KEY = "argus-atlas"
SOFTWARE = f"argus-atlas {__version__}"
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def bake_payload(
    sidecar: JobSidecar,
    params: BakeParams,
    *,
    checkpoint: ModelRef,
    lora: ModelRef | None,
    backend: str,
    engine_version: str,
    workflow_sha256: str | None,
) -> dict:
    """What a bake *is*: enough to reproduce it and to rank it."""
    return {
        "generator": SOFTWARE,
        "atlas_version": sidecar.atlas_version,
        "job_id": sidecar.job_id,
        "bbox": sidecar.bbox.as_text(),
        "crs": sidecar.grid.crs,
        "size": [sidecar.grid.width, sidecar.grid.height],
        "resolution_m": round(sidecar.resolution, 4),
        "native_zoom": sidecar.native_zoom,
        "controls": sorted(sidecar.controls),
        "dem_sha256": sidecar.dem.sha256,
        "params": params.model_dump(mode="json"),
        "checkpoint": checkpoint.model_dump(),
        "lora": lora.model_dump() if lora else None,
        "backend": backend,
        "engine_version": engine_version,
        "workflow_sha256": workflow_sha256,
    }


def payload_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


# -- EXIF -------------------------------------------------------------------------

_TAG_IMAGE_DESCRIPTION = 0x010E
_TAG_SOFTWARE = 0x0131
_TAG_EXIF_IFD = 0x8769
_TAG_USER_COMMENT = 0x9286
_ASCII, _LONG, _UNDEFINED = 2, 4, 7


def build_exif(description: str, software: str = SOFTWARE) -> bytes:
    """A little-endian TIFF/EXIF structure: IFD0 {ImageDescription, Software,
    ExifIFD} and an Exif IFD {UserComment = description}."""
    desc = description.encode("ascii", "replace") + b"\x00"
    soft = software.encode("ascii", "replace") + b"\x00"
    comment = b"ASCII\x00\x00\x00" + description.encode("ascii", "replace")

    ifd0_off = 8
    ifd0_len = 2 + 3 * 12 + 4
    desc_off = ifd0_off + ifd0_len
    soft_off = desc_off + len(desc)
    exif_ifd_off = soft_off + len(soft)
    exif_ifd_len = 2 + 1 * 12 + 4
    comment_off = exif_ifd_off + exif_ifd_len

    def entry(tag: int, kind: int, count: int, value_or_offset: int) -> bytes:
        return struct.pack("<HHII", tag, kind, count, value_or_offset)

    ifd0 = struct.pack("<H", 3)
    ifd0 += entry(_TAG_IMAGE_DESCRIPTION, _ASCII, len(desc), desc_off)
    ifd0 += entry(_TAG_SOFTWARE, _ASCII, len(soft), soft_off)
    ifd0 += entry(_TAG_EXIF_IFD, _LONG, 1, exif_ifd_off)
    ifd0 += struct.pack("<I", 0)
    exif_ifd = struct.pack("<H", 1) + entry(_TAG_USER_COMMENT, _UNDEFINED, len(comment), comment_off)
    exif_ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd0_off) + ifd0 + desc + soft + exif_ifd + comment


def parse_exif_description(blob: bytes) -> str | None:
    """``ImageDescription`` out of a little- or big-endian TIFF/EXIF blob (ours, or anyone's)."""
    if len(blob) < 8 or blob[:2] not in (b"II", b"MM"):
        return None
    end = "<" if blob[:2] == b"II" else ">"
    (ifd_off,) = struct.unpack(end + "I", blob[4:8])
    if ifd_off + 2 > len(blob):
        return None
    (n,) = struct.unpack(end + "H", blob[ifd_off : ifd_off + 2])
    for i in range(n):
        at = ifd_off + 2 + 12 * i
        tag, kind, count, value = struct.unpack(end + "HHII", blob[at : at + 12])
        if tag == _TAG_IMAGE_DESCRIPTION and kind == _ASCII:
            raw = blob[at + 8 : at + 12] if count <= 4 else blob[value : value + count]
            return raw.rstrip(b"\x00").decode("ascii", "replace")
    return None


# -- PNG chunks --------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _chunks(data: bytes):  # noqa: ANN202 - Iterator[tuple[bytes, bytes]]
    if data[:8] != _SIGNATURE:
        raise AtlasError("not a PNG")
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        yield kind, body
        pos += 12 + length


def embed_png(path: Path, payload: dict) -> None:
    """Rewrite *path* with the payload in an ``eXIf`` and a ``tEXt`` chunk (replacing
    any earlier ones of ours); IHDR/IDAT/IEND and every other chunk are kept."""
    text = payload_json(payload)
    data = path.read_bytes()
    out = bytearray(_SIGNATURE)
    for kind, body in _chunks(data):
        if kind == b"eXIf" or (kind == b"tEXt" and body.startswith(TEXT_KEY.encode() + b"\x00")):
            continue
        if kind == b"IEND":
            out += _chunk(b"eXIf", build_exif(text))
            out += _chunk(b"tEXt", TEXT_KEY.encode("latin-1") + b"\x00" + text.encode("latin-1", "replace"))
        out += _chunk(kind, body)
    path.write_bytes(bytes(out))


def read_png_payload(path: Path) -> dict | None:
    """The embedded payload, from the ``tEXt`` chunk, else the ``eXIf`` description; ``None`` if absent."""
    data = path.read_bytes()
    exif_text = None
    for kind, body in _chunks(data):
        if kind == b"tEXt" and body.startswith(TEXT_KEY.encode() + b"\x00"):
            return json.loads(body[len(TEXT_KEY) + 1 :].decode("latin-1"))
        if kind == b"eXIf":
            exif_text = parse_exif_description(body)
    return json.loads(exif_text) if exif_text else None
