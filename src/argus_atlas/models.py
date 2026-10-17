"""Pydantic wire types for argus-atlas — the job sidecar is the API contract.

``job.json`` (:class:`JobSidecar`) is the **georeferencing authority** for a
bake: the diffusion model paints pixels whose geotransform was fixed here before
generation and is checked against it afterwards (:mod:`argus_atlas.georef`).
The control PNGs handed to the model carry no georeferencing of their own.

Everything a later reader needs to trust the overlay is pinned in the sidecar:
the DEM's SHA256, the exact GDAL argv arrays that were run, the canvas grid, the
checkpoint/LoRA digests of the bake, and the zoom range of the tile archive.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Sidecar schema version. Bump the minor for additive fields, the major for a
# breaking change to what a reader must understand.
ATLAS_VERSION = "0.1"

CRS = "EPSG:3857"
EPSG_CODE = 3857

# Canvas sizing. The long side defaults to 1024 and is hard-capped at 1536 in v0;
# both sides are rounded up to a multiple of 64 (SDXL latent alignment).
DEFAULT_LONG_SIDE = 1024
MAX_LONG_SIDE = 1536
SIDE_MULTIPLE = 64

# Generation defaults (overridable by CLI flags / request bodies).
DEFAULT_DENOISE = 0.55  # high enough to restyle, low enough that relief survives
DEFAULT_STEPS = 30
DEFAULT_CFG = 6.0
DEFAULT_LORA_WEIGHT = 0.8
DEFAULT_SAMPLER = "dpmpp_2m"
DEFAULT_SCHEDULER = "karras"
# Labels and compass roses fight georeferencing and tile seams, so they are
# prompted away by default.
DEFAULT_PROMPT = (
    "antique fantasy map, ink coastlines, parchment, illustrated relief, no text, no labels, no compass rose"
)
DEFAULT_NEGATIVE = "text, watermark, letters, words, frame, border, photograph, satellite"

# Tile pyramid: native-ZOOM_SPAN .. native, clipped to [0, MAX_ZOOM]. Never a
# zoom finer than the canvas.
ZOOM_SPAN = 2
MAX_ZOOM = 14

# Corner coordinates of the stamped GeoTIFF must match the sidecar this closely
# (map units, metres in EPSG:3857).
ALIGN_TOLERANCE = 1e-3

# Web Mercator is undefined at the poles; this is the conventional latitude cap.
MAX_LATITUDE = 85.05112878

SIDECAR_NAME = "job.json"

ControlKind = Literal["hillshade", "slope", "land"]
CONTROL_KINDS: tuple[ControlKind, ...] = ("hillshade", "slope", "land")
PreviewKind = Literal["hillshade", "slope", "land", "fantasy"]


class AtlasError(RuntimeError):
    """A user-facing failure: bad AOI, GDAL refused, alignment gate failed."""


class BBox(BaseModel):
    """A geographic AOI in EPSG:4326 degrees: ``west,south,east,north``."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, text: str) -> BBox:
        """Parse ``"west,south,east,north"`` (the CLI / request-body spelling)."""
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 4:
            raise AtlasError(f"bbox must be west,south,east,north — got {text!r}")
        try:
            values = [float(p) for p in parts]
        except ValueError as exc:
            raise AtlasError(f"bbox must be four numbers — got {text!r}") from exc
        return cls.from_list(values)

    @classmethod
    def from_list(cls, values: list[float] | tuple[float, ...]) -> BBox:
        if len(values) != 4:
            raise AtlasError(f"bbox must have four values — got {len(values)}")
        try:
            return cls(west=values[0], south=values[1], east=values[2], north=values[3])
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise AtlasError(f"invalid bbox: {exc}") from exc

    @model_validator(mode="after")
    def _check_order(self) -> BBox:
        if not (-180.0 <= self.west < self.east <= 180.0):
            raise ValueError(f"bbox longitudes must satisfy -180 <= west < east <= 180 (got {self.west}, {self.east})")
        if not (-MAX_LATITUDE <= self.south < self.north <= MAX_LATITUDE):
            raise ValueError(
                f"bbox latitudes must satisfy -{MAX_LATITUDE} <= south < north <= {MAX_LATITUDE} "
                f"(got {self.south}, {self.north})"
            )
        return self

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]

    def as_text(self, digits: int = 6) -> str:
        return ",".join(f"{v:.{digits}f}" for v in self.as_list())


class Grid(BaseModel):
    """The north-up EPSG:3857 pixel grid of one canvas — the georeferencing authority.

    ``pixel_height`` is negative (north-up). ``ulx``/``uly`` are the outer edge
    of the top-left pixel, matching GDAL's geotransform convention.
    """

    crs: str = CRS
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    ulx: float
    uly: float
    pixel_width: float = Field(gt=0)
    pixel_height: float = Field(lt=0)

    @field_validator("crs")
    @classmethod
    def _crs_is_mercator(cls, value: str) -> str:
        if value.upper() != CRS:
            raise ValueError(f"grid CRS must be {CRS}, got {value!r}")
        return CRS

    @property
    def resolution(self) -> float:
        """Ground resolution in metres per pixel (pixels are square)."""
        return self.pixel_width

    @property
    def lrx(self) -> float:
        return self.ulx + self.width * self.pixel_width

    @property
    def lry(self) -> float:
        return self.uly + self.height * self.pixel_height

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(xmin, ymin, xmax, ymax)`` in EPSG:3857 metres."""
        return (self.ulx, self.lry, self.lrx, self.uly)

    @property
    def geotransform(self) -> tuple[float, float, float, float, float, float]:
        return (self.ulx, self.pixel_width, 0.0, self.uly, 0.0, self.pixel_height)

    @property
    def long_side(self) -> int:
        return max(self.width, self.height)

    def corners_match(self, ulx: float, uly: float, lrx: float, lry: float, tol: float = ALIGN_TOLERANCE) -> bool:
        return (
            math.isclose(ulx, self.ulx, abs_tol=tol)
            and math.isclose(uly, self.uly, abs_tol=tol)
            and math.isclose(lrx, self.lrx, abs_tol=tol)
            and math.isclose(lry, self.lry, abs_tol=tol)
        )


class FileRef(BaseModel):
    """A file pinned by content hash. ``path`` is relative to the job directory
    for job artifacts, absolute for external sources (the DEM, a land polygon)."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GdalCommand(BaseModel):
    """One GDAL invocation, recorded verbatim so a reader can replay the controls."""

    tool: str
    argv: list[str]


class ModelRef(BaseModel):
    """A checkpoint or LoRA, named as the engine loads it and pinned by SHA256."""

    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BakeParams(BaseModel):
    """The generation knobs of one bake (what the operator chose)."""

    checkpoint: str
    lora: str | None = None
    lora_weight: float = DEFAULT_LORA_WEIGHT
    prompt: str = DEFAULT_PROMPT
    negative: str = DEFAULT_NEGATIVE
    seed: int = Field(ge=0)
    steps: int = Field(default=DEFAULT_STEPS, gt=0)
    cfg: float = Field(default=DEFAULT_CFG, gt=0)
    denoise: float = Field(default=DEFAULT_DENOISE, gt=0, le=1)
    sampler: str = DEFAULT_SAMPLER
    scheduler: str = DEFAULT_SCHEDULER


class BakeRecord(BaseModel):
    """What the bake produced. ``aligned`` is the alignment-gate verdict; a
    failed gate keeps ``image`` (the PNG) and leaves ``geotiff`` unset."""

    params: BakeParams
    backend: str
    engine_version: str
    checkpoint: ModelRef
    lora: ModelRef | None = None
    workflow_sha256: str | None = None
    image: FileRef
    geotiff: FileRef | None = None
    aligned: bool
    error: str | None = None
    completed_at: str


class TilesRecord(BaseModel):
    """The PMTiles archive cut from the aligned GeoTIFF."""

    archive: FileRef
    min_zoom: int = Field(ge=0)
    max_zoom: int = Field(ge=0)
    native_zoom: int = Field(ge=0)
    n_tiles: int = Field(ge=0)
    tiler: str
    packer: str
    completed_at: str

    @model_validator(mode="after")
    def _zooms_ordered(self) -> TilesRecord:
        if self.min_zoom > self.max_zoom:
            raise ValueError("min_zoom must be <= max_zoom")
        if self.max_zoom > self.native_zoom:
            raise ValueError("max_zoom must not exceed the canvas's native zoom")
        return self


JobStatus = Literal["controls", "baked", "unaligned", "tiled"]


class JobSidecar(BaseModel):
    """``job.json`` — everything about one canvas, from DEM to archive."""

    atlas_version: str = ATLAS_VERSION
    job_id: str
    created_at: str
    dem: FileRef
    land_source: FileRef | None = None
    bbox: BBox
    grid: Grid
    resolution: float = Field(gt=0)
    native_zoom: int = Field(ge=0)
    zoom_range: tuple[int, int]
    controls: dict[str, FileRef]
    commands: list[GdalCommand] = Field(default_factory=list)
    bake: BakeRecord | None = None
    tiles: TilesRecord | None = None

    @property
    def status(self) -> JobStatus:
        if self.tiles is not None:
            return "tiled"
        if self.bake is not None:
            return "baked" if self.bake.aligned else "unaligned"
        return "controls"

    @property
    def has_land(self) -> bool:
        return "land" in self.controls


class JobDigest(BaseModel):
    """One row of ``GET /jobs`` — enough for a job browser, no paths."""

    job_id: str
    created_at: str
    status: JobStatus
    bbox: BBox
    width: int
    height: int
    resolution: float
    native_zoom: int
    has_land: bool
    has_archive: bool


ProgressType = Literal["controls", "generate", "georeference", "done", "error"]


class ProgressEvent(BaseModel):
    """One NDJSON line of ``POST /bake/stream``, in order: ``controls``,
    ``generate``, ``georeference``, then ``done`` or ``error``."""

    job_id: str
    type: ProgressType
    message: str
