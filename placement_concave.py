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


# =====================================================================
# Main body: largest axis-aligned rectangle inside the polygon
# =====================================================================

def largest_inscribed_rectangle(pts: List[Tuple[float, float]]
                                ) -> Optional[Tuple[float, float, float, float]]:
    """Largest-area axis-aligned rectangle (x0, y0, x1, y1) inside a rectilinear
    polygon, or None if the polygon is not axis-aligned.

    Used to find the "main body" of a room that is a big rectangle with small
    notches or alcoves, so lights and fans can be laid out on that rectangle
    like on a plain room."""
    if not is_axis_aligned(pts):
        return None
    xs = _unique_coords([p[0] for p in pts], tol=AXIS_ALIGN_TOL_MM)
    ys = _unique_coords([p[1] for p in pts], tol=AXIS_ALIGN_TOL_MM)
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx < 1 or ny < 1:
        return None
    # bad[i][j] = 1 when the cell centre is outside the polygon.
    bad = [[0 if point_in_polygon((xs[i] + xs[i + 1]) / 2.0,
                                   (ys[j] + ys[j + 1]) / 2.0, pts) else 1
            for j in range(ny)] for i in range(nx)]
    pre = [[0] * (ny + 1) for _ in range(nx + 1)]
    for i in range(nx):
        for j in range(ny):
            pre[i + 1][j + 1] = bad[i][j] + pre[i][j + 1] + pre[i + 1][j] - pre[i][j]

    def n_bad(i0: int, i1: int, j0: int, j1: int) -> int:
        return pre[i1 + 1][j1 + 1] - pre[i0][j1 + 1] - pre[i1 + 1][j0] + pre[i0][j0]

    best, best_area = None, 0.0
    for i0 in range(nx):
        for i1 in range(i0, nx):
            w = xs[i1 + 1] - xs[i0]
            for j0 in range(ny):
                for j1 in range(j0, ny):
                    if n_bad(i0, i1, j0, j1):
                        break               # taller only gets worse
                    a = w * (ys[j1 + 1] - ys[j0])
                    if a > best_area:
                        best_area = a
                        best = (xs[i0], ys[j0], xs[i1 + 1], ys[j1 + 1])
    return best


def split_body_and_rest(pts: List[Tuple[float, float]]):
    """(body, rests): the largest inscribed rectangle and the rectangles that cover
    the rest of a rectilinear polygon. Returns None if the polygon isn't rectilinear."""
    body = largest_inscribed_rectangle(pts)
    if body is None:
        return None
    xs = _unique_coords([p[0] for p in pts] + [body[0], body[2]], tol=AXIS_ALIGN_TOL_MM)
    ys = _unique_coords([p[1] for p in pts] + [body[1], body[3]], tol=AXIS_ALIGN_TOL_MM)
    nx, ny = len(xs) - 1, len(ys) - 1
    grid = [[False] * ny for _ in range(nx)]
    for i in range(nx):
        cx = (xs[i] + xs[i + 1]) / 2.0
        for j in range(ny):
            cy = (ys[j] + ys[j + 1]) / 2.0
            in_body = body[0] < cx < body[2] and body[1] < cy < body[3]
            grid[i][j] = (not in_body) and point_in_polygon(cx, cy, pts)
    return body, _greedy_rectangle_cover(grid, xs, ys)


# =====================================================================
# Tolerant entry point (hand-drawn polylines are never perfectly straight)
# =====================================================================

def snap_rectilinear(pts: List[Tuple[float, float]], tol: float
                     ) -> Optional[List[Tuple[float, float]]]:
    """Snap x and y coordinates that are within `tol` mm of each other to a common
    value, drop duplicate / collinear vertices, and return the cleaned polygon if
    every edge is now horizontal or vertical (else None)."""
    def cluster(values: List[float]) -> dict:
        vs = sorted(set(values))
        groups, cur = [], [vs[0]]
        for v in vs[1:]:
            if v - cur[-1] <= tol:
                cur.append(v)
            else:
                groups.append(cur)
                cur = [v]
        groups.append(cur)
        out = {}
        for g in groups:
            m = sum(g) / len(g)
            for v in g:
                out[v] = m
        return out

    cx = cluster([p[0] for p in pts])
    cy = cluster([p[1] for p in pts])
    snapped = [(cx[x], cy[y]) for x, y in pts]
    cleaned: List[Tuple[float, float]] = []
    for q in snapped:
        if not cleaned or q != cleaned[-1]:
            cleaned.append(q)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    changed = True
    while changed and len(cleaned) > 3:             # remove collinear vertices
        changed = False
        for i in range(len(cleaned)):
            a, b, c = cleaned[i - 1], cleaned[i], cleaned[(i + 1) % len(cleaned)]
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                cleaned.pop(i)
                changed = True
                break
    if len(cleaned) < 4 or not is_axis_aligned(cleaned, tol=0.5):
        return None
    return cleaned


def _split_raster(pts: List[Tuple[float, float]], step: float = 100.0):
    """Body / rest split for ANY polygon by rasterising it on a `step` mm grid."""
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    nx = max(1, int(round((x1 - x0) / step)))
    ny = max(1, int(round((y1 - y0) / step)))
    sx, sy = (x1 - x0) / nx, (y1 - y0) / ny
    xs = [x0 + i * sx for i in range(nx + 1)]
    ys = [y0 + j * sy for j in range(ny + 1)]
    inside = [[point_in_polygon(x0 + (i + 0.5) * sx, y0 + (j + 0.5) * sy, pts)
               for j in range(ny)] for i in range(nx)]
    # largest rectangle of True cells (histogram method, row by row over j)
    heights = [0] * nx
    best, best_cells = None, 0
    for j in range(ny):
        for i in range(nx):
            heights[i] = heights[i] + 1 if inside[i][j] else 0
        stack: List[Tuple[int, int]] = []              # (start index, height)
        for i in range(nx + 1):
            h = heights[i] if i < nx else 0
            start = i
            while stack and stack[-1][1] >= h:
                st, sh = stack.pop()
                cells = sh * (i - st)
                if cells > best_cells:
                    best_cells = cells
                    best = (st, j - sh + 1, i, j + 1)
                start = st
            stack.append((start, h))
    if not best:
        return None
    bi0, bj0, bi1, bj1 = best
    body = (xs[bi0], ys[bj0], xs[bi1], ys[bj1])
    grid = [[inside[i][j] and not (bi0 <= i < bi1 and bj0 <= j < bj1)
             for j in range(ny)] for i in range(nx)]
    return body, _greedy_rectangle_cover(grid, xs, ys)


def split_body_and_rest_any(pts: List[Tuple[float, float]], snap_tol: float = 80.0):
    """(body, rests, method) for any simple polygon: exact on a rectilinear polygon
    (after snapping hand-drawn coordinates within `snap_tol` mm), otherwise a raster
    estimate. method is "exact", "snapped" or "raster"; None if nothing works."""
    if is_axis_aligned(pts):
        r = split_body_and_rest(pts)
        if r:
            return r[0], r[1], "exact"
    snapped = snap_rectilinear(pts, snap_tol)
    if snapped:
        r = split_body_and_rest(snapped)
        if r:
            return r[0], r[1], "snapped"
    r = _split_raster(pts)
    if r:
        return r[0], r[1], "raster"
    return None
