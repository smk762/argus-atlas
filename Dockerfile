# syntax=docker/dockerfile:1
#
# GDAL is a system dependency (gdalwarp, gdaldem, gdal_rasterize, gdal_translate,
# gdalinfo, gdal2tiles.py), so the image starts from OSGeo's GDAL build rather
# than python:*-slim + apt. ubuntu-small-3.11.4 = Ubuntu 24.04, Python 3.12,
# GDAL 3.11.4 with the Python tools (gdal2tiles.py) included.
#
# No generation weights live here: ComfyUI runs on the host GPU and the
# checkpoints/LoRAs are mounted read-only at /data/models for hashing only.
# PMTiles are packed by the Python `pmtiles` library (the [tiles] extra); the Go
# `pmtiles` CLI is not installed (it cannot pack an XYZ directory).
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.11.4

# Pin past the eMudhra/emSign incident. A floating :latest tag can stay cached
# on a pre-0.13.4 digest, which still trusts emSign ECC Root CA - G3.
# https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app

# hatch-vcs derives the version from git history, so the build context must
# include .git (keep it out of .dockerignore) and the image needs git.
# urllib (OSM fetch, ComfyUI transport) trusts the system bundle. Ubuntu's
# ca-certificates package lags, so distrust emSign ECC Root CA - G3 and the
# Comodo AAA cross-sign root here. Unaffected emSign roots stay enabled,
# matching certifi 2026.10.19 (discuss.python.org/t/109576).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get install -y --only-upgrade ca-certificates \
    && sed -i \
        -e 's|^mozilla/emSign_ECC_Root_CA_-_G3|!mozilla/emSign_ECC_Root_CA_-_G3|' \
        -e 's|^mozilla/Comodo_AAA|!mozilla/Comodo_AAA|' \
        /etc/ca-certificates.conf \
    && update-ca-certificates --fresh >/dev/null \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Extras baked into the image. The system Python is Ubuntu's (externally
# managed), so the package goes into its own venv; gdal2tiles.py keeps using
# /usr/bin/python3 + the system osgeo bindings.
ARG EXTRAS="server,cli,tiles"
# A uv venv has no pip of its own, so uv installs the pinned pip 26.2.2 wheel
# (hash-checked) alongside certifi 2026.10.19+ — both dropped the emSign roots.
RUN uv venv /opt/venv --python /usr/bin/python3 \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
        "certifi>=2026.10.19" ".[${EXTRAS}]"

ENV PATH="/opt/venv/bin:${PATH}" \
    ATLAS_JOBS_DIR=/data/out/atlas \
    ATLAS_DEM_DIR=/data/dem \
    ATLAS_MODELS_DIR=/data/models

EXPOSE 8105
CMD ["argus-atlas", "serve", "--port", "8105", "--cors"]
