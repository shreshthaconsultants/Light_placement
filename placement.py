"""
Grid light-placement engine and POST /layout endpoint.

Public surface
--------------
- `router`                     : FastAPI router exposing POST /layout.
- `compute_layout(req)`        : pure function used by main.py /design.
- `LayoutRequest` / `LayoutResponse` / `Obstacle` / `BBox` / `GridMeta`
                               : Pydantic models reused by /design.
- Geometry helpers              : `signed_area`, `polygon_centroid`, `is_convex`,
                                 `is_self_intersecting`, `point_in_polygon`,
                                 `min_distance_to_polygon`, `point_to_segment_distance`.
                                 Imported by placement_concave.py and photometric.py.
- `optimize_grid(W, H, cfg, target_count=None)` : grid search with optional
                                                  target-count snapping.

All distances on input/output are MILLIMETERS unless explicitly suffixed _m.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Tuple

import math

from config import (
    MIN_OFFSET_MM, MAX_OFFSET_MM, TARGET_OFFSET_MM,
    MIN_SPACING_MM, MAX_SPACING_MM, TARGET_SPACING_MM,
    MIN_WALL_CLEARANCE_MM,
    MIN_OBSTACLE_CLEARANCE_MM,
    ZERO_AREA_TOL, DEGENERATE_AREA_TOL, ROTATED_BBOX_RATIO,
)

router = APIRouter()


# =====================================================================
# Pydantic models
# =====================================================================

class LayoutPoint(BaseModel):
    x: float
    y: float


class Obstacle(BaseModel):
    """A single obstruction (column, beam, duct, sprinkler, diffuser, …).

    Lights inside any obstacle polygon — OR within `clearance_mm` of any
    edge of the obstacle — are dropped during placement.
    """
    polyline: List[LayoutPoint] = Field(..., min_length=3,
                                        description="Obstacle polygon, mm.")
    type: str = Field(default="other",
                      description="column | beam | duct | sprinkler | "
                                  "hvac_diffuser | other")
    clearance_mm: Optional[float] = Field(
        default=None,
        description=f"Per-obstacle clearance override. "
                    f"Default: {MIN_OBSTACLE_CLEARANCE_MM} mm."
    )


class LayoutRequest(BaseModel):
    polyline: List[LayoutPoint] = Field(..., min_length=3,
                                        description="Polyline vertices in mm (auto-closed).")
    min_offset_mm:     float = Field(default=MIN_OFFSET_MM,     gt=0)
    max_offset_mm:     float = Field(default=MAX_OFFSET_MM,     gt=0)
    min_spacing_mm:    float = Field(default=MIN_SPACING_MM,    gt=0)
    max_spacing_mm:    float = Field(default=MAX_SPACING_MM,    gt=0)
    target_spacing_mm: float = Field(default=TARGET_SPACING_MM, gt=0)
    target_offset_mm:  float = Field(default=TARGET_OFFSET_MM,  gt=0)

    # NEW — when provided, the optimizer picks (Nx, Ny) so that
    # Nx*Ny is as close as possible to (and preferably ≥) target_count
    # while still satisfying spacing / offset constraints.
    target_count: Optional[int] = Field(
        default=None, ge=1,
        description="Optional: prefer a grid whose Nx*Ny matches this count."
    )

    # NEW — list of obstacle polygons. Empty / None = no obstacles.
    obstacles: Optional[List[Obstacle]] = Field(
        default=None,
        description="Optional list of obstacles to avoid (with clearance)."
    )

    # NEW — when True (and target_count is provided), the optimizer picks the
    # factor pair (Nx, Ny) of target_count whose aspect best matches the room.
    # Spacing may fall outside [min_spacing, max_spacing] and is reported via
    # a diagnostic note. For prime target_count, only (1, N) / (N, 1) layouts
    # are possible — the optimizer aligns the line with the room's long axis.
    enforce_exact_target: bool = Field(
        default=False,
        description="If True, place exactly target_count lights even if "
                    "spacing falls outside [min_spacing, max_spacing]."
    )


class GridMeta(BaseModel):
    cols: int
    rows: int
    spacing_x_mm: float
    spacing_y_mm: float
    offset_mm: float


class BBox(BaseModel):
    x_min: float
    y_min: float
    x_max: float
    y_max: float


class LayoutResponse(BaseModel):
    lights_mm: List[LayoutPoint]
    count: int
    grid: GridMeta
    bbox_mm: BBox
    notes: List[str] = []
    # NEW — separate drop counts so the caller can show them to the user.
    n_dropped_by_wall_clearance: int = 0
    n_dropped_by_obstacles: int = 0


# =====================================================================
# Geometry helpers (pure stdlib).
# Exposed without leading underscore so other modules can reuse them.
# Underscored aliases are kept for internal references and tests.
# =====================================================================

def signed_area(pts: List[Tuple[float, float]]) -> float:
    """Shoelace signed area. Positive = CCW, negative = CW."""
    n = len(pts)
    total = 0.0
    for i in range(n):
        j = (i + 1) % n
        total += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
    return total / 2.0


def polygon_centroid(pts: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Centroid via shoelace. Falls back to vertex mean if area is zero."""
    a = signed_area(pts)
    if abs(a) < DEGENERATE_AREA_TOL:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return cx, cy
    cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        cross = pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
        cx += (pts[i][0] + pts[j][0]) * cross
        cy += (pts[i][1] + pts[j][1]) * cross
    cx /= (6 * a)
    cy /= (6 * a)
    return cx, cy


def is_convex(pts: List[Tuple[float, float]]) -> bool:
    """True if all cross products of consecutive edges have the same sign."""
    n = len(pts)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        cx, cy = pts[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if cross == 0:
            continue
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif sign != s:
            return False
    return True


def _segments_intersect(p1, p2, p3, p4) -> bool:
    """Proper (interior) intersection between segments p1-p2 and p3-p4."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def is_self_intersecting(pts: List[Tuple[float, float]]) -> bool:
    """O(n^2) pairwise check on non-adjacent edges of the auto-closed polyline."""
    n = len(pts)
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_intersect(edges[i][0], edges[i][1],
                                   edges[j][0], edges[j][1]):
                return True
    return False


def point_to_segment_distance(px: float, py: float,
                              ax: float, ay: float,
                              bx: float, by: float) -> float:
    """Shortest distance from point (px, py) to segment AB."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    fx, fy = ax + t * dx, ay + t * dy
    return math.hypot(px - fx, py - fy)


def min_distance_to_polygon(x: float, y: float,
                            pts: List[Tuple[float, float]]) -> float:
    """Minimum distance from (x, y) to any edge of the (auto-closed) polygon."""
    n = len(pts)
    d = float("inf")
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        s = point_to_segment_distance(x, y, ax, ay, bx, by)
        if s < d:
            d = s
    return d


def point_in_polygon(x: float, y: float,
                     pts: List[Tuple[float, float]]) -> bool:
    """Standard ray-casting with strict y-comparison."""
    n = len(pts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def polygon_area_abs(pts: List[Tuple[float, float]]) -> float:
    """Absolute polygon area via shoelace."""
    return abs(signed_area(pts))


# Internal aliases preserved so other code touching the underscored names
# (callers, tests) still works.
_signed_area               = signed_area
_polygon_centroid          = polygon_centroid
_is_convex                 = is_convex
_is_self_intersecting      = is_self_intersecting
_point_to_segment_distance = point_to_segment_distance
_min_distance_to_polygon   = min_distance_to_polygon
_point_in_polygon          = point_in_polygon


# =====================================================================
# Grid optimizer
# =====================================================================

def _feasible_offset_range(D: float, N: int,
                           S_min: float, S_max: float,
                           O_min: float, O_max: float
                           ) -> Optional[Tuple[float, float]]:
    """Offsets that keep an N-light row's spacing in [S_min, S_max] for axis length D.
    Intersected with [O_min, O_max]. Returns None if infeasible."""
    if N < 2:
        return None
    o_lo = (D - S_max * (N - 1)) / 2.0
    o_hi = (D - S_min * (N - 1)) / 2.0
    lo = max(o_lo, O_min)
    hi = min(o_hi, O_max)
    if lo > hi:
        return None
    return lo, hi


def _max_N(D: float, S_min: float, O_min: float) -> int:
    """Upper bound on N for an axis. Inner span at min offset / min spacing."""
    inner = D - 2 * O_min
    if inner <= 0:
        return 1
    return max(1, int(inner // S_min) + 1)


def _per_axis_options(D: float, cfg: "LayoutRequest") -> List[Tuple[int, Tuple[float, float]]]:
    """All (N≥2, offset_range) options satisfying spacing & offset on one axis."""
    opts: List[Tuple[int, Tuple[float, float]]] = []
    Nmax = _max_N(D, cfg.min_spacing_mm, cfg.min_offset_mm)
    for N in range(2, Nmax + 1):
        rng = _feasible_offset_range(D, N,
                                     cfg.min_spacing_mm, cfg.max_spacing_mm,
                                     cfg.min_offset_mm, cfg.max_offset_mm)
        if rng is not None:
            opts.append((N, rng))
    return opts


def _all_factor_pairs(n: int) -> List[Tuple[int, int]]:
    """Every ordered (a, b) with a*b == n, including reflections (a≠b → both)."""
    if n < 1:
        return []
    pairs: List[Tuple[int, int]] = []
    a = 1
    while a * a <= n:
        if n % a == 0:
            b = n // a
            pairs.append((a, b))
            if a != b:
                pairs.append((b, a))
        a += 1
    return pairs


def _exact_factor_grid(W: float, H: float, cfg: "LayoutRequest",
                       target_count: int) -> Optional[dict]:
    """Pick the (Nx, Ny) factor pair of `target_count` whose aspect best
    matches the room's W/H. Used when `enforce_exact_target=True`.

    Unlike `optimize_grid`'s spacing-window search, the resulting spacing may
    fall outside [min_spacing_mm, max_spacing_mm]. The caller is expected to
    emit a diagnostic note in that case.

    For prime `target_count`, the only factor pairs are (1, N) and (N, 1), so
    the result is a single line along the room's longer axis.

    Returns None if no factor pair yields strictly positive spacing on every
    axis where N>1 (e.g. room is too small for the requested count).
    """
    if target_count < 1:
        return None
    if target_count == 1:
        # Single light — centre handled downstream via centroid fallback.
        return {"Nx": 1, "Ny": 1, "offset_mm": 0.0,
                "spacing_x_mm": 0.0, "spacing_y_mm": 0.0}

    O = min(max(cfg.target_offset_mm, cfg.min_offset_mm), cfg.max_offset_mm)

    long_axis_is_x = W >= H
    # Symmetric (integer-only) aspect distance avoids floating-point
    # asymmetry between log(1/7) and log(7), which can make tie-breaking
    # depend on FP rounding instead of intent.
    if min(W, H) > 0:
        room_aspect = max(W, H) / min(W, H)
    else:
        room_aspect = float("inf")

    best = None
    best_key = None
    for (Nx, Ny) in _all_factor_pairs(target_count):
        Sx = (W - 2 * O) / (Nx - 1) if Nx > 1 else 0.0
        Sy = (H - 2 * O) / (Ny - 1) if Ny > 1 else 0.0
        if (Nx > 1 and Sx <= 0) or (Ny > 1 and Sy <= 0):
            continue

        # Primary: align the axis with more lights with the room's long axis.
        if long_axis_is_x:
            orientation_penalty = 0 if Nx >= Ny else 1
        else:
            orientation_penalty = 0 if Ny >= Nx else 1

        # Secondary: how "square" the layout is vs how square the room is.
        layout_aspect = max(Nx, Ny) / min(Nx, Ny)
        aspect_dist = abs(layout_aspect - room_aspect)

        # Tertiary: prefer spacing close to the configured target.
        spacing_dist = 0.0
        if Nx > 1:
            spacing_dist += abs(Sx - cfg.target_spacing_mm)
        if Ny > 1:
            spacing_dist += abs(Sy - cfg.target_spacing_mm)

        key = (orientation_penalty, aspect_dist, spacing_dist)
        if best_key is None or key < best_key:
            best_key = key
            best = {"Nx": Nx, "Ny": Ny, "offset_mm": O,
                    "spacing_x_mm": Sx, "spacing_y_mm": Sy}
    return best


def optimize_grid(W: float, H: float, cfg: "LayoutRequest",
                  target_count: Optional[int] = None) -> Optional[dict]:
    """Pick the (Nx, Ny, O, Sx, Sy) that fits constraints and scores best.

    Two modes:

    1. **Default / spacing-window mode** — explores all (Nx, Ny) whose spacing
       fits inside [min_spacing, max_spacing] with offset in [min_offset,
       max_offset]. Ranks by closeness to target_count when given, else by
       most lights.

    2. **Exact-target mode** (`cfg.enforce_exact_target=True` + target_count)
       — bypasses the spacing window and picks the factor pair of
       target_count with best room-aspect match. Spacing may be wide/narrow.
       For prime target_count, produces a single-line layout aligned with
       the room's long axis.

    Returns None when no feasible plan exists (caller falls back to centroid).
    """
    if (getattr(cfg, "enforce_exact_target", False)
            and target_count and target_count >= 1):
        return _exact_factor_grid(W, H, cfg, target_count)

    x_opts = _per_axis_options(W, cfg)
    y_opts = _per_axis_options(H, cfg)
    target_S = cfg.target_spacing_mm
    target_O = cfg.target_offset_mm

    combos: List[Tuple[int, int, float, float]] = []
    if x_opts and y_opts:
        for Nx, (xlo, xhi) in x_opts:
            for Ny, (ylo, yhi) in y_opts:
                lo, hi = max(xlo, ylo), min(xhi, yhi)
                if lo <= hi:
                    combos.append((Nx, Ny, lo, hi))
    elif x_opts:
        for Nx, (xlo, xhi) in x_opts:
            combos.append((Nx, 1, xlo, xhi))
    elif y_opts:
        for Ny, (ylo, yhi) in y_opts:
            combos.append((1, Ny, ylo, yhi))
    else:
        return None

    if not combos:
        return None

    long_axis_is_x = W >= H
    target_count = target_count if (target_count and target_count >= 1) else None

    best = None
    best_key = None
    for Nx, Ny, lo, hi in combos:
        O = min(max(target_O, lo), hi)
        Sx = (W - 2 * O) / (Nx - 1) if Nx > 1 else 0.0
        Sy = (H - 2 * O) / (Ny - 1) if Ny > 1 else 0.0

        count = Nx * Ny
        if long_axis_is_x:
            orientation_penalty = 0 if Nx >= Ny else 1
        else:
            orientation_penalty = 0 if Ny >= Nx else 1

        spacing_offset_dist = abs(O - target_O)
        if Nx > 1:
            spacing_offset_dist += abs(Sx - target_S)
        if Ny > 1:
            spacing_offset_dist += abs(Sy - target_S)

        if target_count is None:
            # Original behavior: maximize count, then orientation, then spacing.
            key = (-count, orientation_penalty, spacing_offset_dist)
        else:
            # Snap to target. Prefer the count CLOSEST to target_count.
            # Under-count (below) and over-count (above) are both penalised by
            # their absolute distance — do NOT give blanket preference to grids
            # that exceed the target, because that is precisely what causes the
            # "extra lights" problem.  Only break a tie in favour of the
            # slightly-over grid (penalty weight 0.5 vs 1.0 for under), so we
            # still get at least the required illuminance.
            distance_to_target = abs(count - target_count)
            # Slight asymmetric tie-break: over by 1 scores better than under
            # by 1, but only when the distance is the same.
            over_bonus = 0.0 if count >= target_count else 0.5
            key = (distance_to_target + over_bonus,
                   orientation_penalty,
                   spacing_offset_dist)

        if best_key is None or key < best_key:
            best_key = key
            best = {"Nx": Nx, "Ny": Ny, "offset_mm": O,
                    "spacing_x_mm": Sx, "spacing_y_mm": Sy}
    return best


# Backwards-compatible internal alias.
_optimize_grid = optimize_grid


# =====================================================================
# Obstacle / clearance helpers
# =====================================================================

def _obstacle_blocks_point(x: float, y: float,
                           obs: Obstacle) -> bool:
    """True if (x, y) is inside the obstacle polygon, or within its
    effective clearance distance of any obstacle edge."""
    obs_pts = [(p.x, p.y) for p in obs.polyline]
    if point_in_polygon(x, y, obs_pts):
        return True
    clearance = obs.clearance_mm if obs.clearance_mm is not None \
                else MIN_OBSTACLE_CLEARANCE_MM
    return min_distance_to_polygon(x, y, obs_pts) < clearance


def _any_obstacle_blocks(x: float, y: float,
                         obstacles: Optional[List[Obstacle]]) -> bool:
    if not obstacles:
        return False
    return any(_obstacle_blocks_point(x, y, ob) for ob in obstacles)


def _detect_dark_zones(survivors_grid: set, Nx: int, Ny: int,
                       Sx: float, Sy: float, max_spacing_mm: float) -> List[str]:
    """Walk each grid row/column. If two consecutive survivors are separated
    by more grid cells than `max_spacing_mm` allows, flag a dark zone."""
    notes: List[str] = []
    # rows (constant j)
    for j in range(Ny):
        row = sorted(i for (i, jj) in survivors_grid if jj == j)
        for a, b in zip(row, row[1:]):
            if (b - a) * Sx > max_spacing_mm:
                notes.append(
                    f"Potential dark zone in row {j}: gap of "
                    f"{(b - a) * Sx:.0f} mm exceeds max spacing "
                    f"{max_spacing_mm:.0f} mm (between columns {a} and {b})."
                )
    # columns (constant i)
    for i in range(Nx):
        col = sorted(j for (ii, j) in survivors_grid if ii == i)
        for a, b in zip(col, col[1:]):
            if (b - a) * Sy > max_spacing_mm:
                notes.append(
                    f"Potential dark zone in column {i}: gap of "
                    f"{(b - a) * Sy:.0f} mm exceeds max spacing "
                    f"{max_spacing_mm:.0f} mm (between rows {a} and {b})."
                )
    return notes


# =====================================================================
# Centroid fallback
# =====================================================================

def _centroid_fallback(pts, x_min, y_min, x_max, y_max,
                       note: str) -> LayoutResponse:
    cx, cy = polygon_centroid(pts)
    if not point_in_polygon(cx, cy, pts):
        cx, cy = pts[0]
    return LayoutResponse(
        lights_mm=[LayoutPoint(x=cx, y=cy)],
        count=1,
        grid=GridMeta(cols=1, rows=1, spacing_x_mm=0.0,
                      spacing_y_mm=0.0, offset_mm=0.0),
        bbox_mm=BBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        notes=[note],
        n_dropped_by_wall_clearance=0,
        n_dropped_by_obstacles=0,
    )


# =====================================================================
# Public entry points
# =====================================================================

def compute_layout(req: LayoutRequest) -> LayoutResponse:
    """Pure function form of POST /layout. Used directly by main.py /design
    (without HTTP round-trip) and indirectly by the FastAPI endpoint below.

    Raises HTTPException on validation failure to match endpoint behavior.
    """
    pts = [(p.x, p.y) for p in req.polyline]

    # ----- 1. Validate -----
    area = signed_area(pts)
    if abs(area) < ZERO_AREA_TOL:
        raise HTTPException(status_code=400,
                            detail="Polyline has zero area.")
    if is_self_intersecting(pts):
        raise HTTPException(status_code=400,
                            detail="Polyline is self-intersecting.")

    # ----- 2. Bounding box -----
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    W = x_max - x_min
    H = y_max - y_min

    notes: List[str] = []

    # ----- 3. Concave handling -----
    # If the room is concave we attempt a rectilinear decomposition into
    # sub-rectangles and run the grid in each. If decomposition fails or
    # explodes into too many pieces, we fall back to bbox-grid (current
    # behavior) with a warning.
    convex = is_convex(pts)
    decomposed_response: Optional[LayoutResponse] = None
    if not convex:
        try:
            # Local import keeps placement.py free of decomposition logic.
            from placement_concave import (
                decompose_into_rectangles, DecompositionResult,
            )
            decomp: DecompositionResult = decompose_into_rectangles(pts)
            if decomp.success:
                decomposed_response = _layout_via_decomposition(
                    pts, req, decomp.rectangles, x_min, y_min, x_max, y_max,
                )
            else:
                notes.append(decomp.reason or
                             "Complex concave shape — placement may be "
                             "suboptimal; consider splitting the room into "
                             "multiple polylines.")
        except Exception as exc:  # pragma: no cover  (defensive)
            notes.append(f"Concave decomposition failed ({exc}); "
                         "falling back to bounding-box grid.")

    if decomposed_response is not None:
        # Merge decomposition notes with any pre-existing notes.
        merged_notes = notes + decomposed_response.notes
        decomposed_response.notes = merged_notes
        return decomposed_response

    # ----- 4. Optimize bbox grid -----
    plan = optimize_grid(W, H, req, target_count=req.target_count)
    if plan is None:
        return _centroid_fallback(
            pts, x_min, y_min, x_max, y_max,
            "Room is smaller than the minimum spacing constraint allows; "
            "placed a single light at the polygon centroid.",
        )

    Nx, Ny = plan["Nx"], plan["Ny"]
    O = plan["offset_mm"]
    Sx = plan["spacing_x_mm"]
    Sy = plan["spacing_y_mm"]

    # ----- 5. Generate candidates -----
    candidates: List[Tuple[int, int, float, float]] = []  # (i, j, x, y)
    for i in range(Nx):
        for j in range(Ny):
            x = (x_min + x_max) / 2 if Nx == 1 else x_min + O + i * Sx
            y = (y_min + y_max) / 2 if Ny == 1 else y_min + O + j * Sy
            candidates.append((i, j, x, y))

    # ----- 6. Filter to polygon interior, by wall clearance, by obstacles -----
    in_poly = [(i, j, x, y) for (i, j, x, y) in candidates
               if point_in_polygon(x, y, pts)]

    after_wall = [(i, j, x, y) for (i, j, x, y) in in_poly
                  if min_distance_to_polygon(x, y, pts) >= MIN_WALL_CLEARANCE_MM]
    n_dropped_by_wall = len(in_poly) - len(after_wall)

    after_obs = [(i, j, x, y) for (i, j, x, y) in after_wall
                 if not _any_obstacle_blocks(x, y, req.obstacles)]
    n_dropped_by_obstacles = len(after_wall) - len(after_obs)

    if not after_obs:
        return _centroid_fallback(
            pts, x_min, y_min, x_max, y_max,
            "Grid produced no points inside the polygon "
            "(extreme concavity or all candidates blocked); "
            "placed a single light at the polygon centroid.",
        )

    # ----- 7. Notes -----
    if not convex:
        notes.append("Wall offset constraint applies to bounding-box edges only, "
                     "not to interior walls of concave rooms.")

    bbox_area = W * H
    if convex and bbox_area > 0 and bbox_area / abs(area) > ROTATED_BBOX_RATIO:
        notes.append("Room is rotated relative to its bounding box; wall clearance "
                     "will be uneven. Consider rotating the drawing axes.")

    # Exact-target-mode diagnostics: tell the user when (a) the resulting
    # spacing is outside the configured window, or (b) the layout is a single
    # line because the target count is prime / near-prime.
    if (getattr(req, "enforce_exact_target", False)
            and req.target_count and Nx * Ny == req.target_count):
        out_of_window: List[str] = []
        if Nx > 1 and not (req.min_spacing_mm <= Sx <= req.max_spacing_mm):
            out_of_window.append(f"X spacing {Sx:.0f} mm")
        if Ny > 1 and not (req.min_spacing_mm <= Sy <= req.max_spacing_mm):
            out_of_window.append(f"Y spacing {Sy:.0f} mm")
        if out_of_window:
            notes.append(
                f"Exact-target mode placed exactly {req.target_count} "
                f"fixture(s) using a {Nx}×{Ny} grid. "
                f"{' and '.join(out_of_window)} falls outside the configured "
                f"{req.min_spacing_mm:.0f}–{req.max_spacing_mm:.0f} mm window "
                f"(adjust MIN/MAX_SPACING_MM in config.py if you want a "
                f"tighter window)."
            )
        if (Nx == 1 or Ny == 1) and req.target_count > 3:
            line_axis = "Y (vertical)" if Nx == 1 else "X (horizontal)"
            notes.append(
                f"Target {req.target_count} is prime / has no non-trivial "
                f"factorization — the only valid grids are 1×N and N×1, so "
                f"the fixtures form a single line along the {line_axis} "
                f"axis. Pick a fixture lumen value that yields a composite "
                f"target (e.g. {req.target_count - 1} or {req.target_count + 1}) "
                f"if you want a rectangular layout."
            )

    if n_dropped_by_wall > 0:
        notes.append(
            f"Dropped {n_dropped_by_wall} grid point(s) within "
            f"{MIN_WALL_CLEARANCE_MM:.0f} mm of a wall feature "
            f"(notch / opening / corner)."
        )

    if n_dropped_by_obstacles > 0:
        notes.append(
            f"Dropped {n_dropped_by_obstacles} grid point(s) inside or "
            f"within clearance of an obstacle."
        )
        # Dark-zone detection only matters when obstacles were involved —
        # the wall-clearance drops are inherent to the polygon edges and
        # already reported above.
        survivors_grid = {(i, j) for (i, j, _, _) in after_obs}
        for note in _detect_dark_zones(survivors_grid, Nx, Ny, Sx, Sy,
                                       req.max_spacing_mm):
            notes.append(note)

    if req.target_count is not None and len(after_obs) != req.target_count:
        # Diagnose *why* the placed count differs from the lumen-method target
        # so the user gets actionable guidance instead of a generic message.
        grid_cells = Nx * Ny
        target = req.target_count
        placed = len(after_obs)
        if grid_cells < target:
            # The optimizer itself couldn't fit `target` cells in the bbox under
            # the configured spacing/offset — spacing is the binding constraint.
            notes.append(
                f"Lumen-method target was {target} fixture(s); spacing "
                f"constraints (min {req.min_spacing_mm:.0f} / max "
                f"{req.max_spacing_mm:.0f} mm) only fit a {Nx}×{Ny}={grid_cells} "
                f"grid in this room. Loosen the spacing window or use a "
                f"brighter fixture to reconcile."
            )
        elif placed < target:
            # The grid had room for `target` but clearance/obstacle drops cost
            # some — adjust drop sources, not fixture lumens.
            dropped = grid_cells - placed
            notes.append(
                f"Lumen-method target was {target} fixture(s); the {Nx}×{Ny} "
                f"grid had room for {grid_cells} but {dropped} were dropped "
                f"by wall clearance or obstacles. Review the obstacle/clearance "
                f"drops above; loosening MIN_WALL_CLEARANCE_MM or shifting "
                f"obstacles may recover lights."
            )
        else:
            # placed > target: closest feasible grid is one step above target.
            notes.append(
                f"Lumen-method target was {target} fixture(s); the closest "
                f"feasible grid is {Nx}×{Ny}={placed} — a {placed - target}-light "
                f"over-design. To hit exactly {target}, use a brighter fixture "
                f"(higher lumens) or loosen MIN/MAX_SPACING_MM in config.py."
            )

    return LayoutResponse(
        lights_mm=[LayoutPoint(x=x, y=y) for (_, _, x, y) in after_obs],
        count=len(after_obs),
        grid=GridMeta(
            cols=Nx, rows=Ny,
            spacing_x_mm=round(Sx, 2),
            spacing_y_mm=round(Sy, 2),
            offset_mm=round(O, 2),
        ),
        bbox_mm=BBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        notes=notes,
        n_dropped_by_wall_clearance=n_dropped_by_wall,
        n_dropped_by_obstacles=n_dropped_by_obstacles,
    )


@router.post("/layout", response_model=LayoutResponse)
def layout(req: LayoutRequest):
    """POST /layout — backward-compatible grid placement endpoint.

    Accepts the same body as before plus the new optional `target_count`
    and `obstacles` fields. Old clients that don't send them get exactly
    the same behavior as version 1.0.
    """
    return compute_layout(req)


# =====================================================================
# Decomposition-driven layout (concave rooms)
# =====================================================================

def _layout_via_decomposition(pts: List[Tuple[float, float]],
                              req: LayoutRequest,
                              rectangles: List[Tuple[float, float, float, float]],
                              x_min: float, y_min: float,
                              x_max: float, y_max: float
                              ) -> LayoutResponse:
    """Run the grid optimizer inside each decomposition rectangle and merge.

    Rectangles are (rx_min, ry_min, rx_max, ry_max). target_count, when
    supplied, is distributed across rectangles by area-weight (rounded).
    """
    from config import CONCAVE_DEDUPE_TOL_MM

    total_rect_area = sum((rx2 - rx1) * (ry2 - ry1)
                          for (rx1, ry1, rx2, ry2) in rectangles)
    target = req.target_count if (req.target_count and req.target_count >= 1) else None

    # Area-weighted target distribution using the **largest-remainder** rule
    # so the sub-targets sum exactly to `target` (naive round() can drift by
    # ±1 for split targets like 7 = 3.5 + 3.5 → 4 + 4 = 8). Required when
    # enforce_exact_target is set on the parent request.
    sub_targets: List[Optional[int]] = [None] * len(rectangles)
    if target is not None and total_rect_area > 0:
        raw_shares = [
            target * ((r[2] - r[0]) * (r[3] - r[1])) / total_rect_area
            for r in rectangles
        ]
        floors = [int(s) for s in raw_shares]
        remainders = [s - f for s, f in zip(raw_shares, floors)]
        deficit = target - sum(floors)
        # Give the +1 to the rectangles whose remainder is largest.
        order = sorted(range(len(rectangles)),
                       key=lambda i: -remainders[i])
        for i in order[:max(0, deficit)]:
            floors[i] += 1
        sub_targets = [max(1, f) for f in floors]

    merged_lights: List[Tuple[float, float]] = []
    notes: List[str] = []
    n_wall = 0
    n_obs = 0

    # Use the grid params of the largest rectangle as the "headline" grid.
    headline = max(rectangles, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    headline_grid: Optional[dict] = None

    for idx, (rx_min, ry_min, rx_max, ry_max) in enumerate(rectangles):
        W_r = rx_max - rx_min
        H_r = ry_max - ry_min
        if W_r <= 0 or H_r <= 0:
            continue

        sub_target = sub_targets[idx]

        sub_req = LayoutRequest(
            polyline=req.polyline,  # ignored — sub-rect dims drive the grid
            min_offset_mm=req.min_offset_mm,
            max_offset_mm=req.max_offset_mm,
            min_spacing_mm=req.min_spacing_mm,
            max_spacing_mm=req.max_spacing_mm,
            target_spacing_mm=req.target_spacing_mm,
            target_offset_mm=req.target_offset_mm,
            target_count=sub_target,
            obstacles=req.obstacles,
            enforce_exact_target=getattr(req, "enforce_exact_target", False),
        )
        plan = optimize_grid(W_r, H_r, sub_req, target_count=sub_target)
        if plan is None:
            # Tiny sub-rect: drop a centroid light if inside polygon and clear.
            cx = (rx_min + rx_max) / 2.0
            cy = (ry_min + ry_max) / 2.0
            if (point_in_polygon(cx, cy, pts)
                    and min_distance_to_polygon(cx, cy, pts) >= MIN_WALL_CLEARANCE_MM
                    and not _any_obstacle_blocks(cx, cy, req.obstacles)):
                merged_lights.append((cx, cy))
            continue

        if (rx_min, ry_min, rx_max, ry_max) == headline:
            headline_grid = plan

        Nx, Ny = plan["Nx"], plan["Ny"]
        O = plan["offset_mm"]
        Sx = plan["spacing_x_mm"]
        Sy = plan["spacing_y_mm"]

        for i in range(Nx):
            for j in range(Ny):
                x = (rx_min + rx_max) / 2 if Nx == 1 else rx_min + O + i * Sx
                y = (ry_min + ry_max) / 2 if Ny == 1 else ry_min + O + j * Sy
                if not point_in_polygon(x, y, pts):
                    continue
                if min_distance_to_polygon(x, y, pts) < MIN_WALL_CLEARANCE_MM:
                    n_wall += 1
                    continue
                if _any_obstacle_blocks(x, y, req.obstacles):
                    n_obs += 1
                    continue
                merged_lights.append((x, y))

    # Dedupe near-coincident lights from shared seams.
    deduped: List[Tuple[float, float]] = []
    for (x, y) in merged_lights:
        if any(math.hypot(x - dx, y - dy) < CONCAVE_DEDUPE_TOL_MM
               for (dx, dy) in deduped):
            continue
        deduped.append((x, y))

    if not deduped:
        return _centroid_fallback(
            pts, x_min, y_min, x_max, y_max,
            "Concave decomposition produced no valid placements; "
            "placed a single light at the polygon centroid.",
        )

    notes.append(
        f"Concave room decomposed into {len(rectangles)} sub-rectangle(s); "
        f"grid was run inside each."
    )
    if n_wall:
        notes.append(f"Dropped {n_wall} sub-rect grid point(s) within "
                     f"{MIN_WALL_CLEARANCE_MM:.0f} mm of a wall.")
    if n_obs:
        notes.append(f"Dropped {n_obs} sub-rect grid point(s) due to obstacles.")

    if headline_grid is None:
        headline_grid = {"Nx": 0, "Ny": 0,
                         "offset_mm": 0.0,
                         "spacing_x_mm": 0.0, "spacing_y_mm": 0.0}

    return LayoutResponse(
        lights_mm=[LayoutPoint(x=x, y=y) for (x, y) in deduped],
        count=len(deduped),
        grid=GridMeta(
            cols=headline_grid["Nx"],
            rows=headline_grid["Ny"],
            spacing_x_mm=round(headline_grid["spacing_x_mm"], 2),
            spacing_y_mm=round(headline_grid["spacing_y_mm"], 2),
            offset_mm=round(headline_grid["offset_mm"], 2),
        ),
        bbox_mm=BBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        notes=notes,
        n_dropped_by_wall_clearance=n_wall,
        n_dropped_by_obstacles=n_obs,
    )