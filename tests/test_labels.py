from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_atlas import labels
from argus_atlas.cli import app
from argus_atlas.hashing import sha256_file
from argus_atlas.jobs import JobStore, copy_job, load_sidecar
from argus_atlas.models import AtlasError, BBox, JobSidecar
from argus_atlas.osm import build_query, place_classes
from tests.conftest import TEST_BBOX, requires_gdal

runner = CliRunner()


def _point(lon: float, lat: float, **props: object) -> dict:
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _write(path: Path, features: list[dict]) -> Path:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    return path


@pytest.fixture
def ne_places(tmp_path: Path) -> Path:
    """Natural Earth spelling: one town inside TEST_BBOX, one outside it, one inside but too small at zoom 6."""
    return _write(
        tmp_path / "ne_places.geojson",
        [
            _point(10.10, 45.05, NAME="Inside", SCALERANK=2, FEATURECLA="Populated place"),
            _point(10.50, 45.05, NAME="Outside", SCALERANK=0, FEATURECLA="Admin-0 capital"),
            _point(10.05, 45.03, NAME="Hamlet", SCALERANK=8, FEATURECLA="Populated place"),
        ],
    )


def test_cutoffs_by_zoom() -> None:
    assert labels.max_scalerank(0) == 0 and labels.max_scalerank(6) == 4 and labels.max_scalerank(19) == 10
    assert place_classes(4) == ["city"]
    assert place_classes(11) == ["city", "town", "village"]
    assert "hamlet" in place_classes(13) and "locality" not in place_classes(14)


def test_rank_features_osm_scheme_and_drops() -> None:
    features = [
        _point(10.10, 45.05, name="Town", place="town"),
        _point(10.11, 45.05, name="Village", place="village"),
        _point(10.12, 45.05, name="Hamlet", place="hamlet"),
        _point(10.13, 45.05, place="city"),  # unnamed
        {"type": "Feature", "properties": {"name": "Area", "place": "city"}, "geometry": None},
    ]
    kept, scheme, dropped = labels.rank_features(features, TEST_BBOX, 11)
    assert scheme == "osm"
    assert [(f["properties"]["name"], f["properties"]["rank"]) for f in kept] == [("Town", 1), ("Village", 2)]
    assert dropped == {"not_a_point": 1, "outside": 0, "unnamed": 1, "below_cutoff": 1}


def test_rank_features_refuses_an_unrankable_file() -> None:
    with pytest.raises(AtlasError, match="SCALERANK"):
        labels.rank_features([_point(10.1, 45.05, name="X", population=3)], TEST_BBOX, 11)
    kept, scheme, _ = labels.rank_features([], TEST_BBOX, 11)
    assert kept == [] and scheme is None


def test_places_preset_queries_nodes() -> None:
    query = build_query(TEST_BBOX, ["places"], 11)
    assert 'node["place"~"^(city|town|village)$"]' in query and "way[" not in query


@requires_gdal
def test_build_labels_keeps_only_the_ranked_name_inside(ne_places: Path, tmp_path: Path) -> None:
    out = tmp_path / "labels.geojson"
    manifest = labels.build_labels(ne_places, TEST_BBOX, 6, out)
    features = json.loads(out.read_text())["features"]
    assert [f["properties"] for f in features] == [{"name": "Inside", "rank": 2, "kind": "populated place"}]
    assert features[0]["geometry"]["coordinates"] == [10.1, 45.05]
    assert manifest["scheme"] == "naturalearth" and manifest["cutoff"] == "scalerank <= 4"
    assert manifest["count"] == 1 and manifest["dropped"]["below_cutoff"] == 1
    assert manifest["source"]["sha256"] == sha256_file(ne_places)
    assert manifest["output"]["sha256"] == sha256_file(out)
    assert manifest["commands"][0]["tool"] == "ogr2ogr" and "-spat" in manifest["commands"][0]["argv"]
    assert json.loads(labels.manifest_path(out).read_text()) == manifest


@requires_gdal
def test_build_labels_reads_an_osm_extract(tmp_path: Path) -> None:
    osm = tmp_path / "extract.osm"
    osm.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
<node id="1" lat="45.05" lon="10.10"><tag k="place" v="town"/><tag k="name" v="Townsby"/></node>
<node id="2" lat="45.04" lon="10.12"><tag k="place" v="hamlet"/><tag k="name" v="Wee"/></node>
<node id="3" lat="46.00" lon="10.10"><tag k="place" v="city"/><tag k="name" v="Faraway"/></node>
<node id="4" lat="45.06" lon="10.06"><tag k="amenity" v="pub"/><tag k="name" v="The Bell"/></node>
</osm>
""",
        encoding="utf-8",
    )
    out = tmp_path / "labels.geojson"
    manifest = labels.build_labels(osm, TEST_BBOX, 11, out)
    assert [f["properties"]["name"] for f in json.loads(out.read_text())["features"]] == ["Townsby"]
    assert manifest["scheme"] == "osm" and manifest["source"]["layer"] == "points"


@requires_gdal
def test_build_labels_with_nothing_inside(ne_places: Path, tmp_path: Path) -> None:
    far = BBox(west=-1.0, south=50.0, east=-0.5, north=50.5)
    manifest = labels.build_labels(ne_places, far, 6, tmp_path / "labels.geojson")
    assert manifest["count"] == 0 and manifest["scheme"] is None and manifest["cutoff"] == "none"


def test_build_labels_input_errors(ne_places: Path, tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="not found"):
        labels.build_labels(tmp_path / "nope.geojson", TEST_BBOX, 6, tmp_path / "labels.geojson")
    with pytest.raises(AtlasError, match="must end in .geojson"):
        labels.build_labels(ne_places, TEST_BBOX, 6, tmp_path / "labels.json")


@requires_gdal
def test_label_job_records_and_serves(
    job: tuple[Path, JobSidecar], ne_places: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, sidecar = job
    assert sidecar.labels is None and load_sidecar(job_dir).labels is None  # unchanged until labelled
    record = labels.label_job(job_dir, ne_places, zoom=6)
    stored = load_sidecar(job_dir)
    assert stored.labels == record and record.count == 1 and record.file.path == labels.LABELS_NAME
    assert record.source.sha256 == sha256_file(ne_places)
    assert stored.grid == sidecar.grid and stored.controls == sidecar.controls
    assert JobStore(job_dir.parent).list()[0].has_labels

    # a variant keeps the overlay: it is about the grid, not the bake
    assert copy_job(job_dir, tmp_path / "variant").labels == record

    from fastapi.testclient import TestClient

    from argus_atlas.server import create_app

    client = TestClient(create_app(jobs_dir=job_dir.parent))
    res = client.get(f"/jobs/{job_dir.name}/labels")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/geo+json")
    assert [f["properties"]["name"] for f in res.json()["features"]] == ["Inside"]
    assert client.get("/jobs/variant/labels").json() == res.json()


@requires_gdal
def test_cli_labels(job: tuple[Path, JobSidecar], ne_places: Path, tmp_path: Path) -> None:
    job_dir, sidecar = job
    result = runner.invoke(app, ["labels", "--source", str(ne_places), "--job", str(job_dir)])
    assert result.exit_code == 0, result.output
    assert load_sidecar(job_dir).labels.zoom == sidecar.native_zoom  # type: ignore[union-attr]
    assert "labels:" in runner.invoke(app, ["inspect", str(job_dir)]).output

    out = tmp_path / "free.geojson"
    args = ["labels", "--source", str(ne_places), "--bbox", TEST_BBOX.as_text(), "--zoom", "6", "--out", str(out)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "1 names (scalerank <= 4)" in result.output and (tmp_path / "free.json").is_file()


def test_cli_labels_input_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["labels", "--source", str(tmp_path / "p.geojson"), "--bbox", "10,45,10.1,45.1"])
    assert result.exit_code == 2 and "--zoom and --out" in result.output
    result = runner.invoke(
        app, ["labels", "--source", "p.geojson", "--job", str(tmp_path), "--out", str(tmp_path / "x.geojson")]
    )
    assert result.exit_code == 2 and "drop --bbox/--out" in result.output


def test_server_labels_404_without_a_record(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from argus_atlas.server import create_app
    from tests.test_server import _seed_job

    _seed_job(tmp_path)
    client = TestClient(create_app(jobs_dir=tmp_path))
    assert client.get("/jobs/seeded/labels").status_code == 404
    assert client.get("/jobs/nope/labels").status_code == 404
    assert client.get("/jobs/seeded").json()["sidecar"]["labels"] is None
