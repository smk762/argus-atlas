from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest

from argus_atlas import gdal_ops

FEET_WKT = (
    'PROJCRS["NAD83 / California zone 5 (ftUS)",BASEGEOGCRS["NAD83",DATUM["North American Datum 1983",'
    'ELLIPSOID["GRS 1980",6378137,298.257222101,LENGTHUNIT["metre",1]]],ANGLEUNIT["degree",0.0174532925199433]],'
    'CS[Cartesian,2],AXIS["easting (X)",east,LENGTHUNIT["US survey foot",0.304800609601219]],'
    'AXIS["northing (Y)",north,LENGTHUNIT["US survey foot",0.304800609601219]],ID["EPSG",2229]]'
)


def _info(monkeypatch: pytest.MonkeyPatch, gt: list[float], wkt: str) -> None:
    monkeypatch.setattr(
        gdal_ops, "gdalinfo_json", lambda *_a, **_k: {"geoTransform": gt, "coordinateSystem": {"wkt": wkt}}
    )


def test_geographic_cell_scales_only_longitude(monkeypatch: pytest.MonkeyPatch) -> None:
    _info(monkeypatch, [0, 1 / 3600, 0, 0, 0, -1 / 3600], 'GEOGCRS["WGS 84"]')
    got = gdal_ops.dem_pixel_metres(Path("x.tif"), 60.0)
    want = (111320.0 * math.cos(math.radians(60)) + 110574.0) / 2 / 3600
    assert got == pytest.approx(want)


def test_projected_feet_are_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    _info(monkeypatch, [0, 10.0, 0, 0, 0, -10.0], FEET_WKT)
    assert gdal_ops.dem_pixel_metres(Path("x.tif"), 35.0) == pytest.approx(3.048, rel=1e-5)
    assert gdal_ops.linear_unit_metres('PROJCS["x",GEOGCS["y",UNIT["degree",0.0174]],UNIT["metre",1]]') == 1.0


COMPOUND_FEET_WKT = (
    'COMPD_CS["UTM 10N + NAVD88 height (ftUS)",PROJCS["WGS 84 / UTM zone 10N",GEOGCS["WGS 84",'
    'DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],UNIT["degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],PARAMETER["central_meridian",-123],UNIT["metre",1]],'
    'VERT_CS["NAVD88 height (ftUS)",VERT_DATUM["North American Vertical Datum 1988",2005],'
    'UNIT["US survey foot",0.304800609601219]]]'
)
COMPOUND_GEOG_WKT = (
    'COMPOUNDCRS["WGS 84 + EGM96 height",GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",'
    'ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]]],CS[ellipsoidal,2],'
    'AXIS["latitude",north,ANGLEUNIT["degree",0.0174532925199433]],'
    'AXIS["longitude",east,ANGLEUNIT["degree",0.0174532925199433]]],'
    'VERTCRS["EGM96 height",VDATUM["EGM96 geoid"],CS[vertical,1],AXIS["up",up,LENGTHUNIT["metre",1]]]]'
)
BOUND_GEOG_WKT = (
    'BOUNDCRS[SOURCECRS[GEOGCRS["WGS 84",DATUM["WGS84",ELLIPSOID["WGS 84",6378137,298.257223563]],'
    'CS[ellipsoidal,2],AXIS["lat",north,ANGLEUNIT["degree",0.0174532925199433]]]],'
    'TARGETCRS[GEOGCRS["WGS 84"]],ABRIDGEDTRANSFORMATION["towgs84",METHOD["Position Vector"]]]'
)


def test_the_horizontal_unit_ignores_vertical_and_angular_units() -> None:
    assert gdal_ops.linear_unit_metres(COMPOUND_FEET_WKT) == 1.0
    assert gdal_ops.linear_unit_metres(COMPOUND_GEOG_WKT) == 1.0
    assert gdal_ops.linear_unit_metres('GEOGCS["WGS 84",UNIT["degree",0.0174532925199433]]') == 1.0
    assert gdal_ops.linear_unit_metres(FEET_WKT) == pytest.approx(0.3048006096)
    assert gdal_ops.linear_unit_metres("not wkt [") == 1.0


@pytest.mark.parametrize("wkt", [COMPOUND_GEOG_WKT, BOUND_GEOG_WKT])
def test_a_wrapped_geographic_crs_is_still_in_degrees(monkeypatch: pytest.MonkeyPatch, wkt: str) -> None:
    _info(monkeypatch, [0, 1 / 3600, 0, 0, 0, -1 / 3600], wkt)
    got = gdal_ops.dem_pixel_metres(Path("x.tif"), 60.0)
    assert got == pytest.approx((111320.0 * math.cos(math.radians(60)) + 110574.0) / 2 / 3600)
    assert not gdal_ops.is_geographic(COMPOUND_FEET_WKT)


def test_deeply_nested_wkt_is_unparseable() -> None:
    modest = "Z"
    for _ in range(8):
        modest = f"N[{modest}]"
    assert gdal_ops._parse_wkt(modest) is not None
    wkt = "Z"
    for _ in range(80):
        wkt = f"N[{wkt}]"
    assert gdal_ops._parse_wkt(wkt) is None


def test_gdalinfo_stats_never_writes_or_reads_an_aux_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(gdal_ops.subprocess, "run", fake)
    gdal_ops.gdalinfo_json(Path("dem.tif"), stats=True)
    assert seen.get("GDAL_PAM_ENABLED") == "NO"


def test_gdalinfo_timeout_is_a_gdal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(gdal_ops.subprocess, "run", slow)
    with pytest.raises(gdal_ops.GdalError, match="timed out"):
        gdal_ops.gdalinfo_json(Path("big.tif"), stats=True)


def test_sea_mask_excludes_the_warp_nodata() -> None:
    argv = gdal_ops.sea_mask_argv("gdal_calc.py", Path("dem.tif"), Path("sea.tif"), 0.0)
    assert f"A!={gdal_ops.DEM_NODATA}" in argv[argv.index("--calc") + 1]
    warp = gdal_ops.warp_argv(Path("in.tif"), Path("out.tif"), _grid())
    assert warp[warp.index("-dstnodata") + 1] == "-9999"


def _grid():  # noqa: ANN202
    from argus_atlas.models import Grid

    return Grid(width=64, height=64, ulx=0.0, uly=0.0, pixel_width=1.0, pixel_height=-1.0)


def test_slope_refuses_a_z_factor_that_is_not_positive(tmp_path: Path) -> None:
    for bad in (0.0, -2.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="z_factor must be a positive number"):
            gdal_ops.slope_argv(tmp_path / "dem.tif", tmp_path / "slope.tif", z_factor=bad)
    argv = gdal_ops.slope_argv(tmp_path / "dem.tif", tmp_path / "slope.tif", z_factor=2.0)
    assert argv[argv.index("-s") + 1] == "0.5"


def test_a_failing_gdalinfo_has_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gdal_ops.shutil, "which", lambda name: "/usr/bin/" + name)

    def broken(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, 127, stdout="", stderr="gdalinfo: error while loading shared libraries"
        )

    monkeypatch.setattr(gdal_ops.subprocess, "run", broken)
    assert gdal_ops.gdal_version() is None
    with pytest.raises(gdal_ops.GdalError, match="not usable"):
        gdal_ops.require_gdal()
    ok = subprocess.CompletedProcess([], 0, stdout="GDAL 3.11.4, released 2025/09/04\n", stderr="")
    monkeypatch.setattr(gdal_ops.subprocess, "run", lambda *_a, **_k: ok)
    assert gdal_ops.gdal_version() == "GDAL 3.11.4, released 2025/09/04"


def test_the_dem_fill_reaches_gdalwarp_exactly(tmp_path: Path) -> None:
    def init_dest(fill: float) -> float:
        argv = gdal_ops.dem_clip_argv(["a.tif"], tmp_path / "out.tif", (0.0, 0.0, 1.0, 1.0), (0.5, 0.5), fill=fill)
        return float(argv[argv.index("-wo") + 1].removeprefix("INIT_DEST="))

    for fill in (0.0, -9999.0, 1234567.0, -3.4028234663852886e38):
        assert init_dest(fill) == fill
