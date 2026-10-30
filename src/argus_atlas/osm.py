"""OpenStreetMap features for an AOI, as per-layer GeoJSON a ``controls`` job can burn.

``fetch_osm`` asks an Overpass API for every feature a set of *layers* needs
inside a bbox, saves the raw ``.osm`` XML, and lets GDAL's OSM driver (which
assembles multipolygon relations) split it into one EPSG:4326 GeoJSON per
layer. The query, the Overpass endpoint, the data timestamp and the SHA256 of
every output go into ``osm.json`` next to them, so a job's ``layer_sources``
can be traced back to an OSM extract.

Layers are **scale-aware**: ``roads`` at zoom 9 keeps motorways and trunks,
at zoom 11 adds primaries, at 13 secondaries and tertiaries, at 14 and above
everything that is not a service road or track; ``buildings`` only exist from
zoom 13. Pass the job's native zoom (``controls`` prints it) so a 78 m/px
canvas is not swamped by footpaths.

This and the `dem` command are where atlas talks to the network; it is a CLI step the
operator runs (never a server route): OSM data is ODbL and the extract is a
recorded input like the DEM.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from argus_atlas import gdal_ops
from argus_atlas.hashing import sha256_file
from argus_atlas.models import AtlasError, BBox, GdalCommand

logger = structlog.get_logger()

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "argus-atlas (+https://github.com/smk762/argus-atlas)"
EXTRACT_NAME = "extract.osm"
MANIFEST_NAME = "osm.json"


@dataclass(frozen=True)
class LayerPreset:
    """One thematic layer: which OSM layer GDAL exposes it on, and the tag
    rules per zoom (the entry with the largest ``min_zoom <= zoom`` applies)."""

    geometry: str  # "polygon" | "line" | "point"
    osm_layer: str  # "multipolygons" | "lines" | "points" (GDAL OSM driver layer names)
    rules: dict[int, dict[str, list[str] | None]] = field(default_factory=dict)  # min_zoom -> {tag: values|None(any)}
    min_zoom: int = 0  # below this the layer is empty on purpose

    def tags_for(self, zoom: int) -> dict[str, list[str] | None]:
        applicable = [z for z in self.rules if z <= zoom]
        return self.rules[max(applicable)] if applicable else {}


ROADS_MAJOR = ["motorway", "motorway_link", "trunk", "trunk_link"]
ROADS_PRIMARY = ROADS_MAJOR + ["primary", "primary_link"]
ROADS_SECONDARY = ROADS_PRIMARY + ["secondary", "secondary_link", "tertiary", "tertiary_link"]
ROADS_ALL = ROADS_SECONDARY + ["unclassified", "residential", "living_street", "pedestrian"]

# place=* classes, most important first; their index is a label's rank. The entry with the largest
# min_zoom <= zoom says which classes survive at that zoom (`argus-atlas labels`, the `places` layer).
PLACE_ORDER = ["city", "town", "village", "suburb", "hamlet", "neighbourhood", "locality", "isolated_dwelling"]
PLACE_RULES: dict[int, list[str]] = {
    0: PLACE_ORDER[:1],
    8: PLACE_ORDER[:2],
    11: PLACE_ORDER[:3],
    13: PLACE_ORDER[:5],
    15: PLACE_ORDER,
}


def place_classes(zoom: int) -> list[str]:
    """The ``place=*`` values whose names survive at *zoom*."""
    return PLACE_RULES[max(z for z in PLACE_RULES if z <= zoom)]


LAYER_PRESETS: dict[str, LayerPreset] = {
    "water": LayerPreset(
        "polygon",
        "multipolygons",
        {
            0: {
                "natural": ["water", "bay", "strait"],
                "landuse": ["reservoir", "basin"],
            }
        },
    ),
    "rivers": LayerPreset(
        "line", "lines", {0: {"waterway": ["river", "canal"]}, 12: {"waterway": ["river", "canal", "stream"]}}
    ),
    "roads": LayerPreset(
        "line",
        "lines",
        {
            0: {"highway": ROADS_MAJOR},
            11: {"highway": ROADS_PRIMARY},
            13: {"highway": ROADS_SECONDARY},
            14: {"highway": ROADS_ALL},
        },
    ),
    "rail": LayerPreset("line", "lines", {0: {"railway": ["rail", "light_rail", "narrow_gauge"]}}),
    "forest": LayerPreset("polygon", "multipolygons", {0: {"landuse": ["forest"], "natural": ["wood"]}}),
    "urban": LayerPreset(
        "polygon",
        "multipolygons",
        {0: {"landuse": ["residential", "industrial", "commercial", "retail"]}},
    ),
    "buildings": LayerPreset("polygon", "multipolygons", {13: {"building": None}}, min_zoom=13),
    "glaciers": LayerPreset("polygon", "multipolygons", {0: {"natural": ["glacier"]}}),
    # Named place nodes for `argus-atlas labels` (a vector overlay); not a control the model paints.
    "places": LayerPreset("point", "points", {z: {"place": classes} for z, classes in PLACE_RULES.items()}),
}


def _overpass_filter(tag: str, values: list[str] | None) -> str:
    if values is None:
        return f'["{tag}"]'
    if len(values) == 1:
        return f'["{tag}"="{values[0]}"]'
    return f'["{tag}"~"^({"|".join(values)})$"]'


def _sql_filter(tag: str, values: list[str] | None) -> str:
    if values is None:
        return f"{tag} IS NOT NULL"
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{tag} IN ({quoted})"


def build_query(bbox: BBox, layers: list[str], zoom: int, *, timeout: int = 180) -> str:
    """The Overpass QL that fetches every element the *layers* need at *zoom*."""
    area = f"({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    clauses: list[str] = []
    for name in layers:
        preset = _preset(name)
        for tag, values in preset.tags_for(zoom).items():
            flt = _overpass_filter(tag, values)
            if preset.geometry == "point":
                clauses.append(f"node{flt}{area};")
                continue
            clauses.append(f"way{flt}{area};")
            if preset.geometry == "polygon":
                clauses.append(f"relation{flt}{area};")
    if not clauses:
        raise AtlasError(f"no OSM features to fetch for {layers} at zoom {zoom}")
    # Recurse down to the nodes and print everything in Overpass's default order
    # (nodes, then ways, then relations, by id): GDAL's OSM driver reads the file
    # sequentially and drops a way whose nodes it has not seen yet, so the
    # smaller "out body; >; out skel qt;" form (ways first, nodes by quadtile)
    # yields zero features.
    return f"[out:xml][timeout:{timeout}];({''.join(clauses)});(._;>;);out body;"


def where_clause(name: str, zoom: int) -> str:
    """The OGR ``-where`` filter that picks *name*'s features out of the extract at *zoom*."""
    tags = _preset(name).tags_for(zoom)
    if not tags:
        raise AtlasError(f"layer {name!r} is empty below zoom {_preset(name).min_zoom}")
    return " OR ".join(_sql_filter(tag, values) for tag, values in tags.items())


def _preset(name: str) -> LayerPreset:
    try:
        return LAYER_PRESETS[name]
    except KeyError as exc:
        raise AtlasError(f"unknown OSM layer {name!r}; choose from {', '.join(LAYER_PRESETS)}") from exc


def overpass_fetch(query: str, url: str = DEFAULT_OVERPASS_URL, *, timeout: float = 300.0) -> bytes:
    """POST *query* to an Overpass endpoint and return the XML. Overpass refuses
    requests without a User-Agent, so one is always sent."""
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(400).decode("utf-8", "replace").strip()
        raise AtlasError(f"Overpass {url} answered HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AtlasError(f"Overpass {url} unreachable: {exc}") from exc
    if not body.lstrip().startswith(b"<?xml"):
        raise AtlasError(f"Overpass {url} returned something that is not OSM XML: {body[:200]!r}")
    return body


def extract_layers(osm: Path, out_dir: Path, layers: list[str], zoom: int) -> tuple[dict[str, Path], list[GdalCommand]]:
    """Split an ``.osm`` file into ``<layer>.geojson`` files with ``ogr2ogr``.

    Layers that are empty at *zoom* (``buildings`` below 13) are skipped, not
    written empty, so a job never burns a blank mask by accident.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    commands: list[GdalCommand] = []
    for name in layers:
        preset = _preset(name)
        if zoom < preset.min_zoom:
            logger.info("osm.layer_skipped", layer=name, zoom=zoom, min_zoom=preset.min_zoom)
            continue
        dst = out_dir / f"{name}.geojson"
        dst.unlink(missing_ok=True)
        commands.append(gdal_ops.run(gdal_ops.osm_layer_argv(osm, dst, preset.osm_layer, where_clause(name, zoom))))
        written[name] = dst
    return written, commands


def osm_timestamp(osm: Path) -> str | None:
    """The ``osm_base`` timestamp in the extract's header, if present."""
    with osm.open("rb") as f:
        head = f.read(2048).decode("utf-8", "replace")
    marker = 'osm_base="'
    if marker in head:
        start = head.index(marker) + len(marker)
        return head[start : head.index('"', start)]
    return None


def fetch_osm(
    bbox: BBox,
    out_dir: Path,
    layers: list[str],
    zoom: int,
    *,
    url: str = DEFAULT_OVERPASS_URL,
    fetcher=None,  # noqa: ANN001 - Callable[[str, str], bytes]; tests inject one
) -> dict:
    """Fetch, split and record. Returns the manifest that was written to ``osm.json``."""
    for name in layers:
        _preset(name)
    gdal_ops.require_gdal()
    out_dir.mkdir(parents=True, exist_ok=True)
    query = build_query(bbox, layers, zoom)
    logger.info("osm.fetch", url=url, layers=layers, zoom=zoom, bbox=bbox.as_text())
    body = (fetcher or overpass_fetch)(query, url)
    extract = out_dir / EXTRACT_NAME
    extract.write_bytes(body)
    written, commands = extract_layers(extract, out_dir, layers, zoom)
    manifest = {
        "source": "openstreetmap",
        "license": "ODbL 1.0 — © OpenStreetMap contributors",
        "overpass_url": url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "osm_base": osm_timestamp(extract),
        "bbox": bbox.as_text(),
        "zoom": zoom,
        "query": query,
        "extract": {"path": extract.name, "sha256": sha256_file(extract), "bytes": extract.stat().st_size},
        "layers": {
            name: {"path": path.name, "sha256": sha256_file(path), "where": where_clause(name, zoom)}
            for name, path in written.items()
        },
        "skipped": [name for name in layers if name not in written],
        "commands": [c.model_dump() for c in commands],
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("osm.done", layers=sorted(written), skipped=manifest["skipped"], bytes=manifest["extract"]["bytes"])
    return manifest
