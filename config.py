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

    # --- Added room types (typical CIBSE / NBC India design illuminance) ---
    "auditorium":      {"lux": 200, "uf": 0.50, "mf": 0.80},
    "hall":            {"lux": 200, "uf": 0.50, "mf": 0.80},
    "banquet_hall":    {"lux": 200, "uf": 0.50, "mf": 0.80},
    "theatre":         {"lux": 150, "uf": 0.50, "mf": 0.80},
    "meeting_room":    {"lux": 300, "uf": 0.55, "mf": 0.80},
    "cabin":           {"lux": 400, "uf": 0.55, "mf": 0.80},
    "library":         {"lux": 400, "uf": 0.55, "mf": 0.80},
    "laboratory":      {"lux": 500, "uf": 0.55, "mf": 0.80},
    "workshop":        {"lux": 300, "uf": 0.50, "mf": 0.70},
    "warehouse":       {"lux": 150, "uf": 0.50, "mf": 0.65},
    "store_room":      {"lux": 100, "uf": 0.50, "mf": 0.65},
    "server_room":     {"lux": 300, "uf": 0.50, "mf": 0.80},
    "electrical_room": {"lux": 200, "uf": 0.50, "mf": 0.70},
    "utility":         {"lux": 100, "uf": 0.45, "mf": 0.70},
    "pantry":          {"lux": 200, "uf": 0.50, "mf": 0.75},
    "canteen":         {"lux": 200, "uf": 0.55, "mf": 0.75},
    "cafeteria":       {"lux": 200, "uf": 0.55, "mf": 0.75},
    "restaurant":      {"lux": 150, "uf": 0.55, "mf": 0.80},
    "hotel_room":      {"lux": 150, "uf": 0.55, "mf": 0.80},
    "hospital_ward":   {"lux": 150, "uf": 0.55, "mf": 0.80},
    "waiting_area":    {"lux": 200, "uf": 0.55, "mf": 0.80},
    "lift_lobby":      {"lux": 150, "uf": 0.50, "mf": 0.80},
    "passage":         {"lux": 100, "uf": 0.50, "mf": 0.80},
    "gym":             {"lux": 300, "uf": 0.50, "mf": 0.75},
    "showroom":        {"lux": 500, "uf": 0.55, "mf": 0.80},
    "retail":          {"lux": 300, "uf": 0.55, "mf": 0.75},
    "lounge":          {"lux": 150, "uf": 0.55, "mf": 0.80},
    "prayer_room":     {"lux": 150, "uf": 0.55, "mf": 0.80},
    "balcony":         {"lux":  75, "uf": 0.40, "mf": 0.70},
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


# ============================================================
# Even light count  (main.py / POST /design)
# ============================================================

# When True, /design rounds the lumen-method count UP to the next even number
# (2, 4, 6, ...) so lights always form a symmetric grid.
EVEN_COUNT_ONLY = True

# How many even totals above the target the grid selector may consider
# (target, target+2, ... target+2*N). 0 = exactly the rounded-up target.
GRID_CANDIDATE_EXTRA_STEPS = 2

# Largest number of candidate grids sent to Jev (keeps the question small).
GRID_MAX_CANDIDATES = 12


# ============================================================
# Ceiling fans  (fan_placement.py / POST /design with place_fans=true)
# ============================================================
# Rules of thumb (ENERGY STAR / manufacturer sizing charts, adapted to metric):
#   * one fan covers about 9-16 m2: 900 mm sweep to ~9 m2, 1050 to ~12 m2,
#     1200 to ~16 m2, 1400 beyond that;
#   * blade tips stay >= 450 mm (18 in) from any wall;
#   * big halls get SEVERAL fans on an even grid rather than one huge fan.
# NBC India has no fan-count rule, so these are design-practice values.

# Room types that never get ceiling fans (wet, service, circulation, plant).
FAN_EXCLUDED_ROOM_TYPES = {
    "toilet", "bathroom", "kitchen", "staircase", "corridor", "passage",
    "parking", "lift_lobby", "server_room", "electrical_room", "utility",
    "balcony", "storage", "store_room",
}

FAN_SWEEPS_MM = [900, 1050, 1200, 1400, 1500]   # standard blade sweeps, small -> large
FAN_AREA_LIMITS_M2 = [9.0, 12.0, 16.0]      # (legacy, no longer used for sizing)
FAN_MAX_AREA_PER_FAN_M2 = 26.0              # more area than this per fan is outside the standard chart
FAN_MIN_AREA_PER_FAN_M2 = 4.0
# Generic sizing (rooms wider than the chart): (area per fan up to, sweep); above -> 1500.
FAN_AREA_SWEEP_STEPS = [(11.0, 1050), (14.0, 1200), (21.0, 1400)]
FAN_ZONE_SPAN_MM = 5750.0                   # one fan per ~5.75 m of each side (matches the chart)

# Standard chart supplied by the user: room width (m) -> room length (m) -> (sweep mm, fan count).
# Fans are spread along the LENGTH. Width 5 m, length 14/16 m were cut off in the source and are extrapolated.
FAN_CHART_LENGTHS_M = [4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16]
FAN_CHART = {
    3: [(1200, 1), (1400, 1), (1500, 1), (1050, 2), (1200, 2), (1400, 2),
        (1400, 2), (1400, 2), (1200, 3), (1400, 3), (1400, 3)],
    4: [(1200, 1), (1400, 1), (1200, 2), (1200, 2), (1200, 2), (1400, 2),
        (1400, 2), (1500, 2), (1200, 3), (1400, 3), (1500, 3)],
    5: [(1400, 1), (1400, 1), (1400, 2), (1400, 2), (1400, 2), (1400, 2),
        (1400, 2), (1500, 2), (1400, 3), (1400, 3), (1400, 3)],
}
FAN_CHART_MAX_WIDTH_M = 5.5                 # wider rooms use the generic zone rule
FAN_WALL_CLEARANCE_MM = 450.0               # blade tip to wall
FAN_LIGHT_CLEARANCE_MM = 250.0              # extra gap between fan blade tip and a light
FAN_MIN_CELL_MM = 2400.0                    # smallest fan-to-fan spacing allowed
FAN_MAX_CELL_ASPECT = 1.8                  # keep each fan's zone close to square
FAN_BLOCK_BASE_SWEEP_MM = 1200.0            # sweep drawn by the plugin's fan symbol at scale 1


# ============================================================
# Jev decision model  (jev_client.py)
# ============================================================
# Jev (TypeSafe AI) is a "decision model": it answers choice / score / yes-no
# questions with probabilities instead of generating text. We use ONE choice
# question per room: "which candidate grid is best?".
# The API key is read from the environment, never stored in code:
#     set TYPESAFE_API_KEY=<your key>
import os

JEV_ENABLED   = os.environ.get("JEV_ENABLED", "1") != "0"
JEV_BASE_URL  = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai")
JEV_PATH      = "/v1/systemone"
JEV_MODEL     = os.environ.get("JEV_MODEL", "jev-latest")   # pin e.g. "jev-1.13.0"
JEV_API_KEY   = os.environ.get("TYPESAFE_API_KEY", "")
JEV_TIMEOUT_S = float(os.environ.get("JEV_TIMEOUT_S", "30"))
JEV_MAX_RETRIES = 2          # retries on 429 / 529 with exponential backoff
# Below this confidence the choice is distrusted and the rule-based pick is used.
JEV_MIN_CONFIDENCE = float(os.environ.get("JEV_MIN_CONFIDENCE", "0.7"))