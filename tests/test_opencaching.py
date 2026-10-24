from __future__ import annotations

import pytest

from argus_atlas.models import AtlasError
from argus_atlas.opencaching import AU, allow_reuse


def test_australian_reuse_carries_the_licence() -> None:
    record = allow_reuse("au")
    assert record["source"] == AU.site
    assert record["license"] == "CC-BY-NC-SA-2.5"
    assert record["license_url"].endswith("/by-nc-sa/2.5/")
    assert "Geocaching Australia" in record["attribution"]
    assert record["commercial"] is False
    assert record["share_alike"] is True
    assert record["api"] == "https://geocaching.com.au/api/services/"


def test_commercial_reuse_of_australian_listings_is_refused() -> None:
    with pytest.raises(AtlasError, match="CC-BY-NC-SA-2.5"):
        allow_reuse("au", commercial=True)


def test_paid_app_is_outside_the_australian_api_terms() -> None:
    with pytest.raises(AtlasError, match="without cost"):
        allow_reuse("au", app_is_free=False)


def test_geocaching_com_stays_rejected() -> None:
    with pytest.raises(AtlasError, match="Geocaching.com"):
        allow_reuse("geocaching.com")


def test_other_nodes_are_not_licensed_yet() -> None:
    with pytest.raises(AtlasError, match="licensed so far: au"):
        allow_reuse("de")
