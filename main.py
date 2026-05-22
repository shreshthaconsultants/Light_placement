"""
Light Placement Backend (FastAPI)
---------------------------------

Endpoints
~~~~~~~~~
GET  /              health probe
GET  /health        detailed status + config snapshot
GET  /room-types    supported room types with default lux/UF/MF
POST /calculate     lumen-method count for a polygon in METERS (backwards compat)
POST /layout        grid placement for a polygon in MILLIMETERS (backwards compat)
POST /validate      point-by-point illuminance check for a placement
POST /design        UNIFIED: calculate + layout (with target count) + validate
                    + ZWCAD block insertion data, in one round trip

`/design` is the recommended path for new clients. The two-call workflow
(`/calculate` → `/layout`) is kept intact so existing integrations don't break.
See MIGRATION.md for upgrade guidance.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
import math

from config import (
    DEFAULT_FIXTURE_LUMENS,
    ROOM_DEFAULTS,
    SERVER_HOST, SERVER_PORT,
    API_VERSION,
    DEFAULT_MOUNTING_HEIGHT_M, DEFAULT_WORKING_PLANE_M,
    DEFAULT_VALIDATION_SPACING_MM,
    DEFAULT_FIXTURE_TYPE, DEFAULT_FIXTURE_LAYER,
    FIXTURE_BLOCKS,
    MIN_OFFSET_MM, MAX_OFFSET_MM, TARGET_OFFSET_MM,
    MIN_SPACING_MM, MAX_SPACING_MM, TARGET_SPACING_MM,
    MIN_WALL_CLEARANCE_MM, MIN_OBSTACLE_CLEARANCE_MM,
    UNIFORMITY_WARNING_THRESHOLD,
    CONCAVE_MAX_PIECES,
    SQUARE_ROOM_ASPECT_THRESHOLD,
)

from placement import (
    router as layout_router,
    LayoutRequest, LayoutResponse, LayoutPoint, Obstacle,
    GridMeta, BBox,
    compute_layout,
    polygon_area_abs,
)
from photometric import (
    PhotometricResult, validate_placement,
)


# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("light-placement")


# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(title="Light Placement API", version=API_VERSION)
app.include_router(layout_router)


# -------------------------------------------------------------------
# Backwards-compatible /calculate models (METERS in)
# -------------------------------------------------------------------
class Point(BaseModel):
    x: float
    y: float


class LightRequest(BaseModel):
    room_type: str = Field(..., description="e.g. bedroom, office, kitchen")
    polyline: List[Point] = Field(..., min_length=3,
                                  description="Closed polyline vertices in meters")
    fixture_lumens: float = Field(default=DEFAULT_FIXTURE_LUMENS, gt=0)


class LightResponse(BaseModel):
    room_type: str
    area_m2: float
    required_lux: int
    required_lumens: float
    fixture_lumens: float
    number_of_lights: int
    message: str


# -------------------------------------------------------------------
# New /design and /validate models
# -------------------------------------------------------------------
class BlockData(BaseModel):
    """ZWCAD-ready insertion data. The plugin uses these fields to either
    insert a BlockReference of `block_name` or fall back to a circle on
    `layer_name`. `fixture_attributes` is also attached as XData."""
    block_name: str
    layer_name: str
    rotation_deg: float
    scale: float
    fixture_attributes: Dict[str, Any]


class DesignRequest(BaseModel):
    room_type: str
    polyline: List[LayoutPoint] = Field(..., min_length=3,
                                        description="Polyline vertices in mm.")
    fixture_lumens: float = Field(default=DEFAULT_FIXTURE_LUMENS, gt=0)

    # Photometric inputs.
    mounting_height_m: float = Field(default=DEFAULT_MOUNTING_HEIGHT_M, gt=0)
    working_plane_m:   float = Field(default=DEFAULT_WORKING_PLANE_M,   ge=0)
    validation_sample_spacing_mm: float = Field(
        default=DEFAULT_VALIDATION_SPACING_MM, gt=0)

    # Block / fixture identity.
    fixture_type: str = Field(default=DEFAULT_FIXTURE_TYPE,
                              description="Key into config.FIXTURE_BLOCKS.")

    # Optional grid overrides (mirror LayoutRequest).
    min_offset_mm:     Optional[float] = None
    max_offset_mm:     Optional[float] = None
    min_spacing_mm:    Optional[float] = None
    max_spacing_mm:    Optional[float] = None
    target_spacing_mm: Optional[float] = None
    target_offset_mm:  Optional[float] = None

    # Optional obstacles (mm).
    obstacles: Optional[List[Obstacle]] = None

    # When True, derate the lumen-method formula by the room's UF and MF
    # defaults — produces a more realistic (higher) fixture count. Default
    # False to preserve the original simple count.
    use_uf_mf: bool = Field(
        default=False,
        description="Apply utilization × maintenance factors from "
                    "config.ROOM_DEFAULTS when computing the target count."
    )


class DesignResponse(BaseModel):
    # Lumen method ------------------------------------------------
    room_type: str
    area_m2: float
    required_lux: int
    required_lumens: float
    fixture_lumens: float
    target_count: int              # what the lumen method asked for
    # Populated only when the request set use_uf_mf=True. Null otherwise so
    # legacy callers see the same response shape.
    uf: Optional[float] = None
    mf: Optional[float] = None
    effective_flux_per_fixture: Optional[float] = None
    # Placement ---------------------------------------------------
    number_of_lights: int          # what the optimizer actually placed
    lights_mm: List[LayoutPoint]
    grid: GridMeta
    bbox_mm: BBox
    notes: List[str]
    n_dropped_by_wall_clearance: int
    n_dropped_by_obstacles: int
    # Photometric -------------------------------------------------
    photometric: PhotometricResult
    # ZWCAD ------------------------------------------------------
    block_data: BlockData
    # Human-readable -----------------------------------------------
    message: str


class ValidateRequest(BaseModel):
    room_type: str
    polyline: List[LayoutPoint] = Field(..., min_length=3,
                                        description="Polyline vertices in mm.")
    lights:   List[LayoutPoint] = Field(..., min_length=1,
                                        description="Light positions in mm.")
    fixture_lumens: float = Field(default=DEFAULT_FIXTURE_LUMENS, gt=0)
    mounting_height_m: float = Field(default=DEFAULT_MOUNTING_HEIGHT_M, gt=0)
    working_plane_m:   float = Field(default=DEFAULT_WORKING_PLANE_M,   ge=0)
    sample_spacing_mm: float = Field(default=DEFAULT_VALIDATION_SPACING_MM, gt=0)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _normalize_room_type(raw: str) -> str:
    rt = raw.strip().lower().replace(" ", "_")
    if rt not in ROOM_DEFAULTS:
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown room type '{raw}'. "
                    f"Supported: {list(ROOM_DEFAULTS.keys())}")
        )
    return rt


def _polygon_area_m2_from_mm(polyline_mm: List[LayoutPoint]) -> float:
    """Polygon area in m² from a polyline in mm.

    Shoelace on mm coordinates gives mm² → multiply by 1e-6 to get m².
    """
    pts_mm = [(p.x, p.y) for p in polyline_mm]
    return polygon_area_abs(pts_mm) * 1e-6


def _polyline_bbox_dims_mm(polyline_mm: List[LayoutPoint]) -> tuple:
    """(width, height) of the polyline's axis-aligned bounding box, mm."""
    xs = [p.x for p in polyline_mm]
    ys = [p.y for p in polyline_mm]
    return (max(xs) - min(xs), max(ys) - min(ys))


def _is_prime(n: int) -> bool:
    """True for n ≥ 2 with no non-trivial divisors. Used by /design to detect
    target counts that can only form a 1×N grid."""
    if n < 2:
        return False
    if n < 4:
        return True            # 2 and 3 are prime
    if n % 2 == 0:
        return False
    d = 3
    while d * d <= n:
        if n % d == 0:
            return False
        d += 2
    return True


def _lumen_method(area_m2: float, room_defaults: dict, fixture_lumens: float,
                  use_uf_mf: bool = False) -> Dict[str, Any]:
    """Lumen-method fixture count.

    Without derating (default, current behaviour):
        N = ceil(area * required_lux / fixture_lumens)

    With derating (`use_uf_mf=True`):
        N = ceil(area * required_lux / (fixture_lumens * UF * MF))

    UF (utilization factor) and MF (maintenance factor) are taken from the
    room type's defaults in `config.ROOM_DEFAULTS`. Since UF × MF < 1, the
    derated formula always produces ≥ as many fixtures — typically 2-3× more,
    which is the realistic count once optical efficiency and dirt/aging are
    accounted for.
    """
    required_lux = room_defaults["lux"]
    required_lumens = required_lux * area_m2

    uf = mf = None
    effective_flux = fixture_lumens
    if use_uf_mf:
        uf = room_defaults["uf"]
        mf = room_defaults["mf"]
        effective_flux = fixture_lumens * uf * mf

    n_lights = max(1, math.ceil(required_lumens / effective_flux))
    return {
        "required_lux": required_lux,
        "required_lumens": required_lumens,
        "number_of_lights": n_lights,
        "uf": uf,
        "mf": mf,
        "effective_flux_per_fixture": effective_flux if use_uf_mf else None,
    }


def _resolve_block_data(fixture_type: str,
                        fixture_lumens: float,
                        mounting_height_m: float) -> BlockData:
    """Look up FIXTURE_BLOCKS entry, fall back to a generic record on miss."""
    entry = FIXTURE_BLOCKS.get(fixture_type)
    if entry is None:
        log.warning("Unknown fixture_type %r — using generic fallback.", fixture_type)
        entry = {
            "block_name":   f"LIGHT_FIXTURE_{fixture_type.upper()}",
            "layer_name":   DEFAULT_FIXTURE_LAYER,
            "rotation_deg": 0.0,
            "scale":        1.0,
            "wattage":      0.0,
        }
    return BlockData(
        block_name=entry["block_name"],
        layer_name=entry["layer_name"],
        rotation_deg=entry["rotation_deg"],
        scale=entry["scale"],
        fixture_attributes={
            "lumens":            fixture_lumens,
            "wattage":           entry["wattage"],
            "mounting_height_m": mounting_height_m,
            "fixture_type":      fixture_type,
        },
    )


def _design_request_to_layout_request(
    req: DesignRequest, target_count: int,
) -> LayoutRequest:
    """Build the LayoutRequest passed to compute_layout().

    /design always runs the optimizer in **exact-target mode** — the placed
    count matches the lumen-method count fixture-for-fixture, even if that
    means spacing falls outside the configured window or (for prime targets)
    the layout collapses to a single line. The diagnostic notes in the
    response explain any such trade-offs.
    """
    kwargs = dict(
        polyline=req.polyline,
        target_count=target_count,
        obstacles=req.obstacles,
        enforce_exact_target=True,
    )
    if req.min_offset_mm     is not None: kwargs["min_offset_mm"]     = req.min_offset_mm
    if req.max_offset_mm     is not None: kwargs["max_offset_mm"]     = req.max_offset_mm
    if req.min_spacing_mm    is not None: kwargs["min_spacing_mm"]    = req.min_spacing_mm
    if req.max_spacing_mm    is not None: kwargs["max_spacing_mm"]    = req.max_spacing_mm
    if req.target_spacing_mm is not None: kwargs["target_spacing_mm"] = req.target_spacing_mm
    if req.target_offset_mm  is not None: kwargs["target_offset_mm"]  = req.target_offset_mm
    return LayoutRequest(**kwargs)


# -------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "light-placement-api",
            "version": API_VERSION}


@app.get("/health")
def health():
    """Diagnostic snapshot: server status, version, and the key config values
    a caller might want to inspect (offsets, spacing, clearances)."""
    return {
        "status": "ok",
        "service": "light-placement-api",
        "version": API_VERSION,
        "config": {
            "default_fixture_lumens":   DEFAULT_FIXTURE_LUMENS,
            "default_mounting_height_m": DEFAULT_MOUNTING_HEIGHT_M,
            "default_working_plane_m":   DEFAULT_WORKING_PLANE_M,
            "default_validation_spacing_mm": DEFAULT_VALIDATION_SPACING_MM,
            "min_offset_mm":   MIN_OFFSET_MM,
            "max_offset_mm":   MAX_OFFSET_MM,
            "target_offset_mm": TARGET_OFFSET_MM,
            "min_spacing_mm":   MIN_SPACING_MM,
            "max_spacing_mm":   MAX_SPACING_MM,
            "target_spacing_mm": TARGET_SPACING_MM,
            "min_wall_clearance_mm":     MIN_WALL_CLEARANCE_MM,
            "min_obstacle_clearance_mm": MIN_OBSTACLE_CLEARANCE_MM,
            "uniformity_warning_threshold": UNIFORMITY_WARNING_THRESHOLD,
            "concave_max_pieces": CONCAVE_MAX_PIECES,
            "supported_room_types": list(ROOM_DEFAULTS.keys()),
            "supported_fixture_types": list(FIXTURE_BLOCKS.keys()),
        },
    }


@app.get("/room-types")
def room_types():
    """List supported room types with their lux / UF / MF defaults."""
    return ROOM_DEFAULTS


@app.post("/calculate", response_model=LightResponse)
def calculate_lights(req: LightRequest):
    """Lumen-method count for a polygon in METERS.

    Kept for backwards compatibility. New clients should use POST /design.
    """
    rt = _normalize_room_type(req.room_type)

    pts = [(p.x, p.y) for p in req.polyline]
    area = polygon_area_abs(pts)
    if area <= 0:
        raise HTTPException(status_code=400,
                            detail="Polyline area is zero. Check vertices.")

    res = _lumen_method(area, ROOM_DEFAULTS[rt], req.fixture_lumens)

    return LightResponse(
        room_type=rt,
        area_m2=round(area, 3),
        required_lux=res["required_lux"],
        required_lumens=round(res["required_lumens"], 2),
        fixture_lumens=req.fixture_lumens,
        number_of_lights=res["number_of_lights"],
        message=(f"Place {res['number_of_lights']} light(s) of "
                 f"{int(req.fixture_lumens)} lm in {rt} "
                 f"({round(area, 2)} m²) to achieve {res['required_lux']} lux.")
    )


@app.post("/validate", response_model=PhotometricResult)
def validate(req: ValidateRequest):
    """Standalone photometric validation for an already-placed set of lights."""
    rt = _normalize_room_type(req.room_type)
    rd = ROOM_DEFAULTS[rt]
    log.info("validate: rt=%s n_lights=%d", rt, len(req.lights))
    return validate_placement(
        polyline_mm=req.polyline,
        lights_mm=req.lights,
        fixture_lumens=req.fixture_lumens,
        required_lux=rd["lux"],
        mounting_height_m=req.mounting_height_m,
        working_plane_m=req.working_plane_m,
        sample_spacing_mm=req.sample_spacing_mm,
    )


@app.post("/design", response_model=DesignResponse)
def design(req: DesignRequest):
    """Unified design endpoint.

    Orchestrates:
      1. Lumen-method  → target_count for the lighting design.
      2. Grid placement (with target_count + obstacles + concave handling).
      3. Photometric validation of the final placement.
      4. Block-insertion metadata for the ZWCAD plugin.

    All distances in the request and response are MILLIMETERS unless
    explicitly suffixed `_m` (meters) or `_m2` (square meters).
    """
    rt = _normalize_room_type(req.room_type)
    rd = ROOM_DEFAULTS[rt]

    # ----- 1. Lumen method -----
    area_m2 = _polygon_area_m2_from_mm(req.polyline)
    if area_m2 <= 0:
        raise HTTPException(status_code=400,
                            detail="Polyline area is zero. Check vertices.")
    calc = _lumen_method(area_m2, rd, req.fixture_lumens, req.use_uf_mf)
    target_count = calc["number_of_lights"]
    log.info("design: rt=%s area=%.2f m² target_count=%d use_uf_mf=%s",
             rt, area_m2, target_count, req.use_uf_mf)

    # ----- 1b. Square-room anti-line bump -----
    # In a square-ish room, a prime target (3, 5, 7, …) only factors as 1×N
    # so the optimizer is forced into a single line of fixtures. Bumping by
    # 1 makes the count composite and lets a 2×N rectangular grid form.
    # Corridors (aspect below SQUARE_ROOM_ASPECT_THRESHOLD) stay as a line,
    # which is the right answer for them.
    W_mm, H_mm = _polyline_bbox_dims_mm(req.polyline)
    short_side = min(W_mm, H_mm)
    long_side  = max(W_mm, H_mm)
    square_ish = long_side > 0 and (short_side / long_side) >= SQUARE_ROOM_ASPECT_THRESHOLD
    layout_target  = target_count
    square_bump_note = None
    if square_ish and target_count > 2 and _is_prime(target_count):
        layout_target = target_count + 1
        rows = layout_target // 2
        square_bump_note = (
            f"Square-room layout: lumen-method asked for {target_count} fixture(s); "
            f"placing {layout_target} so the grid forms a 2×{rows} rectangle "
            f"instead of a 1×{target_count} line."
        )
        log.info("design: square room, prime target %d → bumped to %d for 2×%d layout",
                 target_count, layout_target, rows)

    # ----- 2. Grid placement (with target_count) -----
    layout_req = _design_request_to_layout_request(req, layout_target)
    layout_resp: LayoutResponse = compute_layout(layout_req)
    if square_bump_note:
        layout_resp.notes = [square_bump_note] + layout_resp.notes

    # ----- 3. Photometric validation -----
    photo = validate_placement(
        polyline_mm=req.polyline,
        lights_mm=layout_resp.lights_mm,
        fixture_lumens=req.fixture_lumens,
        required_lux=rd["lux"],
        mounting_height_m=req.mounting_height_m,
        working_plane_m=req.working_plane_m,
        sample_spacing_mm=req.validation_sample_spacing_mm,
    )

    # ----- 4. Block / fixture metadata -----
    block_data = _resolve_block_data(
        req.fixture_type, req.fixture_lumens, req.mounting_height_m,
    )

    # ----- 5. Human-readable message -----
    placed = layout_resp.count
    if placed == target_count:
        recon = f"matches the {target_count} required by the lumen method."
    elif placed > target_count:
        if square_bump_note:
            recon = (f"is {placed - target_count} more than the lumen-method "
                     f"target of {target_count} (bumped to break the prime "
                     f"factorization for a rectangular layout).")
        else:
            recon = (f"is {placed - target_count} more than the lumen-method "
                     f"target of {target_count} (grid snapped up to satisfy "
                     f"spacing constraints).")
    else:
        recon = (f"is {target_count - placed} fewer than the lumen-method "
                 f"target of {target_count}; consider a brighter fixture or "
                 f"loosening spacing constraints.")

    uf_mf_clause = ""
    if req.use_uf_mf and calc["uf"] is not None and calc["mf"] is not None:
        uf_mf_clause = (
            f" UF×MF = {calc['uf']:.2f}×{calc['mf']:.2f} "
            f"= {calc['uf'] * calc['mf']:.2f} applied."
        )

    message = (
        f"Placed {placed} × {int(req.fixture_lumens)} lm fixture(s) in {rt} "
        f"({round(area_m2, 2)} m²). Count {recon}{uf_mf_clause} "
        f"Average illuminance {photo.avg_lux:.0f} lux "
        f"(target {photo.required_lux}), uniformity "
        f"{photo.uniformity_ratio:.2f}."
    )

    return DesignResponse(
        room_type=rt,
        area_m2=round(area_m2, 3),
        required_lux=calc["required_lux"],
        required_lumens=round(calc["required_lumens"], 2),
        fixture_lumens=req.fixture_lumens,
        target_count=target_count,
        uf=calc["uf"],
        mf=calc["mf"],
        effective_flux_per_fixture=(
            round(calc["effective_flux_per_fixture"], 2)
            if calc["effective_flux_per_fixture"] is not None else None
        ),
        number_of_lights=placed,
        lights_mm=layout_resp.lights_mm,
        grid=layout_resp.grid,
        bbox_mm=layout_resp.bbox_mm,
        notes=layout_resp.notes,
        n_dropped_by_wall_clearance=layout_resp.n_dropped_by_wall_clearance,
        n_dropped_by_obstacles=layout_resp.n_dropped_by_obstacles,
        photometric=photo,
        block_data=block_data,
        message=message,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
