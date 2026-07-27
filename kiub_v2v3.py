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

# V2/V3 rotation code (0-7) -> V4.60 signed degrees*64 value.
# Side and angle share one small code: 0-3 = top layer, 4-7 = bottom layer
# (sign of the resulting value is what actually signals "bottom" downstream,
# see kiub.py's *C/*X handling). NOT a simple code*90 progression on either
# half -- confirmed against Ultiboard V5/V5.72 directly (opening a test
# board built with one shape per rotation code):
#   top    (codes 0-3): 0 deg, 270 deg, 180 deg,  90 deg  (angle runs BACKWARDS)
#   bottom (codes 4-7): 0 deg,  90 deg, 180 deg, 270 deg  (angle runs FORWARDS)
ROT_MAP = {
    0: 0, 1: 17280, 2: 11520, 3: 5760,
    4: -23040, 5: -5760, 6: -11520, 7: -17280
}

# ---------------------------------------------------------------------------
# Fine-tunable text-geometry estimation ratios (see kiub.py's FINE_TUNING_SPEC,
# entries 'v2v3_text_width_ratio' / 'v2v3_text_thickness_ratio'). Unlike V4/V5,
# V2/V3 DDFs only store a text *height* per shape/alias/component-text record
# -- width and stroke thickness aren't stored at all, so both are estimated
# from height using these empirically-fitted ratios. When this module is
# loaded by kiub.py's open_ddf(), these two module attributes may be
# overridden per-conversion from a CLI flag or the GUI's "Fine-tuning…"
# dialog; running this file standalone always uses the defaults below.
TEXT_WIDTH_RATIO: float = 0.8       # estimated text width = height * this
TEXT_THICKNESS_RATIO: float = 0.1667  # estimated text stroke thickness = height * this


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

        self.i += 1  # skip version line -- major/minor is only used upstream
                      # (kiub.py's open_ddf) to decide whether to invoke this
                      # pre-converter at all; its content is discarded here
                      # and replaced unconditionally with "4 60" below.

        dims = self.lines[self.i].strip()
        self.i += 1

        # V2/V3's own bounds line is "<width>, <height>, <grid>, <field4>;"
        # (NOT a pair of outline corners like V4/V5's own bounds line) --
        # only the first two fields (board width/height, in the same
        # database-unit system as everything else) are used. <grid> and
        # <field4> are read here via the same regex but never assigned,
        # since kiub.py itself never reads either field back out.
        n = nums(dims)
        self.board_w, self.board_h = n[0], n[1]

        self.out.append(header)
        self.out.append("4 60")
        # Everything from here down is fabricated: V2/V3 has no outline
        # corners, layer-lamination string, reference point, router
        # options, layer-direction flags, or power-plane data at all, and
        # kiub.py itself never reads any of these fields back out (they
        # only exist to satisfy V4/V5's fixed header grammar). The one
        # field worth a comment is the hardcoded max-layer count of 22:
        # unlike the rest, this one IS meaningful -- it's the documented
        # maximum layer count for the V2/V3 format per the Ultiboard
        # reference manual, not an arbitrary filler value.
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
                    # V2/V3's own *TP value (e.g. "fffff000") is discarded
                    # and replaced unconditionally -- kiub.py never reads
                    # this field back out either way.
                    line = '*TP ffffffff'
                if line.startswith("*TC"):
                    # V2/V3 only ever defines trace codes 0-15; V4/V5's own
                    # table spans 0-31 (kiub.py's TT handler). Backfill the
                    # upper half with a harmless default so a standalone
                    # V2v3-converted file opened directly in Ultiboard V5
                    # (not just fed to kiub.py) has a defined entry for
                    # every code it might reference.
                    for r in range(16, 32):
                        self.out.append(f"*TT {r}, 0, 30")
                    # NOTE: V2/V3's own *TC has only ONE field (a bare
                    # board clearance, e.g. "*TC 2") -- no leading drill
                    # tolerance like V4/V5's "*TC <tol> <clearance>". It's
                    # passed through unmodified here; kiub.py's own *TC
                    # handler treats a missing second field as "no board
                    # clearance given" and falls back to its
                    # default_clearance setting, so every V2/V3 board ends
                    # up using that fallback rather than a real per-board
                    # value.
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
                    # V2/V3 *TD diameters are stored in deci-millimetres
                    # (0.1 mm units) -- NOT the 1/1200-inch database-unit
                    # system every other V2/V3 coordinate field uses.
                    # 254 = 25.4 mm/inch * 10 (the deci-mm-per-inch
                    # constant), so this converts deci-mm -> 1/1200 inch.
                    # Confirmed against sample drill values: raw values
                    # like 6, 9, 11... only make sense as 0.6mm, 0.9mm,
                    # 1.1mm... under this reading.
                    new_val = int(val * 1200 / 254)
                    new_line = f"*TD {idx}, {new_val}"
                else:
                    new_line = line

                td_block.append(new_line)

                # V2/V3 has no separate via-code range at all -- pads and
                # vias share the same 0-15 drill/pad-table numbering, and
                # a *V record's own pad_code field references that same
                # low-numbered table directly (see handle_V). Duplicate
                # every low code up to 240-255 as well, matching V4/V5's
                # convention of reserving 240-255 for vias -- needed for
                # the same standalone-Ultiboard-V5-compatibility reason
                # as the *TT backfill above, not merely cosmetic.
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

                if len(vals) < 9:  # V2/V3 only stores 5 fields after the
                                   # code (x1, x2, y, radius, clearance) --
                                   # none of V4/V5's four aperture fields
                                   # exist natively. Pad with zeros; those
                                   # fields are unused by kiub.py in either
                                   # version anyway.
                    rest = rest.rstrip() + ", 0, 0, 0, 0"

                new_line = prefix + "," + rest
                t_blocks[rec].append(new_line)

                # DUPLICATE 0-15 -> 240-255, same reasoning as *TD above.
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
            # *SNO_SHP is V2/V3's "no shape" placeholder for components
            # without meaningful physical footprint geometry. It DOES
            # carry real (if minimal) outline/pad data in the V2/V3
            # source, but we discard it entirely here and substitute a
            # fixed, hand-built flag-shaped marker instead -- every
            # *SNO_SHP instance in the output is identical regardless of
            # what the source actually stored. There is no V4/V5
            # equivalent of this record at all.
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

        # V2/V3 has only ONE 4-field text descriptor line per shape
        # ("x y height rotation"), not V4/V5's two 6-field lines
        # (reference + alias, each with width/thickness). We reuse this
        # single line for both of V4/V5's descriptor lines below; width
        # is estimated from height (TEXT_WIDTH_RATIO) and thickness is
        # fixed, since V2/V3 stores neither.
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
        # NOTE: a V2/V3 shape body has only these two sections -- there is
        # no third arc/circle section at all (confirmed: real V2/V3
        # shapes never have curved outline primitives). An empty arcs
        # section (bare ";") is appended after PADS below, purely to
        # satisfy V4/V5's three-section shape grammar (kiub.py expects
        # outline + pads + arcs).
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
                    # (0,0) appears to be a V2/V3 sentinel meaning "use
                    # the shape's own geometric centre" rather than a
                    # literal origin placement. NOTE: this only reads
                    # outline[0] (the first physical line of the outline
                    # stream) -- if a shape's outline ever spans more
                    # than one physical line before its terminating ';',
                    # points on later lines would be excluded from this
                    # centre calculation. No sample file in hand has a
                    # multi-line outline, so unconfirmed in practice.
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
                # rotation: raw 0-7 code -> V4/V5 degrees*64 (ROT_MAP).
                # layerset: V2/V3's own file assigns layer bits
                # SEQUENTIALLY, 12 bits higher than needed (bit 12 = Top,
                # bit 13 = Bottom, bit 14 = In1, bit 15 = In2, ...); V4/V5's
                # own convention instead INTERLEAVES each inner-layer pair
                # (bit 0 = Top, bit 1 = Bottom, bit 2 = In2, bit 3 = In1,
                # bit 4 = In4, bit 5 = In3, ...). The single >>12 shift
                # happens to produce the correct V4/V5 bit order directly
                # -- confirmed bit-for-bit against a per-layer test board
                # opened in real Ultiboard V5/V5.72 (e.g. the pad drawn on
                # In1 reads back as In2 and vice versa, matching V4/V5's
                # interleaving exactly). Same transform used for *C
                # pin/net lines below and for *V via layersets (handle_V).
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

        # Empty arcs section (V2/V3 shapes never have one, see note above)
        # plus one extra trailing bare ';' -- matching an extra stray blank
        # line observed after the arc section in genuine V4/V5 shape
        # records themselves (harmless either way: kiub.py's own top-level
        # dispatch loop silently skips any line not starting with '*').
        self.out.append(";\n;")

    # =========================================================
    def handle_C(self):
        header = self.lines[self.i].strip()
        self.i += 1

        data = self.lines[self.i].strip()
        self.i += 1

        # V2/V3's *C header carries NO shape-name field at all (just
        # "*C <refdes> /<alias>") -- the component's shape is identified
        # purely by shape_id: a 0-based index into self.shapes in file
        # order. The V4/V5-style shape name only gets attached to the
        # header line we emit below, via that lookup.
        n        = nums(data)
        shape_id = n[0]
        x, y     = n[1], n[2]
        rot      = ROT_MAP.get(n[3], 0)

        # Signed integer correction for the reference x,y values: V2/V3
        # stores these two text-position offsets as unsigned 16-bit
        # values (0-65535) rather than using a literal '-' sign like every
        # other coordinate field in the file, so two's-complement
        # correction is needed here specifically.
        n_x        = (n[4] if n[4] <= 32768 else n[4] - 65536) + self.shapes[shape_id]['X']
        n_y        = (n[5] if n[5] <= 32768 else n[5] - 65536) + self.shapes[shape_id]['Y']
        n_h        = self.shapes[shape_id]['Height']
        n_w        = int(n_h * TEXT_WIDTH_RATIO)
        n_t        = int(n_h * TEXT_THICKNESS_RATIO)
        # NOTE: self.shapes[shape_id]['Rot'] is already a converted V4/V5
        # degrees*64 value (see handle_S), not a raw 0-7 code -- so this
        # ROT_MAP.get() lookup never matches (ROT_MAP's keys are only
        # 0-7) and always falls back to its default, 0. This is
        # deliberate, not a bug: kiub.py's own *C handler ADDS this field
        # to the component's own placement rotation (it's not an
        # absolute angle), so emitting 0 here means the REFDES/VALUE text
        # simply rotates in lock-step with the component itself, with no
        # extra per-shape offset on top of that.
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
        # V2/V3 has no force/thermal/power-simulation line at all --
        # the position line is followed directly by pin/net lines.
        # V4/V5's grammar requires a third line here; synthesize a fixed
        # all-zero placeholder since there's no source data to carry over.
        self.out.append("0,0,0,0,0,0,0")

        # --------------------------------------------------
        # Read net lines until NEXT RECORD (line starting with '*')
        # (V4.60 doesn't always terminate this block with a bare ';' --
        # kiub.py's own *C handler tolerates that by pushing an
        # unexpected '*' line back into its main dispatch loop, so we
        # don't need to worry about it here.)
        net_lines = []
        while self.i < len(self.lines):
            line = self.lines[self.i].rstrip("\n")
            if line.startswith("*"):
                break
            # pad/pin layerset: same >>12 bit-position remap as shape pad
            # descriptors above (see the comment there) -- applied here to
            # every other whitespace token (the layerset values; the
            # alternating net-number tokens are left untouched).
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
    # V2/V3's *LH (Horizontal) and *LV (Vertical) are folded into V4/V5's
    # single *LT (orthogonal trace) record here, with an explicit numeric
    # <trace_type> ("0") and <orientation> ("1"=H, "2"=V) appended in
    # place of V2/V3's own trailing F/V (Fixed/Variable) suffix.
    #
    # NAMING COLLISION: V2/V3's *LV means "Vertical trace" -- it has
    # NOTHING to do with V4/V5's own *LV record, which means "arbitrary-
    # angle Vector trace" (a completely different record kiub.py also
    # dispatches on this same two-character tag). We only ever emit *LT
    # here, never a real V4/V5-style *LV, so this collision never
    # actually surfaces in our own output.
    #
    # NO DIAGONAL/45 DEGREE SUPPORT: V2/V3 has no record at all
    # equivalent to V4/V5's diagonal-trace encoding (the 4/8 orientation
    # codes on *LT, see kiub.py). A diagonal trace drawn in V2/V3 is
    # stored in the DDF only as a "staircase" of ordinary *LH/*LV
    # segments -- confirmed that Ultiboard's own Gerber output still
    # renders a true diagonal for such a trace, but every other output
    # path (including this DDF data itself) only ever sees the
    # staircase, so that's what gets converted here too. There is no
    # lossless way to recover the original diagonal from V2/V3 data.
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
            if len(parts) >= 4:
                # pad code: V2/V3 has no separate via-code range, it reuses
                # the same 0-15 pad-code table as ordinary pads. Offset by
                # 240 so the via references the duplicated high-numbered
                # twin (see handle_T), matching V4/V5's own convention of
                # reserving codes 240-255 for vias.
                parts[2] = str(int(parts[2]) + 240)
                # layerset: same bit-position offset as pads/pins (handle_S,
                # handle_C) - applies unconditionally, not just to the
                # all-layers sentinel.
                parts[3] = f"{(int(parts[3], 16) >> 12):08x}"

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
    # V2/V3's *X record has only 5 numeric fields, in a DIFFERENT order
    # from V4/V5's 7-field record: "x y height LAYER ROTATION text" here,
    # vs V4/V5's "x y height width thickness ROTATION LAYER text" --
    # note layer/rotation are swapped. There's no width/thickness field
    # at all (both synthesized below: width = height, thickness fixed to
    # 100). <layer> is also offset by one from V4/V5's convention (V2/V3
    # layer 1 = V4/V5 layer 0 = silkscreen), hence "layer - 1" below.
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
    # Fabricate *TS and *SBOARD from scratch: NEITHER exists anywhere in
    # the V2/V3 source. *TS (wave-solder direction) is a fixed, inert
    # placeholder -- kiub.py never reads this field back out either way.
    # *SBOARD's outline is a plain rectangle built directly from the
    # header's own board width/height (handle_P), since V2/V3 has no
    # concept of the board outline as its own named shape the way V4/V5
    # does.
    def emit_TS_and_SBOARD(self):
        self.out.append("*TS H 0 0")

        # odd()/even() apply V4/V5's own outline-stream encoding: each
        # segment's START point must have an odd X (the marker bit for
        # "this begins a new disconnected segment"), its END point an
        # even X. Used here purely to make this synthetic rectangle
        # parse correctly through kiub.py's ordinary outline-stream logic
        # -- same convention real V4/V5 shape/board outlines use.
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
            # empty pads + empty arcs sections -- a board shape has neither
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
