# Light Placement — ZWCAD plugin + FastAPI backend

A production-grade MEP lighting design tool. The ZWCAD `LIGHT` command asks
for a room type, fixture and mounting parameters, picks a closed polyline
(plus any obstacle polylines), POSTs the geometry to a local FastAPI server,
and inserts real fixture **blocks** (with circle fallback) at every grid
point. A second command `UNDOLIGHT` erases the last run.

```
+----------+   single POST /design   +-------------------------------+
|  ZWCAD   |  -------------------->  |  FastAPI backend              |
|  plugin  |   room + polyline + …   |  /design  unified pipeline:   |
|  LIGHT   |                         |    1. lumen method            |
|  UNDO    |                         |    2. grid (target count,     |
|  LIGHT   |                         |       obstacles, concave)     |
|          |  <--------------------  |    3. photometric validation  |
+----------+    lights + block_data  |    4. block insertion data    |
       |                             +-------------------------------+
       v
   BlockReference per light  (or Circle fallback) on the E-LITE layer
   + XData fixture attributes (lumens, wattage, mounting height, type)
   MessageBox with photometric pass/fail + uniformity warning
```

## Folder layout

```
light_placement/
├── backend/
│   ├── main.py              # FastAPI app: /, /health, /room-types,
│   │                        #              /calculate, /layout,
│   │                        #              /design, /validate
│   ├── placement.py         # grid optimizer, target-count, obstacles
│   ├── placement_concave.py # rectilinear decomposition (NEW)
│   ├── photometric.py       # point-by-point illuminance (NEW)
│   ├── config.py            # all tunable constants (incl. FIXTURE_BLOCKS)
│   └── requirements.txt
└── plugin/
    ├── LightPlacementPlugin.cs       # LIGHT + UNDOLIGHT commands
    └── LightPlacementPlugin.csproj   # build file (net48, x64)
```

---

## 1. Run the backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Sanity check:

| URL                                           | Returns                          |
|-----------------------------------------------|----------------------------------|
| `http://127.0.0.1:8000/`                      | service status                   |
| `http://127.0.0.1:8000/health`                | status + config snapshot         |
| `http://127.0.0.1:8000/room-types`            | room → {lux, UF, MF}             |
| `http://127.0.0.1:8000/docs`                  | Swagger UI                       |

The math is the standard **lumen method**:

```
total_lumens = (required_lux × area) / (UF × MF)
target_count = ceil(total_lumens / fixture_lumens)
```

The grid optimizer then picks `(Nx × Ny)` close to (and ≥) `target_count`,
respecting min/max spacing and offset constraints from `config.py`. After
placement, every fixture's contribution is summed at sample points on the
working plane (inverse-square + cosine correction) to validate the result.

---

## 2. Endpoints

| Method | Path         | Purpose                                           |
|--------|--------------|---------------------------------------------------|
| GET    | `/`          | Service banner                                    |
| GET    | `/health`    | Status + config snapshot + version                |
| GET    | `/room-types`| Supported room types                              |
| POST   | `/calculate` | Lumen-method count for a polygon in **meters**   |
| POST   | `/layout`    | Grid placement for a polygon in **mm** (legacy)  |
| POST   | `/validate`  | Point-by-point lux check for an existing layout   |
| POST   | `/design`    | **Recommended**: unified calculate + layout + validate + block-data |

All polylines in `/design`, `/layout`, `/validate` are in **mm**.
`mounting_height_m` and `working_plane_m` are in **meters**.

`/design` request body (only `room_type` and `polyline` are required):

```json
{
  "room_type": "office",
  "polyline": [{"x": 0, "y": 0}, {"x": 6000, "y": 0},
               {"x": 6000, "y": 4500}, {"x": 0, "y": 4500}],
  "fixture_lumens": 3600,
  "fixture_type": "panel_2x2",
  "mounting_height_m": 3.0,
  "working_plane_m": 0.8,
  "obstacles": [
    {"polyline": [{"x":3000,"y":2000},{"x":3300,"y":2000},
                  {"x":3300,"y":2300},{"x":3000,"y":2300}],
     "type": "column"}
  ]
}
```

`/design` response highlights:

```json
{
  "target_count": 9,
  "number_of_lights": 9,
  "lights_mm": [{"x": ..., "y": ...}, ...],
  "grid": {"cols": 3, "rows": 3, "spacing_x_mm": 2400.0, ...},
  "n_dropped_by_wall_clearance": 0,
  "n_dropped_by_obstacles": 1,
  "photometric": {
    "avg_lux": 425.0, "min_lux": 312.0, "max_lux": 540.0,
    "uniformity_ratio": 0.73, "meets_target": true,
    "uniformity_warning": false
  },
  "block_data": {
    "block_name": "LIGHT_FIXTURE_2X2_LED",
    "layer_name": "E-LITE",
    "rotation_deg": 0.0, "scale": 1.0,
    "fixture_attributes": {"lumens": 3600, "wattage": 36.0,
                           "mounting_height_m": 3.0,
                           "fixture_type": "panel_2x2"}
  },
  "notes": ["..."]
}
```

See `MIGRATION.md` for moving from the old two-call (`/calculate` +
`/layout`) flow to `/design`.

---

## 3. Build the ZWCAD plugin

1. Open `plugin/LightPlacementPlugin.csproj` in Visual Studio 2022.
2. In the `<Reference Include="ZwManaged">` and `<Reference Include="ZwDatabaseMgd">`
   blocks, set `HintPath` to the actual DLLs inside your ZWCAD install folder,
   e.g. `C:\Program Files\ZWSOFT\ZWCAD 2026\ZwManaged.dll`.
3. If your ZWCAD uses the older namespace `ZWCAD.*` instead of
   `ZwSoft.ZwCAD.*`, do a find/replace in `LightPlacementPlugin.cs`:
   `ZwSoft.ZwCAD` → `ZWCAD`.
4. Build (Release, x64) → produces `LightPlacementPlugin.dll`.

### (Optional) backend autostart

Drop a one-line text file named `backend.autostart` next to the built DLL,
containing the command that starts the FastAPI server, e.g.

```
uvicorn main:app --host 127.0.0.1 --port 8000
```

If the backend is unreachable when the user runs `LIGHT`, the plugin offers
to launch this command in a new console window.

---

## 4. Load and run inside ZWCAD

1. Start ZWCAD.
2. Make sure the FastAPI server is running.
3. In the ZWCAD command line: `NETLOAD` → pick `LightPlacementPlugin.dll`.
4. Type `LIGHT`. Prompts (each height field accepts Enter for the default):
   - **Room type** → `bedroom` / `office` / `kitchen` / …
   - **Fixture lumens** [`1000`] → numeric, Enter for default
   - **Fixture type** [`panel_2x2`] → `downlight` / `panel_2x2` /
     `panel_2x4` / `strip` / `surface_mount`
   - **Mounting height in meters** [`3.0`] → numeric
   - **Working plane height in meters** [`0.8`] → numeric
   - **Room polyline** → click your closed polyline
   - **Obstacle polylines** → click columns/ducts/diffusers (or Enter to skip)
5. A MessageBox shows the lumen-method count, placed count, drop counts,
   photometric stats (avg/min/max lux + uniformity), and any backend notes.
   The icon turns **Warning** when the uniformity ratio falls below `0.7`.
6. Real fixture blocks (e.g. `LIGHT_FIXTURE_2X2_LED`) are inserted on the
   `E-LITE` layer. If that block is missing from the drawing, circles are
   drawn on the `LIGHTS` layer instead (and the popup says so).

### UNDOLIGHT

Type `UNDOLIGHT` after a `LIGHT` run to erase every fixture that run
inserted. The plugin tracks the inserted `ObjectId`s in a static field for
the duration of the ZWCAD session.

---

## 5. Supported room types

`bedroom, living_room, kitchen, toilet, bathroom, corridor, staircase,
office, conference, classroom, reception, storage, parking, lobby,
dining_room`

Hit `GET /room-types` for the current list and lux / UF / MF defaults.

## Supported fixture types

`downlight, panel_2x2, panel_2x4, strip, surface_mount`

Each maps to a block name + wattage in `config.FIXTURE_BLOCKS`. Add your
own keys there to register new fixture catalogs.

---

## 6. Reading XData back

Every inserted fixture carries XData under the app name `LIGHTPLACEMENT`:

```csharp
ResultBuffer rb = ent.GetXDataForApplication("LIGHTPLACEMENT");
// rb contains "key=value" ASCII strings:
//   lumens=3600
//   wattage=36
//   mounting_height_m=3.0
//   fixture_type=panel_2x2
```

This lets downstream schedules and BOQs recover fixture metadata without a
separate database.

---

## 7. Lumen method vs point-by-point — why they disagree

The lumen method `(E·A)/(UF·MF)` is an *area-averaged* calculation: it
assumes light spreads evenly. Point-by-point validation is honest about
the inverse-square falloff at corners and the cosine of incidence at
oblique angles, then re-applies `UF × MF` as a real-world derating.

Consequence: even when the optimizer places exactly the lumen-method
target count, the average lux from `/validate` is typically lower than
the room's target (often 70–85 % of nominal in well-shaped rooms, less
in long/narrow rooms). `meets_target` is the honest answer; the IES
uniformity warning (< 0.7) is the second honest answer. Treat them as
the design's *real* lighting performance, not a bug.

When the popup shows `meets_target: NO`, the practical levers are:

1. Bump fixture lumens 20 – 30 % and re-run.
2. Loosen `MIN_SPACING_MM` so the grid can densify.
3. Reduce `MAX_OFFSET_MM` so corner samples sit closer to a fixture.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not reach backend` | Start the FastAPI server, or set up `backend.autostart` next to the DLL. Check port 8000 isn't blocked. |
| `Unknown room type` popup | Type one of the supported names from `/room-types`. |
| Popup says "Block ... not found" | Insert the named block into your drawing template (or change `config.FIXTURE_BLOCKS[<type>].block_name` to a block you already have). Circles are placed as a fallback. |
| Photometric warning despite enough lights | Uniformity ratio (`min/avg`) is < 0.7. Add fixtures near the corners or shrink spacing in `config.py`. |
| Build error: `ZwSoft.ZwCAD not found` | Wrong HintPath / wrong namespace. See section 3. |
| `Polyline rejected` | Must be an `LWPOLYLINE`, not a `Line` or 3D `Polyline`. |
