"""OpenCaching-family listings. The first installation is Geocaching Australia.

Geocaching.com (Groundspeak) is rejected: there is no public geodatabase, and
the partner API licence does not allow an anonymous copy (issue #10).
Geocaching Australia (https://geocaching.com.au/) is the Australian open
listing site. It is run by volunteers, unaffiliated with Groundspeak, and its
API speaks OKAPI-shaped JSON. Uploaded content is licensed CC BY-NC-SA 2.5,
stated on the site footer and in the API's ``attribution_note`` field.

This module is the licence gate. A later fetch has to call :func:`allow_reuse`
before it writes an extract, so a commercial bake cannot take the coordinates
silently. Nothing here talks to the network: the API also requires a consumer
key and a logged-in member, and it refuses anonymous calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from argus_atlas.models import AtlasError

# The sentence Geocaching Australia returns in ``attribution_note``, plus the
# site the attribution has to name. CC BY requires the author and the source.
AU_ATTRIBUTION = (
    "Geocaching Australia (https://geocaching.com.au/). "
    "Information licensed under the Creative Commons Attribution-NonCommercial-ShareAlike 2.5 deed "
    "(https://creativecommons.org/licenses/by-nc-sa/2.5/)."
)

# Sources this package will not grow an installation for. Kept here so a later
# change has to delete the rejection rather than add a key beside it.
REJECTED_SOURCES: dict[str, str] = {
    "geocaching.com": (
        "Geocaching.com (Groundspeak) is not a source: no public geodatabase, "
        "and the partner API licence does not allow anonymous redistribution"
    ),
}


@dataclass(frozen=True)
class DataLicence:
    """The content licence of one installation, separate from its API access rules."""

    spdx: str
    url: str
    attribution: str
    commercial: bool
    share_alike: bool


@dataclass(frozen=True)
class Installation:
    """One national listing site atlas is willing to read, and the terms it reads under."""

    code: str
    name: str
    site: str
    api: str
    licence: DataLicence
    # The Australian API is free only when the calling app is also free, and
    # bulk dumps are arranged with the site administrators rather than pulled
    # through the API. Both are access rules, not the content licence.
    app_must_be_free: bool = True


AU = Installation(
    code="au",
    name="Geocaching Australia",
    site="https://geocaching.com.au/",
    api="https://geocaching.com.au/api/services/",
    licence=DataLicence(
        spdx="CC-BY-NC-SA-2.5",
        url="https://creativecommons.org/licenses/by-nc-sa/2.5/",
        attribution=AU_ATTRIBUTION,
        commercial=False,
        share_alike=True,
    ),
)

INSTALLATIONS: dict[str, Installation] = {AU.code: AU}


def allow_reuse(installation: str = "au", *, commercial: bool = False, app_is_free: bool = True) -> dict:
    """Provenance a manifest must store, or a refusal when the reuse is outside the licence.

    ``commercial`` is the caller's declaration. Geocaching Australia's content
    licence is non-commercial, so a paid hunt cannot carry these coordinates.
    ``app_is_free`` is the API access rule: the key is provided without cost
    only when the app is too.
    """
    if installation in REJECTED_SOURCES:
        raise AtlasError(REJECTED_SOURCES[installation])
    try:
        inst = INSTALLATIONS[installation]
    except KeyError:
        known = ", ".join(sorted(INSTALLATIONS))
        raise AtlasError(f"unknown OpenCaching installation {installation!r}; licensed so far: {known}") from None
    if commercial and not inst.licence.commercial:
        raise AtlasError(f"{inst.name} listings are {inst.licence.spdx}; commercial reuse is outside that licence")
    if inst.app_must_be_free and not app_is_free:
        raise AtlasError(
            f"the {inst.name} API is provided without cost only when the app is also provided without cost"
        )
    return {
        "source": inst.site,
        "name": inst.name,
        "api": inst.api,
        "license": inst.licence.spdx,
        "license_url": inst.licence.url,
        "attribution": inst.licence.attribution,
        "commercial": False,
        "share_alike": inst.licence.share_alike,
    }
