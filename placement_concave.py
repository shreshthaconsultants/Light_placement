"""
Concave room decomposition (axis-aligned rectilinear polygons).

Approach
--------
For most real-world MEP rooms (L-shapes, T-shapes, U-shapes with optional
notches for columns or stair cores) the polygon is axis-aligned. We exploit
that here:

1. If the polygon is *not* axis-aligned (any edge is neither horizontal nor
   vertical, within `AXIS_ALIGN_TOL_MM`), we give up and let the caller fall
   back to the bounding-box grid with a warning. Rotated rectilinear shapes
   could be supported by rotating to axes first; that's left as future work.

2. Otherwise we use the polygon vertices to define a *coordinate grid* of
   candidate cell boundaries, then mark each cell as inside / outside the
   polygon by checking its centroid. The inside cells form a perfect
   orthogonal cover.

3. Inside cells are merged greedily into maximal axis-aligned rectangles.
   This is not the provably-minimum decomposition (that's NP-hard in
   general for arbitrary holes) but for the small polygons we see in
   practice (a dozen vertices, no holes) it produces a clean ≤ 6-rectangle
   cover that the grid optimizer can run inside.

4. If the result still exceeds `CONCAVE_MAX_PIECES`, we report failure so
   the caller can fall back to a bbox grid.

Public surface
--------------
- `decompose_into_rectangles(pts)` → `DecompositionResult(success, rectangles, reason)`
- `is_axis_aligned(pts)` (utility)

Rectangle convention: tuple `(x_min, y_min, x_max, y_max)`, all in mm.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config import (
    AXIS_ALIGN_TOL_MM,
    CONCAVE_MAX_PIECES,
)
from placement import point_in_polygon


# =====================================================================
# Result dataclass
# =====================================================================

@dataclass
class DecompositionResult:
    success: bool
    rectangles: List[Tuple[float, float, float, float]] = field(default_factory=list)
    reason: Optional[str] = None


# =====================================================================
# Axis alignment
# =====================================================================

def is_axis_aligned(pts: List[Tuple[float, float]], 
                    tol: float = AXIS_ALIGN_TOL_MM) -> bool:
    """True if every edge of the polygon is (within `tol` mm) horizontal or
    vertical. Tolerance lets drawings that are mostly snapped but slightly
    off-grid still take the rectilinear path."""
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if abs(x2 - x1) > tol and abs(y2 - y1) > tol:
            return False
    return True


# =====================================================================
# Coordinate dedup with tolerance
# =====================================================================

def _unique_coords(values: List[float], tol: float) -> List[float]:
    """Sorted unique values, collapsing duplicates within `tol`."""
    if not values:
        return []
    s = sorted(values)
    out = [s[0]]
    for v in s[1:]:
        if v - out[-1] > tol:
            out.append(v)
    return out


# =====================================================================
# Greedy maximal-rectangle cover
# =====================================================================

def _greedy_rectangle_cover(grid: List[List[bool]],
                            xs: List[float],
                            ys: List[float]) -> List[Tuple[float, float, float, float]]:
    """Cover all True cells with axis-aligned rectangles using a greedy
    grow-right-then-up algorithm.

    `grid[i][j]` is True iff the cell `xs[i]..xs[i+1]` × `ys[j]..ys[j+1]`
    is inside the polygon. Visit cells in row-major order; for each
    unvisited True cell, grow rightward as far as possible, then upward as
    far as possible while preserving rectangularity. Mark cells visited.
    """
    nx = len(xs) - 1
    ny = len(ys) - 1
    visited = [[False] * ny for _ in range(nx)]
    rects: List[Tuple[float, float, float, float]] = []

    for j in range(ny):
        for i in range(nx):
            if not grid[i][j] or visited[i][j]:
                continue

            # Grow right.
            i_right = i
            while (i_right + 1 < nx
                   and grid[i_right + 1][j]
                   and not visited[i_right + 1][j]):
                i_right += 1

            # Grow up — keep adding rows as long as the entire row segment
            # [i, i_right] is True and unvisited.
            j_top = j
            while j_top + 1 < ny:
                row_ok = True
                for k in range(i, i_right + 1):
                    if not grid[k][j_top + 1] or visited[k][j_top + 1]:
                        row_ok = False
                        break
                if row_ok:
                    j_top += 1
                else:
                    break

            for k in range(i, i_right + 1):
                for m in range(j, j_top + 1):
                    visited[k][m] = True

            rects.append((xs[i], ys[j], xs[i_right + 1], ys[j_top + 1]))

    return rects


# =====================================================================
# Decomposition entry point
# =====================================================================

def decompose_into_rectangles(pts: List[Tuple[float, float]]
                              ) -> DecompositionResult:
    """Decompose a concave (axis-aligned, rectilinear) polygon into a set of
    maximal axis-aligned rectangles. Returns a `DecompositionResult` whose
    `success` is False if:

    - The polygon is not axis-aligned (rotated or curved edges), or
    - The decomposition produces more than `CONCAVE_MAX_PIECES` rectangles.

    In both failure modes `rectangles` is empty and `reason` carries a
    human-readable note suitable for the LayoutResponse `notes` list.
    """
    if not is_axis_aligned(pts):
        return DecompositionResult(
            success=False,
            reason=("Complex concave shape — placement may be suboptimal; "
                    "consider splitting the room into multiple polylines."),
        )

    xs = _unique_coords([p[0] for p in pts], tol=AXIS_ALIGN_TOL_MM)
    ys = _unique_coords([p[1] for p in pts], tol=AXIS_ALIGN_TOL_MM)
    if len(xs) < 2 or len(ys) < 2:
        return DecompositionResult(
            success=False,
            reason="Polygon collapsed to a line/point after coordinate dedup; "
                   "bbox-grid fallback.",
        )

    # Build inside/outside grid using cell centroids.
    nx = len(xs) - 1
    ny = len(ys) - 1
    grid = [[False] * ny for _ in range(nx)]
    for i in range(nx):
        x_mid = (xs[i] + xs[i + 1]) / 2.0
        for j in range(ny):
            y_mid = (ys[j] + ys[j + 1]) / 2.0
            if point_in_polygon(x_mid, y_mid, pts):
                grid[i][j] = True

    rectangles = _greedy_rectangle_cover(grid, xs, ys)

    if not rectangles:
        return DecompositionResult(
            success=False,
            reason="Decomposition produced no rectangles; bbox-grid fallback.",
        )

    if len(rectangles) > CONCAVE_MAX_PIECES:
        return DecompositionResult(
            success=False,
            reason=(f"Complex concave shape — decomposition produced "
                    f"{len(rectangles)} sub-rectangles (limit "
                    f"{CONCAVE_MAX_PIECES}); placement may be suboptimal. "
                    f"Consider splitting the room into multiple polylines."),
        )

    return DecompositionResult(success=True, rectangles=rectangles, reason=None)
