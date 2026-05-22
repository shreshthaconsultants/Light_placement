# Migration guide: `/calculate` + `/layout` → `/design`

The 2.0 backend introduces `POST /design`, a unified endpoint that runs the
lumen-method **and** the grid placement **and** photometric validation in a
single round trip — using the lumen-method's count as the **target** for the
grid optimizer so the two no longer disagree. This is the recommended path
for all new clients.

`/calculate` and `/layout` are preserved verbatim, with new optional fields,
so existing scripts keep working without changes.

---

## Why move?

Before:

```
client → POST /calculate → "you need 12 lights"
       → POST /layout    → "grid says 9 lights"
       → client tells the user "use a brighter/dimmer fixture to reconcile"
```

After:

```
client → POST /design   → "target was 12 → placed 12 with 600 mm wall offset,
                          dropped 1 to obstacle, avg 425 lux (target 400),
                          uniformity 0.74 — OK"
```

The grid optimizer now accepts `target_count`. When set, it picks `(Nx, Ny)`
such that `Nx × Ny` is as close to (and not less than) `target_count` as the
spacing constraints allow.

---

## What changed in the existing endpoints

| Endpoint     | Backwards compatible? | Notes                                               |
|--------------|-----------------------|-----------------------------------------------------|
| `/`          | Yes                   | now also returns `version`                          |
| `/room-types`| Yes                   | unchanged                                           |
| `/calculate` | Yes                   | unchanged behavior                                  |
| `/layout`    | **Yes**               | added optional `target_count`, `obstacles`. Without them, behavior is identical to v1. The response gained two zero-defaulted fields `n_dropped_by_wall_clearance`, `n_dropped_by_obstacles`. |

If you only consume known fields from responses, no client changes are needed.

---

## What's new

- `POST /design` — see `README.md` for the full request/response shape.
- `POST /validate` — standalone point-by-point photometric check for an
  already-placed set of lights.
- `GET  /health`  — status + config snapshot suitable for diagnostics.
- `placement_concave.py` — rectilinear decomposition of L/T/U-shaped rooms.
- `photometric.py` — point-by-point illuminance with cosine correction.

---

## Migrating a client script

### Old two-call code (Python pseudocode)

```python
calc   = requests.post(CALC_URL,   json={
    "room_type": rt,
    "polyline":  poly_in_meters,
    "fixture_lumens": lm,
}).json()
layout = requests.post(LAYOUT_URL, json={
    "polyline": poly_in_mm,
}).json()

n_lumen = calc["number_of_lights"]
n_grid  = layout["count"]
if n_lumen != n_grid:
    warn_user("counts disagree, use a different fixture")

for p in layout["lights_mm"]:
    draw_circle(p["x"], p["y"])
```

### New one-call code

```python
design = requests.post(DESIGN_URL, json={
    "room_type": rt,
    "polyline":  poly_in_mm,          # mm now, not meters
    "fixture_lumens":     lm,
    "fixture_type":       "panel_2x2",
    "mounting_height_m":  3.0,
    "working_plane_m":    0.8,
    "obstacles":          obstacle_polys_mm,   # optional
}).json()

# Reconciliation is built in — no more disagreement.
for p in design["lights_mm"]:
    insert_block(
        design["block_data"]["block_name"],
        p["x"], p["y"],
        attribs=design["block_data"]["fixture_attributes"],
    )

photo = design["photometric"]
if not photo["meets_target"]:
    warn_user(f"Average lux {photo['avg_lux']} < target {photo['required_lux']}")
if photo["uniformity_warning"]:
    warn_user(f"Uniformity {photo['uniformity_ratio']} < 0.7")
```

### Key field renames / additions

| Old (`/calculate`)             | New (`/design`)                              |
|--------------------------------|----------------------------------------------|
| `number_of_lights`             | `target_count` (lumen method asked for this) |
| —                              | `number_of_lights` (what was actually placed) |
| `area_m2`                      | `area_m2` (computed from mm polyline)        |

| Old (`/layout`)                | New (`/design`)                                |
|--------------------------------|------------------------------------------------|
| `count`                        | `number_of_lights`                             |
| `lights_mm`                    | `lights_mm`                                    |
| `grid`, `bbox_mm`, `notes`     | same — passed through                          |
| —                              | `photometric`                                  |
| —                              | `block_data`                                   |
| —                              | `n_dropped_by_wall_clearance`                  |
| —                              | `n_dropped_by_obstacles`                       |

### Unit reminder

| Field                    | Unit          |
|--------------------------|---------------|
| `polyline` (in `/design`)| **mm**        |
| `polyline` (in `/calculate`)| **meters** (unchanged) |
| `mounting_height_m`      | meters        |
| `working_plane_m`        | meters        |
| Everything else          | as before     |

---

## Migrating the ZWCAD plugin

If you are using the bundled `LightPlacementPlugin.cs`, **no action needed**
— it has already been updated to call `/design`, prompt for mounting/working
plane heights, accept obstacle polylines, insert real blocks (with circle
fallback), display photometric results, and register the new `UNDOLIGHT`
command.

If you have a forked plugin, mirror these changes:

1. Replace `CallCalculate` + `CallLayout` with a single `CallDesign` that
   sends the polyline in **mm** (not meters), `mounting_height_m`,
   `working_plane_m`, `fixture_type`, and an optional `obstacles` array.
2. Use `design["block_data"]["block_name"]` to look up a BlockReference;
   fall back to a circle on `LIGHTS` only if `BlockTable.Has(name)` is false.
3. Attach `design["block_data"]["fixture_attributes"]` as XData under app
   name `LIGHTPLACEMENT` (or your own — pick something unique).
4. Drop the "lumen-method count differs from grid count" reconciliation
   message — `/design` reconciles internally and emits a note when the
   target couldn't be exactly matched.
5. Use `design["photometric"]["uniformity_warning"]` to drive a Warning
   MessageBox icon when the design isn't uniform enough.
6. Wrap the drawing transaction so any exception aborts it (don't call
   `Commit` on failure), and track inserted `ObjectId`s in a static list
   so a sibling `UNDOLIGHT` command can erase them later.

---

## Frequently asked

**Q: My old `/layout` clients send no `target_count` — what changes for them?**
A: Nothing. With `target_count == None`, the optimizer uses the same scoring
as v1 (maximize count, then orientation, then closeness to target spacing).

**Q: Will `/design` reject polygons that `/layout` accepted?**
A: No. It uses the exact same geometry validation (`zero area`, `self-intersecting`).

**Q: Do I need to install new Python packages?**
A: No. `requirements.txt` is unchanged — all new code is pure stdlib.

**Q: How do I add a custom fixture catalog?**
A: Add a key to `config.FIXTURE_BLOCKS`. The plugin then accepts that key as
the "Fixture type" prompt input. Make sure the matching block definition
exists in your ZWCAD drawing template (or accept the circle fallback).
