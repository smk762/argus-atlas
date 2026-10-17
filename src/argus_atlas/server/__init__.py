"""argus-atlas HTTP server (FastAPI, port 8105). Requires the ``[server]`` extra."""

from argus_atlas.server.app import create_app

__all__ = ["create_app"]
