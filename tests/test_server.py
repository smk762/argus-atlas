from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus_atlas import __version__
from argus_atlas.jobs import JobStore
from argus_atlas.models import BBox, FileRef, Grid, JobSidecar
from argus_atlas.png import write_png
from argus_atlas.server import create_app
from tests.conftest import HAS_GDAL, requires_gdal
from tests.fakebackend import FakeBackend


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    for name in ("jobs", "dem", "models"):
        (tmp_path / name).mkdir()
    return {"jobs": tmp_path / "jobs", "dem": tmp_path / "dem", "models": tmp_path / "models"}


@pytest.fixture
def client(roots: dict[str, Path]) -> TestClient:
    """Default posture: baking disabled (no ATLAS_ALLOW_BAKE)."""
    return TestClient(create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"], models_dir=roots["models"]))


def _seed_job(jobs_root: Path, job_id: str = "seeded") -> JobSidecar:
    """A hand-written controls job (no GDAL needed) with a real hillshade PNG."""
    grid = Grid(width=128, height=64, ulx=1114000.0, uly=5640000.0, pixel_width=100.0, pixel_height=-100.0)
    store = JobStore(jobs_root)
    job_dir = store.dir(job_id)
    png = write_png(job_dir / "hillshade.png", 128, 64)
    from argus_atlas.hashing import sha256_file

    sidecar = JobSidecar(
        job_id=job_id,
        created_at="2026-10-17T00:00:00+00:00",
        dem=FileRef(path="/data/dem/dem.tif", sha256="a" * 64),
        bbox=BBox(west=10.0, south=45.0, east=10.1, north=45.05),
        grid=grid,
        resolution=100.0,
        native_zoom=11,
        zoom_range=(9, 11),
        controls={"hillshade": FileRef(path="hillshade.png", sha256=sha256_file(png))},
    )
    store.save(sidecar)
    return sidecar


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["service"] == "argus-atlas" and body["version"] == __version__
    assert body["read_only"] is True  # baking disabled by default
    assert body["osm_allowed"] is False  # and so is fetching OSM from the server
    assert (body["gdal"] is None) is (not HAS_GDAL)


def test_osm_403_when_disabled_and_inputs_lists_dem_dir(client: TestClient, roots: dict[str, Path]) -> None:
    resp = client.post("/osm", json={"name": "x", "bbox": "10,45,10.1,45.1"})
    assert resp.status_code == 403 and "ATLAS_ALLOW_OSM" in resp.json()["detail"]
    (roots["dem"] / "srtm.tif").write_bytes(b"not really")
    (roots["dem"] / "osm" / "garda").mkdir(parents=True)
    (roots["dem"] / "osm" / "garda" / "water.geojson").write_text("{}")
    (roots["dem"] / "osm" / "garda" / "osm.json").write_text(
        json.dumps({"bbox": "10,45,11,46", "zoom": 11, "osm_base": "t", "layers": {"water": {"path": "water.geojson"}}})
    )
    (roots["dem"] / "too" / "deep" / "for" / "us").mkdir(parents=True)
    (roots["dem"] / "too" / "deep" / "for" / "us" / "x.tif").write_bytes(b"")
    body = client.get("/inputs").json()
    assert body["dems"] == ["srtm.tif"]
    assert body["vectors"] == ["osm/garda/water.geojson"]
    assert body["osm"] == [
        {
            "name": "osm/garda",
            "bbox": "10,45,11,46",
            "zoom": 11,
            "osm_base": "t",
            "layers": {"water": "osm/garda/water.geojson"},
        }
    ]


@requires_gdal
def test_osm_route_writes_under_dem_dir(roots: dict[str, Path]) -> None:
    from tests.test_osm import _OSM_XML

    app = create_app(
        jobs_dir=roots["jobs"],
        dem_dir=roots["dem"],
        models_dir=roots["models"],
        allow_osm=True,
        osm_fetcher=lambda q, u: _OSM_XML.encode(),
    )
    c = TestClient(app)
    assert c.get("/health").json()["osm_allowed"] is True
    assert c.post("/osm", json={"name": "../escape", "bbox": "10.02,45.01,10.18,45.09"}).status_code == 400
    resp = c.post(
        "/osm",
        json={"name": "aoi", "bbox": "10.02,45.01,10.18,45.09", "zoom": 11, "layers": ["water", "roads", "buildings"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layers"] == {"water": "osm/aoi/water.geojson", "roads": "osm/aoi/roads.geojson"} and body[
        "skipped"
    ] == ["buildings"]
    assert (roots["dem"] / "osm" / "aoi" / "osm.json").is_file()
    assert c.get("/inputs").json()["osm"][0]["name"] == "osm/aoi"
    # baking stays refused: the guard is per-route
    assert c.post("/bake/stream", json={"job_id": "x", "checkpoint": "c"}).status_code == 403


def test_bake_stream_403_when_disabled_but_jobs_readable(client: TestClient, roots: dict[str, Path]) -> None:
    _seed_job(roots["jobs"])
    refused = client.post("/bake/stream", json={"job_id": "seeded", "checkpoint": "c"})
    assert refused.status_code == 403
    assert "ATLAS_ALLOW_BAKE" in refused.json()["detail"]
    # An invalid body still 403s: the guard runs before validation.
    assert client.post("/bake/stream", json={}).status_code == 403
    # Stored-job reads keep working.
    assert client.get("/jobs").status_code == 200
    assert [j["job_id"] for j in client.get("/jobs").json()["jobs"]] == ["seeded"]
    got = client.get("/jobs/seeded")
    assert got.status_code == 200 and got.json()["status"] == "controls"
    assert got.json()["sidecar"]["grid"]["width"] == 128
    preview = client.get("/jobs/seeded/preview/hillshade")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    assert client.get("/jobs/seeded/preview/fantasy").status_code == 404
    assert client.get("/jobs/seeded/preview/evil").status_code == 400
    assert client.get("/jobs/seeded/archive").status_code == 404
    assert client.get("/jobs/nope").status_code == 404
    # An id that fails the strict charset is a 400; an encoded slash never even matches a route.
    assert client.get("/jobs/..evil/preview/hillshade").status_code == 400
    assert client.get("/jobs/..%2F..%2Fetc/preview/hillshade").status_code in (400, 404)


def test_allow_bake_from_env_and_kwarg(roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_ALLOW_BAKE", "1")
    env_app = TestClient(create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"]))
    assert env_app.get("/health").json()["read_only"] is False
    # explicit kwarg wins over the env; a typo'd value resolves to off
    off = TestClient(create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"], allow_bake=False))
    assert off.get("/health").json()["read_only"] is True
    monkeypatch.setenv("ATLAS_ALLOW_BAKE", "enabled")
    assert (
        TestClient(create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"])).get("/health").json()["read_only"] is True
    )


def test_403_keeps_cors_headers(roots: dict[str, Path]) -> None:
    app = TestClient(
        create_app(jobs_dir=roots["jobs"], dem_dir=roots["dem"], cors=True, cors_origins=["http://studio.local"])
    )
    refused = app.post("/bake/stream", json={}, headers={"Origin": "http://studio.local"})
    assert refused.status_code == 403
    assert refused.headers["access-control-allow-origin"] == "http://studio.local"
    preflight = app.options(
        "/bake/stream", headers={"Origin": "http://studio.local", "Access-Control-Request-Method": "POST"}
    )
    assert preflight.status_code == 200


def test_controls_path_escape_is_400(client: TestClient, roots: dict[str, Path]) -> None:
    body = {"dem": "../../etc/passwd", "bbox": [10.02, 45.01, 10.18, 45.09]}
    resp = client.post("/controls", json=body)
    assert resp.status_code == 400 and "escapes" in resp.json()["detail"]
    outside = roots["dem"].parent / "outside.tif"
    outside.write_bytes(b"x")
    assert client.post("/controls", json={**body, "dem": str(outside)}).status_code == 400
    # A name under the root that doesn't exist is a 404, not a traversal error.
    assert client.post("/controls", json={**body, "dem": "missing.tif"}).status_code == 404
    # A land file is held to the same root.
    (roots["dem"] / "dem.tif").write_bytes(b"x")
    assert client.post("/controls", json={**body, "dem": "dem.tif", "land": "/etc/hosts"}).status_code == 400
    # Bad bbox strings are a 400 too, before GDAL is consulted.
    assert client.post("/controls", json={"dem": "dem.tif", "bbox": "1,2,3"}).status_code == 400


def test_doctor_reports_without_gdal_or_engine(client: TestClient) -> None:
    body = client.get("/doctor").json()
    assert set(body) >= {"gdal", "pmtiles", "backend", "bake_allowed", "jobs_dir", "dem_dir"}
    assert body["bake_allowed"] is False
    assert body["backend"]["name"] == "comfyui" and body["backend"]["reachable"] is False


@requires_gdal
def test_full_flow_controls_bake_tiles(roots: dict[str, Path], dem: Path, models_dir: Path) -> None:
    import shutil

    shutil.copy(dem, roots["dem"] / "dem.tif")
    app = TestClient(
        create_app(
            jobs_dir=roots["jobs"],
            dem_dir=roots["dem"],
            models_dir=models_dir,
            allow_bake=True,
            backend_factory=FakeBackend,
        )
    )
    made = app.post(
        "/controls", json={"dem": "dem.tif", "bbox": "10.02,45.01,10.18,45.09", "long_side": 256, "job_id": "flow"}
    )
    assert made.status_code == 200, made.text
    assert made.json()["sidecar"]["grid"]["width"] % 64 == 0
    assert (
        app.post(
            "/controls", json={"dem": "dem.tif", "bbox": [10.02, 45.01, 10.18, 45.09], "job_id": "flow"}
        ).status_code
        == 409
    )

    # Tiles before a bake: nothing aligned to tile yet.
    assert app.post("/tiles", json={"job_id": "flow"}).status_code == 409

    with app.stream(
        "POST",
        "/bake/stream",
        json={"job_id": "flow", "checkpoint": "sd_xl_base_1.0.safetensors", "lora": "fantasy_map.safetensors"},
    ) as resp:
        assert resp.status_code == 200
        frames = [json.loads(line) for line in resp.iter_lines() if line]
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "controls" and kinds[-1] == "done"
    assert kinds.index("generate") < kinds.index("georeference")
    assert app.get("/jobs/flow").json()["status"] == "baked"
    assert app.get("/jobs/flow/preview/fantasy").status_code == 200

    tiled = app.post("/tiles", json={"job_id": "flow"})
    assert tiled.status_code == 200, tiled.text
    record = tiled.json()
    assert record["max_zoom"] == record["native_zoom"] and record["n_tiles"] > 0
    assert app.post("/tiles", json={"job_id": "flow", "max_zoom": record["native_zoom"] + 1}).status_code == 400
    archive = app.get("/jobs/flow/archive")
    assert archive.status_code == 200 and archive.content[:7] == b"PMTiles"
    assert app.get("/jobs/flow").json()["status"] == "tiled"
    # the archive carries the bake payload, so a tileset can be ranked without its job dir
    from pmtiles.reader import MemorySource, Reader

    meta = Reader(MemorySource(archive.content)).metadata()
    assert meta["argus_bake"]["job_id"] == "flow" and "params" in meta["argus_bake"]
    # a preset by name fills the knobs, and a bad one is a 400
    assert (
        app.post(
            "/bake/stream", json={"job_id": "flow", "checkpoint": "sd_xl_base_1.0.safetensors", "preset": "nope"}
        ).status_code
        == 400
    )
    assert app.get("/presets").json()["presets"][0]["name"]
