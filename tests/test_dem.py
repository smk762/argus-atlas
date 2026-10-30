from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_atlas import dem
from argus_atlas.cli import app
from argus_atlas.gdal_ops import gdalinfo_json
from argus_atlas.hashing import sha256_file
from argus_atlas.models import AtlasError, BBox
from tests.conftest import requires_gdal

runner = CliRunner()

# The stand-in for one GLO-30 COG: 100x100 cells of 0.01° over N45 E010, with the half-pixel
# shift the real tiles carry (their lattice starts half a cell west and north of the degree line).
CELL = 0.01
KEY = dem.tile_key(45, 10)


@pytest.fixture(scope="module")
def mirror(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A local dir laid out like the bucket: <key>/<key>.tif, with N45 E010 only."""
    from tests.conftest import _skip_without_gdal

    _skip_without_gdal()
    root = tmp_path_factory.mktemp("glo30")
    n = 100
    lines = [f"ncols {n}", f"nrows {n}", f"xllcorner {10 - CELL / 2}", f"yllcorner {45 - CELL / 2}", f"cellsize {CELL}"]
    lines += [" ".join(f"{100 + 3 * c + 2 * r}" for c in range(n)) for r in range(n)]
    asc = root / "tile.asc"
    asc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / KEY).mkdir()
    subprocess.run(
        [
            "gdal_translate",
            "-q",
            "-ot",
            "Float32",
            "-a_srs",
            "EPSG:4326",
            "-of",
            "COG",
            str(asc),
            str(root / KEY / f"{KEY}.tif"),
        ],
        check=True,
        capture_output=True,
    )
    return root


def _value(path: Path, lon: float, lat: float) -> float:
    proc = subprocess.run(
        ["gdallocationinfo", "-valonly", "-wgs84", str(path), str(lon), str(lat)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def test_tile_keys_and_cover() -> None:
    assert dem.tile_key(45, 10) == "Copernicus_DSM_COG_10_N45_00_E010_00_DEM"
    assert dem.tile_key(-34, -1) == "Copernicus_DSM_COG_10_S34_00_W001_00_DEM"
    # north to south, west to east; an edge on the degree line does not pull in the next tile
    keys = dem.tiles_for(BBox(west=-0.5, south=44.5, east=1.0, north=46.0))
    assert keys == [dem.tile_key(45, -1), dem.tile_key(45, 0), dem.tile_key(44, -1), dem.tile_key(44, 0)]
    assert dem.tile_location("https://x.test/", KEY) == f"/vsicurl/https://x.test/{KEY}/{KEY}.tif"
    assert dem.tile_location("/m", KEY) == f"/m/{KEY}/{KEY}.tif"


def test_snap_inward_stays_inside() -> None:
    bbox = BBox(west=10.2, south=45.3, east=10.6, north=45.7)
    xmin, ymin, xmax, ymax = dem.snap_inward(bbox, (9.995, 46.005), (CELL, CELL))
    assert (xmin, ymin, xmax, ymax) == pytest.approx((10.205, 45.305, 10.595, 45.695))
    with pytest.raises(AtlasError, match="smaller than one DEM pixel"):
        dem.snap_inward(BBox(west=10.201, south=45.3, east=10.204, north=45.7), (9.995, 46.005), (CELL, CELL))


def test_snap_outward_contains_the_bbox() -> None:
    bbox = BBox(west=10.2, south=45.3, east=10.6, north=45.7)
    assert dem.snap_outward(bbox, (9.995, 46.005), (CELL, CELL)) == pytest.approx((10.195, 45.295, 10.605, 45.705))
    # an edge already on the lattice stays put
    on = BBox(west=10.205, south=45.305, east=10.595, north=45.695)
    assert dem.snap_outward(on, (9.995, 46.005), (CELL, CELL)) == pytest.approx((10.205, 45.305, 10.595, 45.695))


def test_refuses_too_many_tiles_and_a_wrong_suffix(tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match="touches 25 DEM tiles"):
        dem.fetch_dem(BBox(west=0, south=40, east=5, north=45), tmp_path / "d.tif", source=str(tmp_path))
    with pytest.raises(AtlasError, match="cap is 30"):
        dem.fetch_dem(BBox(west=0, south=40, east=6, north=46), tmp_path / "d.tif", source=str(tmp_path), max_tiles=30)
    with pytest.raises(AtlasError, match="must end in .tif"):
        dem.fetch_dem(BBox(west=0, south=40, east=0.5, north=40.5), tmp_path / "d.png", source=str(tmp_path))


def test_tile_exists_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(code: int | None):
        def urlopen(req, timeout):  # noqa: ANN001, ANN202
            assert req.get_method() == "HEAD" and req.full_url.endswith(f"{KEY}/{KEY}.tif")
            if code is None:
                return io.BytesIO(b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)  # type: ignore[arg-type]

        return urlopen

    monkeypatch.setattr(dem.urllib.request, "urlopen", answer(None))
    assert dem.tile_exists("https://bucket.test", KEY)
    monkeypatch.setattr(dem.urllib.request, "urlopen", answer(404))
    assert not dem.tile_exists("https://bucket.test", KEY)
    monkeypatch.setattr(dem.urllib.request, "urlopen", answer(503))
    with pytest.raises(dem.DemSourceError, match="HTTP 503"):
        dem.tile_exists("https://bucket.test", KEY)


@requires_gdal
def test_fetch_dem_clips_inside_the_bbox_without_resampling(mirror: Path, tmp_path: Path) -> None:
    bbox = BBox(west=10.2, south=45.3, east=10.6, north=45.7)
    out = tmp_path / "dem.tif"
    manifest = dem.fetch_dem(bbox, out, source=str(mirror))
    info = gdalinfo_json(out)
    ulx, rx, _, uly, _, ry = info["geoTransform"]
    w, h = info["size"]
    assert (rx, -ry) == pytest.approx((CELL, CELL))
    assert bbox.west <= ulx and ulx + w * rx <= bbox.east and bbox.south <= uly + h * ry and uly <= bbox.north
    assert (w, h) == (39, 39)
    # the same value at the same ground as the tile: nothing was resampled
    assert _value(out, 10.40, 45.50) == _value(mirror / KEY / f"{KEY}.tif", 10.40, 45.50)
    assert manifest["tiles"] == [KEY] and manifest["missing"] == [] and manifest["filled"] is None
    assert manifest["output"]["sha256"] == sha256_file(out)
    assert json.loads(out.with_suffix(".json").read_text()) == manifest

    from argus_atlas.controls import build_controls

    sidecar = build_controls(
        out, BBox(west=10.25, south=45.35, east=10.55, north=45.65), tmp_path / "job", long_side=256
    )
    assert sidecar.dem.sha256 == sha256_file(out)


@requires_gdal
def test_fetch_dem_names_the_missing_tile(mirror: Path, tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match=dem.tile_key(45, 12)) as exc:
        dem.fetch_dem(BBox(west=12.1, south=45.1, east=12.4, north=45.4), tmp_path / "d.tif", source=str(mirror))
    assert "--allow-missing" not in str(exc.value)  # nothing there at all: filling would write only sea
    assert not (tmp_path / "d.tif").exists()

    across = BBox(west=10.8, south=45.2, east=11.3, north=45.5)
    with pytest.raises(AtlasError, match=f"{dem.tile_key(45, 11)}.*--allow-missing"):
        dem.fetch_dem(across, tmp_path / "d.tif", source=str(mirror))
    manifest = dem.fetch_dem(across, tmp_path / "d.tif", source=str(mirror), allow_missing=True)
    assert manifest["tiles"] == [KEY] and manifest["missing"] == [dem.tile_key(45, 11)] and manifest["filled"] == 0
    assert _value(tmp_path / "d.tif", 11.2, 45.3) == 0.0


@requires_gdal
def test_cover_snaps_outward_and_fetches_the_tile_half_a_pixel_away(mirror: Path, tmp_path: Path) -> None:
    bbox = BBox(west=10.2, south=45.3, east=10.6, north=45.7)
    out = tmp_path / "cover.tif"
    manifest = dem.fetch_dem(bbox, out, source=str(mirror), cover=True)
    ulx, rx, _, uly, _, ry = gdalinfo_json(out)["geoTransform"]
    w, h = gdalinfo_json(out)["size"]
    assert ulx <= bbox.west and bbox.east <= ulx + w * rx and uly + h * ry <= bbox.south and bbox.north <= uly
    assert (w, h) == (41, 41) and manifest["snap"] == "outward"
    assert _value(out, 10.40, 45.50) == _value(mirror / KEY / f"{KEY}.tif", 10.40, 45.50)

    # E011's lattice starts half a pixel west of 11°: covering an edge on the degree line needs it
    edge = BBox(west=10.8, south=45.2, east=11.0, north=45.5)
    with pytest.raises(AtlasError, match=dem.tile_key(45, 11)):
        dem.fetch_dem(edge, tmp_path / "edge.tif", source=str(mirror), cover=True)
    filled = dem.fetch_dem(edge, tmp_path / "edge.tif", source=str(mirror), cover=True, allow_missing=True)
    assert filled["missing"] == [dem.tile_key(45, 11)]
    assert dem.fetch_dem(edge, tmp_path / "in.tif", source=str(mirror))["missing"] == []  # inward: E010 alone


@requires_gdal
def test_a_chunk_canvas_is_covered(mirror: Path, tmp_path: Path) -> None:
    from argus_atlas.chunks import canvas_bbox, chunk_grids
    from argus_atlas.mercator import bbox_to_mercator

    tile, size, margin = (10, 541, 367), 512, 64  # lon 10.20..10.55, lat 45.09..45.34
    canvas, _ = chunk_grids(*tile, size=size, margin=margin)
    out = tmp_path / "chunk.tif"
    result = runner.invoke(
        app,
        ["dem", "--tile", "10/541/367", "--size", str(size), "--margin", str(margin)]
        + ["--out", str(out), "--source", str(mirror)],
    )
    assert result.exit_code == 0, result.output
    info = gdalinfo_json(out)
    ulx, rx, _, uly, _, ry = info["geoTransform"]
    w, h = info["size"]
    xmin, ymin, xmax, ymax = bbox_to_mercator(BBox(west=ulx, south=uly + h * ry, east=ulx + w * rx, north=uly))
    cxmin, cymin, cxmax, cymax = canvas.bounds
    assert xmin <= cxmin and cxmax <= xmax and ymin <= cymin and cymax <= ymax
    assert canvas_bbox(*tile, size=size, margin=margin).west >= ulx


@requires_gdal
def test_cli_dem(mirror: Path, tmp_path: Path) -> None:
    out = tmp_path / "dem.tif"
    result = runner.invoke(app, ["dem", "--bbox", "10.2,45.3,10.6,45.7", "--out", str(out), "--source", str(mirror)])
    assert result.exit_code == 0, result.output
    assert KEY in result.output and "Copernicus WorldDEM-30" in result.output and out.is_file()
    missing = runner.invoke(app, ["dem", "--bbox", "12.1,45.1,12.4,45.4", "--out", str(out), "--source", str(mirror)])
    assert missing.exit_code == 2 and dem.tile_key(45, 12) in missing.output
    both = runner.invoke(app, ["dem", "--bbox", "10.2,45.3,10.6,45.7", "--tile", "10/541/367", "--out", str(out)])
    assert both.exit_code == 2 and "exactly one of --bbox or --tile" in both.output


def test_server_has_no_dem_route() -> None:
    from argus_atlas.server import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert not any("dem" in p for p in paths), paths
