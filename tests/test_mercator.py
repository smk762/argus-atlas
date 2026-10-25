from __future__ import annotations

import math

import pytest

from argus_atlas.mercator import (
    ORIGIN_SHIFT,
    bbox_to_mercator,
    intersects,
    lonlat_to_mercator,
    mercator_to_bbox,
    mercator_to_lonlat,
    native_zoom,
    resolution_at_zoom,
    tile_bounds,
    tiles_covering,
    zoom_range,
)
from argus_atlas.models import MAX_LATITUDE, BBox


def test_projection_known_points() -> None:
    assert lonlat_to_mercator(0, 0) == (0.0, 0.0)
    x, y = lonlat_to_mercator(180, 0)
    assert math.isclose(x, ORIGIN_SHIFT, rel_tol=1e-12)
    _, y45 = lonlat_to_mercator(0, 45)
    assert math.isclose(y45, 5621521.486, abs_tol=0.01)
    lon, lat = mercator_to_lonlat(x, y45)
    assert math.isclose(lon, 180) and math.isclose(lat, 45, abs_tol=1e-9)


def test_bbox_round_trip() -> None:
    bbox = BBox(west=10.02, south=45.01, east=10.18, north=45.09)
    xmin, ymin, xmax, ymax = bbox_to_mercator(bbox)
    assert xmin < xmax and ymin < ymax
    west, south = mercator_to_lonlat(xmin, ymin)
    assert math.isclose(west, bbox.west, abs_tol=1e-9) and math.isclose(south, bbox.south, abs_tol=1e-9)


def test_zoom_resolution_and_native() -> None:
    assert math.isclose(resolution_at_zoom(0), 156543.03392804097)
    assert math.isclose(resolution_at_zoom(14), 9.554628535647032)
    assert native_zoom(9.55) == 14
    assert native_zoom(19.1) == 13
    assert native_zoom(70.0) == 11  # z11 = 76.4 m, z12 = 38.2 m
    # never finer than the cap, even for a 1 m/px canvas
    assert native_zoom(1.0) == 14
    assert native_zoom(200000.0) == 0
    with pytest.raises(ValueError):
        native_zoom(0)


def test_zoom_range_clips_at_zero() -> None:
    assert zoom_range(11) == (9, 11)
    assert zoom_range(1) == (0, 1)
    assert zoom_range(0) == (0, 0)


def test_tile_bounds_and_cover() -> None:
    # The single z0 tile is the whole world.
    assert tile_bounds(0, 0, 0) == (-ORIGIN_SHIFT, -ORIGIN_SHIFT, ORIGIN_SHIFT, ORIGIN_SHIFT)
    # z1: tile (1, 0) is the north-east quadrant.
    xmin, ymin, xmax, ymax = tile_bounds(1, 1, 0)
    assert (xmin, ymin, xmax, ymax) == (0.0, 0.0, ORIGIN_SHIFT, ORIGIN_SHIFT)
    box = bbox_to_mercator(BBox(west=10.02, south=45.01, east=10.18, north=45.09))
    tx0, ty0, tx1, ty1 = tiles_covering(box, 11)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            assert intersects(tile_bounds(11, tx, ty), box)
    # the neighbours just outside the range do not intersect
    assert not intersects(tile_bounds(11, tx0 - 1, ty0), box)
    assert not intersects(tile_bounds(11, tx1 + 1, ty0), box)


def test_mercator_to_bbox_clamps_the_world_edges() -> None:
    # +-ORIGIN_SHIFT unprojects to +-180.00000000000003, which BBox would refuse.
    bbox = mercator_to_bbox(-ORIGIN_SHIFT, -ORIGIN_SHIFT, ORIGIN_SHIFT, ORIGIN_SHIFT)
    assert (bbox.west, bbox.east) == (-180.0, 180.0)
    assert -MAX_LATITUDE <= bbox.south < bbox.north <= MAX_LATITUDE
