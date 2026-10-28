"""
Ceiling-fan placement.

Decides HOW MANY fans a room gets, their blade sweep, and where they go.

Two stages (same pattern as the light grid):
  1. build_candidates() — the rules produce a short list of legal fan layouts
     (cols x rows zones), each already sized, positioned and checked.
     candidates[0] is the standard-chart layout.
  2. The caller picks one: Jev (jev_client.choose_fan_layout) when available,
     otherwise candidates[0], the rule-based best.

Design rules (numbers in config.py "Ceiling fans"):
  * Some room types never get fans (toilets, kitchens, corridors, plant...).
  * Sweep and count come from the standard chart (FAN_CHART: room width x
    length -> sweep/count). Fans are spread along the room's length in equal
    zones. Rooms wider than the chart use the same rule on both sides:
    one fan per ~5.75 m of each side, sweep from the area each fan serves.
  * Blade tips stay >= 450 mm from every wall (the sweep is shrunk if needed).
  * Fans sit at the centre of their equal zone, so the pattern is even. If a
    fan would sit on a light, the WHOLE pattern is shifted together (never one
    fan alone), so the fans stay evenly spaced.

All distances are MILLIMETERS.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from config import (
    FAN_EXCLUDED_ROOM_TYPES, FAN_SWEEPS_MM, FAN_AREA_SWEEP_STEPS,
    FAN_MAX_AREA_PER_FAN_M2, FAN_MIN_AREA_PER_FAN_M2, FAN_WALL_CLEARANCE_MM,
    FAN_LIGHT_CLEARANCE_MM, FAN_MIN_CELL_MM, FAN_ZONE_SPAN_MM,
    FAN_CHART, FAN_CHART_LENGTHS_M, FAN_CHART_MAX_WIDTH_M,
)
from placement import point_in_polygon, min_distance_to_polygon

Pt = Tuple[float, float]

MAX_ALTERNATES = 3


def _sweep_for_area(area_per_fan_m2: float) -> int:
    """Generic blade sweep for the area one fan serves."""
    for limit, sweep in FAN_AREA_SWEEP_STEPS:
        if area_per_fan_m2 <= limit:
            return sweep
    return FAN_SWEEPS_MM[-1]


def _chart_plan(W: float, H: float, area_m2: float) -> Tuple[int, int, int, str]:
    """Standard layout for a W x H room: (cols, rows, sweep_mm, source).

    Narrow rooms (short side <= 5.5 m) read the chart; wider rooms use the
    generic zone rule on both axes."""
    short_m, long_m = min(W, H) / 1000.0, max(W, H) / 1000.0
    along_x = W >= H                                   # fans run along the longer side

    if short_m <= FAN_CHART_MAX_WIDTH_M and long_m <= FAN_CHART_LENGTHS_M[-1] + 1.0:
        row_w = min(FAN_CHART, key=lambda w: abs(w - max(short_m, 3.0)))
        idx = min(range(len(FAN_CHART_LENGTHS_M)),
                  key=lambda i: abs(FAN_CHART_LENGTHS_M[i] - long_m))
        sweep, n = FAN_CHART[row_w][idx]
        return (n, 1, sweep, "chart") if along_x else (1, n, sweep, "chart")

    cols = max(1, math.ceil(W / FAN_ZONE_SPAN_MM))
    rows = max(1, math.ceil(H / FAN_ZONE_SPAN_MM))
    return cols, rows, _sweep_for_area(area_m2 / (cols * rows)), "generic"


def _clear(x: float, y: float, radius: float, pts: List[Pt], wall_gap: float,
           lights: List[Pt], light_gap: float, fans: List[Pt],
           fan_gap: float) -> bool:
    if not point_in_polygon(x, y, pts):
        return False
    if min_distance_to_polygon(x, y, pts) < radius + wall_gap:
        return False
    for lx, ly in lights:
        if math.hypot(x - lx, y - ly) < radius + light_gap:
            return False
    for fx, fy in fans:
        if math.hypot(x - fx, y - fy) < fan_gap:
            return False
    return True


def _place_pattern(centres: List[Pt], radius: float, reach: float, pts: List[Pt],
                   lights: List[Pt]) -> Tuple[List[Pt], int]:
    """Place the fans so the pattern stays even.

    1. Centres as-is, then the whole pattern shifted by a small common offset
       (smallest first) until every fan clears walls and lights.
    2. Only if no common shift works, nudge fans one by one (relaxed gaps).
    Returns (positions, number of fans that moved off their zone centre)."""
    fan_gap = 2 * radius + 300.0

    def ok(cs: List[Pt], wall_gap: float, light_gap: float) -> bool:
        placed: List[Pt] = []
        for x, y in cs:
            if not _clear(x, y, radius, pts, wall_gap, lights, light_gap, placed, fan_gap):
                return False
            placed.append((x, y))
        return True

    offsets: List[Tuple[float, float]] = [(0.0, 0.0)]
    step = 150.0
    r = step
    while r <= reach:
        n = max(8, int(2 * math.pi * r / step))
        offsets += [(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n))
                    for k in range(n)]
        r += step

    for wall_gap, light_gap in ((FAN_WALL_CLEARANCE_MM, FAN_LIGHT_CLEARANCE_MM),
                                (FAN_WALL_CLEARANCE_MM, 0.0)):
        for dx, dy in offsets:
            shifted = [(x + dx, y + dy) for x, y in centres]
            if ok(shifted, wall_gap, light_gap):
                return shifted, (len(shifted) if (dx or dy) else 0)

    # Last resort: per-fan nudge with relaxed gaps.
    out: List[Pt] = []
    moved = 0
    for nx, ny in centres:
        for wall_gap, light_gap in ((FAN_WALL_CLEARANCE_MM, FAN_LIGHT_CLEARANCE_MM),
                                    (300.0, 0.0)):
            spot = None
            for dx, dy in offsets:
                if _clear(nx + dx, ny + dy, radius, pts, wall_gap, lights, light_gap,
                          out, fan_gap):
                    spot = (nx + dx, ny + dy)
                    break
            if spot:
                moved += bool(spot != (nx, ny))
                out.append(spot)
                break
    return out, moved


def _layout_for_grid(cols: int, rows: int, sweep_hint: int, matches_chart: bool,
                     polyline: List[Pt], lights: List[Pt], area_m2: float,
                     x_min: float, y_min: float,
                     W: float, H: float) -> Optional[Dict[str, Any]]:
    """Size, position and check the fans for one (cols, rows) grid."""
    cell_x, cell_y = W / cols, H / rows
    zones = cols * rows

    # Blade tips must clear the walls even for a fan in the middle of its zone.
    fit = min(cell_x, cell_y) - 2 * FAN_WALL_CLEARANCE_MM
    allowed = [s for s in FAN_SWEEPS_MM if s <= sweep_hint and s <= fit]
    if not allowed:
        return None
    sweep = allowed[-1]
    radius = sweep / 2.0

    centres = [(x_min + (i + 0.5) * cell_x, y_min + (j + 0.5) * cell_y)
               for i in range(cols) for j in range(rows)]
    reach = 0.3 * min(cell_x, cell_y)
    fans, nudged = _place_pattern(centres, radius, reach, polyline, lights)
    if not fans:
        return None

    nearest = min((math.hypot(fx - lx, fy - ly) for fx, fy in fans for lx, ly in lights),
                  default=0.0)
    per_fan = area_m2 / len(fans)
    return {
        "id": f"{cols}x{rows}", "cols": cols, "rows": rows,
        "count": len(fans), "zones": zones, "sweep_mm": sweep,
        "area_per_fan_m2": round(per_fan, 1),
        "spacing_x_mm": round(cell_x), "spacing_y_mm": round(cell_y),
        "blade_to_wall_mm": round(min(cell_x, cell_y) / 2 - radius),
        "nearest_light_to_blade_mm": round(nearest - radius),
        "nudged_clear_of_lights": nudged,
        # Explicit rule checks so the choice is easy to make and to explain.
        "matches_standard_chart": matches_chart,
        "zone_shape_ratio": round(max(cell_x, cell_y) / min(cell_x, cell_y), 2),
        "area_per_fan_ok": FAN_MIN_AREA_PER_FAN_M2 <= per_fan <= FAN_MAX_AREA_PER_FAN_M2,
        "wall_clearance_ok": min(cell_x, cell_y) / 2 - radius >= FAN_WALL_CLEARANCE_MM,
        "all_zones_filled": len(fans) == zones,
        "_fans": fans,
        "_score": (not matches_chart, zones - len(fans), len(fans), nudged),
    }


def build_candidates(polyline: List[Pt], lights: List[Pt], room_type: str,
                     area_m2: float) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return (candidates best-first by rules, notes).

    `candidates` is empty when the room gets no fans; `notes` then says why.
    candidates[0] follows the standard chart. A few alternates (one fan more or
    fewer along an axis) are added so the decision model has a real choice.
    Each candidate keeps its positions under "_fans" (not sent to Jev).
    """
    if room_type in FAN_EXCLUDED_ROOM_TYPES:
        return [], [f"No ceiling fans for room type '{room_type}'."]

    xs = [p[0] for p in polyline]
    ys = [p[1] for p in polyline]
    x_min, y_min = min(xs), min(ys)
    W, H = max(xs) - x_min, max(ys) - y_min

    cols, rows, sweep, _src = _chart_plan(W, H, area_m2)
    grids: List[Tuple[int, int, int, bool]] = [(cols, rows, sweep, True)]
    alt = []
    for c, r in ((cols + 1, rows), (cols, rows + 1), (cols - 1, rows), (cols, rows - 1)):
        if c < 1 or r < 1 or (c, r) == (cols, rows):
            continue
        if min(W / c, H / r) < FAN_MIN_CELL_MM:
            continue
        alt.append((c, r, _sweep_for_area(area_m2 / (c * r)), False))
    grids += alt[:MAX_ALTERNATES]

    cands = []
    for c, r, s, is_chart in grids:
        lay = _layout_for_grid(c, r, s, is_chart, polyline, lights, area_m2,
                               x_min, y_min, W, H)
        if lay:
            cands.append(lay)
    if not cands:
        return [], [f"Room too small (or too irregular) for a ceiling fan with "
                    f"{FAN_WALL_CLEARANCE_MM:.0f} mm blade-to-wall clearance."]
    cands.sort(key=lambda c: c["_score"])
    return cands, []


def describe(c: Dict[str, Any], decided_by: str) -> str:
    """One-line note for the chosen layout."""
    s = (f"Fans: {c['count']} x {c['sweep_mm']} mm sweep ({c['cols']}x{c['rows']} "
         f"zones, about {c['area_per_fan_m2']} m2 per fan, chosen by {decided_by}).")
    if c["nudged_clear_of_lights"]:
        s += f" Pattern shifted to stay clear of lights."
    if c["count"] < c["zones"]:
        s += f" {c['zones'] - c['count']} zone(s) skipped: no legal spot."
    return s
