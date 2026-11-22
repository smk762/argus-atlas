"""The must-nevers in CLAUDE.md that no behavioural test elsewhere covers (#55).

Each test names the rule it checks; CLAUDE.md's "Must never happen" table points here. They read the repo
(pyproject, uv.lock, compose, Dockerfile, docs, workflows) or the app's routes, so a change that breaks a rule fails
CI instead of relying on someone to remember the prose.
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from argus_atlas.server import create_app
from tests.conftest import TEST_BBOX, TEST_LONG_SIDE, requires_gdal
from tests.fakebackend import FakeBackend

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "argus_atlas"


# --- GDAL is a system package, never pip; rasterio is not a dependency -----------------------------------------

# Wheels that bundle or bind their own GDAL: a second libgdal next to the system one, or the binding the image
# deliberately leaves to the system python3-gdal.
BANNED_PACKAGES = {"gdal", "pygdal", "gdal-utils", "rasterio", "fiona", "osgeo"}


def test_no_gdal_wheel_or_rasterio_is_a_dependency() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {pkg["name"].lower() for pkg in lock["package"]}
    assert not locked & BANNED_PACKAGES, sorted(locked & BANNED_PACKAGES)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"]) + [d for ds in project["optional-dependencies"].values() for d in ds]
    names = {re.split(r"[\[<>=!~ ;]", d, maxsplit=1)[0].lower() for d in declared}
    assert not names & BANNED_PACKAGES, sorted(names & BANNED_PACKAGES)


# --- docs/ has no machine names, host paths, LAN addresses or emails -------------------------------------------

# Studio and Realms import docs/ verbatim, and README/CHANGELOG are published on PyPI and GitHub Releases. The same
# gate as their importers: private IPs, home and mount paths, emails, plus the private names (the suite's GPU hosts and
# the like) in OPSEC_DENYLIST. The names are never committed, not even in this gate: CI fills OPSEC_DENYLIST from the
# Actions secret of the same name, and a local run reads it from the environment or the untracked `.opsec-denylist`
# file (gitignored). Comma- or newline-separated, matched case-insensitively as whole words. With neither set, the
# name check is skipped (with one notice) and the other checks still run.
OPSEC_FILES = sorted({*ROOT.glob("docs/**/*.md"), ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / ".env.example"})
OPSEC_PATTERNS = {
    "private IPv4": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "home or mount path": re.compile(r"(?:/home/|/Users/|/root/|/mnt/|/media/|[A-Z]:\\Users\\)[\w.-]+"),
    "email": re.compile(r"\b[\w.%+-]+@(?!example\.(?:com|org)\b)[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"),
}
ALLOWED_EMAILS = {"noreply@anthropic.com"}
OPSEC_DENYLIST_FILE = ROOT / ".opsec-denylist"


def _denylist(file: Path | None = None) -> list[str]:
    file = OPSEC_DENYLIST_FILE if file is None else file
    raw = os.environ.get("OPSEC_DENYLIST", "")
    if file.is_file():
        raw += "\n" + file.read_text(encoding="utf-8")
    return [term.strip() for term in re.split(r"[,\n]", raw) if term.strip()]


def _denylist_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """Every term as a whole word, case-insensitively; None when there are none."""
    if not terms:
        return None
    alternatives = "|".join(re.escape(term) for term in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def _opsec_findings(text: str, terms: list[str]) -> list[str]:
    found = [
        f"{label}: {m.group(0)!r}"
        for label, pattern in OPSEC_PATTERNS.items()
        for m in pattern.finditer(text)
        if not (label == "email" and m.group(0).lower() in ALLOWED_EMAILS)
    ]
    denied = _denylist_pattern(terms)
    hits = {m.group(0).lower() for m in denied.finditer(text)} if denied else set()
    found += ["OPSEC_DENYLIST term"] * len(hits)  # one per term found, never echoing it
    return found


def test_the_opsec_denylist_is_configured() -> None:
    """The one notice that the machine-name check is off: skipped, never failed, so a fork's PR (no secrets) passes."""
    if not _denylist():
        pytest.skip(
            "OPSEC_DENYLIST is not configured (env, Actions secret or .opsec-denylist): machine names unchecked"
        )


@pytest.mark.parametrize("path", OPSEC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_published_docs_pass_the_opsec_gate(path: Path) -> None:
    found = _opsec_findings(path.read_text(encoding="utf-8"), _denylist())
    assert not found, f"{path.relative_to(ROOT)}: {found}"


def test_the_opsec_gate_catches_what_it_is_for() -> None:
    hits = {
        label
        for label, pattern in OPSEC_PATTERNS.items()
        for sample in ("http://192.168.1.20:8188", "/home/someone/models", "ops@corp.io")
        if pattern.search(sample)
    }
    assert hits == set(OPSEC_PATTERNS)
    clean = "http://<GPU_HOST>:8188 dragonhound.dev 127.0.0.1 /data/models user@example.com"
    assert not any(pattern.search(clean) for pattern in OPSEC_PATTERNS.values())


# Obviously fake names: the real ones live only in the secret.
FAKE_TERMS = ["examplehost", "Other-Box"]


def test_the_denylist_matches_whole_words_case_insensitively_and_never_echoes_a_term() -> None:
    for text in ("ssh examplehost", "http://EXAMPLEHOST:8188/", "on examplehost.", "via other-box today"):
        assert _opsec_findings(text, FAKE_TERMS) == ["OPSEC_DENYLIST term"], text
    for text in ("examplehosts", "myexamplehost", "examplehost_2", "other-boxes", "another-box"):
        assert _opsec_findings(text, FAKE_TERMS) == [], text  # a term inside a longer word is not that name
    assert not any("examplehost" in f.lower() for f in _opsec_findings("examplehost", FAKE_TERMS))
    assert _opsec_findings("examplehost, EXAMPLEHOST", FAKE_TERMS) == ["OPSEC_DENYLIST term"]  # per term, not per hit


def test_the_denylist_comes_from_the_env_and_the_untracked_file(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "absent"
    monkeypatch.delenv("OPSEC_DENYLIST", raising=False)
    assert _denylist(missing) == []
    assert _opsec_findings("ssh examplehost from 127.0.0.1", _denylist(missing)) == []  # unconfigured: skipped
    assert _opsec_findings("ssh examplehost at 10.0.0.5", _denylist(missing)) == ["private IPv4: '10.0.0.5'"]

    monkeypatch.setenv("OPSEC_DENYLIST", " examplehost ,\nOther-Box\n")
    assert _denylist(missing) == ["examplehost", "Other-Box"]
    file = tmp_path / ".opsec-denylist"
    file.write_text("filehost\n", encoding="utf-8")
    assert _denylist(file) == ["examplehost", "Other-Box", "filehost"]


def test_the_denylist_file_is_never_committed() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert OPSEC_DENYLIST_FILE.name in ignored or f"/{OPSEC_DENYLIST_FILE.name}" in ignored


# --- No route takes a filesystem path ----------------------------------------------------------------------------

# Every field a client can send, by request model. A new one fails here until someone decides whether it names a file
# and, if it does, resolves it under a root (config.resolve_under_root) or by (job_id, kind) and adds an escape test.
REQUEST_FIELDS = {
    "BakeStreamRequest": {
        "job_id", "checkpoint", "lora", "lora_weight", "preset", "prompt", "negative", "seed", "steps", "cfg", "denoise",
    },
    "ControlsRequest": {
        "dem", "bbox", "land", "layers", "sea_level", "composite", "resampling", "long_side", "resolution", "job_id",
        "z_factor", "vertical_exaggeration",
    },
    "OsmRequest": {"name", "bbox", "zoom", "layers"},
    "TilesRequest": {"job_id", "min_zoom", "max_zoom"},
}  # fmt: skip
PATH_PARAMS = {"job_id", "kind"}  # job ids match the safe-id rule; kinds are looked up, never joined as paths


def test_no_route_accepts_a_field_nobody_has_reviewed_for_paths() -> None:
    spec = create_app().openapi()
    bodies = {
        ref.rsplit("/", 1)[-1]
        for item in spec["paths"].values()
        for op in item.values()
        if (
            ref := op.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref")
        )
    }
    assert bodies == set(REQUEST_FIELDS)
    for name, fields in REQUEST_FIELDS.items():
        assert set(spec["components"]["schemas"][name]["properties"]) == fields, name
    params = {p["name"] for item in spec["paths"].values() for op in item.values() for p in op.get("parameters", [])}
    assert params <= PATH_PARAMS, params - PATH_PARAMS


def test_every_file_naming_field_is_held_to_its_root(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    jobs, dem = tmp_path / "jobs", tmp_path / "dem"
    jobs.mkdir()
    dem.mkdir()
    (dem / "dem.tif").write_bytes(b"x")
    client = TestClient(create_app(jobs_dir=jobs, dem_dir=dem))
    body = {"dem": "dem.tif", "bbox": [10.02, 45.01, 10.18, 45.09]}
    for escape in ("../outside.tif", "/etc/hosts"):
        for field in ({"dem": escape}, {"land": escape}, {"layers": {"water": escape}}):
            resp = client.post("/controls", json={**body, **field})
            assert resp.status_code == 400, (field, resp.json())
    # Model names never leave the models root either (the bake resolves them through it).
    from argus_atlas.backends.base import make_dir_resolver

    resolve = make_dir_resolver(tmp_path / "models")
    for name in ("../dem/dem.tif", "/etc/hosts"):
        with pytest.raises(FileNotFoundError):
            resolve(name)


# --- No route builds labels; place names never reach the model ---------------------------------------------------


def test_no_route_builds_labels() -> None:
    labelled = [
        (method, route.path)
        for route in create_app().routes
        if "label" in getattr(route, "path", "")
        for method in getattr(route, "methods", set())
    ]
    assert labelled and all(method in {"GET", "HEAD"} for method, _ in labelled), labelled


@requires_gdal
def test_place_names_never_reach_the_model(dem: Path, tmp_path: Path, models_dir: Path) -> None:
    import json

    from argus_atlas.bake import run_bake
    from argus_atlas.controls import build_controls
    from argus_atlas.labels import LABELS_NAME, label_job
    from argus_atlas.models import BakeParams

    places = tmp_path / "places.geojson"
    point = {"type": "Point", "coordinates": [10.10, 45.05]}
    feature = {"type": "Feature", "properties": {"NAME": "Inside", "SCALERANK": 2}, "geometry": point}
    places.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}), encoding="utf-8")
    job_dir = tmp_path / "aoi-1"
    build_controls(dem, TEST_BBOX, job_dir, long_side=TEST_LONG_SIDE)
    assert label_job(job_dir, places, zoom=6).count == 1

    backend = FakeBackend()
    sidecar = run_bake(
        job_dir, BakeParams(checkpoint="sd_xl_base_1.0.safetensors", seed=1), backend, models_root=models_dir
    )
    assert not {"labels", "places"} & set(sidecar.controls)
    [request] = backend.requests
    assert all(path.name != LABELS_NAME and path.suffix == ".png" for path in request.controls.values())


# --- A future geocache fetch calls allow_reuse ---------------------------------------------------------------------


def test_every_geocache_source_goes_through_the_licence_gate() -> None:
    """Any module that mentions a geocache source must call ``allow_reuse`` (opencaching.py is the gate itself)."""
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "opencaching.py":
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"geocach|opencach", text, re.IGNORECASE):
            continue
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Call)
        }
        assert "allow_reuse" in calls, f"{path.relative_to(ROOT)} touches a geocache source without allow_reuse"


# --- Weights are mounted, never copied; the checkout is never written as root ------------------------------------


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_weights_are_mounted_read_only_and_never_copied_into_the_image() -> None:
    services = _compose()["services"]
    for name in ("atlas", "serve"):
        [models] = [v for v in services[name]["volumes"] if isinstance(v, dict) and v["target"] == "/data/models"]
        assert models["read_only"] is True, name
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        if re.match(r"\s*(COPY|ADD)\b", line, re.IGNORECASE):
            assert not re.search(r"models|\.safetensors|\.ckpt|\.pt\b|\.bin\b", line, re.IGNORECASE), line
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "data/*/*" in ignored  # ./data/models, the default models mount, stays out of `COPY . .`


def test_compose_never_runs_as_root_on_the_mounts() -> None:
    services = _compose()["services"]
    for name in ("atlas", "serve", "test"):
        user = str(services[name].get("user", ""))
        assert user.startswith("${UID") and ":${GID" in user, (name, user)
    assert services["test"]["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"


# --- Release candidates never move :latest -----------------------------------------------------------------------


def test_only_a_final_release_moves_latest() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    release = "\n".join(line.split(" #", 1)[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r"^\s*latest=auto\s*$", release, re.MULTILINE)
    assert not re.search(r"latest=true|value=latest|:latest\b", release)
    tags = re.findall(r"type=(\w+),(?:pattern|value)=", release)  # image tags, not the gha build cache
    assert tags and set(tags) == {"pep440"}  # pep440 + latest=auto: alphas and rcs get their version tag only
