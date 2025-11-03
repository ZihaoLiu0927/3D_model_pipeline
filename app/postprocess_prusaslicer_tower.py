#!/usr/bin/env python3
"""
Post-processing script for PrusaSlicer G-code:
- Summarizes Model vs Wipe Tower extrusion
- Adds a report to G-code header

Usage:
    python postprocess_prusaslicer_tower.py <input.gcode>

You can also register this file in PrusaSlicer:
  Print Settings → Output options → Post-processing scripts
"""

import re
import sys
from pathlib import Path

def parse_extrusion(gcode_lines):
    e_model, e_tower = 0.0, 0.0
    e_last, in_tower = 0.0, False
    e_pattern = re.compile(r"(?<=\sE)([-+]?[0-9]*\.?[0-9]+)")
    tower_tag = re.compile(r";TYPE:(Wipe tower|Wipe Tower|WIPE TOWER)", re.IGNORECASE)

    for line in gcode_lines:
        # Tower tag switches mode
        if tower_tag.search(line):
            in_tower = True
        elif line.startswith(";TYPE:"):
            in_tower = False

        # accumulate extrusion
        m = e_pattern.search(line)
        if m:
            e_val = float(m.group(1))
            if e_val >= e_last:  # extrusion move
                delta = e_val - e_last
                if in_tower:
                    e_tower += delta
                else:
                    e_model += delta
            e_last = e_val
        elif line.startswith("G92 E0"):
            e_last = 0.0
    return e_model, e_tower

def main():
    if len(sys.argv) < 2:
        print("Usage: postprocess_prusaslicer_tower.py <file.gcode>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Error: {path} not found")
        sys.exit(2)

    lines = path.read_text().splitlines()
    e_model, e_tower = parse_extrusion(lines)
    e_total = e_model + e_tower

    header_report = [
        f"; ---- Postprocess summary ----",
        f"; Model extrusion (E): {e_model:.2f}",
        f"; Tower extrusion (E): {e_tower:.2f}",
        f"; Total extrusion (E): {e_total:.2f}",
        f"; -----------------------------",
    ]

    # Insert after the first existing header block (before first G-code)
    insert_index = next(
        (i for i, l in enumerate(lines) if not l.startswith(";")), len(lines)
    )
    new_content = lines[:insert_index] + header_report + [""] + lines[insert_index:]
    path.write_text("\n".join(new_content))

    print(f"Updated: {path}")
    print("\n".join(header_report))

if __name__ == "__main__":
    main()
