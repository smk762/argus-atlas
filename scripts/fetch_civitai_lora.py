#!/usr/bin/env python3
"""Fetch SDXL LoRAs from Civitai into a ComfyUI-style ``loras/`` tree, pinned by SHA256.

Operator helper, not part of argus-atlas (atlas never downloads). Standard
library only, so it runs anywhere: ``python3 -I fetch_civitai_lora.py ...``.

    # see what a model id is before spending bandwidth (lists every SDXL version)
    python3 -I scripts/fetch_civitai_lora.py --list 382959 694762
    # a particular version (e.g. a lighter strength) instead of the newest
    python3 -I scripts/fetch_civitai_lora.py --out ./models --version 427436 382959

    # download the newest SDXL version of each into ./loras/sdxl/cartography/<slug>/
    CIVITAI_TOKEN=... python3 -I scripts/fetch_civitai_lora.py --out ./models 382959

    # then ship the tree to the GPU host, into the tree that holds the checkpoint
    rsync -av ./models/loras/ morpheus:/srv/imogen/fast/models-hot/loras/

Each file lands next to a ``<name>.civitai.json`` sidecar (the same convention
as the checkpoints on morpheus) holding the model/version ids, base model,
trigger words and licence flags. The SHA256 is checked against Civitai's when
the API reports one.

Many Civitai downloads need a logged-in token (``--token`` / ``CIVITAI_TOKEN``);
the metadata API does not. Civitai is region-blocked on some networks, in which
case run this somewhere else and rsync. Interrupted downloads resume from the
``.part`` file on the next run.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://civitai.com/api/v1/models/"
UA = "argus-atlas-fetch/1 (+https://github.com/smk762/argus-atlas)"


def model_id(arg: str) -> int:
    """Accept a bare id or a civitai.com/models/<id>[/slug] URL."""
    m = re.search(r"(?:^|/models/)(\d+)", arg)
    if not m:
        raise argparse.ArgumentTypeError(f"not a Civitai model id or URL: {arg!r}")
    return int(m.group(1))


def fetch_json(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if "error" in data and "name" not in data:
        raise SystemExit(f"{url}: {data['error']}")
    return data


def matching_versions(model: dict, base_prefix: str) -> list[dict]:
    """Versions whose baseModel starts with *base_prefix* (case-insensitive), newest first."""
    return [
        v for v in model.get("modelVersions", []) if (v.get("baseModel") or "").lower().startswith(base_prefix.lower())
    ]


def pick_version(model: dict, base_prefix: str, wanted: int | None = None) -> dict | None:
    """The version with id *wanted*, else the newest one matching *base_prefix*."""
    if wanted is not None:
        return next((v for v in model.get("modelVersions", []) if v["id"] == wanted), None)
    versions = matching_versions(model, base_prefix)
    return versions[0] if versions else None


def primary_file(version: dict) -> dict | None:
    files = [f for f in version.get("files", []) if f.get("name", "").endswith(".safetensors")]
    if not files:
        return None
    files.sort(key=lambda f: (not f.get("primary", False), f.get("sizeKB", 0)))
    return files[0]


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s[:60] or "lora"


def describe_version(version: dict) -> str:
    f = primary_file(version) or {}
    size = round((f.get("sizeKB") or 0) / 1024)
    return (
        f"    version {version['id']} {version['name']!r} base={version.get('baseModel')} words={version.get('trainedWords')}"
        f"\n        file {f.get('name')} {size} MB sha256={(f.get('hashes') or {}).get('SHA256', '?')}"
    )


def describe(model: dict, version: dict | None, *, all_matching: list[dict] | None = None) -> str:
    stats = model.get("stats", {})
    head = (
        f"{model['id']}: {model['name']}  [{model.get('type')}]  "
        f"downloads={stats.get('downloadCount')} rating={stats.get('rating')}  "
        f"commercial={model.get('allowCommercialUse')} derivatives={model.get('allowDerivatives')}"
    )
    if all_matching:
        return (
            head
            + "\n"
            + "\n".join(describe_version(v) for v in all_matching)
            + "\n    (newest is picked; --version <id> for another)"
        )
    if version is None:
        bases = sorted({v.get("baseModel") for v in model.get("modelVersions", [])})
        return head + f"\n    no matching version; available base models: {bases}"
    return head + "\n" + describe_version(version)


HTTP_HINTS = {
    401: "token missing or rejected — create one at civitai.com/user/account (API Keys)",
    403: "forbidden — the model may require logging in and accepting its terms on the site first",
    404: "no file at that download URL",
    451: "region-blocked",
}


def _hash_existing(part: Path, h) -> int:  # noqa: ANN001 - a hashlib object
    """Feed the bytes already in *part* to *h*; return how many there were."""
    done = 0
    with part.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            done += len(chunk)
    return done


def download(url: str, dest: Path, token: str | None, expected_sha: str | None, *, attempts: int = 5) -> str:
    """Stream *url* to *dest* via ``dest.part``, resuming whatever is already there; return the SHA256 hex.

    A ``.part`` from an earlier run is continued with a ``Range`` request (and
    re-hashed first, so the final SHA256 covers the whole file). A connection
    that drops mid-transfer is retried up to *attempts* times from where it
    stopped; an HTTP error is reported and stops the run.
    """
    if token:
        url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    done = _hash_existing(part, h) if part.is_file() else 0
    if done:
        print(f"    resuming at {done / 1048576:,.0f} MB", file=sys.stderr)

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if done:
            req.add_header("Range", f"bytes={done}-")
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and done:  # range past the end: the .part is already complete
                break
            body = exc.read(300).decode("utf-8", "replace").strip()
            raise SystemExit(
                f"download failed: HTTP {exc.code} {exc.reason}; {HTTP_HINTS.get(exc.code, '')}\n    {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise SystemExit(f"download failed after {attempts} attempts: {exc}; rerun to resume") from exc
            print(f"    connect failed ({exc}); retrying {attempt}/{attempts}", file=sys.stderr)
            continue

        with resp:
            if "text/html" in resp.headers.get("Content-Type", ""):
                raise SystemExit(
                    f"{resp.geturl()}: got an HTML page instead of a file — the model probably needs a token"
                )
            if done and resp.status != 206:  # server ignored the Range: start over
                print("    server does not resume; restarting from 0", file=sys.stderr)
                h = hashlib.sha256()
                done = 0
            total = done + int(resp.headers.get("Content-Length") or 0)
            try:
                with part.open("ab" if done else "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total:
                            print(f"\r    {done / 1048576:,.0f} / {total / 1048576:,.0f} MB", end="", file=sys.stderr)
            except (TimeoutError, OSError, http.client.IncompleteRead) as exc:
                print(f"\n    transfer dropped at {done / 1048576:,.0f} MB ({exc})", file=sys.stderr)
                if attempt == attempts:
                    raise SystemExit(f"download failed after {attempts} attempts; rerun to resume the .part") from exc
                continue
        print(file=sys.stderr)
        if not total or done >= total:
            break
        print(f"    short read ({done} of {total} bytes); resuming", file=sys.stderr)
    else:
        raise SystemExit(f"download incomplete after {attempts} attempts; rerun to resume the .part")

    digest = h.hexdigest()
    if expected_sha and digest.lower() != expected_sha.lower():
        part.unlink(missing_ok=True)
        raise SystemExit(
            f"{dest.name}: SHA256 mismatch (got {digest}, Civitai says {expected_sha}); .part removed, rerun"
        )
    part.replace(dest)
    return digest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="+", type=model_id, help="Civitai model ids or URLs")
    ap.add_argument(
        "--out", type=Path, default=Path("models"), help="root that gets loras/<subdir>/ (default ./models)"
    )
    ap.add_argument("--subdir", default="sdxl/cartography", help="folder under loras/ (default sdxl/cartography)")
    ap.add_argument("--base", default="SDXL", help="baseModel prefix to accept (default SDXL)")
    ap.add_argument("--version", type=int, default=None, help="a specific modelVersion id instead of the newest match")
    ap.add_argument("--token", default=os.environ.get("CIVITAI_TOKEN"), help="API key (default $CIVITAI_TOKEN)")
    ap.add_argument("--list", action="store_true", help="only print what would be downloaded")
    args = ap.parse_args()

    for mid in args.models:
        try:
            model = fetch_json(f"{API}{mid}", args.token)
        except urllib.error.HTTPError as exc:
            hint = " (region-blocked: run this somewhere else and rsync)" if exc.code == 451 else ""
            print(f"{mid}: HTTP {exc.code}{hint}", file=sys.stderr)
            continue
        version = pick_version(model, args.base, args.version)
        if args.list:
            print(describe(model, version, all_matching=matching_versions(model, args.base)))
            continue
        print(describe(model, version))
        if version is None:
            continue
        f = primary_file(version)
        if f is None:
            print("    no .safetensors file in that version", file=sys.stderr)
            continue
        folder = args.out / "loras" / args.subdir / slug(model["name"])
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f["name"]
        expected = (f.get("hashes") or {}).get("SHA256")
        if dest.is_file():
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if expected and digest.lower() == expected.lower():
                print(f"    already present: {dest}")
                continue
        print(f"    -> {dest}")
        digest = download(f.get("downloadUrl") or version["downloadUrl"], dest, args.token, expected)
        sidecar = {
            "source": "civitai",
            "model_id": model["id"],
            "model_name": model["name"],
            "version_id": version["id"],
            "version_name": version["name"],
            "base_model": version.get("baseModel"),
            "trained_words": version.get("trainedWords"),
            "sha256": digest,
            "allow_commercial_use": model.get("allowCommercialUse"),
            "allow_derivatives": model.get("allowDerivatives"),
            "allow_no_credit": model.get("allowNoCredit"),
            "url": f"https://civitai.com/models/{model['id']}?modelVersionId={version['id']}",
        }
        (folder / (Path(f["name"]).stem + ".civitai.json")).write_text(json.dumps(sidecar, indent=2) + "\n")
        print(f"    ok  sha256={digest}  comfyui name: {args.subdir}/{slug(model['name'])}/{f['name']}")

    print(
        f"\nship it:  rsync -av {args.out}/loras/ morpheus:/srv/imogen/fast/models-hot/loras/   "
        "(the tree that holds the checkpoint you bake with; atlas resolves one --models-dir)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
