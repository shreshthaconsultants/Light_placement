"""
Configuration / hyperparameters for the Light Placement backend.

All tunable values live here. main.py, placement.py, placement_concave.py and
photometric.py import what they need. Edit values below and `uvicorn --reload`
will pick them up.

This file is documentation as much as configuration — every constant has a
short docstring explaining what it does and why a value was chosen.
"""

import math

# ============================================================
# Server / service metadata
# ============================================================
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

# Bumped to 2.0 when /design, /validate and /health were introduced.
API_VERSION = "2.0"


# ============================================================
# Lumen-method defaults  (main.py / POST /calculate, /design)
# ============================================================

# Default fixture lumens used when the request doesn't specify one.
DEFAULT_FIXTURE_LUMENS = 1000.0

# Per-room design defaults.
#   lux : recommended illuminance for the room type (IES / CIBSE typical values)
#   uf  : utilization factor — fraction of fixture lumens reaching the work plane
#   mf  : maintenance factor — derating for aging + dust over the cleaning cycle
ROOM_DEFAULTS = {
    "bedroom":      {"lux": 150, "uf": 0.55, "mf": 0.80},
    "living_room":  {"lux": 200, "uf": 0.55, "mf": 0.80},
    "kitchen":      {"lux": 300, "uf": 0.50, "mf": 0.75},
    "toilet":       {"lux": 100, "uf": 0.40, "mf": 0.80},
    "bathroom":     {"lux": 200, "uf": 0.40, "mf": 0.80},
    "corridor":     {"lux": 150, "uf": 0.50, "mf": 0.80},
    "staircase":    {"lux": 150, "uf": 0.50, "mf": 0.80},
    "office":       {"lux": 400, "uf": 0.55, "mf": 0.80},
    "conference":   {"lux": 300, "uf": 0.55, "mf": 0.80},
    "classroom":    {"lux": 300, "uf": 0.55, "mf": 0.80},
    "reception":    {"lux": 200, "uf": 0.55, "mf": 0.80},
    "storage":      {"lux": 100, "uf": 0.50, "mf": 0.65},
    "parking":      {"lux":  75, "uf": 0.50, "mf": 0.65},
    "lobby":        {"lux": 200, "uf": 0.50, "mf": 0.80},
    "dining_room":  {"lux": 200, "uf": 0.55, "mf": 0.80},
}


# ============================================================
# Grid placement defaults  (placement.py / POST /layout, /design)
# All distances in MILLIMETERS.
# ============================================================

# Wall offset — distance from polyline to the outermost row of lights.
MIN_OFFSET_MM    = 600.0
MAX_OFFSET_MM    = 650.0
TARGET_OFFSET_MM = 600.0   # optimizer aims for this within the [min, max] window

# Light-to-light spacing within the grid.
# MAX_SPACING_MM is set large so the optimizer can freely explore all grid
# sizes (1×N through N×M). The target_count scoring — not this cap — is the
# primary driver for how many lights get placed. Tighten if you want to
# enforce a hard spacing ceiling for uniformity reasons.
MIN_SPACING_MM    = 1200.0
MAX_SPACING_MM    = 8000.0
TARGET_SPACING_MM = 2200.0


# Minimum clearance from any polyline edge (wall, notch, doorway) to a placed light.
# Acts on the actual polygon, not just the bounding box — so notches and cut-outs
# count. Grid candidates within this distance of any edge are dropped.
MIN_WALL_CLEARANCE_MM = 400.0


# ============================================================
# Obstacle handling  (placement.py / DesignRequest.obstacles)
# ============================================================

# Default clearance from any obstacle (column, beam, duct, sprinkler, …) when
# the request does not specify a per-obstacle override.
MIN_OBSTACLE_CLEARANCE_MM = 300.0


# ============================================================
# Concave room decomposition  (placement_concave.py)
# ============================================================

# Hard cap on the number of sub-rectangles produced by decomposition. If the
# polygon decomposes into more pieces than this, we abandon the decomposition
# and fall back to a single bounding-box grid (with a clear warning note).
CONCAVE_MAX_PIECES = 6

# Tolerance (mm) for deduplicating lights that fall on shared seams between
# adjacent decomposition rectangles.
CONCAVE_DEDUPE_TOL_MM = 100.0

# Edge alignment tolerance (mm). An edge is considered axis-aligned if its
# orthogonal-axis delta is below this. Lets non-perfectly-snapped drawings
# still take the rectilinear decomposition path.
AXIS_ALIGN_TOL_MM = 1.0


# ============================================================
# Photometric validation  (photometric.py / POST /validate, /design)
# ============================================================

# Mounting height (ceiling) and working plane height. Both in METERS.
DEFAULT_MOUNTING_HEIGHT_M = 3.0
DEFAULT_WORKING_PLANE_M   = 0.8

# Validation grid sample spacing in MILLIMETERS — distance between adjacent
# sample points inside the polygon when computing point-by-point illuminance.
# 500 mm is a reasonable engineering default that balances accuracy and speed.
DEFAULT_VALIDATION_SPACING_MM = 500.0

# Uniformity ratio (min / avg lux) below which the validation result is
# flagged as a warning. 0.7 matches the IES standard for offices/classrooms.
UNIFORMITY_WARNING_THRESHOLD = 0.7

# Lambertian downlight intensity model:  I = lumens / divisor.
# π gives the on-axis intensity of a perfect Lambertian source emitting
# `lumens` total flux into a hemisphere. Override if a different photometric
# distribution is desired (e.g. narrow-beam fixture).
LAMBERTIAN_INTENSITY_DIVISOR = math.pi


# ============================================================
# ZWCAD block / fixture catalog  (main.py / DesignResponse.block_data)
# ============================================================

# Layer name used when inserting fixture blocks in the drawing.
DEFAULT_FIXTURE_LAYER = "E-LITE"

# Default fixture type when the caller doesn't pick one.
DEFAULT_FIXTURE_TYPE = "panel_2x2"

# Mapping fixture_type → ZWCAD insertion data. The plugin first attempts to
# insert `block_name`; if the block does not exist in the active drawing it
# falls back to a circle on `layer_name` (with a note in the popup).
#
# `wattage` is informational and is attached to the entity as XData so that
# downstream tools (BOQs, schedules) can recover it.
FIXTURE_BLOCKS = {
    "downlight": {
        "block_name":   "LIGHT_FIXTURE_DOWNLIGHT_LED",
        "layer_name":   "E-LITE",
        "rotation_deg": 0.0,
        "scale":        1.0,
        "wattage":      12.0,
    },
    "panel_2x2": {
        "block_name":   "LIGHT_FIXTURE_2X2_LED",
        "layer_name":   "E-LITE",
        "rotation_deg": 0.0,
        "scale":        1.0,
        "wattage":      36.0,
    },
    "panel_2x4": {
        "block_name":   "LIGHT_FIXTURE_2X4_LED",
        "layer_name":   "E-LITE",
        "rotation_deg": 0.0,
        "scale":        1.0,
        "wattage":      48.0,
    },
    "strip": {
        "block_name":   "LIGHT_FIXTURE_STRIP_LED",
        "layer_name":   "E-LITE",
        "rotation_deg": 0.0,
        "scale":        1.0,
        "wattage":      24.0,
    },
    "surface_mount": {
        "block_name":   "LIGHT_FIXTURE_SURFACE_LED",
        "layer_name":   "E-LITE",
        "rotation_deg": 0.0,
        "scale":        1.0,
        "wattage":      18.0,
    },
}


# ============================================================
# Geometry tolerances  (placement.py)
# ============================================================

# Polygon is rejected (HTTP 400) if abs(shoelace area) is below this.
ZERO_AREA_TOL = 1e-6

# Centroid formula falls back to vertex mean if signed area is below this.
DEGENERATE_AREA_TOL = 1e-9

# Emit "room is rotated relative to its bbox" note when bbox area / polygon area
# exceeds this ratio (only for convex polygons).
ROTATED_BBOX_RATIO = 1.15


# ============================================================
# Square-room target bump  (main.py / POST /design)
# ============================================================

# A room is considered "square-ish" when min(W,H) / max(W,H) ≥ this value.
# In such rooms, a prime-numbered lumen target (3, 5, 7, …) only factors as
# 1×N — producing an unflattering line of fixtures. /design bumps the
# target up by 1 in that case so a 2×N rectangular grid becomes feasible.
# Corridors and long-thin rooms (ratio below the threshold) are left alone
# because a single line of lights is the right answer for them.
SQUARE_ROOM_ASPECT_THRESHOLD = 0.6