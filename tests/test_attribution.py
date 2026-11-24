"""Attribution (#61): every published tile set carries the Copernicus and OSM (ODbL) credit its inputs require."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from argus_atlas import attribution as attr
from argus_atlas.hashing import sha256_file
from argus_atlas.models import FileRef, LabelsRecord, SourceRecord
from tests.conftest import requires_gdal

runner = CliRunner()


def _copernicus_dem(src: Path, root: Path) -> Path:
    """A copy of *src* with the provenance ``argus-atlas dem`` writes beside what it fetched."""
    root.mkdir(parents=True, exist_ok=True)
    dem = root / "strait.tif"
    shutil.copy(src, dem)
    manifest = {"product": "Copernicus GLO-30 DSM", "output": {"path": dem.name, "sha256": sha256_file(dem)}}
    dem.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")
    return dem


def _osm_layer(root: Path, name: str, feature_collection: dict) -> Path:
    """A layer with the ``osm.json`` that ``argus-atlas osm`` writes beside its extracts."""
    root.mkdir(parents=True, exist_ok=True)
    layer = root / f"{name}.geojson"
    layer.write_text(json.dumps(feature_collection), encoding="utf-8")
    manifest = {"source": "openstreetmap", "layers": {name: {"path": layer.name, "sha256": sha256_file(layer)}}}
    (root / "osm.json").write_text(json.dumps(manifest), encoding="utf-8")
    return layer


def _ref(path: Path) -> FileRef:
    return FileRef(path=str(path.resolve()), sha256=sha256_file(path))


# --- recognising a source -------------------------------------------------------------------------------------


def test_a_dem_is_copernicus_only_when_its_provenance_vouches_for_these_bytes(tmp_path: Path) -> None:
    dem = tmp_path / "dem" / "strait.tif"
    dem.parent.mkdir()
    dem.write_bytes(b"heights")
    assert attr.dem_source(_ref(dem)).key == attr.UNKNOWN  # no provenance: an operator's own DEM
    manifest = {"product": "Copernicus GLO-30 DSM", "output": {"path": dem.name, "sha256": sha256_file(dem)}}
    dem.with_suffix(".json").write_text(json.dumps(manifest))
    src = attr.dem_source(_ref(dem))
    assert (src.key, src.used_for) == ("copernicus-glo30", ["dem"]) and "WorldDEM-30" in (src.attribution or "")
    dem.write_bytes(b"other heights")  # the provenance describes a file that is no longer this one
    assert attr.dem_source(_ref(dem)).key == attr.UNKNOWN
    dem.with_suffix(".json").write_text("not json")
    assert attr.dem_source(_ref(dem)).key == attr.UNKNOWN


def test_a_layer_is_osm_only_when_its_osm_json_lists_it(tmp_path: Path) -> None:
    layer = _osm_layer(tmp_path / "osm", "water", {"type": "FeatureCollection", "features": []})
    src = attr.layer_source("water", _ref(layer))
    assert (src.key, src.used_for, src.license) == ("openstreetmap", ["water"], "ODbL-1.0")
    other = tmp_path / "osm" / "mine.geojson"  # same dir, not in the manifest
    other.write_text("{}")
    assert attr.layer_source("mine", _ref(other)) == SourceRecord(key="unknown", name="mine.geojson", used_for=["mine"])
    layer.write_text('{"edited": true}')  # an OSM extract edited by hand is no longer the extract
    assert attr.layer_source("water", _ref(layer)).key == attr.UNKNOWN


def test_labels_are_credited_by_their_scheme(tmp_path: Path) -> None:
    ref = FileRef(path=str(tmp_path / "places.geojson"), sha256="0" * 64)
    record = LabelsRecord(
        source=ref, scheme="naturalearth", zoom=6, cutoff="SCALERANK <= 4", count=1, file=ref, completed_at="t"
    )
    assert attr.labels_source(record).key == "naturalearth"
    assert attr.labels_source(record.model_copy(update={"scheme": "osm"})).key == "openstreetmap"
    assert attr.labels_source(record.model_copy(update={"scheme": None})).key == attr.UNKNOWN


def test_merge_unions_uses_and_puts_unknown_sources_last() -> None:
    osm = attr.OPENSTREETMAP
    merged = attr.merge_sources(
        [
            SourceRecord(key="unknown", name="dem.tif", used_for=["dem"]),
            osm.model_copy(update={"used_for": ["water"]}),
            attr.COPERNICUS.model_copy(update={"used_for": ["dem"]}),
            osm.model_copy(update={"used_for": ["roads", "water"]}),
            SourceRecord(key="unknown", name="dem.tif", used_for=["dem"]),
        ]
    )
    assert [(s.key, s.used_for) for s in merged] == [
        ("copernicus-glo30", ["dem"]),
        ("openstreetmap", ["roads", "water"]),
        ("unknown", ["dem"]),
    ]
    text = attr.attribution_text(merged)
    assert text.startswith("Copernicus GLO-30: produced using") and text.endswith(
        "© OpenStreetMap contributors (ODbL 1.0)"
    )
    assert attr.unattributed(merged) == ["dem.tif (dem)"]
    assert attr.attribution_text([]) == ""


# --- published outputs ----------------------------------------------------------------------------------------


def _archive_metadata(path: Path) -> dict:
    from pmtiles.reader import MmapSource, Reader

    with path.open("rb") as f:
        return Reader(MmapSource(f)).metadata()


@requires_gdal
def test_tiles_and_mosaic_carry_copernicus_and_osm_attribution(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    """A chunk block built from a Copernicus DEM and an OSM layer, labelled from Natural Earth, then mosaicked."""
    from argus_atlas.chunk_pipeline import run_chunk
    from argus_atlas.chunks import chunk_job_id, parse_tile
    from argus_atlas.cli import app
    from argus_atlas.jobs import load_sidecar
    from argus_atlas.labels import label_job
    from argus_atlas.mosaic import mosaic
    from tests.test_blocks import _tile_square
    from tests.test_mosaic import MARGIN, RECIPE, SIZE, A, B

    copernicus = _copernicus_dem(dem, tmp_path / "dem")
    water = _osm_layer(
        tmp_path / "osm", "water", {"type": "FeatureCollection", "features": [_tile_square(*parse_tile(A))]}
    )
    places = tmp_path / "places.geojson"
    point = {"type": "Point", "coordinates": [10.0, 45.0]}
    places.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    style = tmp_path / "antique"
    from tests.fakebackend import FakeBackend

    jobs = []
    for tile in (A, B):
        job_dir = style / chunk_job_id("antique", *parse_tile(tile))
        sidecar = run_chunk(
            copernicus, tile, RECIPE, job_dir, backend=FakeBackend(), models_root=models_dir,
            layers={"water": water}, size=SIZE, margin=MARGIN,
        )  # fmt: skip
        jobs.append(job_dir)
        # Recorded at controls time, so a later move of the inputs can't lose it.
        assert [(s.key, s.used_for) for s in sidecar.sources or []] == [
            ("copernicus-glo30", ["dem"]),
            ("openstreetmap", ["water"]),
        ]
        assert sidecar.tiles is not None and sidecar.tiles.attribution is not None
        meta = _archive_metadata(job_dir / sidecar.tiles.archive.path)
        assert meta["attribution"] == sidecar.tiles.attribution
        assert "WorldDEM-30" in meta["attribution"] and "© OpenStreetMap contributors" in meta["attribution"]
        assert {s["key"] for s in json.loads(meta["argus_sources"])} == {"copernicus-glo30", "openstreetmap"}

    # Name the place at the middle of chunk A, from a Natural Earth file.
    from argus_atlas.mercator import mercator_to_bbox, tile_bounds

    c = mercator_to_bbox(*tile_bounds(*parse_tile(A)))
    point["coordinates"] = [(c.west + c.east) / 2, (c.south + c.north) / 2]
    props = {"NAME": "Here", "SCALERANK": 0, "FEATURECLA": "Populated place"}
    places.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": props, "geometry": point}]}
        )
    )
    assert label_job(jobs[0], places).count == 1
    assert load_sidecar(jobs[0]).tiles.attribution == load_sidecar(jobs[1]).tiles.attribution  # labels change no raster

    out = tmp_path / "antique.pmtiles"
    manifest = mosaic(style, out, overview_min=13)
    meta = _archive_metadata(out)
    assert "WorldDEM-30" in meta["attribution"] and "© OpenStreetMap contributors" in meta["attribution"]
    assert "Natural Earth" not in meta["attribution"]  # the raster has no names in it
    assert [(s["key"], s["used_for"]) for s in manifest["sources"]] == [
        ("copernicus-glo30", ["dem"]),
        ("naturalearth", ["labels"]),
        ("openstreetmap", ["water"]),
    ]
    assert "Made with Natural Earth" in manifest["attribution"] and "WorldDEM-30" in manifest["attribution"]
    assert json.loads(out.with_suffix(".json").read_text())["attribution"] == manifest["attribution"]
    labels = json.loads((tmp_path / "antique.labels.geojson").read_text())
    assert labels["attribution"] == "Made with Natural Earth"

    xyz = mosaic(style, tmp_path / "xyz", overview_min=13)  # an XYZ dir has no archive metadata: the manifest has it
    assert json.loads((tmp_path / "xyz" / "mosaic.json").read_text())["attribution"] == xyz["attribution"]

    result = runner.invoke(app, ["tiles", str(jobs[1])])
    assert result.exit_code == 0 and "attribution: Copernicus GLO-30" in result.output
    assert "warning" not in result.output


@requires_gdal
def test_a_dem_of_unknown_origin_is_published_with_a_warning(job, tmp_path: Path, models_dir: Path) -> None:  # noqa: ANN001
    from argus_atlas.bake import run_bake
    from argus_atlas.cli import app
    from argus_atlas.models import BakeParams
    from tests.fakebackend import FakeBackend

    job_dir, sidecar = job
    assert [(s.key, s.used_for) for s in sidecar.sources or []] == [("unknown", ["dem"])]
    run_bake(
        job_dir, BakeParams(checkpoint="sd_xl_base_1.0.safetensors", seed=1), FakeBackend(), models_root=models_dir
    )
    result = runner.invoke(app, ["tiles", str(job_dir)])
    assert result.exit_code == 0, result.output
    assert "attribution: none known" in result.output
    assert "warning: no known attribution for dem.tif (dem)" in result.output
    meta = _archive_metadata(job_dir / "fantasy.pmtiles")
    assert "attribution" not in meta and json.loads(meta["argus_sources"])[0]["key"] == "unknown"


def test_a_job_from_before_0_5_has_its_sources_worked_out_from_its_paths(tmp_path: Path) -> None:
    from argus_atlas.models import JobSidecar
    from tests.test_server import _seed_job

    sidecar = _seed_job(tmp_path)
    old = JobSidecar.model_validate({**sidecar.model_dump(), "sources": None})
    assert [s.key for s in attr.job_sources(old)] == ["unknown"]
