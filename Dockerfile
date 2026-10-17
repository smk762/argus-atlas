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

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# hatch-vcs derives the version from git history, so the build context must
# include .git (keep it out of .dockerignore) and the image needs git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Extras baked into the image. The system Python is Ubuntu's (externally
# managed), so the package goes into its own venv; gdal2tiles.py keeps using
# /usr/bin/python3 + the system osgeo bindings.
ARG EXTRAS="server,cli,tiles"
RUN uv venv /opt/venv --python /usr/bin/python3 \
    && uv pip install --python /opt/venv/bin/python --no-cache ".[${EXTRAS}]"

ENV PATH="/opt/venv/bin:${PATH}" \
    ATLAS_JOBS_DIR=/data/out/atlas \
    ATLAS_DEM_DIR=/data/dem \
    ATLAS_MODELS_DIR=/data/models

EXPOSE 8105
CMD ["argus-atlas", "serve", "--port", "8105", "--cors"]
