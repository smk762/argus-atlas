"""Who a published tile set must credit: the DEM, the vector layers and the place names behind it.

Copernicus GLO-30 and OpenStreetMap (ODbL) both require attribution wherever their derivatives are
published, so atlas records each input's source when the controls are built (``JobSidecar.sources``)
and writes the credit into every archive it publishes (``tiles``: the PMTiles ``attribution``
metadata, which MapLibre shows; ``mosaic``: the same, plus the manifest).

A source is recognised from the provenance atlas wrote when it fetched it, never from a file name:

* a DEM is Copernicus GLO-30 when ``<dem>.json`` (written by ``argus-atlas dem``) says so and its
  output digest matches the DEM the job used;
* a layer is OpenStreetMap when the ``osm.json`` beside it (written by ``argus-atlas osm``) lists it;
* labels are Natural Earth or OpenStreetMap by the scheme :mod:`argus_atlas.labels` detected.

Anything else is an ``unknown`` source: recorded by its file name with no attribution, so the
manifests say plainly that the operator must credit it, and ``tiles``/``mosaic`` warn.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from argus_atlas.dem import ATTRIBUTION as DEM_ATTRIBUTION
from argus_atlas.models import FileRef, JobSidecar, LabelsRecord, SourceRecord

UNKNOWN = "unknown"

COPERNICUS = SourceRecord(
    key="copernicus-glo30",
    name="Copernicus GLO-30 DEM",
    attribution=f"Copernicus GLO-30: {DEM_ATTRIBUTION}",  # the exact notice the licence asks for
    license="Copernicus DEM licence",
    url="https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model",
)
OPENSTREETMAP = SourceRecord(
    key="openstreetmap",
    name="OpenStreetMap",
    attribution="© OpenStreetMap contributors (ODbL 1.0)",
    license="ODbL-1.0",
    url="https://www.openstreetmap.org/copyright",
)
NATURAL_EARTH = SourceRecord(
    key="naturalearth",
    name="Natural Earth",
    attribution="Made with Natural Earth",
    license="public domain",
    url="https://www.naturalearthdata.com",
)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unknown(path: str, used_for: str) -> SourceRecord:
    return SourceRecord(key=UNKNOWN, name=Path(path).name, used_for=[used_for])


def dem_source(dem: FileRef) -> SourceRecord:
    """Copernicus GLO-30 when the DEM's ``argus-atlas dem`` provenance vouches for these exact bytes."""
    path = Path(dem.path)
    manifest = _read_json(path.with_suffix(".json"))
    output = (manifest or {}).get("output") or {}
    if (
        manifest is not None
        and str(manifest.get("product", "")).startswith("Copernicus GLO-30")
        and isinstance(output, dict)
        and output.get("path") == path.name
        and output.get("sha256") == dem.sha256
    ):
        return COPERNICUS.model_copy(update={"used_for": ["dem"]})
    return _unknown(dem.path, "dem")


def layer_source(name: str, ref: FileRef) -> SourceRecord:
    """OpenStreetMap when the ``osm.json`` beside the layer lists this file with these bytes."""
    path = Path(ref.path)
    manifest = _read_json(path.parent / "osm.json")
    if manifest is not None and manifest.get("source") == "openstreetmap":
        layers = manifest.get("layers") or {}
        for entry in layers.values() if isinstance(layers, dict) else []:
            if isinstance(entry, dict) and entry.get("path") == path.name and entry.get("sha256") == ref.sha256:
                return OPENSTREETMAP.model_copy(update={"used_for": [name]})
    return _unknown(ref.path, name)


def labels_source(record: LabelsRecord) -> SourceRecord:
    """The source of a job's place names, by the scheme the places file was read with."""
    if record.scheme == "naturalearth":
        return NATURAL_EARTH.model_copy(update={"used_for": ["labels"]})
    if record.scheme == "osm":
        return OPENSTREETMAP.model_copy(update={"used_for": ["labels"]})
    return _unknown(record.source.path, "labels")


def merge_sources(sources: Iterable[SourceRecord]) -> list[SourceRecord]:
    """One record per source (unknown ones per file name), their ``used_for`` unioned, known sources first."""
    merged: dict[tuple[str, str], SourceRecord] = {}
    for src in sources:
        key = (src.key, src.name if src.key == UNKNOWN else "")
        held = merged.get(key)
        used = sorted({*(held.used_for if held else []), *src.used_for})
        merged[key] = (held or src).model_copy(update={"used_for": used})
    return sorted(merged.values(), key=lambda s: (s.key == UNKNOWN, s.key, s.name))


def input_sources(dem: FileRef, layers: dict[str, FileRef]) -> list[SourceRecord]:
    """The sources of a job's DEM and layers, for ``JobSidecar.sources`` (the ``sea`` mask is the DEM's)."""
    return merge_sources([dem_source(dem), *(layer_source(name, ref) for name, ref in sorted(layers.items()))])


def job_sources(sidecar: JobSidecar, *, labels: bool = True) -> list[SourceRecord]:
    """What a job's published output must credit: its recorded inputs (worked out from the recorded paths
    for a job from before 0.5), plus its labels' source unless *labels* is false. The raster archive passes
    false: names are never in its pixels, and its credit must not depend on when the job was labelled."""
    sources = (
        list(sidecar.sources) if sidecar.sources is not None else input_sources(sidecar.dem, sidecar.layer_sources)
    )
    if labels and sidecar.labels is not None and sidecar.labels.count:
        sources.append(labels_source(sidecar.labels))
    return merge_sources(sources)


def attribution_text(sources: Iterable[SourceRecord]) -> str:
    """The credit line for an archive: every known source's attribution, in order, once."""
    seen: list[str] = []
    for src in sources:
        if src.attribution and src.attribution not in seen:
            seen.append(src.attribution)
    return " | ".join(seen)


def unattributed(sources: Iterable[SourceRecord]) -> list[str]:
    """The names of sources atlas could not attribute: the operator must credit them."""
    return [f"{src.name} ({', '.join(src.used_for)})" for src in sources if src.key == UNKNOWN]
