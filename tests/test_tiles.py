from __future__ import annotations

from pathlib import Path

import pytest

from argus_atlas.models import Grid
from argus_atlas.png import write_png
from argus_atlas.tiles import TilesError, pack_pmtiles

pytest.importorskip("pmtiles")

GRID = Grid(width=256, height=256, ulx=0.0, uly=2560.0, pixel_width=10.0, pixel_height=-10.0)


def test_packing_no_tiles_is_a_tiles_error(tmp_path: Path) -> None:
    with pytest.raises(TilesError, match="no tiles to pack"):
        pack_pmtiles([], tmp_path / "out.pmtiles", GRID, name="empty")
    assert not (tmp_path / "out.pmtiles").exists()


def test_a_failed_pack_keeps_the_archive_already_there(tmp_path: Path) -> None:
    out = tmp_path / "fantasy.pmtiles"
    tile = write_png(tmp_path / "0.png", 4, 4)
    assert pack_pmtiles([(14, 8192, 8191, tile)], out, GRID, name="first") == 1
    good = out.read_bytes()

    with pytest.raises(OSError):  # the second tile is gone before it is read
        pack_pmtiles([(14, 8192, 8191, tile), (14, 8193, 8191, tmp_path / "gone.png")], out, GRID, name="second")
    assert out.read_bytes() == good
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0.png", "fantasy.pmtiles"], "no partial file is left behind"
