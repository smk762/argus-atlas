"""Injectable HTTP transport for the ComfyUI adapter (stdlib ``urllib``).

Tests pass a fake :class:`Transport`; production uses :class:`UrllibTransport`,
so the backend has no HTTP runtime dependency. Transport-level failures become
:class:`BackendError` so callers see one error type.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from typing import Protocol

from argus_atlas.backends.base import BackendError


class Transport(Protocol):
    """The small HTTP surface the adapter needs."""

    def post_json(self, path: str, payload: dict) -> dict: ...
    def get_json(self, path: str) -> dict: ...
    def get_bytes(self, path: str) -> bytes: ...
    def post_file(self, path: str, *, field: str, filename: str, data: bytes, fields: dict[str, str]) -> dict: ...


def encode_multipart(
    *, field: str, filename: str, data: bytes, fields: dict[str, str], content_type: str = "image/png"
) -> tuple[bytes, str]:
    """``(body, boundary)`` of a multipart/form-data upload with one file."""
    boundary = "----argus-atlas-" + secrets.token_hex(12)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(),
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


class UrllibTransport:
    """Default :class:`Transport` over the stdlib."""

    def __init__(self, base_url: str, *, timeout: float = 180.0, label: str = "ComfyUI") -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.label = label

    def _request(
        self, path: str, *, data: bytes | None = None, method: str = "GET", headers: dict | None = None
    ) -> bytes:
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", "replace") if exc.fp else ""
            raise BackendError(f"{self.label} {method} {path} returned HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BackendError(f"{self.label} request to {req.full_url} failed: {exc}") from exc

    def _loads(self, raw: bytes) -> dict:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise BackendError(f"{self.label} returned a non-JSON response") from exc

    def post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        return self._loads(self._request(path, data=body, method="POST", headers={"Content-Type": "application/json"}))

    def get_json(self, path: str) -> dict:
        return self._loads(self._request(path))

    def get_bytes(self, path: str) -> bytes:
        return self._request(path)

    def post_file(self, path: str, *, field: str, filename: str, data: bytes, fields: dict[str, str]) -> dict:
        body, boundary = encode_multipart(field=field, filename=filename, data=data, fields=fields)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._loads(self._request(path, data=body, method="POST", headers=headers))
