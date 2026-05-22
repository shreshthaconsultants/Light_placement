"""
One-shot codegen: parse the LIGHT and FAN block definitions out of
Drawing1.dxf and emit C# helper methods (EnsureCustomLightBlock_FromDxf,
EnsureCustomFanBlock_FromDxf) that recreate those blocks programmatically.

DXF is a list of (group-code, value) pairs. Each entity is delimited by a
`0 <type>` marker. We only support the entity types actually used by the
two blocks in this drawing: CIRCLE, LINE, HATCH (solid annular fill).

Run:    python extract_blocks.py
Writes: generated_blocks.cs
"""

from __future__ import annotations
import os
from typing import List, Tuple, Optional

DXF_PATH = os.path.join(os.path.dirname(__file__), "Drawing1.dxf")

# Insertion-time scales observed in the DXF.  Baking these into the block
# definition lets the plugin insert with scale=1.0 and still get mm-sized
# symbols.
LIGHT_SCALE = 33.333333  # C-LIGHT block was inserted at ~33.33x in the source drawing
FAN_SCALE   = 1.0        # CEILING FAN was inserted at 1.0 (already mm)


def load_pairs(path: str) -> List[Tuple[int, str]]:
    """Return DXF as a list of (code, value)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = [ln.rstrip("\r\n") for ln in f]
    pairs: List[Tuple[int, str]] = []
    i = 0
    while i + 1 < len(raw):
        code_s = raw[i].strip()
        try:
            code = int(code_s)
        except ValueError:
            i += 1
            continue
        pairs.append((code, raw[i + 1]))
        i += 2
    return pairs


def find_block(pairs, name: str) -> Tuple[int, int]:
    """Return (start, end) index range into pairs covering one BLOCK by name."""
    i = 0
    while i < len(pairs):
        code, value = pairs[i]
        if code == 0 and value == "BLOCK":
            # name is in the next `2` code
            j = i + 1
            bname = None
            while j < len(pairs):
                c2, v2 = pairs[j]
                if c2 == 0:
                    break
                if c2 == 2 and bname is None:
                    bname = v2
                j += 1
            if bname == name:
                # Find ENDBLK
                k = i + 1
                while k < len(pairs):
                    c3, v3 = pairs[k]
                    if c3 == 0 and v3 == "ENDBLK":
                        return (i, k)
                    k += 1
        i += 1
    raise RuntimeError(f"Block {name!r} not found")


def parse_entities(pairs, start: int, end: int, scale: float):
    """Walk pairs[start..end] and yield entity dicts."""
    entities = []
    i = start + 1
    while i < end:
        code, value = pairs[i]
        if code == 0 and value in ("CIRCLE", "LINE", "HATCH"):
            ent = {"type": value}
            i += 1
            # Hatch boundary parsing carries state across multiple paths.
            hatch_paths = []
            current_path = None
            while i < end:
                c2, v2 = pairs[i]
                if c2 == 0:
                    break
                if value == "HATCH":
                    # Hatch boundary parsing — we only handle CIRCLE edges (72=2)
                    if c2 == 91:
                        ent["n_paths"] = int(v2)
                    elif c2 == 92:
                        if current_path:
                            hatch_paths.append(current_path)
                        current_path = {"flags": int(v2), "edges": []}
                    elif c2 == 93 and current_path is not None:
                        current_path["n_edges"] = int(v2)
                    elif c2 == 72 and current_path is not None:
                        current_path["_edge_type"] = int(v2)
                    elif c2 == 10 and current_path is not None and current_path.get("_edge_type") == 2:
                        current_path.setdefault("cx", float(v2) * scale)
                    elif c2 == 20 and current_path is not None and current_path.get("_edge_type") == 2:
                        current_path.setdefault("cy", float(v2) * scale)
                    elif c2 == 40 and current_path is not None and current_path.get("_edge_type") == 2:
                        current_path.setdefault("r", float(v2) * scale)
                    elif c2 == 62:
                        ent["color"] = int(v2)
                else:
                    if c2 == 10:   ent["x1"]    = float(v2) * scale
                    elif c2 == 20: ent["y1"]    = float(v2) * scale
                    elif c2 == 11: ent["x2"]    = float(v2) * scale
                    elif c2 == 21: ent["y2"]    = float(v2) * scale
                    elif c2 == 40: ent["radius"] = float(v2) * scale
                    elif c2 == 62: ent["color"]  = int(v2)
                i += 1
            if value == "HATCH":
                if current_path:
                    hatch_paths.append(current_path)
                # Take the two circular paths as inner/outer of an annulus.
                radii = sorted([p["r"] for p in hatch_paths if "r" in p])
                if len(radii) == 2:
                    cx = next((p["cx"] for p in hatch_paths if "cx" in p), 0.0)
                    cy = next((p["cy"] for p in hatch_paths if "cy" in p), 0.0)
                    ent["donut"] = {
                        "cx": cx, "cy": cy,
                        "r_inner": radii[0], "r_outer": radii[1],
                    }
            entities.append(ent)
            continue
        i += 1
    return entities


def emit_circle(ent):
    cx = ent.get("x1", 0.0)
    cy = ent.get("y1", 0.0)
    r = ent.get("radius", 0.0)
    color = ent.get("color", 1)
    return (f"        AppendCircle(btr, tr, {cx:.4f}, {cy:.4f}, {r:.4f}, "
            f"Color.FromColorIndex(ColorMethod.ByAci, {color}));")


def emit_line(ent):
    x1 = ent.get("x1", 0.0); y1 = ent.get("y1", 0.0)
    x2 = ent.get("x2", 0.0); y2 = ent.get("y2", 0.0)
    color = ent.get("color", 1)
    return (f"        AppendLine(btr, tr, {x1:.4f}, {y1:.4f}, {x2:.4f}, {y2:.4f}, "
            f"Color.FromColorIndex(ColorMethod.ByAci, {color}));")


def emit_donut(ent):
    d = ent["donut"]
    cx = d["cx"]; cy = d["cy"]
    r_in = d["r_inner"]; r_out = d["r_outer"]
    color = ent.get("color", 1)
    return (f"        AppendDonut(btr, tr, {cx:.4f}, {cy:.4f}, {r_in:.4f}, {r_out:.4f}, "
            f"Color.FromColorIndex(ColorMethod.ByAci, {color}));")


def emit_block(method_name: str, block_const_name: str, entities) -> str:
    lines = []
    lines.append(f"        private static void {method_name}(Transaction tr, Database db)")
    lines.append("        {")
    lines.append("            BlockTable bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);")
    lines.append(f"            if (bt.Has({block_const_name})) return;")
    lines.append("")
    lines.append("            bt.UpgradeOpen();")
    lines.append("            BlockTableRecord btr = new BlockTableRecord")
    lines.append("            {")
    lines.append(f"                Name   = {block_const_name},")
    lines.append("                Origin = Point3d.Origin,")
    lines.append("            };")
    lines.append("            bt.Add(btr);")
    lines.append("            tr.AddNewlyCreatedDBObject(btr, true);")
    lines.append("")
    for ent in entities:
        t = ent["type"]
        if t == "CIRCLE":
            lines.append(emit_circle(ent))
        elif t == "LINE":
            lines.append(emit_line(ent))
        elif t == "HATCH" and "donut" in ent:
            lines.append(emit_donut(ent))
    lines.append("        }")
    return "\n".join(lines)


def main():
    pairs = load_pairs(DXF_PATH)

    light_start, light_end = find_block(pairs, "C-LIGHT")
    fan_start,   fan_end   = find_block(pairs, "CEILING FAN")

    light_entities = parse_entities(pairs, light_start, light_end, LIGHT_SCALE)
    fan_entities   = parse_entities(pairs, fan_start,   fan_end,   FAN_SCALE)

    print(f"// LIGHT  block: {len(light_entities)} entities")
    print(f"// FAN    block: {len(fan_entities)} entities")

    cs = []
    cs.append("// --- AUTO-GENERATED by extract_blocks.py from Drawing1.dxf ---")
    cs.append("// Faithful replica of the C-LIGHT and CEILING FAN blocks. The")
    cs.append("// LIGHT block has been pre-scaled by 33.33x so it inserts at mm")
    cs.append("// scale 1.0 — the same visual size as in the source drawing.")
    cs.append("")
    cs.append(emit_block("EnsureCustomLightBlock_FromDxf",
                         "CUSTOM_LIGHT_BLOCK", light_entities))
    cs.append("")
    cs.append(emit_block("EnsureCustomFanBlock_FromDxf",
                         "CUSTOM_FAN_BLOCK", fan_entities))
    cs.append("")

    out_path = os.path.join(os.path.dirname(__file__), "generated_blocks.cs")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(cs))
    print(f"\nWrote {out_path} ({len(cs)} lines)")


if __name__ == "__main__":
    main()
