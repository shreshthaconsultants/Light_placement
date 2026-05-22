# Light Placement — ZWCAD plugin + FastAPI backend

A production-grade MEP lighting design tool. The ZWCAD `LIGHT` command asks
once for wattage, UF/MF, and fan preference, then loops over closed
polylines — using each polyline's **layer name** as the room type. For each
room it POSTs the geometry to a local FastAPI server and inserts a
ceiling-light **block** (and optionally a ceiling-fan block) at every grid
point. A second command `UNDOLIGHT` erases every fixture from the last run
across all rooms.

```
+------------+  GET /room-types   +-------------------------------+
|  ZWCAD     | <----------------- |  FastAPI backend              |
|  LIGHT     |                    |  GET /room-types              |
|  (loop)    |  POST /design × N  |  POST /design unified:        |
|  per room: | -----------------> |    1. lumen method            |
|  - select  |  room_type=<layer> |    2. grid (target count,     |
|    polyline|  + polyline (mm)   |       obstacles, concave)     |
|  - read    |                    |    3. photometric validation  |
|    layer   | <----------------- |    4. block insertion data    |
+------------+  lights + photo +  +-------------------------------+
       |       block_data
       v
   BlockReference per light on the E-LITE layer (custom block from code)
   + optional ceiling-fan block on the E-FANS layer
   + XData fixture attributes (lumens, wattage, mounting height, type)
   ObjectIds from EVERY room accumulated into one UNDOLIGHT group
   Final aggregate MessageBox: per-room lux + totals + skipped layers
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

### Prerequisite — name your polyline layers after room types

Each room polyline's **layer name** is what tells the plugin what kind of
room it is. Before running `LIGHT`, set every closed-polyline room to a
layer whose name matches one of the supported room types:

```
bedroom, living_room, kitchen, toilet, bathroom, corridor, staircase,
office, conference, classroom, reception, storage, parking, lobby,
dining_room
```

Case and spaces don't matter — `Living Room`, `LIVING_ROOM`, and
`living_room` all map to the same room type. Polylines on any other layer
(e.g. `0`, `walls`) are skipped with a warning so they don't pollute the
result.

### Running the command

1. Start ZWCAD.
2. Make sure the FastAPI server is running.
3. In the ZWCAD command line: `NETLOAD` → pick `LightPlacementPlugin.dll`.
4. Type `LIGHT`. **Setup prompts (asked ONCE per run, applied to every room):**
   - **Fixture wattage in W** [`10`] → numeric, Enter for default. Lumens
     are derived at 100 lm/W.
   - **Include UF / MF factors?** [`No`] → `Yes` to derate by the room
     type's utilization × maintenance factors.
   - **Place a ceiling fan at the centre of every room?** [`Yes`].
5. **Selection loop (repeats until you press Enter):**
   - `Select room polyline #N (its layer name = room type; press Enter to finish):`
   - Pick a closed polyline. Plugin reads its layer name, looks up the
     room type, POSTs to `/design`, and draws lights (plus a fan if
     enabled and the room shape allows one).
   - Press Enter on an empty selection to end the loop.
6. A single **aggregate MessageBox** lists per-room photometric results
   (area, target count, placed count, avg/min/max lux, uniformity, fan
   position) plus totals across the run and any skipped layers. The icon
   turns **Warning** when any room's uniformity ratio is below `0.7`.
7. Lights are drawn as a custom block (`LIGHT_FIXTURE_CUSTOM_V2`,
   generated from code — no template required) on the `E-LITE` layer.
   Fans use `CEILING_FAN_CUSTOM_V2` on the `E-FANS` layer.

### UNDOLIGHT

Type `UNDOLIGHT` after a `LIGHT` run to erase every fixture that run
inserted **across all rooms** processed in the loop. The plugin
accumulates inserted `ObjectId`s into a single static list during the
selection loop, so one undo wipes the whole multi-room layout.

---

## 5. Supported room types

Set each room polyline's **layer name** to one of:

`bedroom, living_room, kitchen, toilet, bathroom, corridor, staircase,
office, conference, classroom, reception, storage, parking, lobby,
dining_room`

The plugin normalizes layer names to lowercase with underscores
(`Living Room` → `living_room`) before the lookup. Hit `GET /room-types`
for the current list and lux / UF / MF defaults — add new entries by
extending `config.ROOM_DEFAULTS` and restarting the server.

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
| `Polyline is on layer '…' which is not a known room type. Skipping.` | Rename the polyline's layer (e.g. `bedroom`, `office`) to a key from `/room-types`. The plugin lists the valid layer names in the prompt area. |
| `LIGHT: no rooms processed.` | Every polyline you picked was on an unknown layer (or you pressed Enter immediately). Rename layers and re-run. |
| Photometric warning despite enough lights | Uniformity ratio (`min/avg`) is < 0.7 in at least one room. Add fixtures near the corners or shrink spacing in `config.py`. |
| Build error: `ZwSoft.ZwCAD not found` | Wrong HintPath / wrong namespace. See section 3. |
| `Polyline rejected` | Must be an `LWPOLYLINE`, not a `Line` or 3D `Polyline`. |
| Ceiling fan missing in a small/odd room | The plugin only places a fan when the polygon centroid (or bbox centre) is inside the room. Notched/L-shaped rooms may not qualify; the per-room block in the summary will say so. |
