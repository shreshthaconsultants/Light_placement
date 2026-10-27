"""
Ceiling-fan placement.

Decides HOW MANY fans a room gets, their blade sweep, and where they go.

Two stages (same pattern as the light grid):
  1. build_candidates() — the rules produce a short list of legal fan layouts
     (cols x rows zones), each already sized, positioned and checked.
  2. The caller picks one: Jev (jev_client.choose_fan_layout) when available,
     otherwise candidates[0], which is the rule-based best.

Design rules (numbers in config.py "Ceiling fans"):
  * Some room types never get fans (toilets, kitchens, corridors, plant...).
  * Fan count scales with area: roughly one fan per 9-16 m2. Big halls get
    several fans on an even grid of near-square zones, not one huge fan.
  * Blade sweep is picked from the standard sizes by the area each fan serves,
    then shrunk so blade tips stay >= 450 mm from every wall.
  * Fans sit at the centre of their zone; if a fan would sit on a light (or
    too close to another fan) it is nudged to the nearest clear spot.

All distances are MILLIMETERS.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from config import (
    FAN_EXCLUDED_ROOM_TYPES, FAN_SWEEPS_MM, FAN_AREA_LIMITS_M2,
    FAN_MAX_AREA_PER_FAN_M2, FAN_WALL_CLEARANCE_MM, FAN_LIGHT_CLEARANCE_MM,
    FAN_MIN_CELL_MM, FAN_MAX_CELL_ASPECT,
)
from placement import point_in_polygon, min_distance_to_polygon

Pt = Tuple[float, float]

MAX_CANDIDATES = 6


def _sweep_for_area(area_per_fan_m2: float) -> int:
    """Standard blade sweep for the area one fan serves."""
    for limit, sweep in zip(FAN_AREA_LIMITS_M2, FAN_SWEEPS_MM):
        if area_per_fan_m2 <= limit:
            return sweep
    return FAN_SWEEPS_MM[-1]


def _grid_options(W: float, H: float, area_m2: float) -> List[Tuple[int, int]]:
    """Legal (cols, rows) fan-zone grids, best first: each fan serves at most
    FAN_MAX_AREA_PER_FAN_M2, zones are near-square and not tiny."""
    need = max(1, math.ceil(area_m2 / FAN_MAX_AREA_PER_FAN_M2))
    scored = []
    for cols in range(1, 13):
        for rows in range(1, 13):
            total = cols * rows
            if total < need or total > need + 4:
                continue
            sx, sy = W / cols, H / rows
            if min(sx, sy) < FAN_MIN_CELL_MM:
                continue
            if max(sx, sy) / min(sx, sy) > FAN_MAX_CELL_ASPECT:
                continue
            scored.append(((total, abs(sx - sy)), (cols, rows)))
    scored.sort()
    opts = [g for _, g in scored][:MAX_CANDIDATES]
    if opts:
        return opts
    # No ideal grid (narrow / odd room): roughly one fan per 3.6 m of each side.
    return [(max(1, round(W / 3600.0)), max(1, round(H / 3600.0)))]


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


def _nudge(nx: float, ny: float, radius: float, reach: float, pts: List[Pt],
           lights: List[Pt], fans: List[Pt]) -> Optional[Pt]:
    """Closest clear point to (nx, ny) within `reach` (spiral search).
    First pass keeps full clearances; the second relaxes light and wall gaps."""
    fan_gap = 2 * radius + 300.0
    for wall_gap, light_gap in [(FAN_WALL_CLEARANCE_MM, FAN_LIGHT_CLEARANCE_MM),
                                (300.0, 0.0)]:
        if _clear(nx, ny, radius, pts, wall_gap, lights, light_gap, fans, fan_gap):
            return (nx, ny)
        r = 150.0
        while r <= reach:
            steps = max(8, int(2 * math.pi * r / 150.0))
            for k in range(steps):
                a = 2 * math.pi * k / steps
                x, y = nx + r * math.cos(a), ny + r * math.sin(a)
                if _clear(x, y, radius, pts, wall_gap, lights, light_gap, fans, fan_gap):
                    return (x, y)
            r += 150.0
    return None


def _layout_for_grid(cols: int, rows: int, polyline: List[Pt], lights: List[Pt],
                     area_m2: float, x_min: float, y_min: float,
                     W: float, H: float) -> Optional[Dict[str, Any]]:
    """Size, position and check the fans for one (cols, rows) grid."""
    cell_x, cell_y = W / cols, H / rows
    zones = cols * rows
    sweep = _sweep_for_area(area_m2 / zones)

    # Blade tips must clear the walls even for a fan in the middle of its zone.
    fit = min(cell_x, cell_y) - 2 * FAN_WALL_CLEARANCE_MM
    allowed = [s for s in FAN_SWEEPS_MM if s <= sweep and s <= fit]
    if not allowed:
        return None
    sweep = allowed[-1]
    radius = sweep / 2.0

    reach = 0.45 * min(cell_x, cell_y)
    fans: List[Pt] = []
    nudged = 0
    for i in range(cols):
        for j in range(rows):
            nx = x_min + (i + 0.5) * cell_x
            ny = y_min + (j + 0.5) * cell_y
            spot = _nudge(nx, ny, radius, reach, polyline, lights, fans)
            if spot is None:
                continue
            if abs(spot[0] - nx) > 1 or abs(spot[1] - ny) > 1:
                nudged += 1
            fans.append(spot)
    if not fans:
        return None

    nearest = min((math.hypot(fx - lx, fy - ly) for fx, fy in fans for lx, ly in lights),
                  default=0.0)
    return {
        "id": f"{cols}x{rows}", "cols": cols, "rows": rows,
        "count": len(fans), "zones": zones, "sweep_mm": sweep,
        "area_per_fan_m2": round(area_m2 / len(fans), 1),
        "spacing_x_mm": round(cell_x), "spacing_y_mm": round(cell_y),
        "blade_to_wall_mm": round(min(cell_x, cell_y) / 2 - radius),
        "nearest_light_to_blade_mm": round(nearest - radius),
        "nudged_clear_of_lights": nudged,
        # Explicit rule checks so the choice is easy to make and to explain.
        "zone_shape_ratio": round(max(cell_x, cell_y) / min(cell_x, cell_y), 2),
        "area_per_fan_ok": 9.0 <= area_m2 / len(fans) <= FAN_MAX_AREA_PER_FAN_M2,
        "wall_clearance_ok": min(cell_x, cell_y) / 2 - radius >= FAN_WALL_CLEARANCE_MM,
        "all_zones_filled": len(fans) == zones,
        "_fans": fans,
        "_score": (zones - len(fans), len(fans), nudged, abs(cell_x - cell_y)),
    }


def build_candidates(polyline: List[Pt], lights: List[Pt], room_type: str,
                     area_m2: float) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return (candidates best-first by rules, notes).

    `candidates` is empty when the room gets no fans; `notes` then says why.
    Each candidate keeps its positions under "_fans" (not sent to Jev).
    """
    if room_type in FAN_EXCLUDED_ROOM_TYPES:
        return [], [f"No ceiling fans for room type '{room_type}'."]

    xs = [p[0] for p in polyline]
    ys = [p[1] for p in polyline]
    x_min, y_min = min(xs), min(ys)
    W, H = max(xs) - x_min, max(ys) - y_min

    cands = []
    for cols, rows in _grid_options(W, H, area_m2):
        c = _layout_for_grid(cols, rows, polyline, lights, area_m2,
                             x_min, y_min, W, H)
        if c:
            cands.append(c)
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
        s += f" {c['nudged_clear_of_lights']} nudged clear of lights."
    if c["count"] < c["zones"]:
        s += f" {c['zones'] - c['count']} zone(s) skipped: no legal spot."
    return s
