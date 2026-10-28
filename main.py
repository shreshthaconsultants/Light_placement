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
    EVEN_COUNT_ONLY, EVEN_RULE_EXEMPT_ROOM_TYPES, EVEN_RULE_MIN_AREA_M2,
    GRID_CANDIDATE_EXTRA_STEPS, GRID_MAX_CANDIDATES,
)

from placement import (
    router as layout_router,
    LayoutRequest, LayoutResponse, LayoutPoint, Obstacle,
    GridMeta, BBox,
    compute_layout,
    polygon_area_abs,
    is_convex,
    _all_factor_pairs,
)
import jev_client
import fan_placement
from config import (FAN_BLOCK_BASE_SWEEP_MM, MAIN_BODY_MIN_FRACTION,
                    REST_MIN_AREA_M2, REST_MIN_SIDE_MM, PART_MIN_UNIFORMITY,
                    PART_LUX_TOLERANCE)
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
    # When True the response also carries `fans_mm` (ceiling-fan positions)
    # and `fan_info` — see fan_placement.py.
    place_fans: bool = Field(default=False,
                             description="Also decide ceiling-fan count/positions.")

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
    # Grid selection (even-count rule + Jev decision) ---------------
    # Ceiling fans (only when the request set place_fans=true) --------
    fans_mm: List[LayoutPoint] = []
    fan_info: Optional[Dict[str, Any]] = None  # {count, sweep_mm, scale}
    even_target_count: Optional[int] = None   # target after rounding up to even
    grid_selector: Optional[Dict[str, Any]] = None  # who picked the grid + why
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


def _round_up_to_even(n: int) -> int:
    """2, 4, 6, ... — odd counts are bumped up by one."""
    return n if n % 2 == 0 else n + 1


def _build_grid_candidates(req: "DesignRequest", rd: dict,
                           x_min: float, y_min: float,
                           W_mm: float, H_mm: float, even_target: int,
                           allow_lines: bool) -> List[Dict[str, Any]]:
    """All (cols, rows) grids whose total is an EVEN number in
    [even_target, even_target + 2*GRID_CANDIDATE_EXTRA_STEPS] and that fit
    the room.

    Layout is CELL-CENTRED: the room is cut into cols x rows equal cells with
    one light in the middle of each, so the wall gap is half the spacing.
    Every candidate is photometrically simulated, so Jev (and the rule-based
    fallback) can see the real avg lux / uniformity of each option.

    cols run along X, rows along Y. Single-line grids (1xN) are only offered
    for corridor-shaped rooms (`allow_lines`) or when the total is 2.
    Sorted best-first by the rule-based score, so index 0 is also the
    fallback pick when Jev is unavailable.
    """
    MIN_SANE_SPACING = 800.0
    long_x = W_mm >= H_mm
    room_aspect = (max(W_mm, H_mm) / min(W_mm, H_mm)) if min(W_mm, H_mm) > 0 else 1e9
    out: List[Dict[str, Any]] = []
    for step in range(GRID_CANDIDATE_EXTRA_STEPS + 1):
        total = even_target + 2 * step
        for cols, rows in _all_factor_pairs(total):
            if (cols == 1 or rows == 1) and not (allow_lines or total == 2):
                continue
            sx, sy = W_mm / cols, H_mm / rows        # cell size = light spacing
            gap_x, gap_y = sx / 2, sy / 2            # wall gap = half spacing
            if (cols > 1 and sx < MIN_SANE_SPACING) or (rows > 1 and sy < MIN_SANE_SPACING):
                continue
            if min(gap_x, gap_y) < MIN_WALL_CLEARANCE_MM:
                continue
            lights = [LayoutPoint(x=x_min + (i + 0.5) * sx, y=y_min + (j + 0.5) * sy)
                      for i in range(cols) for j in range(rows)]
            photo = validate_placement(
                polyline_mm=req.polyline, lights_mm=lights,
                fixture_lumens=req.fixture_lumens, required_lux=rd["lux"],
                mounting_height_m=req.mounting_height_m,
                working_plane_m=req.working_plane_m,
                sample_spacing_mm=max(req.validation_sample_spacing_mm, 500.0),
            )
            orient_pen = 0 if ((cols >= rows) == long_x or cols == rows) else 1
            layout_aspect = max(cols, rows) / min(cols, rows)
            spacing_gap = abs(sx - sy) if (cols > 1 and rows > 1) else 0.0
            # Rule score: meet target, then uniform, then fewest lights, then shape.
            score = (0 if photo.meets_target else 1,
                     0 if not photo.uniformity_warning else 1,
                     total, orient_pen, abs(layout_aspect - room_aspect), spacing_gap)
            out.append({
                "id": f"{cols}x{rows}", "cols": cols, "rows": rows,
                "total": total,
                "spacing_x_mm": round(sx, 1), "spacing_y_mm": round(sy, 1),
                "wall_gap_x_mm": round(gap_x, 1), "wall_gap_y_mm": round(gap_y, 1),
                "avg_lux": photo.avg_lux, "min_lux": photo.min_lux,
                "uniformity": photo.uniformity_ratio,
                "meets_target_lux": photo.meets_target,
                "uniformity_ok": not photo.uniformity_warning,
                "_score": score,
            })
    out.sort(key=lambda c: c["_score"])
    return out[:GRID_MAX_CANDIDATES]


def _rect_grid_lights(rect: tuple, n: int) -> List[LayoutPoint]:
    """n lights at the centres of equal cells of a rectangle (x0, y0, x1, y1);
    the cols x rows split with the most square cells is used."""
    x0, y0, x1, y1 = rect
    W, H = x1 - x0, y1 - y0
    cols, rows = min(_all_factor_pairs(n),
                     key=lambda cr: (abs(W / cr[0] - H / cr[1]), cr[0] * cr[1]))
    sx, sy = W / cols, H / rows
    return [LayoutPoint(x=x0 + (i + 0.5) * sx, y=y0 + (j + 0.5) * sy)
            for i in range(cols) for j in range(rows)]


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
    # ----- 1-even. Is the even-count rule in force for this room? -----
    # Small service rooms (bathroom, toilet, store...) and tiny rooms keep the plain
    # lumen-method count, which is often a single light.
    even_rule = bool(EVEN_COUNT_ONLY and rt not in EVEN_RULE_EXEMPT_ROOM_TYPES
                     and area_m2 >= EVEN_RULE_MIN_AREA_M2)

    # ----- 1a. Main body + leftover parts of an irregular room -----
    # The largest rectangle is laid out like a plain room (even grid, Jev decision,
    # centred fan). Leftover rectangles (e.g. a leg beside a washroom) get lights
    # from their own area, and one light is added there if the total would be odd.
    light_req = req
    body_note = None
    rest_rects: List[tuple] = []
    body_target = None
    pts_all = [(p.x, p.y) for p in req.polyline]
    if not is_convex(pts_all):
        from placement_concave import split_body_and_rest_any
        sp = split_body_and_rest_any(pts_all)
        if sp:
            (bx0, by0, bx1, by1), rests, split_method = sp
            body_area = (bx1 - bx0) * (by1 - by0) * 1e-6
            if body_area >= MAIN_BODY_MIN_FRACTION * area_m2:
                light_req = req.model_copy(update={"polyline": [
                    LayoutPoint(x=bx0, y=by0), LayoutPoint(x=bx1, y=by0),
                    LayoutPoint(x=bx1, y=by1), LayoutPoint(x=bx0, y=by1)]})
                rest_rects = [r for r in rests
                              if (r[2] - r[0]) * (r[3] - r[1]) * 1e-6 >= REST_MIN_AREA_M2
                              and min(r[2] - r[0], r[3] - r[1]) >= REST_MIN_SIDE_MM]
                log.info("design: irregular room (%s split) body=%.0fx%.0f mm, rests=%s, "
                         "%d vertices, area=%.2f m2",
                         split_method, bx1 - bx0, by1 - by0,
                         [(round(r[2] - r[0]), round(r[3] - r[1])) for r in rest_rects],
                         len(pts_all), area_m2)
                body_target = _lumen_method(body_area, rd, req.fixture_lumens,
                                            req.use_uf_mf)["number_of_lights"]
                body_note = (f"Irregular room: main body {(bx1 - bx0) / 1000:.2f} x "
                             f"{(by1 - by0) / 1000:.2f} m laid out as a plain room"
                             + (f", plus {len(rest_rects)} extra part(s) lit separately."
                                if rest_rects else "."))
    if light_req is req and not is_convex(pts_all):
        log.info("design: irregular room NOT split (no usable main body); %d vertices, "
                 "area=%.2f m2, first vertices=%s", len(pts_all), area_m2,
                 [(round(x), round(y)) for x, y in pts_all[:8]])
    W_mm, H_mm = _polyline_bbox_dims_mm(light_req.polyline)
    short_side = min(W_mm, H_mm)
    long_side  = max(W_mm, H_mm)
    square_ish = long_side > 0 and (short_side / long_side) >= SQUARE_ROOM_ASPECT_THRESHOLD
    layout_target  = body_target if body_target is not None else target_count
    square_bump_note = None
    if even_rule and square_ish and layout_target > 2 and _is_prime(layout_target):
        layout_target = layout_target + 1
        rows = layout_target // 2
        square_bump_note = (
            f"Square-room layout: lumen-method asked for {target_count} fixture(s); "
            f"placing {layout_target} so the grid forms a 2×{rows} rectangle "
            f"instead of a 1×{target_count} line."
        )
        log.info("design: square room, prime target %d → bumped to %d for 2×%d layout",
                 target_count, layout_target, rows)

    # ----- 1c. Even-count rule -----
    # Lights always come in pairs (2, 4, 6, ...) so the grid is symmetric.
    even_target = _round_up_to_even(layout_target) if even_rule else layout_target
    layout_target = even_target

    # ----- 1d. Grid decision (rules build candidates, Jev picks) -----
    forced_grid = None
    selector: Dict[str, Any] = {"decided_by": "rules", "candidates": 0}
    convex = is_convex([(p.x, p.y) for p in light_req.polyline])
    if even_rule and convex:
        cands = _build_grid_candidates(
            light_req, rd, min(p.x for p in light_req.polyline),
            min(p.y for p in light_req.polyline),
            W_mm, H_mm, even_target,
            allow_lines=(short_side / long_side < SQUARE_ROOM_ASPECT_THRESHOLD)
            if long_side > 0 else False,
        )
        selector["candidates"] = len(cands)
        if cands:
            pick = cands[0]                         # rule-based default
            jev_pick = jev_client.choose_grid(
                room={
                    "room_type": rt, "area_m2": round(area_m2, 2),
                    "width_mm": round(W_mm), "height_mm": round(H_mm),
                    "required_lux": rd["lux"],
                    "fixture_lumens": req.fixture_lumens,
                    "target_count": even_target,
                    "mounting_height_m": req.mounting_height_m,
                    "working_plane_m": req.working_plane_m,
                    "room_shape": ("main body of an irregular room; the extra parts "
                                   "listed in extra_parts are lit separately"
                                   if light_req is not req else "plain rectangle"),
                    "extra_parts": [{"width_mm": round(r[2] - r[0]),
                                     "height_mm": round(r[3] - r[1]),
                                     "area_m2": round((r[2] - r[0]) * (r[3] - r[1]) * 1e-6, 2)}
                                    for r in rest_rects],
                    "whole_room_area_m2": round(area_m2, 2),
                    # Above ~1.5 x (mounting - working plane) lights get patchy.
                    "max_good_spacing_mm": round(1500 * (req.mounting_height_m
                                                         - req.working_plane_m)),
                },
                candidates=[{k: v for k, v in c.items() if k != "_score"}
                            for c in cands],
            )
            if jev_pick:
                match = next((c for c in cands if c["id"] == jev_pick["id"]), None)
                if match:                           # validated: even + fits room
                    pick = match
                    selector = {"decided_by": "jev", "model": jev_pick["model"],
                                "confidence": jev_pick["confidence"],
                                "probabilities": jev_pick["probabilities"],
                                "candidates": len(cands)}
            forced_grid = (pick["cols"], pick["rows"])
            selector["chosen"] = pick["id"]
            layout_target = pick["total"]

    # ----- 2. Grid placement (with target_count) -----
    layout_req = _design_request_to_layout_request(light_req, layout_target)
    if forced_grid:
        layout_req.forced_grid = forced_grid
    layout_resp: LayoutResponse = compute_layout(layout_req)
    part_notes: List[str] = []
    if rest_rects:
        extra: List[LayoutPoint] = []
        n_body = layout_resp.count
        for r in rest_rects:
            pw, ph_ = r[2] - r[0], r[3] - r[1]
            n0 = max(1, _lumen_method(pw * ph_ * 1e-6, rd, req.fixture_lumens,
                                      req.use_uf_mf)["number_of_lights"])
            poly_r = [LayoutPoint(x=r[0], y=r[1]), LayoutPoint(x=r[2], y=r[1]),
                      LayoutPoint(x=r[2], y=r[3]), LayoutPoint(x=r[0], y=r[3])]
            pcands = []
            for n in range(1, min(n0 + 1, 4) + 1):
                lts = _rect_grid_lights(r, n)
                pp = validate_placement(
                    polyline_mm=poly_r,
                    # light spilling in from the rest of the room counts too
                    lights_mm=list(layout_resp.lights_mm) + extra + lts,
                    fixture_lumens=req.fixture_lumens, required_lux=rd["lux"],
                    mounting_height_m=req.mounting_height_m,
                    working_plane_m=req.working_plane_m,
                    sample_spacing_mm=min(max(req.validation_sample_spacing_mm, 250.0),
                                          500.0),
                )
                cols_ = len({round(p.x) for p in lts})
                rows_ = len({round(p.y) for p in lts})
                pcands.append({
                    "id": str(n), "count": n,
                    "spacing_x_mm": round(pw / cols_, 1), "spacing_y_mm": round(ph_ / rows_, 1),
                    "avg_lux": pp.avg_lux, "min_lux": pp.min_lux,
                    "uniformity": pp.uniformity_ratio,
                    "meets_target_lux": pp.avg_lux >= PART_LUX_TOLERANCE * rd["lux"],
                    "uniformity_ok": pp.uniformity_ratio >= PART_MIN_UNIFORMITY,
                    "is_lumen_method_count": n == n0,
                    "total_lights_in_room_if_chosen": n_body + len(extra) + n,
                    "nearest_light_in_main_part_mm": round(min(
                        math.hypot(a.x - b.x, a.y - b.y)
                        for a in lts for b in layout_resp.lights_mm)),
                })
            # Rule: fewest lights that reach the target with good uniformity.
            pick_p = next((c for c in pcands if c["meets_target_lux"] and c["uniformity_ok"]),
                          next((c for c in pcands if c["meets_target_lux"]), pcands[-1]))
            by = "rules"
            jp = jev_client.choose_part_lights(
                room={"room_type": rt, "required_lux": rd["lux"],
                      "fixture_lumens": req.fixture_lumens,
                      "main_part_lights": n_body,
                      "lights_already_placed_elsewhere": n_body + len(extra),
                      "whole_room_area_m2": round(area_m2, 2),
                      "even_rule_applies_to_extra_parts": False,
                      "mounting_height_m": req.mounting_height_m},
                part={"width_mm": round(pw), "height_mm": round(ph_),
                      "lumen_method_count": n0,
                      "area_m2": round(pw * ph_ * 1e-6, 2)},
                candidates=pcands,
            )
            if jp:
                hit = next((c for c in pcands if c["id"] == jp["id"]), None)
                if hit:
                    pick_p, by = hit, "Jev"
            extra += _rect_grid_lights(r, pick_p["count"])
            part_notes.append(f"Extra part {pw / 1000:.2f} x {ph_ / 1000:.2f} m: "
                              f"{pick_p['count']} light(s) (chosen by {by}).")
        layout_resp.lights_mm = list(layout_resp.lights_mm) + extra
        layout_resp.count = len(layout_resp.lights_mm)
    if square_bump_note:
        layout_resp.notes = [square_bump_note] + layout_resp.notes
    if body_note:
        layout_resp.notes.insert(0, body_note)
    layout_resp.notes.extend(part_notes)
    if selector["decided_by"] == "jev":
        layout_resp.notes.insert(0, f"Grid {selector['chosen']} chosen by Jev "
                                    f"(confidence {selector['confidence']:.2f}).")
    elif jev_client.jev_available() is False and forced_grid:
        layout_resp.notes.insert(0, "Jev not configured (no TYPESAFE_API_KEY) — "
                                    "grid chosen by built-in rules.")
    elif forced_grid:
        layout_resp.notes.insert(0, "Jev unavailable or rejected — "
                                    "grid chosen by built-in rules.")
    if layout_resp.count % 2 != 0 and even_rule and not rest_rects:
        layout_resp.notes.append(
            f"Placed count {layout_resp.count} is odd because wall/obstacle "
            f"clearance dropped a point from the even grid.")

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

    # ----- 3b. Ceiling fans (optional) -----
    fans_out: List[LayoutPoint] = []
    fan_info: Optional[Dict[str, Any]] = None
    if req.place_fans:
        fan_lights = [(p.x, p.y) for p in layout_resp.lights_mm]
        parts = None
        if not is_convex([(p.x, p.y) for p in light_req.polyline]) and light_req is req:
            from placement_concave import decompose_into_rectangles
            dec = decompose_into_rectangles([(p.x, p.y) for p in light_req.polyline])
            if dec.success and len(dec.rectangles) > 1:
                parts = [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                         for x0, y0, x1, y1 in dec.rectangles]
        if parts:
            fan_cands, fan_notes = fan_placement.build_candidates_multi(parts, fan_lights, rt)
        else:
            fan_cands, fan_notes = fan_placement.build_candidates(
                [(p.x, p.y) for p in light_req.polyline], fan_lights,
                rt, _polygon_area_m2_from_mm(light_req.polyline),
            )
        layout_resp.notes.extend(fan_notes)
        fan_info = {"count": 0, "sweep_mm": 0, "block_scale": 1.0,
                    "decided_by": "none"}
        if fan_cands:
            fan_pick = fan_cands[0]                        # rule-based default
            fan_by = "rules"
            jev_fan = jev_client.choose_fan_layout(
                room={
                    "room_type": rt, "area_m2": round(area_m2, 2),
                    "width_mm": round(W_mm), "height_mm": round(H_mm),
                    "mounting_height_m": req.mounting_height_m,
                    "light_count": layout_resp.count,
                },
                candidates=[{k: v for k, v in c.items() if not k.startswith("_")}
                            for c in fan_cands],
            )
            if jev_fan:
                match = next((c for c in fan_cands if c["id"] == jev_fan["id"]), None)
                if match:                                   # validated: legal layout
                    fan_pick, fan_by = match, "jev"
                    fan_info["confidence"] = jev_fan["confidence"]
                    fan_info["probabilities"] = jev_fan["probabilities"]
            fans_out = [LayoutPoint(x=x, y=y) for (x, y) in fan_pick["_fans"]]
            fan_sweep = fan_pick["sweep_mm"]
            for r in rest_rects:                      # leftover parts of the room
                rc, _ = fan_placement.build_candidates(
                    [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])], fan_lights,
                    rt, (r[2] - r[0]) * (r[3] - r[1]) * 1e-6)
                if rc:
                    fans_out += [LayoutPoint(x=x, y=y) for (x, y) in rc[0]["_fans"]]
                    fan_sweep = min(fan_sweep, rc[0]["sweep_mm"])
            fan_info.update({
                "count": len(fans_out), "sweep_mm": fan_sweep,
                "block_scale": round(fan_sweep / FAN_BLOCK_BASE_SWEEP_MM, 4),
                "decided_by": fan_by, "chosen": fan_pick["id"],
                "candidates": len(fan_cands),
            })
            layout_resp.notes.append(fan_placement.describe(fan_pick, fan_by))

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
        fans_mm=fans_out,
        fan_info=fan_info,
        even_target_count=even_target,
        grid_selector=selector,
        block_data=block_data,
        message=message,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
