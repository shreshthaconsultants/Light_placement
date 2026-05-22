// LightPlacementPlugin.cs
// ZWCAD .NET plugin: registers the "LIGHT" and "UNDOLIGHT" commands.
//
// LIGHT workflow (multi-room)
// ---------------------------
//   1. User types LIGHT in ZWCAD.
//   2. ONE-OFF setup prompts (asked once, applied to every room in the run):
//        - fixture wattage  (→ approx lumens via 100 lm/W LED efficacy)
//        - include UF / MF derating factors (Yes/No)
//        - place a ceiling fan in every room (Yes/No)
//   3. GET /room-types is called once so the plugin knows which layer names
//      map to known room types (bedroom, living_room, office, …).
//   4. Selection loop: the user picks a closed polyline whose LAYER NAME
//      identifies the room type. The plugin extracts vertices, looks up the
//      layer, and POSTs to /design for that room. Loop ends when the user
//      presses Enter on an empty selection.
//   5. Per room: insert a BlockReference at every returned light coord (custom
//      light block, generated from code) and optionally one fan block at the
//      polygon centroid. Lights too close to the fan are relocated outside
//      the exclusion ring; otherwise they're suppressed.
//   6. Inserted entities are tagged with fixture attributes via XData so
//      downstream tools (BOQs, schedules) can recover lumens/wattage/etc.
//   7. ObjectIds from EVERY room in the run are accumulated into a single
//      undo group so one UNDOLIGHT call wipes the whole multi-room layout.
//   8. A final aggregate MessageBox lists per-room photometric results plus
//      totals across the run. Skipped polylines (unknown room types) are
//      reported at the bottom.
//
// UNDOLIGHT workflow
// -------------------
//   Erases every entity inserted by the most recent LIGHT run (tracked via
//   static ObjectId list). Safe to call repeatedly; second call is a no-op.
//
// Units
// ------
//   Drawing units are assumed to be MILLIMETERS for all CAD operations.
//   The backend's /design endpoint also expects mm — no scaling on the way
//   out. Mounting/working-plane heights are in meters.
//
// Backend autostart
// -----------------
//   If a `backend.autostart` text file exists next to this DLL with a
//   single-line command (e.g. `uvicorn main:app --host 127.0.0.1 --port 8000`),
//   the plugin will offer to launch it when the backend is unreachable.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

using ZwSoft.ZwCAD.ApplicationServices;
using ZwSoft.ZwCAD.Colors;
using ZwSoft.ZwCAD.DatabaseServices;
using ZwSoft.ZwCAD.EditorInput;
using ZwSoft.ZwCAD.Geometry;
using ZwSoft.ZwCAD.Runtime;
using ZcadApp = ZwSoft.ZwCAD.ApplicationServices.Application;

[assembly: CommandClass(typeof(LightPlacement.LightPlacementCommands))]

namespace LightPlacement
{
    public class LightPlacementCommands
    {
        // -------- HTTP endpoints --------
        private const string BASE_URL        = "http://127.0.0.1:8000";
        private const string DESIGN_URL      = BASE_URL + "/design";
        private const string HEALTH_URL      = BASE_URL + "/health";
        private const string ROOM_TYPES_URL  = BASE_URL + "/room-types";

        // -------- Drawing constants --------
        // mm → m factor (for height fields that need conversion). Unused for
        // polyline serialization now — /design expects mm and we send mm.
        private const double UNIT_SCALE_M = 0.001;

        // Fallback-circle radius in drawing units (mm).
        private const double LIGHT_RADIUS_MM = 250.0;

        // Layer used when the block is missing and we have to draw circles.
        private const string CIRCLE_FALLBACK_LAYER = "LIGHTS";

        // XData app name registered in RegAppTable for fixture attributes.
        private const string XDATA_APP_NAME = "LIGHTPLACEMENT";

        // Name of the optional autostart command file (sits next to the DLL).
        private const string AUTOSTART_FILE_NAME = "backend.autostart";

        // -------- Custom block + ceiling-fan placement --------
        // Both blocks are generated entirely from C# in EnsureCustomLightBlock /
        // EnsureCustomFanBlock — the user's drawing template doesn't need any
        // pre-defined fixtures. The backend's block_data.block_name is still
        // recorded in XData (for BOQs / schedules), but the geometry drawn is
        // always our custom block.
        // Both blocks bumped to V2 when the geometry was redesigned
        // (off-white downlight, white 3-blade fan). The V1 names — if present
        // in a pre-existing drawing — are left untouched.
        private const string CUSTOM_LIGHT_BLOCK = "LIGHT_FIXTURE_CUSTOM_V2";
        private const string CUSTOM_FAN_BLOCK   = "CEILING_FAN_CUSTOM_V2";

        // Ceiling-fan dimensions (mm). The housing radius drives both the
        // drawn symbol and the exclusion zone used to keep lights from
        // sitting under the fan.
        private const double FAN_RADIUS_MM     = 600.0;
        private const double FAN_HUB_RADIUS_MM = 90.0;

        // Extra margin added to the fan housing radius. Any light centre
        // within FAN_RADIUS_MM + FAN_LIGHT_CLEARANCE_MM of the fan centre
        // is suppressed before the lights are drawn.
        private const double FAN_LIGHT_CLEARANCE_MM = 250.0;

        // When a placed light collides with the fan exclusion zone, we try to
        // RELOCATE it to a position just outside the ring instead of dropping
        // it (so a single-light room doesn't end up dark after fan placement).
        // RELOCATE_RING_MARGIN sits the new light slightly outside the
        // exclusion radius; RELOCATE_WALL_CLEARANCE matches the backend's
        // wall clearance so relocated lights are still legal placements.
        private const double FAN_RELOCATE_RING_MARGIN_MM   = 50.0;
        private const double FAN_RELOCATE_WALL_CLEARANCE_MM = 400.0;
        // Minimum separation between a relocated light and any already-kept
        // light, to avoid two relocated lights landing on top of each other.
        private const double FAN_RELOCATE_MIN_SEPARATION_MM = 600.0;

        // Layer used for the ceiling-fan insert. ACI 7 renders as white on
        // dark backgrounds and black on light — the standard "auto-contrast"
        // colour for architectural symbols.
        private const string FAN_LAYER       = "E-FANS";
        private const short  FAN_LAYER_COLOR = 7;

        // -------- HTTP client --------
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(60)
        };

        // -------- UNDOLIGHT tracker --------
        // ObjectIds inserted by the most recent successful LIGHT run, plus
        // the layer they were placed on. Cleared on the next LIGHT run.
        private static readonly List<ObjectId> _lastInsertedIds = new List<ObjectId>();
        private static string _lastInsertedLayer = "";

        // ============================================================
        // LIGHT command (multi-room)
        // ============================================================
        //
        // Workflow summary:
        //   1. Ask wattage, UF/MF, fan once — apply to every room in this run.
        //   2. GET /room-types so we can validate polyline layer names.
        //   3. Loop: prompt for a polyline, read its layer name, treat that
        //      as the room type, POST /design, draw lights + optional fan.
        //   4. Stop the loop when the user presses Enter on the prompt.
        //   5. Show a single aggregate MessageBox at the end.
        [CommandMethod("LIGHT")]
        public void LightCommand()
        {
            Document doc = ZcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            try
            {
                // ---- 1. Fixture wattage → approx lumens (asked once) ----
                // LED efficacy assumed at ~100 lm/W (typical commercial LED).
                // Applied to every room processed in this LIGHT invocation.
                const double LUMENS_PER_WATT = 100.0;
                PromptDoubleOptions pdoWatts = new PromptDoubleOptions(
                    "\nEnter wattage of a single light fixture (W) " +
                    "— applied to every room: ")
                {
                    AllowNegative = false,
                    AllowZero = false,
                    DefaultValue = 10.0,
                    UseDefaultValue = true
                };
                PromptDoubleResult pdrWatts = ed.GetDouble(pdoWatts);
                if (pdrWatts.Status != PromptStatus.OK)
                {
                    ed.WriteMessage("\nCancelled.");
                    return;
                }
                double fixtureWatts = pdrWatts.Value;
                double fixtureLumens = fixtureWatts * LUMENS_PER_WATT;
                ed.WriteMessage("\nApprox lumens: " +
                                fixtureLumens.ToString("F0", CultureInfo.InvariantCulture) +
                                " lm  (" +
                                fixtureWatts.ToString("F1", CultureInfo.InvariantCulture) +
                                " W x " +
                                LUMENS_PER_WATT.ToString("F0", CultureInfo.InvariantCulture) +
                                " lm/W). Applied to every room in this run.");

                // ---- 2. UF / MF derating (asked once) ----
                // When Yes, the backend derates the lumen-method count by the
                // room type's UF × MF defaults (typically 0.4-0.5 combined),
                // producing a more realistic (higher) fixture count.
                PromptKeywordOptions pkoUfMf = new PromptKeywordOptions(
                    "\nInclude utilization (UF) and maintenance (MF) factors " +
                    "for every room?");
                pkoUfMf.Keywords.Add("Yes");
                pkoUfMf.Keywords.Add("No");
                pkoUfMf.Keywords.Default = "No";
                pkoUfMf.AllowNone = true;
                PromptResult prUfMf = ed.GetKeywords(pkoUfMf);
                bool useUfMf = false;
                if (prUfMf.Status == PromptStatus.OK || prUfMf.Status == PromptStatus.None)
                {
                    useUfMf = string.Equals(prUfMf.StringResult, "Yes",
                                            StringComparison.OrdinalIgnoreCase);
                }

                // ---- 3. Ceiling fan (asked once) ----
                // If Yes, a fan is placed at each room's centroid (with the
                // usual exclusion-ring + light-relocation logic). Skipped
                // automatically for rooms where the centroid + bbox-centre
                // both fall outside the polygon (very irregular shapes).
                PromptKeywordOptions pkoFan = new PromptKeywordOptions(
                    "\nPlace a ceiling fan at the centre of every room?");
                pkoFan.Keywords.Add("Yes");
                pkoFan.Keywords.Add("No");
                pkoFan.Keywords.Default = "Yes";
                pkoFan.AllowNone = true;
                PromptResult prFan = ed.GetKeywords(pkoFan);
                bool placeFan = false;
                if (prFan.Status == PromptStatus.OK || prFan.Status == PromptStatus.None)
                {
                    placeFan = string.Equals(prFan.StringResult, "Yes",
                                             StringComparison.OrdinalIgnoreCase);
                }

                // ---- 4. Pre-flight: fetch supported room-type list ----
                // Doubles as a backend reachability check so the user doesn't
                // waste effort selecting polylines if the server is down.
                ed.WriteMessage("\nContacting " + ROOM_TYPES_URL + " ...");
                HashSet<string> knownRoomTypes;
                try
                {
                    knownRoomTypes = FetchRoomTypes().GetAwaiter().GetResult();
                }
                catch (HttpRequestException hex)
                {
                    HandleBackendUnreachable(ed, hex);
                    return;
                }
                ed.WriteMessage(
                    "\nSet each room polyline's LAYER to one of: " +
                    string.Join(", ", knownRoomTypes));

                // ---- 5. Reset undo + aggregate trackers for this invocation ----
                // UNDOLIGHT removes every fixture inserted during this run, so
                // the static tracker is cleared once HERE and DrawLights then
                // APPENDS new ObjectIds across every room in the loop.
                _lastInsertedIds.Clear();
                _lastInsertedLayer = "";
                var layersUsed    = new HashSet<string>();
                var roomResults   = new List<RoomResult>();
                var skippedLayers = new List<string>();

                // ---- 6. Multi-room selection loop ----
                while (true)
                {
                    int roomNum = roomResults.Count + 1;
                    PromptEntityOptions peo = new PromptEntityOptions(
                        "\nSelect room polyline #" + roomNum +
                        " (its layer name = room type; press Enter to finish): ");
                    peo.SetRejectMessage("\nThe selected object must be a polyline.");
                    peo.AddAllowedClass(typeof(Polyline), true);
                    peo.AllowNone = true;
                    PromptEntityResult per = ed.GetEntity(peo);
                    if (per.Status == PromptStatus.None ||
                        per.Status == PromptStatus.Cancel)
                    {
                        break;
                    }
                    if (per.Status != PromptStatus.OK) break;

                    List<double[]> roomVertsMm;
                    bool roomClosed;
                    string layerName;
                    ExtractPolylineVerticesMm(doc, per.ObjectId,
                                              out roomVertsMm,
                                              out roomClosed,
                                              out layerName);

                    if (!roomClosed)
                    {
                        ed.WriteMessage(
                            "\n  Note: polyline is open. " +
                            "Treating it as closed for area calculation.");
                    }
                    if (roomVertsMm.Count < 3 || CountUniqueVertices(roomVertsMm) < 3)
                    {
                        ed.WriteMessage(
                            "\n  -> Polyline has fewer than 3 unique vertices. Skipping.");
                        continue;
                    }

                    // Normalize the layer name the same way the backend does
                    // (lowercase, spaces → underscores) so case / whitespace
                    // mismatches don't reject a valid room type.
                    string normalized = (layerName ?? "")
                        .Trim()
                        .ToLowerInvariant()
                        .Replace(" ", "_");
                    if (!knownRoomTypes.Contains(normalized))
                    {
                        ed.WriteMessage(
                            "\n  -> Polyline is on layer '" + layerName +
                            "' which is not a known room type. Skipping. " +
                            "Rename the layer to one of: " +
                            string.Join(", ", knownRoomTypes));
                        skippedLayers.Add(string.IsNullOrEmpty(layerName)
                                          ? "(no layer)" : layerName);
                        continue;
                    }

                    ed.WriteMessage("\n  -> Room type from layer: " + normalized);

                    RoomResult rr;
                    try
                    {
                        rr = ProcessRoom(doc, ed, normalized, roomVertsMm,
                                         fixtureLumens, useUfMf, placeFan);
                    }
                    catch (HttpRequestException hex)
                    {
                        HandleBackendUnreachable(ed, hex);
                        return;
                    }
                    catch (System.Exception ex)
                    {
                        ed.WriteMessage("\n  -> Error processing room: " + ex.Message);
                        continue;
                    }

                    roomResults.Add(rr);
                    layersUsed.Add(rr.LayerUsed);
                    ed.WriteMessage(
                        "\n  -> [" + rr.RoomType + "] " +
                        rr.AreaM2.ToString("F1", CultureInfo.InvariantCulture) + " m² → " +
                        rr.NumLightsDrawn + " light(s), avg " +
                        rr.AvgLux.ToString("F0", CultureInfo.InvariantCulture) + " lux " +
                        (rr.MeetsTarget ? "OK" : "BELOW TARGET") +
                        (rr.FanPlaced ? "  + 1 fan" : ""));
                }

                // ---- 7. Aggregate summary ----
                if (roomResults.Count == 0)
                {
                    string emptyMsg = "LIGHT: no rooms processed.";
                    if (skippedLayers.Count > 0)
                    {
                        emptyMsg += "\nSkipped layers (unknown room type): " +
                                    string.Join(", ", skippedLayers);
                    }
                    ed.WriteMessage("\n" + emptyMsg);
                    MessageBox.Show(emptyMsg, "Light Placement",
                                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }

                _lastInsertedLayer = string.Join(", ", layersUsed);

                int totalLights = 0, totalFans = 0;
                int totalDroppedWall = 0, totalDroppedObs = 0;
                int totalDroppedFan = 0, totalRelocatedFan = 0;
                bool anyWarn = false;

                StringBuilder popup = new StringBuilder();
                popup.AppendLine("Rooms processed  : " + roomResults.Count);
                popup.AppendLine("Fixture wattage  : " +
                    fixtureWatts.ToString("F1", CultureInfo.InvariantCulture) + " W (" +
                    fixtureLumens.ToString("F0", CultureInfo.InvariantCulture) + " lm)");
                popup.AppendLine("UF/MF applied    : " + (useUfMf ? "Yes" : "No"));
                popup.AppendLine("Fans requested   : " + (placeFan ? "Yes (per room)" : "No"));
                popup.AppendLine();

                foreach (var rr in roomResults)
                {
                    popup.AppendLine("--- " + rr.RoomType + " ---");
                    popup.AppendLine("  Area         : " +
                        rr.AreaM2.ToString("F2", CultureInfo.InvariantCulture) + " m²");
                    popup.AppendLine("  Required lux : " + rr.RequiredLux);
                    popup.AppendLine("  Target count : " + rr.TargetCount);
                    popup.AppendLine("  Lights placed: " + rr.NumLightsDrawn);
                    if (rr.DroppedByWall > 0)
                        popup.AppendLine("  Dropped(wall): " + rr.DroppedByWall);
                    if (rr.DroppedByFan > 0)
                        popup.AppendLine("  Dropped(fan) : " + rr.DroppedByFan);
                    if (rr.RelocatedByFan > 0)
                        popup.AppendLine("  Reloc(fan)   : " + rr.RelocatedByFan);
                    popup.AppendLine("  Avg lux      : " +
                        rr.AvgLux.ToString("F0", CultureInfo.InvariantCulture) +
                        (rr.MeetsTarget ? "  OK" : "  BELOW TARGET"));
                    popup.AppendLine("  Min/Max lux  : " +
                        rr.MinLux.ToString("F0", CultureInfo.InvariantCulture) + " / " +
                        rr.MaxLux.ToString("F0", CultureInfo.InvariantCulture));
                    popup.AppendLine("  Uniformity   : " +
                        rr.Uniformity.ToString("F2", CultureInfo.InvariantCulture) +
                        (rr.UniformWarn ? "  WARN (<0.7)" : "  OK"));
                    if (rr.FanPlaced)
                        popup.AppendLine("  Ceiling fan  : 1 at (" +
                            rr.FanCx.ToString("F0", CultureInfo.InvariantCulture) + ", " +
                            rr.FanCy.ToString("F0", CultureInfo.InvariantCulture) + ") mm");
                    if (rr.Notes != null && rr.Notes.Count > 0)
                    {
                        foreach (var note in rr.Notes)
                            popup.AppendLine("  · " + note);
                    }

                    totalLights       += rr.NumLightsDrawn;
                    if (rr.FanPlaced) totalFans++;
                    totalDroppedWall  += rr.DroppedByWall;
                    totalDroppedObs   += rr.DroppedByObstacles;
                    totalDroppedFan   += rr.DroppedByFan;
                    totalRelocatedFan += rr.RelocatedByFan;
                    if (rr.UniformWarn) anyWarn = true;
                }

                popup.AppendLine();
                popup.AppendLine("--- TOTALS ---");
                popup.AppendLine("Total lights     : " + totalLights);
                popup.AppendLine("Total fans       : " + totalFans);
                if (totalDroppedWall > 0)
                    popup.AppendLine("Dropped (walls)  : " + totalDroppedWall);
                if (totalDroppedFan > 0)
                    popup.AppendLine("Dropped (fan)    : " + totalDroppedFan);
                if (totalRelocatedFan > 0)
                    popup.AppendLine("Relocated (fan)  : " + totalRelocatedFan);
                popup.AppendLine("Layers used      : " + string.Join(", ", layersUsed));

                if (skippedLayers.Count > 0)
                {
                    popup.AppendLine();
                    popup.AppendLine("--- Skipped polylines (unknown room type) ---");
                    var seen = new HashSet<string>();
                    foreach (var sl in skippedLayers)
                    {
                        if (seen.Add(sl))
                            popup.AppendLine("- " + sl);
                    }
                }

                string popupText = popup.ToString();
                ed.WriteMessage("\n" + popupText);

                MessageBoxIcon icon = anyWarn ? MessageBoxIcon.Warning
                                              : MessageBoxIcon.Information;
                MessageBox.Show(popupText, "Light Placement — Multi-room Summary",
                                MessageBoxButtons.OK, icon);
            }
            catch (HttpRequestException hex)
            {
                HandleBackendUnreachable(ed, hex);
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage("\nError: " + ex.Message);
                MessageBox.Show(ex.Message, "Error",
                                MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        // ============================================================
        // Per-room processing (called once per polyline by the loop above)
        // ============================================================

        // What the multi-room loop records for each successfully processed room.
        // Aggregated into the final summary MessageBox at the end of LightCommand.
        private struct RoomResult
        {
            public string RoomType;
            public double AreaM2;
            public int    RequiredLux;
            public int    TargetCount;
            public int    NumLightsDrawn;
            public int    DroppedByWall;
            public int    DroppedByObstacles;
            public int    DroppedByFan;
            public int    RelocatedByFan;
            public bool   FanPlaced;
            public double FanCx;
            public double FanCy;
            public double AvgLux;
            public double MinLux;
            public double MaxLux;
            public double Uniformity;
            public bool   MeetsTarget;
            public bool   UniformWarn;
            public string LayerUsed;
            public List<string> Notes;
        }

        // Process one room: POST /design, run the fan-placement / relocation
        // logic, draw fixtures + optional fan, and return a RoomResult for the
        // caller to aggregate. Throws HttpRequestException up if the backend
        // disappears mid-run so LightCommand can offer autostart.
        private static RoomResult ProcessRoom(
            Document doc, Editor ed, string roomType,
            List<double[]> roomVertsMm, double fixtureLumens,
            bool useUfMf, bool placeFan)
        {
            string designJson = CallDesign(roomType, roomVertsMm, fixtureLumens,
                                           null, useUfMf).GetAwaiter().GetResult();
            var design = (Dictionary<string, object>)
                new JavaScriptSerializer().DeserializeObject(designJson);

            // -------- Unpack response --------
            string rt           = Convert.ToString(design["room_type"],         CultureInfo.InvariantCulture);
            double area         = Convert.ToDouble(design["area_m2"],           CultureInfo.InvariantCulture);
            int    lux          = Convert.ToInt32 (design["required_lux"],      CultureInfo.InvariantCulture);
            int    targetCount  = Convert.ToInt32 (design["target_count"],      CultureInfo.InvariantCulture);

            int n_wall = Convert.ToInt32(design["n_dropped_by_wall_clearance"], CultureInfo.InvariantCulture);
            int n_obs  = Convert.ToInt32(design["n_dropped_by_obstacles"],      CultureInfo.InvariantCulture);

            object[] lightsArr = (object[])design["lights_mm"];
            object[] notesArr  = (object[])design["notes"];

            var block = (Dictionary<string, object>)design["block_data"];
            string layerName   = Convert.ToString(block["layer_name"],  CultureInfo.InvariantCulture);
            double rotationDeg = Convert.ToDouble(block["rotation_deg"], CultureInfo.InvariantCulture);
            double scale       = Convert.ToDouble(block["scale"],       CultureInfo.InvariantCulture);
            var fixAttribs     = (Dictionary<string, object>)block["fixture_attributes"];

            var photo = (Dictionary<string, object>)design["photometric"];
            double avgLux      = Convert.ToDouble(photo["avg_lux"], CultureInfo.InvariantCulture);
            double minLux      = Convert.ToDouble(photo["min_lux"], CultureInfo.InvariantCulture);
            double maxLux      = Convert.ToDouble(photo["max_lux"], CultureInfo.InvariantCulture);
            double uniformity  = Convert.ToDouble(photo["uniformity_ratio"], CultureInfo.InvariantCulture);
            bool   meetsTarget = Convert.ToBoolean(photo["meets_target"]);
            bool   uniformWarn = Convert.ToBoolean(photo["uniformity_warning"]);

            // -------- Fan placement (per room) --------
            // Centre is the polygon centroid (shoelace). For L-/U-shapes
            // where the centroid lies outside the room we fall back to the
            // bbox centre, and if THAT is also outside we skip the fan for
            // this room so we never plant it in a notch.
            double fanCx = 0.0, fanCy = 0.0;
            int lightsRemovedByFan = 0;
            int lightsRelocatedByFan = 0;
            bool effectivePlaceFan = placeFan;
            if (effectivePlaceFan)
            {
                PolygonCentroidMm(roomVertsMm, out fanCx, out fanCy);
                if (!PointInPolygonMm(fanCx, fanCy, roomVertsMm))
                {
                    double bxmin = double.PositiveInfinity, bxmax = double.NegativeInfinity;
                    double bymin = double.PositiveInfinity, bymax = double.NegativeInfinity;
                    foreach (var v in roomVertsMm)
                    {
                        if (v[0] < bxmin) bxmin = v[0];
                        if (v[0] > bxmax) bxmax = v[0];
                        if (v[1] < bymin) bymin = v[1];
                        if (v[1] > bymax) bymax = v[1];
                    }
                    fanCx = (bxmin + bxmax) / 2.0;
                    fanCy = (bymin + bymax) / 2.0;
                    if (!PointInPolygonMm(fanCx, fanCy, roomVertsMm))
                    {
                        effectivePlaceFan = false;
                    }
                }

                if (effectivePlaceFan)
                {
                    // Drop any light whose centre would sit under the fan
                    // housing, but try to RELOCATE each dropped light to a
                    // position just outside the exclusion ring so the lumen-
                    // method count is preserved.
                    double exclusion = FAN_RADIUS_MM + FAN_LIGHT_CLEARANCE_MM;
                    var kept    = new List<object>(lightsArr.Length);
                    var dropped = new List<double[]>();
                    foreach (var item in lightsArr)
                    {
                        var p = (Dictionary<string, object>)item;
                        double lx = Convert.ToDouble(p["x"], CultureInfo.InvariantCulture);
                        double ly = Convert.ToDouble(p["y"], CultureInfo.InvariantCulture);
                        double dx = lx - fanCx, dy = ly - fanCy;
                        if (Math.Sqrt(dx * dx + dy * dy) < exclusion)
                        {
                            lightsRemovedByFan++;
                            dropped.Add(new double[] { lx, ly });
                            continue;
                        }
                        kept.Add(item);
                    }

                    // Existing kept positions constrain where the next
                    // relocation can sit so two replacements don't pile up.
                    var keptPositions = new List<double[]>();
                    foreach (var item in kept)
                    {
                        var p = (Dictionary<string, object>)item;
                        keptPositions.Add(new double[]
                        {
                            Convert.ToDouble(p["x"], CultureInfo.InvariantCulture),
                            Convert.ToDouble(p["y"], CultureInfo.InvariantCulture),
                        });
                    }

                    foreach (var orig in dropped)
                    {
                        double rx, ry;
                        if (TryRelocateOutsideFan(
                                orig[0], orig[1], fanCx, fanCy, exclusion,
                                roomVertsMm, keptPositions, out rx, out ry))
                        {
                            kept.Add(new Dictionary<string, object>
                            {
                                { "x", rx },
                                { "y", ry },
                            });
                            keptPositions.Add(new double[] { rx, ry });
                            lightsRelocatedByFan++;
                        }
                    }

                    lightsArr = kept.ToArray();
                }
            }

            // -------- Draw (transactional, all-or-nothing) --------
            // The backend's block_name is ignored — DrawLights always uses
            // CUSTOM_LIGHT_BLOCK defined entirely in code. The ObjectIds get
            // appended to _lastInsertedIds so UNDOLIGHT wipes every fixture
            // from every room placed in this LIGHT invocation.
            DrawResult draw = DrawLights(
                doc, lightsArr, CUSTOM_LIGHT_BLOCK, layerName,
                rotationDeg * Math.PI / 180.0, scale, fixAttribs,
                effectivePlaceFan, fanCx, fanCy);

            // -------- Collate notes for the room ----
            var notesList = new List<string>();
            if (notesArr != null)
            {
                foreach (var note in notesArr)
                    notesList.Add(Convert.ToString(note, CultureInfo.InvariantCulture));
            }

            return new RoomResult
            {
                RoomType           = rt,
                AreaM2             = area,
                RequiredLux        = lux,
                TargetCount        = targetCount,
                NumLightsDrawn     = draw.Drawn,
                DroppedByWall      = n_wall,
                DroppedByObstacles = n_obs,
                DroppedByFan       = lightsRemovedByFan,
                RelocatedByFan     = lightsRelocatedByFan,
                FanPlaced          = draw.FanDrawn,
                FanCx              = fanCx,
                FanCy              = fanCy,
                AvgLux             = avgLux,
                MinLux             = minLux,
                MaxLux             = maxLux,
                Uniformity         = uniformity,
                MeetsTarget        = meetsTarget,
                UniformWarn        = uniformWarn,
                LayerUsed          = draw.LayerUsed,
                Notes              = notesList,
            };
        }

        // ============================================================
        // UNDOLIGHT command
        // ============================================================
        [CommandMethod("UNDOLIGHT")]
        public void UndoLightCommand()
        {
            Document doc = ZcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            if (_lastInsertedIds.Count == 0)
            {
                string msg = "UNDOLIGHT: nothing to undo from this session.";
                ed.WriteMessage("\n" + msg);
                MessageBox.Show(msg, "UNDOLIGHT",
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            int erased = 0;
            int skipped = 0;

            try
            {
                using (Transaction tr = doc.TransactionManager.StartTransaction())
                {
                    foreach (ObjectId id in _lastInsertedIds)
                    {
                        if (!id.IsValid || id.IsErased)
                        {
                            skipped++;
                            continue;
                        }
                        try
                        {
                            DBObject obj = tr.GetObject(id, OpenMode.ForWrite, false);
                            if (obj == null || obj.IsErased)
                            {
                                skipped++;
                                continue;
                            }
                            obj.Erase();
                            erased++;
                        }
                        catch
                        {
                            skipped++;
                        }
                    }
                    tr.Commit();
                }
            }
            finally
            {
                _lastInsertedIds.Clear();
            }

            string done = "UNDOLIGHT: erased " + erased + " fixture(s)" +
                          (skipped > 0 ? " (" + skipped + " already gone)" : "") +
                          " from layer '" + _lastInsertedLayer + "'.";
            _lastInsertedLayer = "";
            ed.WriteMessage("\n" + done);
            MessageBox.Show(done, "UNDOLIGHT",
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        // ============================================================
        // Helpers
        // ============================================================

        // Extract LWPOLYLINE vertices in raw drawing units (mm), along with
        // the polyline's layer name. The multi-room LIGHT command uses the
        // layer name as the room-type lookup key (e.g., a polyline on layer
        // "bedroom" is treated as a bedroom and gets bedroom lux levels).
        private static void ExtractPolylineVerticesMm(
            Document doc, ObjectId polyId,
            out List<double[]> verts, out bool closed, out string layer)
        {
            verts = new List<double[]>();
            closed = false;
            layer = "";
            using (Transaction tr = doc.TransactionManager.StartTransaction())
            {
                Polyline pl = tr.GetObject(polyId, OpenMode.ForRead) as Polyline;
                if (pl == null) return;
                closed = pl.Closed;
                layer = pl.Layer ?? "";
                for (int i = 0; i < pl.NumberOfVertices; i++)
                {
                    Point2d p = pl.GetPoint2dAt(i);
                    verts.Add(new double[] { p.X, p.Y });
                }
                tr.Commit();
            }
        }

        // Count vertices considering near-duplicates as one.
        private static int CountUniqueVertices(List<double[]> verts)
        {
            const double TOL = 0.5; // mm
            int unique = 0;
            for (int i = 0; i < verts.Count; i++)
            {
                bool dup = false;
                for (int j = 0; j < i; j++)
                {
                    double dx = verts[i][0] - verts[j][0];
                    double dy = verts[i][1] - verts[j][1];
                    if (Math.Abs(dx) < TOL && Math.Abs(dy) < TOL)
                    {
                        dup = true;
                        break;
                    }
                }
                if (!dup) unique++;
            }
            return unique;
        }

        // -------- Drawing --------
        private struct DrawResult
        {
            public int Drawn;
            public bool UsedFallback;   // legacy; always false now that the custom block is generated from code
            public bool FanDrawn;
            public string LayerUsed;
        }

        // Insert a BlockReference at every light coordinate and, when
        // requested, one BlockReference for the ceiling fan at (fanCx, fanCy).
        // Both blocks are guaranteed to exist via EnsureCustomLightBlock /
        // EnsureCustomFanBlock — no template dependency.
        //
        // Transactional all-or-nothing: any exception aborts the transaction
        // via the `using` block (Commit is the LAST line) so half-drawn results
        // never pollute the drawing. ObjectIds (lights AND fan) are appended
        // to the tracker only after the transaction commits, so UNDOLIGHT
        // removes everything from the run in a single sweep.
        private static DrawResult DrawLights(
            Document doc, object[] lightsArr,
            string blockName, string layerName,
            double rotationRad, double scale,
            Dictionary<string, object> fixtureAttribs,
            bool placeFan, double fanCx, double fanCy)
        {
            DrawResult dr = new DrawResult
            {
                Drawn = 0,
                UsedFallback = false,
                FanDrawn = false,
                LayerUsed = layerName,
            };
            int lightCount = lightsArr == null ? 0 : lightsArr.Length;
            if (lightCount == 0 && !placeFan) return dr;

            var tracker = new List<ObjectId>();
            string finalLayer = layerName;

            using (Transaction tr = doc.TransactionManager.StartTransaction())
            {
                Database db = doc.Database;

                // Generate the custom blocks from code if not already present.
                // Idempotent — subsequent LIGHT runs reuse the existing defs.
                EnsureCustomLightBlock(tr, db);
                if (placeFan) EnsureCustomFanBlock(tr, db);

                BlockTable bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                BlockTableRecord ms = (BlockTableRecord)tr.GetObject(
                    bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

                ObjectId lightDefId = bt[blockName]; // blockName == CUSTOM_LIGHT_BLOCK
                ObjectId layerId    = EnsureLayer(tr, db, finalLayer);
                EnsureXDataAppRegistered(tr, db);

                // ---- Lights ----
                if (lightsArr != null)
                {
                    foreach (var item in lightsArr)
                    {
                        var p = (Dictionary<string, object>)item;
                        double x = Convert.ToDouble(p["x"], CultureInfo.InvariantCulture);
                        double y = Convert.ToDouble(p["y"], CultureInfo.InvariantCulture);
                        Point3d pos = new Point3d(x, y, 0);

                        BlockReference br = new BlockReference(pos, lightDefId)
                        {
                            Rotation = rotationRad,
                            ScaleFactors = new Scale3d(scale),
                            LayerId = layerId,
                        };
                        ms.AppendEntity(br);
                        tr.AddNewlyCreatedDBObject(br, true);
                        AttachFixtureXData(br, fixtureAttribs);

                        tracker.Add(br.ObjectId);
                        dr.Drawn++;
                    }
                }

                // ---- Ceiling fan ----
                if (placeFan)
                {
                    ObjectId fanLayerId = EnsureLayer(tr, db, FAN_LAYER, FAN_LAYER_COLOR);
                    ObjectId fanDefId   = bt[CUSTOM_FAN_BLOCK];
                    BlockReference fanBr = new BlockReference(
                        new Point3d(fanCx, fanCy, 0), fanDefId)
                    {
                        Rotation = 0.0,
                        ScaleFactors = new Scale3d(1.0),
                        LayerId = fanLayerId,
                    };
                    ms.AppendEntity(fanBr);
                    tr.AddNewlyCreatedDBObject(fanBr, true);

                    tracker.Add(fanBr.ObjectId);
                    dr.FanDrawn = true;
                }

                tr.Commit();
            }

            // Only after a successful commit do we promote the tracker into
            // the static state used by UNDOLIGHT. We APPEND (not replace) so
            // the multi-room LIGHT loop can accumulate ObjectIds from every
            // room into one undo group; LightCommand clears the list once at
            // the start of each invocation. `_lastInsertedLayer` is set by
            // the caller after the loop completes (it concatenates every
            // layer touched across the run).
            _lastInsertedIds.AddRange(tracker);

            dr.LayerUsed = finalLayer;
            return dr;
        }

        // Ensure the given layer exists; return its ObjectId. `colorIndex`
        // sets the ACI color when the layer has to be created (4 = cyan by
        // default, matching the original LIGHTS layer behaviour). Existing
        // layers are returned unchanged regardless of the requested colour.
        private static ObjectId EnsureLayer(Transaction tr, Database db, string name,
                                            short colorIndex = 4)
        {
            LayerTable lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(name)) return lt[name];

            lt.UpgradeOpen();
            LayerTableRecord ltr = new LayerTableRecord
            {
                Name = name,
                Color = Color.FromColorIndex(ColorMethod.ByAci, colorIndex)
            };
            ObjectId id = lt.Add(ltr);
            tr.AddNewlyCreatedDBObject(ltr, true);
            return id;
        }

        // Ensure the XDATA_APP_NAME is registered so we can attach XData.
        private static void EnsureXDataAppRegistered(Transaction tr, Database db)
        {
            RegAppTable rat = (RegAppTable)tr.GetObject(db.RegAppTableId, OpenMode.ForRead);
            if (rat.Has(XDATA_APP_NAME)) return;
            rat.UpgradeOpen();
            RegAppTableRecord ratr = new RegAppTableRecord { Name = XDATA_APP_NAME };
            rat.Add(ratr);
            tr.AddNewlyCreatedDBObject(ratr, true);
        }

        // Attach fixture attributes as XData "key=value" strings. Read back with:
        //   ResultBuffer rb = ent.GetXDataForApplication("LIGHTPLACEMENT");
        private static void AttachFixtureXData(Entity ent,
                                               Dictionary<string, object> attribs)
        {
            if (attribs == null || attribs.Count == 0) return;

            var values = new List<TypedValue>
            {
                new TypedValue((int)DxfCode.ExtendedDataRegAppName, XDATA_APP_NAME)
            };
            foreach (var kv in attribs)
            {
                string entry = kv.Key + "=" +
                               Convert.ToString(kv.Value, CultureInfo.InvariantCulture);
                // ASCII string XData values are limited to 255 chars — truncate.
                if (entry.Length > 255) entry = entry.Substring(0, 255);
                values.Add(new TypedValue(
                    (int)DxfCode.ExtendedDataAsciiString, entry));
            }
            using (ResultBuffer rb = new ResultBuffer(values.ToArray()))
            {
                ent.XData = rb;
            }
        }

        // -------- HTTP --------

        // GET /room-types and return the set of supported keys (e.g.,
        // "bedroom", "living_room", "office", …). The multi-room LIGHT loop
        // calls this once at the start of the invocation to (a) confirm the
        // backend is reachable before the user starts selecting polylines,
        // and (b) cache the valid room-type set for layer-name validation.
        private static async Task<HashSet<string>> FetchRoomTypes()
        {
            HttpResponseMessage resp = await _http.GetAsync(ROOM_TYPES_URL);
            string body = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
                throw new System.Exception(
                    "Backend " + (int)resp.StatusCode + ": " + body);
            var dict = (Dictionary<string, object>)
                new JavaScriptSerializer().DeserializeObject(body);
            return new HashSet<string>(dict.Keys);
        }

        private static async Task<string> CallDesign(
            string roomType,
            List<double[]> roomVertsMm,
            double fixtureLumens,
            List<List<double[]>> obstacles,
            bool useUfMf)
        {
            var sb = new StringBuilder();
            sb.Append("{");
            sb.Append("\"room_type\":\"").Append(Escape(roomType)).Append("\",");
            sb.Append("\"fixture_lumens\":")
              .Append(fixtureLumens.ToString("G17", CultureInfo.InvariantCulture))
              .Append(",");
            sb.Append("\"use_uf_mf\":").Append(useUfMf ? "true" : "false").Append(",");

            // Room polyline.
            AppendPolyline(sb, "polyline", roomVertsMm);

            // Obstacles: array of {polyline:[…], type:"other"}.
            if (obstacles != null && obstacles.Count > 0)
            {
                sb.Append(",\"obstacles\":[");
                for (int i = 0; i < obstacles.Count; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append("{");
                    AppendPolyline(sb, "polyline", obstacles[i]);
                    sb.Append(",\"type\":\"other\"");
                    sb.Append("}");
                }
                sb.Append("]");
            }

            sb.Append("}");
            return await PostJson(DESIGN_URL, sb.ToString());
        }

        private static void AppendPolyline(StringBuilder sb, string key,
                                           List<double[]> verts)
        {
            sb.Append("\"").Append(key).Append("\":[");
            for (int i = 0; i < verts.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("{\"x\":")
                  .Append(verts[i][0].ToString("G17", CultureInfo.InvariantCulture))
                  .Append(",\"y\":")
                  .Append(verts[i][1].ToString("G17", CultureInfo.InvariantCulture))
                  .Append("}");
            }
            sb.Append("]");
        }

        private static async Task<string> PostJson(string url, string body)
        {
            var content = new StringContent(body, Encoding.UTF8, "application/json");
            HttpResponseMessage resp = await _http.PostAsync(url, content);
            string respBody = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
                throw new System.Exception("Backend " + (int)resp.StatusCode + ": " + respBody);
            return respBody;
        }

        private static string Escape(string s) =>
            s.Replace("\\", "\\\\").Replace("\"", "\\\"");

        // -------- Backend autostart on connection failure --------

        private static void HandleBackendUnreachable(Editor ed, HttpRequestException hex)
        {
            string baseMsg = "Could not reach backend at " + BASE_URL +
                             ".\nIs the FastAPI server running?\n\n" + hex.Message;
            ed.WriteMessage("\n" + baseMsg);

            string cmd = ReadAutostartCommand();
            if (string.IsNullOrEmpty(cmd))
            {
                MessageBox.Show(
                    baseMsg + "\n\n(No '" + AUTOSTART_FILE_NAME +
                    "' file next to the DLL — autostart is not configured.)",
                    "Backend Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            DialogResult choice = MessageBox.Show(
                baseMsg + "\n\nStart it now using:\n  " + cmd,
                "Backend Error", MessageBoxButtons.YesNo, MessageBoxIcon.Question);

            if (choice != DialogResult.Yes) return;

            try
            {
                string dir = Path.GetDirectoryName(
                    Assembly.GetExecutingAssembly().Location);
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = "cmd.exe",
                    Arguments = "/C start \"FastAPI backend\" " + cmd,
                    WorkingDirectory = dir,
                    UseShellExecute = true,
                };
                Process.Start(psi);
                MessageBox.Show(
                    "Autostart launched in a new window. Wait a few seconds " +
                    "for it to bind to " + BASE_URL + ", then run LIGHT again.",
                    "Autostart", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            catch (System.Exception ex)
            {
                MessageBox.Show("Autostart failed: " + ex.Message,
                                "Autostart", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static string ReadAutostartCommand()
        {
            try
            {
                string dir = Path.GetDirectoryName(
                    Assembly.GetExecutingAssembly().Location);
                string path = Path.Combine(dir, AUTOSTART_FILE_NAME);
                if (!File.Exists(path)) return null;
                string cmd = File.ReadAllText(path).Trim();
                return string.IsNullOrWhiteSpace(cmd) ? null : cmd;
            }
            catch
            {
                return null;
            }
        }

        // ============================================================
        // Custom block definitions — geometry below is a faithful copy of
        // the C-LIGHT and CEILING FAN blocks from Drawing1.dxf, transcribed
        // by extract_blocks.py so the user's drawing template no longer
        // needs to be imported. The LIGHT geometry was pre-scaled 33.33x
        // at extract time so both blocks insert correctly at scale = 1.
        // ============================================================

        // Convenience wrappers so the auto-generated entity calls stay
        // single-line. Each one creates the entity, sets its colour, and
        // registers it with the open transaction.
        private static void AppendCircle(BlockTableRecord btr, Transaction tr,
                                         double cx, double cy, double r, Color color)
        {
            Circle c = new Circle(new Point3d(cx, cy, 0), Vector3d.ZAxis, r) { Color = color };
            btr.AppendEntity(c);
            tr.AddNewlyCreatedDBObject(c, true);
        }

        private static void AppendLine(BlockTableRecord btr, Transaction tr,
                                       double x1, double y1, double x2, double y2,
                                       Color color)
        {
            Line ln = new Line(new Point3d(x1, y1, 0), new Point3d(x2, y2, 0)) { Color = color };
            btr.AppendEntity(ln);
            tr.AddNewlyCreatedDBObject(ln, true);
        }

        // Donut: filled annulus between r_inner and r_outer, centred at (cx,cy).
        // Implemented as a 2-vertex closed polyline with bulge = 1.0 (two 180°
        // arcs) and segment width equal to the annulus thickness.
        private static void AppendDonut(BlockTableRecord btr, Transaction tr,
                                        double cx, double cy,
                                        double rInner, double rOuter,
                                        Color color)
        {
            double midR  = (rInner + rOuter) / 2.0;
            double width = rOuter - rInner;
            Polyline pl = new Polyline();
            pl.AddVertexAt(0, new Point2d(cx - midR, cy), 1.0, width, width);
            pl.AddVertexAt(1, new Point2d(cx + midR, cy), 1.0, width, width);
            pl.Closed = true;
            pl.Color = color;
            btr.AppendEntity(pl);
            tr.AddNewlyCreatedDBObject(pl, true);
        }

        // ----- LIGHT_FIXTURE_CUSTOM_V2 — clean recessed downlight in off-white -----
        // Replaces the V1 "C-LIGHT" transcription with a tidier construction:
        //   * outer trim ring at R=100,
        //   * diffuser-glow donut between R=55 and R=88 (the lit aperture),
        //   * solid LED dot at the centre,
        //   * four cardinal crosshair ticks extending from R=105 to R=135.
        // All entities use a TrueColor off-white (antique-white, RGB 250,240,220)
        // so the symbol reads as "lit fixture" on dark CAD backgrounds.
        private static void EnsureCustomLightBlock(Transaction tr, Database db)
        {
            BlockTable bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (bt.Has(CUSTOM_LIGHT_BLOCK)) return;

            bt.UpgradeOpen();
            BlockTableRecord btr = new BlockTableRecord
            {
                Name   = CUSTOM_LIGHT_BLOCK,
                Origin = Point3d.Origin,
            };
            bt.Add(btr);
            tr.AddNewlyCreatedDBObject(btr, true);

            Color offWhite = Color.FromRgb(250, 240, 220);

            // Outer trim ring + diffuser glow + central LED dot.
            AppendCircle(btr, tr, 0.0, 0.0, 100.0, offWhite);
            AppendDonut (btr, tr, 0.0, 0.0,  55.0,  88.0, offWhite);
            AppendCircle(btr, tr, 0.0, 0.0,  15.0, offWhite);

            // Four cardinal crosshair ticks.
            AppendLine(btr, tr, -135.0,    0.0, -105.0,    0.0, offWhite);
            AppendLine(btr, tr,  105.0,    0.0,  135.0,    0.0, offWhite);
            AppendLine(btr, tr,    0.0,  105.0,    0.0,  135.0, offWhite);
            AppendLine(btr, tr,    0.0, -135.0,    0.0, -105.0, offWhite);
        }

        // ----- CEILING_FAN_CUSTOM_V2 — clean 3-blade fan symbol, all white -----
        // Replaces the 51-entity raw transcription from Drawing1.dxf with a
        // tidy 6-entity construction:
        //   * outer sweep circle at FAN_RADIUS_MM (the no-fly zone),
        //   * three tapered blade outlines (closed polylines) at 0°/120°/240°,
        //   * a motor-housing ring (donut) plus a solid cap at the centre.
        // ACI 7 (white on dark, black on light) so the symbol stays readable
        // on either CAD theme.
        private static void EnsureCustomFanBlock(Transaction tr, Database db)
        {
            BlockTable bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (bt.Has(CUSTOM_FAN_BLOCK)) return;

            bt.UpgradeOpen();
            BlockTableRecord btr = new BlockTableRecord
            {
                Name   = CUSTOM_FAN_BLOCK,
                Origin = Point3d.Origin,
            };
            bt.Add(btr);
            tr.AddNewlyCreatedDBObject(btr, true);

            Color white = Color.FromColorIndex(ColorMethod.ByAci, 7);

            // Outer blade-sweep circle.
            AppendCircle(btr, tr, 0.0, 0.0, FAN_RADIUS_MM, white);

            // Blade profile (one blade, pointing along +X before rotation).
            // 4 corners: root-right, tip-right, tip-left, root-left.
            // Slightly wider at the tip than at the root for a natural taper.
            double[][] bladeProfile = new double[][]
            {
                new[] {  95.0, -55.0 },
                new[] { 560.0, -90.0 },
                new[] { 560.0,  90.0 },
                new[] {  95.0,  55.0 },
            };

            for (int b = 0; b < 3; b++)
            {
                double theta = b * 2.0 * Math.PI / 3.0;
                double ca = Math.Cos(theta);
                double sa = Math.Sin(theta);
                Polyline blade = new Polyline();
                for (int i = 0; i < bladeProfile.Length; i++)
                {
                    double bx = bladeProfile[i][0];
                    double by = bladeProfile[i][1];
                    double rx = bx * ca - by * sa;
                    double ry = bx * sa + by * ca;
                    blade.AddVertexAt(i, new Point2d(rx, ry), 0.0, 0.0, 0.0);
                }
                blade.Closed = true;
                blade.Color = white;
                btr.AppendEntity(blade);
                tr.AddNewlyCreatedDBObject(blade, true);
            }

            // Motor housing — a thin ring and a solid centre cap.
            AppendDonut (btr, tr, 0.0, 0.0,
                         FAN_HUB_RADIUS_MM * 0.55, FAN_HUB_RADIUS_MM, white);
            AppendCircle(btr, tr, 0.0, 0.0, FAN_HUB_RADIUS_MM * 0.30, white);
        }

        // ============================================================
        // Polygon geometry helpers used to anchor the fan.
        // ============================================================

        // Shoelace polygon centroid (mm). Falls back to vertex mean if the
        // polygon is degenerate / zero-area.
        private static void PolygonCentroidMm(List<double[]> verts,
                                              out double cx, out double cy)
        {
            int n = verts.Count;
            double signed2 = 0.0;
            double sx = 0.0, sy = 0.0;
            for (int i = 0; i < n; i++)
            {
                double x0 = verts[i][0], y0 = verts[i][1];
                double x1 = verts[(i + 1) % n][0], y1 = verts[(i + 1) % n][1];
                double cross = x0 * y1 - x1 * y0;
                signed2 += cross;
                sx += (x0 + x1) * cross;
                sy += (y0 + y1) * cross;
            }
            if (Math.Abs(signed2) < 1e-9)
            {
                double mx = 0, my = 0;
                foreach (var v in verts) { mx += v[0]; my += v[1]; }
                cx = (n > 0) ? mx / n : 0.0;
                cy = (n > 0) ? my / n : 0.0;
                return;
            }
            cx = sx / (3.0 * signed2);
            cy = sy / (3.0 * signed2);
        }

        // Standard ray-casting point-in-polygon. Used to verify the centroid /
        // bbox-centre falls inside an L-/U-shaped room before planting a fan.
        private static bool PointInPolygonMm(double x, double y, List<double[]> verts)
        {
            int n = verts.Count;
            if (n < 3) return false;
            bool inside = false;
            int j = n - 1;
            for (int i = 0; i < n; i++)
            {
                double xi = verts[i][0], yi = verts[i][1];
                double xj = verts[j][0], yj = verts[j][1];
                if (((yi > y) != (yj > y)) &&
                    (x < (xj - xi) * (y - yi) / (yj - yi) + xi))
                {
                    inside = !inside;
                }
                j = i;
            }
            return inside;
        }

        // Shortest distance from (x, y) to any edge of the closed polygon.
        // Used by TryRelocateOutsideFan to keep relocated lights off the walls.
        private static double MinDistanceToPolygonMm(double x, double y,
                                                     List<double[]> verts)
        {
            int n = verts.Count;
            double best = double.PositiveInfinity;
            for (int i = 0; i < n; i++)
            {
                double ax = verts[i][0], ay = verts[i][1];
                double bx = verts[(i + 1) % n][0], by = verts[(i + 1) % n][1];
                double dx = bx - ax, dy = by - ay;
                double t;
                if (dx == 0.0 && dy == 0.0)
                {
                    t = 0.0;
                }
                else
                {
                    t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy);
                    if (t < 0.0) t = 0.0;
                    else if (t > 1.0) t = 1.0;
                }
                double fx = ax + t * dx, fy = ay + t * dy;
                double d = Math.Sqrt((x - fx) * (x - fx) + (y - fy) * (y - fy));
                if (d < best) best = d;
            }
            return best;
        }

        // Find a position just outside the fan exclusion ring that's
        // (a) inside the room polygon, (b) at least FAN_RELOCATE_WALL_CLEARANCE_MM
        // from any wall, and (c) at least FAN_RELOCATE_MIN_SEPARATION_MM from
        // every already-kept light. Tries the original push direction first,
        // then 12 evenly-spaced compass directions as fallbacks. Returns false
        // if no candidate works (room too small / too cluttered).
        private static bool TryRelocateOutsideFan(
            double origX, double origY,
            double fanCx, double fanCy,
            double exclusionRadius,
            List<double[]> roomVertsMm,
            List<double[]> existingPositions,
            out double newX, out double newY)
        {
            double ringR = exclusionRadius + FAN_RELOCATE_RING_MARGIN_MM;

            var dirs = new List<double[]>();
            double vx = origX - fanCx, vy = origY - fanCy;
            double vlen = Math.Sqrt(vx * vx + vy * vy);
            if (vlen > 1e-6)
            {
                dirs.Add(new[] { vx / vlen, vy / vlen });
            }

            const int compassSteps = 12;
            for (int i = 0; i < compassSteps; i++)
            {
                double angle = 2.0 * Math.PI * i / compassSteps;
                dirs.Add(new[] { Math.Cos(angle), Math.Sin(angle) });
            }

            foreach (var d in dirs)
            {
                double cx = fanCx + d[0] * ringR;
                double cy = fanCy + d[1] * ringR;
                if (!PointInPolygonMm(cx, cy, roomVertsMm)) continue;
                if (MinDistanceToPolygonMm(cx, cy, roomVertsMm)
                        < FAN_RELOCATE_WALL_CLEARANCE_MM) continue;
                bool tooClose = false;
                foreach (var ep in existingPositions)
                {
                    double ddx = cx - ep[0], ddy = cy - ep[1];
                    if (Math.Sqrt(ddx * ddx + ddy * ddy)
                            < FAN_RELOCATE_MIN_SEPARATION_MM)
                    {
                        tooClose = true;
                        break;
                    }
                }
                if (tooClose) continue;

                newX = cx;
                newY = cy;
                return true;
            }

            newX = 0.0;
            newY = 0.0;
            return false;
        }
    }
}
