from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_atlas import osm
from argus_atlas.models import AtlasError, BBox
from tests.conftest import TEST_BBOX, requires_gdal

# A hand-written extract: a closed natural=water way, a trunk road, a residential
# road and a tiny multipolygon forest relation, all inside TEST_BBOX.
_OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
<meta osm_base="2026-10-17T10:00:00Z"/>
<node id="1" lat="45.02" lon="10.02"/><node id="2" lat="45.02" lon="10.08"/>
<node id="3" lat="45.06" lon="10.08"/><node id="4" lat="45.06" lon="10.02"/>
<node id="5" lat="45.01" lon="10.01"/><node id="6" lat="45.09" lon="10.19"/>
<node id="7" lat="45.03" lon="10.12"/><node id="8" lat="45.04" lon="10.15"/>
<node id="9" lat="45.07" lon="10.12"/><node id="10" lat="45.07" lon="10.16"/>
<node id="11" lat="45.09" lon="10.16"/><node id="12" lat="45.09" lon="10.12"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="natural" v="water"/></way>
<way id="101"><nd ref="5"/><nd ref="6"/><tag k="highway" v="trunk"/></way>
<way id="102"><nd ref="7"/><nd ref="8"/><tag k="highway" v="residential"/></way>
<way id="103"><nd ref="9"/><nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="9"/></way>
<relation id="200"><member type="way" ref="103" role="outer"/><tag k="type" v="multipolygon"/><tag k="landuse" v="forest"/></relation>
</osm>
"""


def test_query_is_scale_aware() -> None:
    q9 = osm.build_query(TEST_BBOX, ["roads"], 9)
    q14 = osm.build_query(TEST_BBOX, ["roads"], 14)
    assert "motorway" in q9 and "residential" not in q9
    assert "residential" in q14
    assert q9.startswith("[out:xml]") and q9.endswith("(._;>;);out body;")  # nodes before ways, see build_query
    # polygons ask for relations too, lines do not
    assert "relation" in osm.build_query(TEST_BBOX, ["water"], 11)
    assert "relation" not in q9
    # bbox order is Overpass's (south,west,north,east)
    b = TEST_BBOX
    assert f"({b.south},{b.west},{b.north},{b.east})" in q9


def test_where_clause_and_unknown_layer() -> None:
    assert osm.where_clause("water", 11).startswith(""""natural" IN ('water'""")
    assert osm.where_clause("buildings", 13) == '"building" IS NOT NULL'
    with pytest.raises(AtlasError, match="empty below zoom 13"):
        osm.where_clause("buildings", 11)
    with pytest.raises(AtlasError, match="unknown OSM layer"):
        osm.build_query(TEST_BBOX, ["volcanoes"], 11)


def test_overpass_filter_shapes() -> None:
    assert osm._overpass_filter("building", None) == '["building"]'
    assert osm._overpass_filter("natural", ["water"]) == '["natural"="water"]'
    assert osm._overpass_filter("highway", ["a", "b"]) == '["highway"~"^(a|b)$"]'


@requires_gdal
def test_fetch_osm_splits_layers_and_writes_manifest(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_fetch(query: str, url: str) -> bytes:
        calls.append((query, url))
        return _OSM_XML.encode("utf-8")

    out = tmp_path / "osm"
    manifest = osm.fetch_osm(
        TEST_BBOX, out, ["water", "roads", "forest", "buildings"], 11, url="http://overpass.test", fetcher=fake_fetch
    )
    assert calls and calls[0][1] == "http://overpass.test"
    assert (out / osm.EXTRACT_NAME).is_file() and (out / osm.MANIFEST_NAME).is_file()
    assert manifest["osm_base"] == "2026-10-17T10:00:00Z"
    assert manifest["skipped"] == ["buildings"]  # empty at zoom 11, so not written
    assert set(manifest["layers"]) == {"water", "roads", "forest"}
    for name, ref in manifest["layers"].items():
        features = json.loads((out / ref["path"]).read_text())["features"]
        assert features, name
    roads = json.loads((out / "roads.geojson").read_text())["features"]
    assert [f["properties"]["highway"] for f in roads] == ["trunk"]  # residential filtered out at zoom 11
    forest = json.loads((out / "forest.geojson").read_text())["features"]
    assert forest[0]["geometry"]["type"] in ("Polygon", "MultiPolygon")  # relation assembled by the OSM driver
    assert all(c["tool"] == "ogr2ogr" for c in manifest["commands"])


def test_bbox_parse_roundtrip() -> None:
    assert BBox.parse(TEST_BBOX.as_text()) == TEST_BBOX


@pytest.mark.parametrize("tag, values", [('name" OR 1=1 --', None), ("highway", ['a"];node(1);(']), ("x", ["a|.*"])])
def test_filters_refuse_anything_but_plain_tokens(tag: str, values: list[str] | None) -> None:
    with pytest.raises(AtlasError, match="must be letters"):
        osm._overpass_filter(tag, values)
    with pytest.raises(AtlasError, match="must be letters"):
        osm._sql_filter(tag, values)
    assert osm._sql_filter("name:en", None) == '"name:en" IS NOT NULL'


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://h", "http:///nohost"])
def test_overpass_fetch_refuses_non_http_urls(url: str) -> None:
    with pytest.raises(AtlasError, match="must be http"):
        osm.overpass_fetch("[out:xml];", url)


def test_overpass_runtime_error_in_a_200_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    body = (
        b'<?xml version="1.0" encoding="UTF-8"?><osm version="0.6"><note>x</note>'
        b'<remark> runtime error: Query timed out in "query" at line 1 after 181 seconds. </remark></osm>'
    )

    class Resp(io.BytesIO):
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *a):  # noqa: ANN002, ANN204
            return False

    monkeypatch.setattr(osm._NO_REDIRECT, "open", lambda req, timeout: Resp(body))
    with pytest.raises(AtlasError, match="timed out"):
        osm.overpass_fetch("[out:xml];")
    monkeypatch.setattr(osm._NO_REDIRECT, "open", lambda req, timeout: Resp(b"<html>secret page</html>"))
    with pytest.raises(AtlasError) as info:
        osm.overpass_fetch("[out:xml];")
    assert "secret" not in str(info.value)
    ok = b'<?xml version="1.0"?><osm version="0.6"><remark>fine</remark><node id="1" lat="0" lon="0"/></osm>'
    monkeypatch.setattr(osm._NO_REDIRECT, "open", lambda req, timeout: Resp(ok))
    assert osm.overpass_fetch("[out:xml];") == ok


def test_an_osm_base_cut_off_by_the_read_is_no_timestamp(tmp_path: Path) -> None:
    extract = tmp_path / "x.osm"
    extract.write_text("<osm>" + " " * 2020 + '<meta osm_base="2026-10-17T10:00:00Z"/></osm>', encoding="utf-8")
    assert osm.osm_timestamp(extract) is None  # the closing quote is past the 2048 bytes read
    extract.write_text('<osm><meta osm_base="2026-10-17T10:00:00Z"/></osm>', encoding="utf-8")
    assert osm.osm_timestamp(extract) == "2026-10-17T10:00:00Z"
    extract.write_text("<osm/>", encoding="utf-8")
    assert osm.osm_timestamp(extract) is None


def test_a_layer_is_empty_below_its_min_zoom() -> None:
    preset = osm.LayerPreset("polygon", "multipolygons", {0: {"natural": ["water"]}, 10: {"natural": None}}, min_zoom=8)
    assert preset.tags_for(5) == {} and preset.tags_for(7) == {}
    assert preset.tags_for(8) == {"natural": ["water"]} and preset.tags_for(12) == {"natural": None}
