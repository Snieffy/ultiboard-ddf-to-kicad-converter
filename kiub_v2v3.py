# kiub_v2v3.py  –  Ultiboard DDF V2/V3 → V4.60 pre-processor for KIUB
# Copyright (C) 2026  Snieffy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://gnu.org>.

"""
kiub_v2v3: Ultiboard DDF V2/V3 → V4.60 pre-processor.

Primary API (used by kiub.py):
    converted_str = convert_str(source_str)   # str → str

Standalone CLI:
    python kiub_v2v3.py input[.DDF] [output_V4.DDF]

    - The .DDF extension is added automatically if omitted.
    - If only the input filename is given, the output filename is derived
      automatically by appending _V4 before the extension:
          mon330.DDF  →  mon330_V4.DDF
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROT_MAP = {
    0: 0, 1: 17280, 2: 11520, 3: 5760,
    4: -23040, 5: -5760, 6: -11520, 7: -17280
}


def nums(line):
    return list(map(int, re.findall(r'-?\d+', line)))


class DDFConverter:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0
        self.out = []

        self.shapes = []
        self.board_w = 0
        self.board_h = 0

        self.t_store = {"TD": [], "T0": [], "T1": [], "T2": []}

    # =========================================================
    def convert(self):
        self.srecord_start = False
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()

            if line.startswith("*P"):
                self.handle_P()

            elif line.startswith("*T"):
                self.handle_T()

            elif line.startswith("*S"):
                if not self.srecord_start:
                    self.emit_TS_and_SBOARD()
                    self.srecord_start = True
                self.handle_S()

            elif line.startswith("*N"):
                self.copy_block()

            elif line.startswith("*C"):
                self.handle_C()

            elif line.startswith("*LH") or line.startswith("*LV"):
                self.handle_LH_LV()

            elif line.startswith("*V"):
                self.handle_V()

            elif line.startswith("*X"):
                self.handle_X()

            else:
                self.i += 1

        return "\n".join(self.out)

    # =========================================================
    # *P
    def handle_P(self):
        header = self.lines[self.i].strip()
        self.i += 1

        self.i += 1  # skip version line

        dims = self.lines[self.i].strip()
        self.i += 1

        n = nums(dims)
        self.board_w, self.board_h = n[0], n[1]

        self.out.append(header)
        self.out.append("4 60")
        self.out.append(f"{self.board_w}, {self.board_h}, 0, 0, 6, 0, 22;")
        self.out.append("(|+|+|+|+|+|+|+|+|+|+|)")
        self.out.append("0, 0")
        self.out.append("240 0 0 15 30 1")
        self.out.append(" ".join(["1 2"] * 16))

        for _ in range(5):
            self.out.append("65535 65535 65535 65535 65535 65535")
        self.out.append("65535 65535")

    # =========================================================
    # *T
    def handle_T(self):
        block = []

        # -----------------------------
        # COLLECT *T* BLOCK
        # -----------------------------
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()

            if not line.startswith("*T"):
                break

            block.append(line)
            self.i += 1

        # -----------------------------
        # PROCESS INNER RECORDS
        # -----------------------------
        td_block = []
        td_dups  = []

        t_blocks = {"T0": [], "T1": [], "T2": []}
        t_dups   = {"T0": [], "T1": [], "T2": []}

        for line in block:
            rec = line.split()[0][1:]

            # -------------------------
            # PASS-THROUGH
            # -------------------------
            if rec in ("TP", "TT", "TC"):
                if line.startswith("*TP"):
                    line = '*TP ffffffff'
                if line.startswith("*TC"):
                    for r in range(16, 32):
                        self.out.append(f"*TT {r}, 0, 30")
                self.out.append(line)
                continue

            # -------------------------
            # TD BLOCK
            # -------------------------
            if rec == "TD":
                vals = re.findall(r'-?\d+', line)
                if len(vals) >= 2:
                    idx     = int(line.split(",", 1)[0].split()[1])
                    val     = int(vals[1])
                    new_val = int(val * 1200 / 254)
                    new_line = f"*TD {idx}, {new_val}"
                else:
                    new_line = line

                td_block.append(new_line)

                # prepare duplicate (0–15 → 240–255)
                if len(vals) >= 2 and idx < 16:
                    td_dups.append(f"*TD {idx + 240}, {new_val}")

                continue

            # -------------------------
            # T0 / T1 / T2 BLOCKS
            # -------------------------
            if rec in ("T0", "T1", "T2"):

                try:
                    prefix, rest = line.split(",", 1)
                    idx = int(prefix.split()[1])
                except (ValueError, IndexError):
                    t_blocks[rec].append(line)
                    continue

                vals = [v.strip() for v in rest.split(",")]

                if len(vals) < 9:  # original format has fewer fields
                    rest = rest.rstrip() + ", 0, 0, 0, 0"

                new_line = prefix + "," + rest
                t_blocks[rec].append(new_line)

                # DUPLICATE 0–15 → 240–255
                if idx < 16:
                    prefix_parts    = prefix.split()
                    prefix_parts[1] = str(idx + 240)
                    t_dups[rec].append(" ".join(prefix_parts) + "," + rest)

                continue

        # -------------------------
        # EMIT GROUPED BLOCKS
        # -------------------------
        self.out.extend(td_block)
        self.out.extend(td_dups)

        for rec in ("T0", "T1", "T2"):
            self.out.extend(t_blocks[rec])
            self.out.extend(t_dups[rec])

    # =========================================================
    # *S
    def handle_S(self):
        header = self.lines[self.i].strip()

        # --------------------------------------------------
        # IGNORE SBOARD (NOT indexed)
        if header.startswith("*SBOARD"):
            self.copy_block()
            return

        # --------------------------------------------------
        # HANDLE SNO_SHP
        if header.startswith("*SNO_SHP"):
            self.i += 1
            while self.i < len(self.lines):
                if self.lines[self.i].startswith("*"):
                    break
                self.i += 1

            self.shapes.append({"name": "*SNO_SHP", "X": 30, "Y": -150, "Height": 100, "Rot": 0})

            self.out.extend([
                "*SNO_SHP",
                "30 -150 100 0 80 100",
                "30 -300 100 0 80 100",
                "0.000000",
                "61,180,60,450,-59,480,-60,180;",
                "1,17280,ffffffff,0,0,1;",
                "0,0,60,0,23040,",
                "0,180,60,11520,11520,",
                "-300,780,60,11520,11520,",
                "150,420,95,6940,3399,",
                "-61,751,301,19639,14553,",
                "59,484,120,6658,4862,",
                "-64,756,182,19262,14691;",
                ";"
            ])
            return

        # --------------------------------------------------
        # NORMAL SHAPE
        self.out.append(header)
        self.i += 1

        meta = self.lines[self.i].strip()
        self.i += 1

        n   = nums(meta)
        h   = n[2]
        rot = ROT_MAP.get(n[3], 0)

        self.shapes.append({"name": header[2:], "X": n[0], "Y": n[1], "Height": h, "Rot": rot})

        self.out.append(f"{n[0]} {n[1]} {h} {rot} {h} 100")
        self.out.append(f"{n[0]} {n[1]} {h} {rot} {h} 100")
        self.out.append("0.000000")

        # --------------------------------------------------
        # COLLECT UNTIL NEXT '*'
        collected = []
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()
            if line.startswith("*"):
                break
            collected.append(line)
            self.i += 1

        # --------------------------------------------------
        # SPLIT INTO OUTLINE / PADS
        outline = []
        pads    = []
        mode    = "outline"

        for line in collected:
            if line == ";":
                mode = "pads"
                continue

            if mode == "outline":
                outline.append(line)
                if line.endswith(";"):
                    # if text x,y = 0,0: center text in shape outline
                    if self.shapes[-1]['X'] == 0 and self.shapes[-1]['Y'] == 0:
                        shape_center = [
                            (lambda v: (min(v) + max(v)) // 2)(
                                [int(n) for n in outline[0].strip(';').split(',')][i::2]
                            )
                            for i in (0, 1)
                        ]
                        self.shapes[-1]['X'] = shape_center[0]
                        self.shapes[-1]['Y'] = shape_center[1]
                    mode = "pads"
            else:
                pads.append(line)

        # --------------------------------------------------
        # OUTLINE (ensure exactly one ';')
        if outline:
            for l in outline[:-1]:
                self.out.append(l.rstrip(","))
            last = outline[-1].rstrip(",")
            if not last.endswith(";"):
                last += ";"
            self.out.append(last)
        else:
            self.out.append(";")

        # --------------------------------------------------
        # PADS
        pin = 1

        if pads:
            for i, l in enumerate(pads):
                line    = l.strip()
                if not line:
                    continue
                is_last = (i == len(pads) - 1)
                if line.endswith(";"):
                    line = line[:-1]
                # mapping V2/V3 to V4: rotation and pad layerset
                parts    = line.split(',')
                parts[1] = str(ROT_MAP[int(parts[1])])
                parts[2] = f"{(int(parts[2], 16) >> 12):08x}"
                line     = ",".join(parts)

                if is_last:
                    if not line.endswith(","):
                        line += ","
                    self.out.append(f"{line}{pin};")
                else:
                    self.out.append(f"{line}{pin},")

                pin += 1
        else:
            self.out.append(";")

        self.out.append(";\n;")

    # =========================================================
    def handle_C(self):
        header = self.lines[self.i].strip()
        self.i += 1

        data = self.lines[self.i].strip()
        self.i += 1

        n        = nums(data)
        shape_id = n[0]
        x, y     = n[1], n[2]
        rot      = ROT_MAP.get(n[3], 0)

        # Signed integer correction for the reference x,y values
        n_x        = (n[4] if n[4] <= 32768 else n[4] - 65536) + self.shapes[shape_id]['X']
        n_y        = (n[5] if n[5] <= 32768 else n[5] - 65536) + self.shapes[shape_id]['Y']
        n_h        = self.shapes[shape_id]['Height']
        n_w        = int(n_h * 0.8)
        n_t        = int(n_h / 6)
        n_rot      = ROT_MAP.get(self.shapes[shape_id]['Rot'], 0)
        shape_name = self.shapes[shape_id]['name']

        # --------------------------------------------------
        # Rewrite header
        self.out.append(header + " " + shape_name)
        # Replace data line (REMOVE shape_id)
        # Position line: <x>,<y>,<rotation>,<name_x>,<name_y>,<name_rot>,<name_w>,<name_h>,<name_thick>,
        #                <alias_x>,<alias_y>,<alias_rot>,<alias_w>,<alias_h>,<alias_thick>
        self.out.append(
            f"{x},{y},{rot},{n_x},{n_y},{n_rot},{n_w},{n_h},{n_t},"
            f"{n_x},{n_y},{n_rot},{n_w},{n_h},{n_t}"
        )
        self.out.append("0,0,0,0,0,0,0")

        # --------------------------------------------------
        # Read net lines until NEXT RECORD (line starting with '*')
        net_lines = []
        while self.i < len(self.lines):
            line = self.lines[self.i].rstrip("\n")
            if line.startswith("*"):
                break
            # pad layerset mapping V2/V3 to V4
            line = ' '.join(
                f"{(int(v, 16) >> 12):08x}" if i % 2 == 1 else v
                for i, v in enumerate(line.split())
            )
            net_lines.append(line.strip())
            self.i += 1

        net_lines[-1] += "\n;"
        self.out.extend(net_lines)

    # =========================================================
    # *LH / *LV
    def handle_LH_LV(self):
        header = self.lines[self.i].strip()
        is_LH  = header.startswith("*LH")

        self.out.append("*LT" + header[3:])
        self.i += 1

        while True:
            raw     = self.lines[self.i].strip()
            is_last = raw.endswith(";")
            line    = raw[:-1] if is_last else raw
            parts   = line.split()

            if parts:
                trace = parts[-1]
                if trace.endswith(("F", "V")):
                    parts[-1] = trace[:-1]
                parts.extend(["0", "1" if is_LH else "2"])

            new_line = " ".join(parts)
            if is_last:
                new_line += ";"

            self.out.append(new_line)
            self.i += 1

            if is_last:
                break

    # =========================================================
    # *V
    def handle_V(self):
        self.out.append(self.lines[self.i].strip())
        self.i += 1

        while True:
            line          = self.lines[self.i].strip()
            has_semicolon = line.endswith(";")
            if has_semicolon:
                line = line[:-1]

            parts = line.split()
            if parts and parts[-1].lower() == "fffff000":
                # pad layerset mapping V2/V3 to V4
                parts[-1] = f"{(int(parts[-1], 16) >> 12):08x}"

            line_fixed = " ".join(parts)

            if has_semicolon:
                self.out.append(line_fixed + " 0 0 0 1;")
                self.i += 1
                break
            else:
                self.out.append(line_fixed + " 0 0 0 1")
                self.i += 1

    # =========================================================
    # *X
    def handle_X(self):
        line = self.lines[self.i].rstrip("\n")
        self.i += 1
        # extract first 5 numeric fields only
        match = re.match(r'\*X\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.*)', line)
        if not match:
            self.out.append(line)
            return

        x, y, h, layer, rot, text = match.groups()
        self.out.append(
            f"*X {int(x)} {int(y)} {int(h)} {int(h)} 100 "
            f"{ROT_MAP.get(int(rot), 0)} {int(layer) - 1} {text}"
        )

    # =========================================================
    def emit_TS_and_SBOARD(self):
        self.out.append("*TS H 0 0")

        def odd(x):  return x if x % 2      else x + 1
        def even(x): return x if x % 2 == 0 else x + 1

        w, h = self.board_w, self.board_h
        seg  = [
            (odd(0),  0, even(w), 0),
            (odd(w),  0, even(w), h),
            (odd(w),  h, even(0), h),
            (odd(0),  h, even(0), 0),
        ]

        self.out.extend([
            "*SBOARD",
            "60 90 100 0 100 100",
            "0 0 0 0 0 100",
            "0.000000",
            ",".join(f"{a},{b},{c},{d}" for a, b, c, d in seg) + ";",
            ";\n;"
        ])

    # =========================================================
    def copy_block(self):
        while True:
            line = self.lines[self.i].rstrip("\n")
            self.out.append(line)
            self.i += 1
            if line.endswith(";"):
                break


# =============================================================
# Public API used by kiub.py
# =============================================================

def convert_str(source: str) -> str:
    """Convert a V2/V3 DDF string to a V4.60 DDF string.

    Parameters
    ----------
    source : str
        Full contents of a V2/V3 DDF file decoded as CP437.

    Returns
    -------
    str
        Equivalent V4.60 DDF content as a plain string.
    """
    return DDFConverter(source.splitlines(keepends=True)).convert()


# =============================================================
# Standalone CLI
# =============================================================

def _convert_file(inp: str, outp: str) -> None:
    """Read *inp*, convert V2/V3 → V4.60, write to *outp*."""
    with open(inp, "r", encoding="cp437", errors="ignore") as f:
        source = f.read()
    with open(outp, "w", encoding="cp437") as f:
        f.write(convert_str(source) + "\n")


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        description="Convert an Ultiboard V2/V3 DDF file to V4.60 format."
    )
    _parser.add_argument(
        "infile",
        help="Input DDF file (the .DDF extension is added automatically if omitted).",
    )
    _parser.add_argument(
        "outfile",
        nargs="?",
        default=None,
        help="Output DDF file (default: <infile>_V4.DDF).",
    )
    _args = _parser.parse_args()

    # Add .DDF extension if missing
    if not _args.infile.lower().endswith(".ddf"):
        _args.infile += ".DDF"

    # Validate input file
    if not os.path.exists(_args.infile):
        print(f"Error: File '{_args.infile}' does not exist.")
        sys.exit(1)

    # Derive output filename when not supplied
    if _args.outfile is None:
        _args.outfile = _args.infile[:-4] + "_V4.DDF"

    _convert_file(_args.infile, _args.outfile)
    print(f"Done: {_args.infile}  →  {_args.outfile}")
