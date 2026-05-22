"""
Point-by-point photometric validation.

After lights are placed, we verify the result by sampling the working plane
on a regular grid and computing illuminance contributions from every
fixture using the inverse-square law with cosine correction:

        I = fixture_lumens / LAMBERTIAN_INTENSITY_DIVISOR     (cd, on-axis)
        θ = angle from fixture nadir to sample point
        d = 3-D distance from fixture (at ceiling) to sample (at work plane)
        E_i = I * cos(θ) / d²                                 (lux from one fixture)
        E_total(sample) = Σ_i E_i                             (lux at that sample)

All distances on input are MILLIMETERS (matching the rest of the API);
the photometric math runs in METERS internally because lux = lm / m².
"""

from pydantic import BaseModel, Field
from typing import List, Tuple, Optional
import math

from config import (
    LAMBERTIAN_INTENSITY_DIVISOR,
    UNIFORMITY_WARNING_THRESHOLD,
    DEFAULT_VALIDATION_SPACING_MM,
)
from placement import LayoutPoint, point_in_polygon


# =====================================================================
# Pydantic result model
# =====================================================================

class PhotometricResult(BaseModel):
    """Aggregated illuminance statistics over the working-plane sample grid."""
    avg_lux: float = Field(..., description="Mean lux over all sample points.")
    min_lux: float = Field(..., description="Minimum lux across the sample grid.")
    max_lux: float = Field(..., description="Maximum lux across the sample grid.")
    uniformity_ratio: float = Field(
        ..., description="min_lux / avg_lux. IES recommends ≥ 0.7 for "
                        "offices and classrooms.")
    meets_target: bool = Field(
        ..., description="True iff avg_lux ≥ required_lux.")
    uniformity_warning: bool = Field(
        ..., description="True iff uniformity_ratio < UNIFORMITY_WARNING_THRESHOLD.")
    required_lux: float = Field(..., description="Target lux for this room type.")
    sample_count: int = Field(..., description="Number of sample points used.")
    sample_spacing_mm: float = Field(..., description="Sample grid spacing in mm.")
    mounting_height_m: float
    working_plane_m: float


# =====================================================================
# Sample grid generation
# =====================================================================

def _generate_sample_points(pts: List[Tuple[float, float]],
                            spacing_mm: float
                            ) -> List[Tuple[float, float]]:
    """Generate a regular grid of sample points (mm) inside the polygon.

    The grid is anchored to the bounding box and only keeps points whose
    centroid lies strictly inside the polygon (ray-casting).
    """
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    # Number of intervals; +1 for the right/top edge.
    nx = max(1, int(math.ceil((x_max - x_min) / spacing_mm)))
    ny = max(1, int(math.ceil((y_max - y_min) / spacing_mm)))

    # Centre the grid in the bbox so we don't bias toward a corner.
    sx = (x_max - x_min) / nx if nx > 0 else 0.0
    sy = (y_max - y_min) / ny if ny > 0 else 0.0

    samples: List[Tuple[float, float]] = []
    for i in range(nx + 1):
        x = x_min + i * sx
        for j in range(ny + 1):
            y = y_min + j * sy
            if point_in_polygon(x, y, pts):
                samples.append((x, y))
    return samples


# =====================================================================
# Illuminance computation
# =====================================================================

def _illuminance_at_sample(sample_xy_m: Tuple[float, float],
                           working_plane_m: float,
                           fixtures_m: List[Tuple[float, float]],
                           mounting_height_m: float,
                           fixture_lumens: float) -> float:
    """Sum of E_i = (I × cos θ) / d² over all fixtures, in lux.

    All XY in METERS; mounting and working plane in METERS.
    """
    I = fixture_lumens / LAMBERTIAN_INTENSITY_DIVISOR    # candelas, on-axis
    dh = mounting_height_m - working_plane_m
    if dh <= 0:
        # Degenerate: fixture at or below the working plane. Skip the cosine
        # term and use the (small but finite) horizontal distance only.
        dh = 1e-6

    sx, sy = sample_xy_m
    total = 0.0
    for fx, fy in fixtures_m:
        dx = sx - fx
        dy = sy - fy
        d2 = dx * dx + dy * dy + dh * dh
        d = math.sqrt(d2)
        cos_theta = dh / d
        total += I * cos_theta / d2
    return total


# =====================================================================
# Entry point
# =====================================================================

def validate_placement(polyline_mm: List[LayoutPoint],
                       lights_mm: List[LayoutPoint],
                       fixture_lumens: float,
                       required_lux: float,
                       mounting_height_m: float,
                       working_plane_m: float,
                       sample_spacing_mm: Optional[float] = None
                       ) -> PhotometricResult:
    """Run the validation grid and return aggregated statistics.

    Parameters
    ----------
    polyline_mm           Closed room polygon (mm).
    lights_mm             Placed light positions (mm).
    fixture_lumens        Total flux per fixture (lm).
    required_lux          Target illuminance for this room type.
    mounting_height_m     Ceiling height where fixtures sit, in meters.
    working_plane_m       Plane height where we evaluate lux, in meters.
    sample_spacing_mm     Grid spacing for sample points (mm).
                          Defaults to DEFAULT_VALIDATION_SPACING_MM.

    Returns
    -------
    PhotometricResult with avg / min / max / uniformity, plus pass/warn flags.
    Lux values are raw (no UF/MF derating) to match the simple lumen formula
    N = ceil(area * required_lux / fixture_lumens).
    """
    if sample_spacing_mm is None or sample_spacing_mm <= 0:
        sample_spacing_mm = DEFAULT_VALIDATION_SPACING_MM

    pts = [(p.x, p.y) for p in polyline_mm]
    samples_mm = _generate_sample_points(pts, sample_spacing_mm)

    # Empty room / degenerate sampling: return zeros with a clear false flag.
    if not samples_mm or not lights_mm:
        return PhotometricResult(
            avg_lux=0.0, min_lux=0.0, max_lux=0.0,
            uniformity_ratio=0.0,
            meets_target=False,
            uniformity_warning=True,
            required_lux=required_lux,
            sample_count=len(samples_mm),
            sample_spacing_mm=sample_spacing_mm,
            mounting_height_m=mounting_height_m,
            working_plane_m=working_plane_m,
        )

    # Convert once.
    MM_PER_M = 1000.0
    fixtures_m = [(p.x / MM_PER_M, p.y / MM_PER_M) for p in lights_mm]

    lux_values: List[float] = []
    for (smx, smy) in samples_mm:
        sample_xy_m = (smx / MM_PER_M, smy / MM_PER_M)
        lux_values.append(_illuminance_at_sample(
            sample_xy_m, working_plane_m,
            fixtures_m, mounting_height_m, fixture_lumens,
        ))

    avg_lux = sum(lux_values) / len(lux_values)
    min_lux = min(lux_values)
    max_lux = max(lux_values)
    uniformity = (min_lux / avg_lux) if avg_lux > 0 else 0.0
    meets_target = avg_lux >= required_lux
    uniformity_warning = uniformity < UNIFORMITY_WARNING_THRESHOLD

    return PhotometricResult(
        avg_lux=round(avg_lux, 2),
        min_lux=round(min_lux, 2),
        max_lux=round(max_lux, 2),
        uniformity_ratio=round(uniformity, 3),
        meets_target=meets_target,
        uniformity_warning=uniformity_warning,
        required_lux=required_lux,
        sample_count=len(lux_values),
        sample_spacing_mm=sample_spacing_mm,
        mounting_height_m=mounting_height_m,
        working_plane_m=working_plane_m,
    )
