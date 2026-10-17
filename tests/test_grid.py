from __future__ import annotations

import pytest

from argus_atlas.controls import plan_grid
from argus_atlas.mercator import bbox_to_mercator
from argus_atlas.models import MAX_LONG_SIDE, SIDE_MULTIPLE, AtlasError, BBox

BBOX = BBox(west=10.02, south=45.01, east=10.18, north=45.09)


def test_bbox_parse_and_validation() -> None:
    assert BBox.parse("10.02, 45.01,10.18,45.09").as_list() == [10.02, 45.01, 10.18, 45.09]
    with pytest.raises(AtlasError, match="west,south,east,north"):
        BBox.parse("1,2,3")
    with pytest.raises(AtlasError, match="four numbers"):
        BBox.parse("a,b,c,d")
    with pytest.raises(AtlasError):
        BBox.from_list([10.2, 45.0, 10.0, 45.1])  # west > east
    with pytest.raises(AtlasError):
        BBox.from_list([10.0, 86.0, 10.2, 87.0])  # beyond the Mercator cap


def test_default_grid_fits_long_side_and_multiples_of_64() -> None:
    grid = plan_grid(BBOX)
    assert grid.long_side == 1024
    assert grid.width % SIDE_MULTIPLE == 0 and grid.height % SIDE_MULTIPLE == 0
    assert grid.pixel_width > 0 and grid.pixel_height == -grid.pixel_width
    # The grid covers the AOI (padding only ever grows it) and stays centred on it.
    xmin, ymin, xmax, ymax = bbox_to_mercator(BBOX)
    assert grid.ulx <= xmin + 1e-6 and grid.lrx >= xmax - 1e-6
    assert grid.lry <= ymin + 1e-6 and grid.uly >= ymax - 1e-6
    assert abs((xmin - grid.ulx) - (grid.lrx - xmax)) < 1e-6
    assert grid.crs == "EPSG:3857"


def test_long_side_validation() -> None:
    assert plan_grid(BBOX, long_side=MAX_LONG_SIDE).long_side == MAX_LONG_SIDE
    with pytest.raises(AtlasError, match="multiple of 64"):
        plan_grid(BBOX, long_side=1000)
    with pytest.raises(AtlasError, match="multiple of 64"):
        plan_grid(BBOX, long_side=2048)
    with pytest.raises(AtlasError, match="not both"):
        plan_grid(BBOX, long_side=1024, resolution=10.0)


def test_explicit_resolution_too_fine_fails_with_max_extent() -> None:
    # ~17.8 km wide at 5 m/px needs ~3560 px: over the 1536 cap.
    with pytest.raises(AtlasError) as excinfo:
        plan_grid(BBOX, resolution=5.0)
    message = str(excinfo.value)
    assert "1536" in message and "--bbox" in message
    # The suggested extent is parseable and fits.
    suggested = message.split("--bbox ")[1].split(" ")[0]
    fit = BBox.parse(suggested)
    assert BBOX.west <= fit.west < fit.east <= BBOX.east
    assert plan_grid(fit, resolution=5.0).long_side <= MAX_LONG_SIDE


def test_explicit_coarse_resolution_is_honoured() -> None:
    grid = plan_grid(BBOX, resolution=100.0)
    assert grid.resolution == 100.0
    assert grid.width % 64 == 0 and grid.height % 64 == 0
    assert grid.long_side <= MAX_LONG_SIDE
    with pytest.raises(AtlasError, match="positive"):
        plan_grid(BBOX, resolution=0)
