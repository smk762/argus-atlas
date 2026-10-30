"""Place names for the drape: a GeoJSON overlay drawn by the viewer, never by the model.

The packaged prompts forbid text because diffusion invents illegible lettering
and breaks tile seams, so the painted map has no names. ``build_labels`` reads
a places file the caller already has (Natural Earth populated places, a
Geofabrik PBF, the ``places`` layer of an ``argus-atlas osm`` extract), keeps
the named points inside a lon/lat box whose cartographic rank survives at a
zoom, and writes them as EPSG:4326 GeoJSON with ``name``, ``rank`` (lower is
more important) and ``kind`` only. ``labels.json`` next to it records the
source file and its SHA256, the cutoff and the ogr2ogr argv, the way
``osm.json`` records an extract.

Two ranking schemes, told apart by the fields the points carry:

* Natural Earth: ``SCALERANK`` (0 = megacity) must be at most ``zoom - 2``,
  so zoom 6 keeps scalerank 0–4 and zoom 12 keeps all of them.
* OSM: the ``place`` class must be one of :func:`argus_atlas.osm.place_classes`
  at the zoom (cities from 0, towns from 8, villages from 11, suburbs and
  hamlets from 13, the rest from 15); the rank is the class's index.

``label_job`` does this for a job: the box is the canvas (a chunk's tile, so
neighbouring chunks never repeat a name), the zoom its native zoom, and the
result is recorded as ``labels`` in ``job.json``. Nothing here touches the
network, the model, the GeoTIFF or the PMTiles.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import load_sidecar, save_sidecar
from argus_atlas.mercator import mercator_to_bbox
from argus_atlas.models import AtlasError, BBox, FileRef, LabelsRecord
from argus_atlas.osm import PLACE_ORDER, place_classes

logger = structlog.get_logger()

LABELS_NAME = "labels.geojson"
MAX_SCALERANK = 10
OSM_SUFFIXES = (".osm", ".pbf")
LICENSES = {"naturalearth": "public domain — Natural Earth", "osm": "ODbL 1.0 — © OpenStreetMap contributors"}
# Decimal places of the written coordinates: 1e-7 degrees is about a centimetre.
COORD_DIGITS = 7


def max_scalerank(zoom: int) -> int:
    """The largest Natural Earth ``SCALERANK`` whose names survive at *zoom*."""
    return max(0, min(MAX_SCALERANK, zoom - 2))


def manifest_path(out: Path) -> Path:
    """``labels.json`` for ``labels.geojson``: the provenance next to the overlay."""
    return out.with_suffix(".json")


def _field(props: dict, name: str) -> object:
    """A property by name, ignoring case (Natural Earth spells them ``SCALERANK`` or ``scalerank``)."""
    for key, value in props.items():
        if key.lower() == name:
            return value
    return None


def _scheme(features: list[dict]) -> str | None:
    keys = {key.lower() for f in features for key in (f.get("properties") or {})}
    if "scalerank" in keys:
        return "naturalearth"
    if "place" in keys:
        return "osm"
    if features:
        raise AtlasError(
            "cannot rank these places: expected a Natural Earth SCALERANK field or an OSM place field, "
            f"found {', '.join(sorted(keys)) or 'no fields'}"
        )
    return None


def _rank(props: dict, scheme: str, zoom: int) -> tuple[int, str] | None:
    """``(rank, kind)`` if the point survives at *zoom*, else ``None``."""
    if scheme == "naturalearth":
        try:
            rank = int(float(_field(props, "scalerank")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        kind = str(_field(props, "featurecla") or "populated place").lower()
        return (rank, kind) if rank <= max_scalerank(zoom) else None
    place = str(_field(props, "place") or "")
    return (PLACE_ORDER.index(place), place) if place in place_classes(zoom) else None


def _inside(lon: float, lat: float, bbox: BBox) -> bool:
    return bbox.west <= lon <= bbox.east and bbox.south <= lat <= bbox.north


def rank_features(features: list[dict], bbox: BBox, zoom: int) -> tuple[list[dict], str | None, dict[str, int]]:
    """The named points inside *bbox* that survive at *zoom*, most important first.

    Returns the output features, the scheme and how many were dropped for which reason.
    """
    scheme = _scheme(features)
    kept: list[dict] = []
    dropped = {"not_a_point": 0, "outside": 0, "unnamed": 0, "below_cutoff": 0}
    for feature in features:
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if geometry.get("type") != "Point":
            dropped["not_a_point"] += 1
            continue
        lon, lat = (float(v) for v in geometry["coordinates"][:2])
        if not _inside(lon, lat, bbox):  # -spat filters by envelope; re-check against the box itself
            dropped["outside"] += 1
            continue
        name = str(_field(props, "name") or _field(props, "nameascii") or "").strip()
        if not name:
            dropped["unnamed"] += 1
            continue
        ranked = _rank(props, scheme, zoom)  # type: ignore[arg-type]  # scheme is set when features exist
        if ranked is None:
            dropped["below_cutoff"] += 1
            continue
        rank, kind = ranked
        kept.append(
            {
                "type": "Feature",
                "properties": {"name": name, "rank": rank, "kind": kind},
                "geometry": {"type": "Point", "coordinates": [round(lon, COORD_DIGITS), round(lat, COORD_DIGITS)]},
            }
        )
    kept.sort(key=lambda f: (f["properties"]["rank"], f["properties"]["name"]))
    return kept, scheme, dropped


def describe_cutoff(scheme: str | None, zoom: int) -> str:
    if scheme == "naturalearth":
        return f"scalerank <= {max_scalerank(zoom)}"
    if scheme == "osm":
        return "place in " + ",".join(place_classes(zoom))
    return "none"


def _is_osm(source: Path) -> bool:
    return source.name.lower().endswith(OSM_SUFFIXES)


def build_labels(source: Path, bbox: BBox, zoom: int, out: Path, *, layer: str | None = None) -> dict:
    """Write the ranked place names of *source* inside *bbox* to *out* and its manifest.

    *source* is any OGR-readable points file; an ``.osm``/``.pbf`` file is read through
    its ``points`` layer, pre-filtered to the place classes of *zoom*. Returns the manifest.
    """
    source = source.expanduser()
    if not source.is_file():
        raise AtlasError(f"places file not found: {source}")
    if out.suffix.lower() != ".geojson":
        raise AtlasError(f"labels are written as GeoJSON: {out} must end in .geojson")
    gdal_ops.require_gdal()
    where = None
    if _is_osm(source):
        layer = layer or "points"
        where = "place IN (" + ", ".join(f"'{c}'" for c in place_classes(zoom)) + ")"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".labels-") as tmp:
        raw = Path(tmp) / "places.geojson"
        command = gdal_ops.run(
            gdal_ops.places_argv(source, raw, tuple(bbox.as_list()), layer=layer, where=where)  # type: ignore[arg-type]
        )
        features = json.loads(raw.read_text(encoding="utf-8")).get("features") or []
    kept, scheme, dropped = rank_features(features, bbox, zoom)
    collection = {"type": "FeatureCollection", "features": kept}
    tmp_out = out.with_name(out.name + ".tmp")
    tmp_out.write_text(json.dumps(collection, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_out.replace(out)
    manifest = {
        "source": {
            "path": str(source.resolve()),
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
            "layer": layer,
        },
        "scheme": scheme,
        "license": LICENSES.get(scheme or ""),
        "bbox": bbox.as_text(),
        "zoom": zoom,
        "cutoff": describe_cutoff(scheme, zoom),
        "count": len(kept),
        "dropped": dropped,
        "output": {"path": out.name, "sha256": sha256_file(out)},
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commands": [command.model_dump()],
    }
    manifest_path(out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("labels.done", count=len(kept), scheme=scheme, cutoff=manifest["cutoff"], dropped=dropped)
    return manifest


def label_job(job_dir: Path, source: Path, *, zoom: int | None = None, layer: str | None = None) -> LabelsRecord:
    """Label a job's canvas (a chunk's tile) at its native zoom and record it in ``job.json``."""
    sidecar = load_sidecar(job_dir)
    grid = sidecar.chunk.inner if sidecar.chunk else sidecar.grid
    zoom = sidecar.native_zoom if zoom is None else zoom
    manifest = build_labels(source, mercator_to_bbox(*grid.bounds), zoom, job_dir / LABELS_NAME, layer=layer)
    record = LabelsRecord(
        source=FileRef(path=manifest["source"]["path"], sha256=manifest["source"]["sha256"]),
        scheme=manifest["scheme"],
        zoom=zoom,
        cutoff=manifest["cutoff"],
        count=manifest["count"],
        file=FileRef(path=LABELS_NAME, sha256=manifest["output"]["sha256"]),
        completed_at=manifest["created_at"],
    )
    save_sidecar(job_dir, sidecar.model_copy(update={"labels": record}))
    return record
