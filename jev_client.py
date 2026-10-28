"""
Jev client — asks the Jev decision model to choose the best light grid.

Jev (TypeSafe AI) is not a chat model. One request carries:
    state      what the questions are about (here: the room + candidate grids)
    questions  typed questions (here: a single `choice` question)
and the response carries probabilities, not text.

Flow used by /design:
    1. main.py builds a short list of candidate grids (all with EVEN totals).
    2. choose_grid() sends them to Jev as one `choice` question.
    3. Jev returns the winning option id + probabilities + confidence.
    4. The caller VALIDATES the pick (even, fits the room). On any failure
       (no key, network error, bad answer) choose_grid() returns None and the
       caller falls back to the rule-based grid pick.

Pure stdlib (urllib) — no new dependencies.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import (
    JEV_ENABLED, JEV_BASE_URL, JEV_PATH, JEV_MODEL, JEV_API_KEY,
    JEV_TIMEOUT_S, JEV_MAX_RETRIES, JEV_MIN_CONFIDENCE,
)

log = logging.getLogger("light-placement.jev")

QUESTION_ID = "best_grid"


def jev_available() -> bool:
    """True when Jev is enabled and an API key is configured."""
    return bool(JEV_ENABLED and JEV_API_KEY)


def _post(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST to Jev with retry on 429/529 (exponential backoff)."""
    url = JEV_BASE_URL.rstrip("/") + JEV_PATH
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {JEV_API_KEY}",
        "Content-Type": "application/json",
    })
    for attempt in range(JEV_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=JEV_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < JEV_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Jev HTTP {e.code}: {detail}") from e
    raise RuntimeError("Jev: retries exhausted")


def build_request(room: Dict[str, Any],
                  candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the Jev request body (kept separate so it can be tested offline)."""
    def _describe(c: Dict[str, Any]) -> str:
        s = (f"{c['cols']} columns x {c['rows']} rows = {c['total']} lights, "
             f"spacing {c['spacing_x_mm']:.0f} x {c['spacing_y_mm']:.0f} mm, "
             f"gap to wall {c.get('wall_gap_x_mm', 0):.0f} x "
             f"{c.get('wall_gap_y_mm', 0):.0f} mm")
        if "avg_lux" in c:
            s += (f", simulated average {c['avg_lux']:.0f} lux "
                  f"(target met: {'yes' if c.get('meets_target_lux') else 'no'}), "
                  f"uniformity {c.get('uniformity', 0):.2f} "
                  f"({'good' if c.get('uniformity_ok') else 'poor, below 0.7'})")
        return s

    criteria = {c["id"]: _describe(c) for c in candidates}
    return {
        "model": JEV_MODEL,
        "state": {"room": room, "candidate_grids": candidates},
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": (
                    "Which candidate grid in `candidate_grids` is the best "
                    "ceiling-light layout for `room`? Each candidate places "
                    "one light in the centre of equal cells, so the gap to "
                    "the wall is half the spacing. Decide in this order: "
                    "(1) the simulated average lux must reach "
                    "`room.required_lux`; (2) uniformity must be at least "
                    "0.7, higher is better; (3) light spacing should stay "
                    "below `room.max_good_spacing_mm` and be similar in X and "
                    "Y, so the lights look evenly spread with no big dark "
                    "gaps or lights crowded against walls; (4) among options "
                    "that satisfy 1-3 choose the one with the fewest lights, "
                    "preferring a grid whose longer side follows the room's "
                    "longer side."
                ),
                "criteria": criteria,
            }
        },
    }


FAN_QUESTION_ID = "best_fan_layout"


def choose_fan_layout(room: Dict[str, Any],
                      candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Ask Jev which ceiling-fan layout is best (same contract as choose_grid).

    candidates: [{"id": "2x4", "count": 8, "sweep_mm": 1200, "area_per_fan_m2": ...,
                  "spacing_x_mm": ..., "blade_to_wall_mm": ...,
                  "nearest_light_to_blade_mm": ..., ...}, ...]
    """
    if not jev_available() or len(candidates) < 2:
        return None

    criteria = {
        c["id"]: (f"{c['count']} fans of {c['sweep_mm']} mm sweep in {c['cols']} x "
                  f"{c['rows']} zones, {c['area_per_fan_m2']} m2 per fan, fan spacing "
                  f"{c['spacing_x_mm']} x {c['spacing_y_mm']} mm, blade tip to wall "
                  f"{c['blade_to_wall_mm']} mm, nearest light to blade "
                  f"{c['nearest_light_to_blade_mm']} mm, "
                  f"{c['nudged_clear_of_lights']} fan(s) moved to avoid lights; "
                  f"zone shape ratio {c.get('zone_shape_ratio')} (1.0 = square); "
                  f"matches standard fan chart: "
                  f"{'yes' if c.get('matches_standard_chart') else 'no'}; "
                  f"area per fan within standard range: "
                  f"{'yes' if c.get('area_per_fan_ok') else 'no'}; "
                  f"wall clearance ok: "
                  f"{'yes' if c.get('wall_clearance_ok') else 'no'}")
        for c in candidates
    }
    body = {
        "model": JEV_MODEL,
        "state": {"room": room, "candidate_fan_layouts": candidates},
        "questions": {
            FAN_QUESTION_ID: {
                "type": "choice",
                "instructions": (
                    "Which fan layout in `candidate_fan_layouts` is best for "
                    "`room`? Work through these steps. Step 1: discard layouts "
                    "where `area_per_fan_ok` is false or `wall_clearance_ok` "
                    "is false or `all_zones_filled` is false. Step 2: of the "
                    "rest, choose the layout with `matches_standard_chart` "
                    "true (it follows the standard room size to fan size and "
                    "count chart). Step 3: only if none matches, prefer the "
                    "`zone_shape_ratio` closest to 1.0, then fewer fans. Large halls and "
                    "auditoriums need several fans, never one."
                ),
                "criteria": criteria,
            }
        },
    }
    valid_ids = set(criteria)
    try:
        resp = _post(body)
        ans = resp["answers"][FAN_QUESTION_ID]
        pick = ans["choice"]
        conf = float(ans.get("confidence", 0.0))
    except Exception as exc:
        log.warning("Jev fan call failed (%s) — using rule-based fans.", exc)
        return None
    if pick not in valid_ids or conf < JEV_MIN_CONFIDENCE:
        log.info("Jev fan pick %r rejected (confidence %.2f).", pick, conf)
        return None
    log.info("Jev chose fan layout %s (confidence %.2f)", pick, conf)
    return {"id": pick, "confidence": conf,
            "probabilities": ans.get("probabilities"), "model": resp.get("model")}


def choose_grid(room: Dict[str, Any],
                candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Ask Jev which candidate grid is best.

    room        facts about the room (type, size, target lux, fixture lumens...)
    candidates  [{"id": "3x2", "cols": 3, "rows": 2, "total": 6,
                  "spacing_x_mm": ..., "spacing_y_mm": ...}, ...]

    Returns {"id", "confidence", "probabilities", "model"} or None on any
    failure / low confidence (caller then uses the rule-based fallback).
    """
    if not jev_available() or len(candidates) < 2:
        return None

    body = build_request(room, candidates)
    valid_ids = set(body["questions"][QUESTION_ID]["criteria"])

    try:
        resp = _post(body)
        ans = resp["answers"][QUESTION_ID]
        pick = ans["choice"]
        conf = float(ans.get("confidence", 0.0))
    except Exception as exc:                       # network, 4xx/5xx, bad shape
        log.warning("Jev call failed (%s) — using rule-based grid.", exc)
        return None

    if pick not in valid_ids:
        log.warning("Jev returned unknown option %r — ignoring.", pick)
        return None
    if conf < JEV_MIN_CONFIDENCE:
        log.info("Jev confidence %.2f < %.2f — ignoring.", conf, JEV_MIN_CONFIDENCE)
        return None

    log.info("Jev chose %s (confidence %.2f, model %s)",
             pick, conf, resp.get("model"))
    return {"id": pick, "confidence": conf,
            "probabilities": ans.get("probabilities"),
            "model": resp.get("model")}
