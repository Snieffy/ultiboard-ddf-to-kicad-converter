# KIUB - KiCad Ultiboard Import Tool
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
KIUB: A modern Python-based converter for Ultiboard DDF to KiCad PCB.
Based on: Ultiboard 32bit DOS and Windows95 - Reference Manual - Appendix A (1997).
Version: 2.1.0
"""
from __future__ import annotations

import sys
import os
import math
import pathlib
import json
import argparse
import re
from collections import defaultdict
from itertools import compress, islice
from typing import IO, Any

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# A pad-descriptor dict as stored in pinDescr / shape['pinDescr'].
PadDescriptor = dict[str, Any]

# A KiCad layer entry: [kicad_layer_number, layer_name_string]
LayerEntry = list[int | str]

# An (x, y) coordinate pair in mm.
Point2D = tuple[float, float]

# ---------------------------------------------------------------------------
# Lightweight DataFrame replacement (no numpy/pandas dependency)
# ---------------------------------------------------------------------------

class SimpleDataFrame:
    def __init__(self, rows: int, cols: int, columns: list[str]) -> None:
        self.columns   = columns
        self.data: list[list[float]] = [[0.0] * cols for _ in range(rows)]
        self.col_index: dict[str, int] = {c: i for i, c in enumerate(columns)}

    def __getitem__(self, key: str):
        idx = self.col_index[key]
        return [row[idx] for row in self.data]

    # --- .loc: label-based access; scalar (row, col) or row-only → RowProxy ---
    class RowProxy:
        def __init__(self, outer: SimpleDataFrame, row: int) -> None:
            self.outer = outer
            self.row   = row

        def __getitem__(self, col: str) -> float:
            return self.outer.data[self.row][self.outer.col_index[col]]

        def __setitem__(self, col: str, value: float) -> None:
            self.outer.data[self.row][self.outer.col_index[col]] = value

        def to_dict(self) -> dict[str, float]:
            return {col: self.outer.data[self.row][i]
                    for col, i in self.outer.col_index.items()}

    # --- .at: fast scalar access by (row_index, column_name) ---
    class AtIndexer:
        def __init__(self, outer: SimpleDataFrame) -> None:
            self.outer = outer

        def __getitem__(self, key: tuple[int, str]) -> float:
            row, col = key
            return self.outer.data[row][self.outer.col_index[col]]

        def __setitem__(self, key: tuple[int, str], value: float) -> None:
            row, col = key
            self.outer.data[row][self.outer.col_index[col]] = value

    # --- .iloc: row-based integer index, returns a RowProxy ---
    class IlocIndexer:
        def __init__(self, outer: SimpleDataFrame) -> None:
            self.outer = outer

        def __getitem__(self, row: int) -> SimpleDataFrame.RowProxy:
            return SimpleDataFrame.RowProxy(self.outer, row)

        def __setitem__(self, row: int,
                        values: list[float] | tuple[float, ...] | dict[str, float]) -> None:
            if isinstance(values, (list, tuple)):
                if len(values) != len(self.outer.columns):
                    raise ValueError("Column count mismatch in iloc assignment")
                for i, v in enumerate(values):
                    self.outer.data[row][i] = v
            elif isinstance(values, dict):
                for col, v in values.items():
                    self.outer.data[row][self.outer.col_index[col]] = v
            else:
                raise TypeError("Unsupported assignment type for iloc")

    # --- .loc: label-based access; scalar (row, col) or row-only → RowProxy ---
    class LocIndexer:
        def __init__(self, outer: SimpleDataFrame) -> None:
            self.outer = outer

        def __getitem__(self, key: tuple[int, str] | int
                        ) -> float | SimpleDataFrame.RowProxy:
            if isinstance(key, tuple):
                row, col = key
                return self.outer.data[row][self.outer.col_index[col]]
            return SimpleDataFrame.RowProxy(self.outer, key)

        def __setitem__(self, key: tuple[int, str], value: float) -> None:
            row, col = key
            self.outer.data[row][self.outer.col_index[col]] = value

    @property
    def at(self) -> SimpleDataFrame.AtIndexer:
        return SimpleDataFrame.AtIndexer(self)

    @property
    def iloc(self) -> SimpleDataFrame.IlocIndexer:
        return SimpleDataFrame.IlocIndexer(self)

    @property
    def loc(self) -> SimpleDataFrame.LocIndexer:
        return SimpleDataFrame.LocIndexer(self)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

# KiCad 6+ canonical copper-layer ordinals:
#   F.Cu = 0, In1.Cu = 1 … In30.Cu = 30, B.Cu = 31
# The list is kept in the same positional order as before so that the
# compress()-based bit-mask lookups still work correctly; only the
# ordinal values (first element of each pair) have changed.
layersCu: list[LayerEntry] = [
    [0,  "F.Cu"],    [31, "B.Cu"],    [2,  "In2.Cu"],  [1,  "In1.Cu"],
    [4,  "In4.Cu"],  [3,  "In3.Cu"],  [6,  "In6.Cu"],  [5,  "In5.Cu"],
    [8,  "In8.Cu"],  [7,  "In7.Cu"],  [10, "In10.Cu"], [9,  "In9.Cu"],
    [12, "In12.Cu"], [11, "In11.Cu"], [14, "In14.Cu"], [13, "In13.Cu"],
    [16, "In16.Cu"], [15, "In15.Cu"], [18, "In18.Cu"], [17, "In17.Cu"],
    [20, "In20.Cu"], [19, "In19.Cu"], [22, "In22.Cu"], [21, "In21.Cu"],
    [24, "In24.Cu"], [23, "In23.Cu"], [26, "In26.Cu"], [25, "In25.Cu"],
    [28, "In28.Cu"], [27, "In27.Cu"], [30, "In30.Cu"], [29, "In29.Cu"],
]

# Mapping table: Ultiboard CP437/CP850 font codes → DejaVu Sans Mono Unicode.
ub_fontmap: dict[bytes, str] = {
    b'\x20': "\u0020", b'\x21': "\u0021", b'\x22': "\u0022", b'\x23': "\u0023",
    b'\x24': "\u0024", b'\x25': "\u0025", b'\x26': "\u0026", b'\x27': "\u0027",
    b'\x28': "\u0028", b'\x29': "\u0029", b'\x2a': "\u002a", b'\x2b': "\u002b",
    b'\x2c': "\u002c", b'\x2d': "\u002d", b'\x2e': "\u002e", b'\x2f': "\u002f",
    b'\x30': "\u0030", b'\x31': "\u0031", b'\x32': "\u0032", b'\x33': "\u0033",
    b'\x34': "\u0034", b'\x35': "\u0035", b'\x36': "\u0036", b'\x37': "\u0037",
    b'\x38': "\u0038", b'\x39': "\u0039", b'\x3a': "\u003a", b'\x3b': "\u003b",
    b'\x3c': "\u003c", b'\x3d': "\u003d", b'\x3e': "\u003e", b'\x3f': "\u003f",
    b'\x40': "\u0040", b'\x41': "\u0041", b'\x42': "\u0042", b'\x43': "\u0043",
    b'\x44': "\u0044", b'\x45': "\u0045", b'\x46': "\u0046", b'\x47': "\u0047",
    b'\x48': "\u0048", b'\x49': "\u0049", b'\x4a': "\u004a", b'\x4b': "\u004b",
    b'\x4c': "\u004c", b'\x4d': "\u004d", b'\x4e': "\u004e", b'\x4f': "\u004f",
    b'\x50': "\u0050", b'\x51': "\u0051", b'\x52': "\u0052", b'\x53': "\u0053",
    b'\x54': "\u0054", b'\x55': "\u0055", b'\x56': "\u0056", b'\x57': "\u0057",
    b'\x58': "\u0058", b'\x59': "\u0059", b'\x5a': "\u005a", b'\x5b': "\u005b",
    b'\x5c': "\u005c", b'\x5d': "\u005d", b'\x5e': "\u002a", b'\x5f': "\u005f",
    b'\x60': "\u0060", b'\x61': "\u0061", b'\x62': "\u0062", b'\x63': "\u0063",
    b'\x64': "\u0064", b'\x65': "\u0065", b'\x66': "\u0066", b'\x67': "\u0067",
    b'\x68': "\u0068", b'\x69': "\u0069", b'\x6a': "\u006a", b'\x6b': "\u006b",
    b'\x6c': "\u006c", b'\x6d': "\u006d", b'\x6e': "\u006e", b'\x6f': "\u006f",
    b'\x70': "\u0070", b'\x71': "\u0071", b'\x72': "\u0072", b'\x73': "\u0073",
    b'\x74': "\u0074", b'\x75': "\u0075", b'\x76': "\u0076", b'\x77': "\u0077",
    b'\x78': "\u0078", b'\x79': "\u0079", b'\x7a': "\u007a", b'\x7b': "\u007b",
    b'\x7c': "\u00a6", b'\x7d': "\u007d", b'\x7e': "\u007e", b'\xa6': "\u00aa",
    b'\xc7': "\u255f", b'\xfc': "\u03b7", b'\xe9': "\u03b8", b'\xe2': "\u0393",
    b'\xe4': "\u03a3", b'\xe0': "\u03b1", b'\xe5': "\u03c3", b'\xe7': "\u03c4",
    b'\xea': "\u03a9", b'\xeb': "\u03b4", b'\xe8': "\u03a6", b'\xef': "\u03a0",
    b'\xee': "\u03b5", b'\xec': "\u233d", b'\xc4': "\u2500", b'\xc5': "\u253c",
    b'\xc9': "\u2554", b'\xe6': "\u03bc", b'\xc6': "\u255e", b'\xf4': "\u23a7",
    b'\xf6': "\u00f7", b'\xf2': "\u2265", b'\xfb': "\u221a", b'\xf9': "\u2758",
    b'\xd6': "\u2553", b'\xdc': "\u25b7", b'\xf8': "\u00b0", b'\xa3': "\u00f9",
    b'\xd8': "\u256a", b'\xd7': "\u256b", b'\x83': "\u00e2", b'\xe1': "\u03b2",
    b'\xed': "\u2205", b'\xf3': "\u2264", b'\xfa': "\u22c5", b'\xf1': "\u00b1",
    b'\xd1': "\u2564", b'\xaa': "\u2510", b'\xba': "\u2551", b'\xbf': "\u2510",
    b'\xae': "\u00ab", b'\xac': "\u00bc", b'\xbd': "\u255c", b'\xbc': "\u255d",
    b'\xa1': "\u00ec", b'\xab': "\u00bd", b'\xbb': "\u2557", b'\xc1': "\u2534",
    b'\xc2': "\u252c", b'\xc0': "\u2514", b'\xa9': "\u250c", b'\xa2': "\u00f2",
    b'\xa5': "\u00d1", b'\xe3': "\u03c0", b'\xc3': "\u251c", b'\xa4': "\u00f1",
    b'\xf0': "\u2261", b'\xd0': "\u2568", b'\xca': "\u2569", b'\xcb': "\u2566",
    b'\xc8': "\u255a", b'\xcd': "\u2550", b'\xce': "\u256c", b'\xcf': "\u2567",
    b'\xaf': "\u00bb", b'\xcc': "\u2560", b'\xd3': "\u2559", b'\xdf': "\u22b2",
    b'\xd4': "\u2558", b'\xd2': "\u2565", b'\xf5': "\u23ad", b'\xd5': "\u2552",
    b'\xb5': "\u2561", b'\xfe': "\u25a5", b'\xde': "\u25ab", b'\xda': "\u250c",
    b'\xdb': "\u2293", b'\xd9': "\u2518", b'\xfd': "\u00b2", b'\xdd': "\u2277",
    b'\xb4': "\u2524", b'\xad': "\u2193", b'\xb1': "\u00a0", b'\xbe': "\u255b",
    b'\xb6': "\u2562", b'\xa7': "\u235a", b'\xf7': "\u2248", b'\xb8': "\u2555",
    b'\xb0': "\u00a0", b'\xa8': "\u00bf", b'\xb7': "\u2556", b'\xb9': "\u2563",
    b'\xb3': "\u2502", b'\xb2': "\u00a0",
}

# KiCad PCB file header template
# Layer ordinals match KiCad 6+ canonical numbering:
#   Cu: F.Cu=0, In1..In30=1..30, B.Cu=31
#   Tech: B.Adhes=32, F.Adhes=33, B.Paste=34, F.Paste=35,
#         B.SilkS=36, F.SilkS=37, B.Mask=38, F.Mask=39
#   User: Dwgs.User=40, Cmts.User=41, Eco1.User=42, Eco2.User=43,
#         Edge.Cuts=44, Margin=45, B.CrtYd=46, F.CrtYd=47,
#         B.Fab=48, F.Fab=49, User.1=50..User.4=53
HEADER_TEMPLATE: str = """\
(kicad_pcb (version 20241229) (generator "KIUB") (generator_version "1.1.0")

  (general
    (thickness {board_thickness})
    (legacy_teardrops no)
  )

  (paper {papersize})
  (layers\n{PCBlayers}
\t\t(32 "B.Adhes" user "B.Adhesive")
\t\t(33 "F.Adhes" user "F.Adhesive")
\t\t(34 "B.Paste" user)
\t\t(35 "F.Paste" user)
\t\t(36 "B.SilkS" user "B.Silkscreen")
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(38 "B.Mask" user)
\t\t(39 "F.Mask" user)
\t\t(40 "Dwgs.User" user "User.Drawings")
\t\t(41 "Cmts.User" user "User.Comments")
\t\t(42 "Eco1.User" user "User.Eco1")
\t\t(43 "Eco2.User" user "User.Eco2")
\t\t(44 "Edge.Cuts" user)
\t\t(45 "Margin" user)
\t\t(46 "B.CrtYd" user "B.Courtyard")
\t\t(47 "F.CrtYd" user "F.Courtyard")
\t\t(48 "B.Fab" user)
\t\t(49 "F.Fab" user)
\t\t(50 "User.1" user)
\t\t(51 "User.2" user)
\t\t(52 "User.3" user)
\t\t(53 "User.4" user)
  )

  (setup
    (pad_to_mask_clearance {pad_to_mask_clearance})
    (solder_mask_min_width {solder_mask_min_width})
    (pad_to_paste_clearance {pad_to_paste_clearance})
    (pad_to_paste_clearance_ratio {pad_to_paste_clearance_ratio})
  )

"""

# ---------------------------------------------------------------------------
# Board-defaults registry (single source of truth for the editable clearance
# options below). Each entry is (name, default, lo, hi, description, target)
# where target is 'kicad_pcb' (written to the (setup) section) or
# 'kicad_pro' (written to board.design_settings.rules). Both the CLI's
# argparse defaults and the GUI's fine-tuning pop-up (kiub_gui.py's
# _BoardDefaultsDialog) walk this table, so adding a new tunable later only
# requires adding one entry here. Mirrors KIUC's TUNING_SPEC/kiuc.ini pattern
# (kiuc_writer.py / kiuc_gui.py) so both tools share the same UX.
# ---------------------------------------------------------------------------

BOARD_DEFAULTS_SPEC: list[tuple[str, float, float, float, str, str]] = [
    ('pad_to_mask_clearance', 0.05, -1.0, 1.0,
     "Solder mask expansion around each pad, mm. Positive values enlarge "
     "the mask opening beyond the pad edge.",
     'kicad_pcb'),
    ('solder_mask_min_width', 0.15, 0.0, 1.0,
     "Minimum solder mask web width between adjacent mask openings, mm. "
     "Openings closer than this are merged by KiCad's plotter.",
     'kicad_pcb'),
    ('pad_to_paste_clearance', 0.0, -1.0, 1.0,
     "Solder paste absolute clearance from the pad edge, mm (negative "
     "shrinks the paste aperture, e.g. for fine-pitch parts).",
     'kicad_pcb'),
    ('pad_to_paste_clearance_ratio', 0.0, -1.0, 1.0,
     "Solder paste relative clearance, as a ratio of pad size (added to "
     "the absolute clearance above).",
     'kicad_pcb'),
    ('solder_mask_to_copper_clearance', 0.0, 0.0, 1.0,
     "Minimum clearance the DRC enforces between solder mask openings and "
     "copper, mm.",
     'kicad_pro'),
]


def get_board_default(name: str) -> float:
    """Return the spec default for a board-defaults entry by name."""
    for n, default, *_ in BOARD_DEFAULTS_SPEC:
        if n == name:
            return default
    raise KeyError(name)


# ---------------------------------------------------------------------------
# NUT constants (Non-User-Tunable)
#
# Fixed by the DDF format, the KiCad file format, or a DRC/connectivity
# requirement. Never exposed via CLI flag, GUI dialog, or FINE_TUNING_SPEC --
# changing any of these either produces an invalid file, changes what a
# value structurally *means* (not just its magnitude), or risks a DRC
# violation with no corresponding benefit.
# ---------------------------------------------------------------------------

BOARD_THICKNESS_MM: float = 1.6
# Fixed PCB stack thickness written to kicad_pcb's (general) section.
# Ultiboard DOS has no per-board thickness setting to derive this from --
# it's a KiCad-only concept, so there's nothing meaningful to "tune" it
# against.

DIFF_PAIR_GAP_RATIO: float = 1.5
# diff_pair_gap / diff_pair_via_gap = min_clearance * this ratio, written to
# each net class in the .kicad_pro. KiCad-only concept -- Ultiboard DOS has
# no differential-pair support to mirror or validate this against.

PTH_PAD_MIN_ANNULAR_MM: float = 0.01
# Added to the drill diameter when auto-generating a copper pad for a PTH
# hole that has no explicit pad code in the DDF (round pads only -- see
# PTH_PAD_ROUND_RATIO below). This is a DELIBERATE minimum annular ring,
# not a float-rounding fudge -- an earlier version of this code carried a
# comment claiming it avoided "IEEE noise", which was incorrect. KiCad
# requires some copper annulus around a plated hole for electrical
# connectivity; a zero-width annulus produces a DRC error (traces routed
# near an effectively padless PTH hole). Do not remove or reduce this.

VIA_MICRODRILL_MM: float = 0.001
# Drill written for a via whose via code has a pad but no drill. Ultiboard
# accepts such a via and still connects its layers; KiCad only connects
# layers through a hole, and silently replaces a zero drill with 0.3 mm
# when the file is loaded. A 0.001 mm drill keeps the via (and the
# netlist) intact; the hole is not meant to be drilled, which the user
# has to tell the manufacturer -- KIUB says so in its output. Excluded
# from the via/through-hole defaults in .kicad_pro.

CHECK_LAYER: str = "User.1"
CHECK_LAYER_NAME: str = "KIUB.Check"
CHECK_MARK_WIDTH_MM: float = 0.2
CHECK_MARK_MARGIN_MM: float = 0.2
# Library boards (<name>_lib.kicad_pcb): a footprint with pads written at size 0
# (pad code without a size in the technology tables) gets a rectangle with
# both diagonals around its outline, pads and arcs, on CHECK_LAYER, named
# CHECK_LAYER_NAME in the board's layer list. It is part of the footprint,
# so it goes with it into a footprint library and keeps flagging it there
# until deleted. KiCad takes a layer's colour from the user's colour theme,
# not from the board: the user picks it (e.g. red) once.

PTH_PAD_ROUND_RATIO: float = 0.5
# roundrect_rratio for the auto-generated PTH pads described above; 0.5
# makes the pad a perfect circle. This is what actually declares "this is
# a round pad", not merely how rounded its corners are -- changing it
# changes the pad shape itself, so it must stay fixed.

NPTH_ZERO_CLEARANCE_MM: float = 0.15
# Clearance substituted for NPTH pads whose DDF-specified clearance is
# zero. Kept fixed for now. If this is ever made user-tunable, it must be
# hard-clamped to 0.10-0.15mm -- values outside that band have been
# observed to introduce DRC clearance violations.

SMD_MIN_DRILL_MM: float = 0.05
# Drills smaller than this are treated as the special "SMD trick" case and
# set to KiCad's -1 sentinel (never actually drilled). Ultiboard DOS cannot
# place two SMD pads from opposite layers at exactly the same X/Y
# coordinate, so such layouts are built in Ultiboard with a tiny
# non-drilled microhole between the two pads purely to satisfy Ultiboard's
# own placement rules; that hole must not become a real drilled hole in
# KiCad.

FONT_THICKNESS_DIVISOR: int = 1000
# Ultiboard's own font-format definition: stroke thickness = thickness
# field x text height / 1000. Not a KIUB choice -- fixed by the DDF font
# encoding itself.

_ANNULAR_WIDTH_NOISE_EPS_MM: float = 1e-4
# Below this, an apparent annular-ring width is treated as float-rounding
# noise (i.e. effectively zero) rather than a genuine measurement, when
# scanning pads/vias for the smallest annular width on the board.

_KICAD_NEAR_ZERO_MM: float = 0.000001
# Near-zero sentinel that KiCad accepts without silently rewriting to a
# different value, used as a fallback where a true zero would be
# ambiguous with "field not set".


# ---------------------------------------------------------------------------
# Fine-tuning registry (user-adjustable geometry/style constants)
#
# Surfaced in kiub_gui.py's separate "Fine-tuning…" dialog (kept apart from
# "Board defaults…", which holds only true manufacturing clearances written
# straight into kicad_pcb/kicad_pro). Mirrors KIUC's TUNING_SPEC/kiuc.ini
# pattern (kiuc_writer.py / kiuc_gui.py) so both tools share the same UX.
#
# category:
#   'geometry'  -- affects visual/rendering fit only (text size, line
#                  widths, outline snapping); safe to adjust freely.
#   'clearance' -- fallback copper clearances/widths used only where the
#                  DDF doesn't specify a value of its own; alter cautiously,
#                  since values set too aggressively can trigger DRC
#                  clearance violations elsewhere on the board.
# ---------------------------------------------------------------------------

FINE_TUNING_SPEC: list[tuple[str, float, float, float, str, str]] = [
    ('font_height_ratio', 1.208, 1.0, 1.5,
     "KiCad text height = Ultiboard text height \u00f7 this ratio. "
     "Empirically fitted so converted text visually matches Ultiboard's "
     "own rendering.",
     'geometry'),
    ('font_width_ratio', 1.186, 1.0, 1.5,
     "KiCad text width = Ultiboard text width \u00d7 this ratio. "
     "Empirically fitted, paired with font_height_ratio.",
     'geometry'),
    ('line_width', 0.075, 0.01, 0.5,
     "Default width for lines, arcs, and circles (board outline, silk, "
     "etc.) wherever the DDF doesn't specify one of its own, mm.",
     'geometry'),
    ('snap_tolerance', 0.1, 0.0, 1.0,
     "Maximum gap, mm, between adjacent board-outline endpoints before "
     "they're snapped closed into a single continuous outline.",
     'geometry'),
    ('default_clearance', 0.254, 0.05, 0.5,
     "Fallback copper clearance, mm, used where the DDF doesn't specify "
     "one. Alter cautiously -- values set too low can produce DRC "
     "clearance violations elsewhere on the board.",
     'clearance'),
    ('default_width', 0.254, 0.05, 0.5,
     "Fallback copper trace width, mm, used where the DDF doesn't specify "
     "one. Alter cautiously -- affects current-carrying capacity and DRC.",
     'clearance'),
    ('default_thermal_gap', 0.254, 0.05, 0.5,
     "Fallback thermal-relief air gap (spoke-to-pad), mm, used where the "
     "DDF doesn't specify one. Alter cautiously.",
     'clearance'),
    ('default_thermal_width', 0.254, 0.05, 0.5,
     "Fallback thermal-relief spoke width, mm, used where the DDF doesn't "
     "specify one. Alter cautiously.",
     'clearance'),
    ('v2v3_text_width_ratio', 0.8, 0.3, 1.5,
     "DDF V2/V3 pre-conversion only: estimated text width = text height "
     "\u00d7 this ratio (unlike V4/V5, V2/V3 DDFs don't store text width "
     "directly).",
     'geometry'),
    ('v2v3_text_thickness_ratio', 0.1667, 0.05, 0.5,
     "DDF V2/V3 pre-conversion only: estimated text stroke thickness = "
     "text height \u00d7 this ratio.",
     'geometry'),
]


def get_fine_tuning_default(name: str) -> float:
    """Return the spec default for a fine-tuning entry by name."""
    for n, default, *_ in FINE_TUNING_SPEC:
        if n == name:
            return default
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Pure helper functions (no state)
# ---------------------------------------------------------------------------

def kicad_string(text: str) -> str:
    """text as the inside of a KiCad "..." string: backslash and double
    quote escaped."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def ubfont(payload: bytes) -> str:
    """Convert CP437/CP850 Ultiboard font bytes to a Unicode string."""

    chars: list[str] = []
    overline: list[str] = []
    overline_active = False

    for b in payload:
        if b == 94:  # Ultiboard uses '^' as start/end toggle for overline text.
            if overline_active:
                # Closing overline marker.
                chars.append(f"~{{{''.join(overline)}}}")
                overline.clear()

            overline_active = not overline_active
            continue

        ch = ub_fontmap.get(bytes([b]), "?")

        # Escape double-quotes and backslashes.
        if ch in ('"', "\\"):
            ch = "\\" + ch

        if overline_active:
            overline.append(ch)
        else:
            chars.append(ch)

    # Handle an unmatched '^' at end of string.
    if overline_active and overline:
        chars.append(f"~{{{''.join(overline)}}}")

    return "".join(chars)


def layer_from_bit(bit_index: int) -> LayerEntry:
    """Return the layersCu entry for a given zero-based bit position."""
    mask_bits = bin(1 << bit_index)[2:].zfill(32)[::-1]
    return list(compress(layersCu, (int(x) for x in mask_bits)))[0]


def calc_arc_points(
    radius: float,
    start_angle_deg: float,
    span_deg: float,
    accuracy: int | None = None,
) -> tuple[Point2D, Point2D, Point2D]:
    """Return centre-relative (x, y) offsets for the start, mid-point, and
    end of an arc.

    Parameters
    ----------
    radius          : arc radius in mm
    start_angle_deg : arc start angle in degrees (DDF *arc1* value / 64)
    span_deg        : arc angular span in degrees (DDF *arc2* value / 64);
                      always positive
    accuracy        : decimal places for rounding. Defaults to None (full
                      float precision) -- both call sites add this result to
                      centre_x/centre_y (+ offsetX/offsetY for board-outline
                      arcs) and round exactly once at that point (via _f() or
                      the board-outline snap/degree-count logic), so rounding
                      here as well would just be a redundant intermediate
                      round. Kept as an explicit optional parameter, not
                      removed, for the same reason as units_to_mm()'s
                      *accuracy* argument.

    Returns
    -------
    (start_pt, mid_pt, end_pt) – each a (dx, dy) tuple in mm

    Angle convention
    ----------------
    DDF arc1 is in the range −360 … +360 degrees.  KiCad measures angles
    CCW from the positive-x axis.

    • ub_start : normalise to 0–360 via  (360 + arc1) % 360
    • ub_mid   : the midpoint angle is negated per Ultiboard convention,
                 always yielding a value ≤ 0.  The original code had an
                 "if arcMid > 360: arcMid -= 360" guard that could never
                 fire and has been removed.
    • ub_end   : normalise to 0–360 via  (ub_start + span) % 360
    """
    deg_to_rad = math.pi / 180

    ub_start = (360 + start_angle_deg) % 360
    ub_mid   = -(ub_start + span_deg / 2)   # always ≤ 0; no wrap-around check needed
    ub_end   = (ub_start + span_deg) % 360

    def _r(val: float) -> float:
        return round(val, accuracy) if accuracy is not None else val

    start_pt: Point2D = (_r(radius * math.cos(deg_to_rad * ub_start)),
                         _r(radius * math.sin(deg_to_rad * ub_start)))
    mid_pt: Point2D   = (_r(radius * math.cos(deg_to_rad * ub_mid)),
                         _r(radius * math.sin(deg_to_rad * ub_mid)))
    end_pt: Point2D   = (_r(radius * math.cos(deg_to_rad * ub_end)),
                         _r(radius * math.sin(deg_to_rad * ub_end)))

    return start_pt, mid_pt, end_pt



# ---------------------------------------------------------------------------
# Converter class
# ---------------------------------------------------------------------------

class Converter:
    """Holds all conversion state and one handler method per DDF record type."""

    # Default settings.
    # NUT (fixed, never overridden from args -- see the NUT constants block
    # near the top of this file for why):
    NPTHclearance:       float = NPTH_ZERO_CLEARANCE_MM  # Hole clearance for NPTH pads – used when pad clearance is zero.
    dcMin:               float = SMD_MIN_DRILL_MM        # Smallest drill diameter (mm); drills below this are set to -1 (SMD special case).
    fontThickRatio:      int   = FONT_THICKNESS_DIVISOR  # Font thickness divisor: thickness = value × height / 1000 (Ultiboard definition).
    # Fine-tunable (see FINE_TUNING_SPEC): these are the built-in defaults;
    # __init__ overrides them per-instance from self.args when a CLI flag or
    # GUI "Fine-tuning…" value is present.
    lineWidth:           float = get_fine_tuning_default('line_width')
    defaultClearance:    float = get_fine_tuning_default('default_clearance')
    defaultWidth:        float = get_fine_tuning_default('default_width')
    defaultThermalGap:   float = get_fine_tuning_default('default_thermal_gap')
    defaultThermalWidth: float = get_fine_tuning_default('default_thermal_width')
    fontHeightRatio:     float = get_fine_tuning_default('font_height_ratio')
    fontWidthRatio:      float = get_fine_tuning_default('font_width_ratio')
    snapTolerance:       float = get_fine_tuning_default('snap_tolerance')

    def __init__(self, ddf: IO[bytes], kicad: IO[str],
                 args: argparse.Namespace) -> None:
        self.ddf   = ddf
        self.kicad = kicad
        self.args  = args

        # Fine-tunable geometry/clearance defaults (see FINE_TUNING_SPEC) --
        # overridden here from a CLI flag or the GUI's "Fine-tuning…" dialog
        # when present, otherwise falling back to the class-level default.
        # NPTHclearance/dcMin/fontThickRatio are NUT and deliberately NOT
        # overridden here -- see the NUT constants block near the top of
        # this file.
        self.lineWidth           = getattr(args, 'line_width', Converter.lineWidth)
        self.defaultClearance    = getattr(args, 'default_clearance', Converter.defaultClearance)
        self.defaultWidth        = getattr(args, 'default_width', Converter.defaultWidth)
        self.defaultThermalGap   = getattr(args, 'default_thermal_gap', Converter.defaultThermalGap)
        self.defaultThermalWidth = getattr(args, 'default_thermal_width', Converter.defaultThermalWidth)
        self.fontHeightRatio     = getattr(args, 'font_height_ratio', Converter.fontHeightRatio)
        self.fontWidthRatio      = getattr(args, 'font_width_ratio', Converter.fontWidthRatio)
        self.snapTolerance       = getattr(args, 'snap_tolerance', Converter.snapTolerance)

        # DDF version and numeric precision – updated in _handle_header
        self.DDF_major: int | str = 4
        self.dr_Ac: int = 2   # Decimal places for drill diameters.
        self.di_Ac: int = 6   # Decimal places for all other dimensional parameters.

        # Layout offsets – set in _handle_header
        self.offsetX:   float = 0.0   # X offset to centre the board on the selected paper frame.
        self.offsetY:   float = 0.0   # Y offset to centre the board on the selected paper frame.
        self.layerMask: str   = "0x3"

        # Technology tables
        self.traceWidth:     dict[int, int]   = {}   # trace code → width in DDF units
        self.traceClearance: dict[int, int]   = {}   # trace code → clearance in DDF units
        self.drillCode:      list[int | float] = [0] * 256  # drill/via code → diameter in mm

        padColumns = ["Xsize", "Ysize", "Xoffset", "roundratio", "clearance"]
        # pads[0] = Inner layers (*T0), pads[1] = Front/F.Cu (*T1), pads[2] = Back/B.Cu (*T2)
        # (0↔1 swap applied in _handle_tech_pad to match KiCad layer numbering)
        self.pads: list[SimpleDataFrame] = [
            SimpleDataFrame(256, 5, padColumns) for _ in range(3)
        ]

        # Net / shape registries
        self.nets:   dict[int, str | int]          = {0: ''}  # KiCad net number → net name (net 0 always empty)
        self.ncount: int                           = 0        # Running net counter
        self.Shapes: dict[str, dict[str, Any]]     = {}
        self._shapes_header_printed: bool          = False    # Tracks whether the "Shapes:" header has been printed.

        # Net-to-trace-code mapping collected from *N records; used by write_kicad_pro
        # for pattern-based netclass assignments.  key = net name, value = trace code.
        self.netTraceCode: dict[str, int] = {}

        # Minimum tracecode clearance (in DDF units) seen on any LT/LV/LA segment
        # for each KiCad net number.  Used in write_kicad_pro to tighten netclass
        # clearances where a net's routed segments use a tighter tracecode than the
        # one assigned in the *N record.
        self.netMinClearance: dict[int, int] = {}

        # Board clearance – set in _handle_tech (*TC record); also written to .kicad_pro
        self.boardClearance: float = 0.0

        # Technology completion (see _finish_tech): set by any *T record,
        # cleared once the drill-only pad codes have been filled in.
        self._tech_pending: bool = False

        # Pads written with size 0 because their pad code has no size in the
        # technology tables: shape name -> {pad code: pad count}, and the
        # shapes already reported (each shape is reported once).
        self._zero_pads: dict[str, dict[int, int]] = {}
        self._zero_pads_reported: set[str] = set()
        self._current_shape: str = ''

        # Vias whose code lacks a drill and/or a pad (see _handle_via):
        # micro-drilled (pad, no drill), minimum-ring (drill, no pad), skipped.
        self._via_microdrill: dict[int, int] = {}
        self._via_min_ring:   dict[int, int] = {}
        self._via_skipped:    dict[int, int] = {}

        # Power-plane zone data – set in _handle_header
        self.zoneParams: dict[str, Any] = {
            key: [] for key in ["pwrPlanes", "extX0", "extY0", "extX1", "extY1"]
        }

        # Raw binary text payload for the current *X record
        self._line_b: bytes = b""

        # One-line pushback buffer used by the convert() main loop
        self._pushback_raw: bytes | None = None

        # Dispatch table: top-level DDF record character → handler method
        self._dispatch: dict[str, Any] = {
            'P': self._handle_header,
            'S': self._handle_shape,
            'T': self._handle_tech,
            'N': self._handle_netlist,
            'C': self._handle_component,
            'L': self._handle_subrecord,
            'V': self._handle_via,
            'X': self._handle_text,
        }

    # -----------------------------------------------------------------------
    # Unit conversion (depends on instance state DDF_major / di_Ac)
    # -----------------------------------------------------------------------

    def units_to_mm(self, val: int | float, accuracy: int | None = None) -> float:
        """Convert DDF database units to mm.

        Returns full float precision unless *accuracy* is explicitly given.
        Rounding by default here was the main source of compounding rounding
        error throughout the file: a value rounded at conversion time, then
        combined with other already-rounded values (offsetX/offsetY, ratios,
        etc.), accumulates noise beyond what a single final rounding would
        produce. Callers are expected to round exactly once, at their own
        terminal output point (via _f() or an explicit round()) -- not here.

        The two call sites that do pass an explicit accuracy (drill
        diameters, rounded to dr_Ac) are a deliberate exception: that
        rounding IS the final, intended precision for those values, matching
        KiCad's own 2-decimal drill-size convention, and those values are
        written to the file without any further rounding step.
        """
        if self.DDF_major == '4':
            val_mm = (val / 1.2) * 0.0254   # 1/1200 inch
        else:
            val_mm = val / 1_000_000         # nanometres
        return round(val_mm, accuracy) if accuracy is not None else val_mm

    def _f(self, val: float) -> str:
        """Round val to di_Ac decimal places and return as a string.

        Every coordinate or dimension that is written into KiCad output must
        pass through this method.  Adding two individually-rounded floats can
        produce IEEE 754 representation noise beyond di_Ac digits (e.g.
        units_to_mm(x) + offsetX may yield 2193.9023770000003 instead of 2193.902377).
        A final round here eliminates that artefact before the value is
        serialised as text.
        """
        return str(round(val, self.di_Ac))


    @staticmethod
    def _map_ddf_to_kicad_net(netnr: int) -> int:
        """Translate DDF net number to KiCad net number (65535 → 0)."""
        return 0 if netnr == 65535 else netnr + 1

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def convert(self) -> None:
        """Read the DDF file line by line and write the KiCad PCB file."""
        while True:
            if self._pushback_raw is not None:
                raw_line, self._pushback_raw = self._pushback_raw, None
            else:
                raw_line = self.ddf.readline()
                if not raw_line:
                    break
            line = raw_line.strip(b"\r")
            if line == b"":
                break
            if line.decode("CP437")[0] != '*':
                continue

            # Extract text payload as raw bytes before any decoding (for case 'X').
            if line.decode("CP437")[1] == "X":
                parts        = line.rstrip(b"\r\n").split(maxsplit=8)
                self._line_b = b"" if len(parts) < 9 else parts[8]
            else:
                self._line_b = b""

            # Names (shapes, components, nets) are CP437 text: a byte above
            # 127 is its character (0xF8 = '\u00b0'), not an escape sequence.
            line = line.strip().decode("CP437")

            # The technology block (*T records) ends at the first other
            # record: complete the pad tables before anything uses them.
            if self._tech_pending and line[1] != 'T':
                self._finish_tech()

            handler = self._dispatch.get(line[1])
            if handler:
                handler(line)

        if self._tech_pending:
            self._finish_tech()
        self._report_via_fixes()
        self._write_power_plane_zones()
        self.kicad.write(')')

    # -----------------------------------------------------------------------
    # *P – Header
    # -----------------------------------------------------------------------

    def _handle_header(self, line: str) -> None:
        line           = self._readline()   # <version number> <revision number>
        self.DDF_major = line[0]            # DDF version: '4' or '5'.
        # V4 and V5 currently use the same accuracy values; kept explicit for forward-compatibility.
        # When converting V5 DDFs: if some round pads change to square pads, try changing di_Ac to 3.
        self.dr_Ac, self.di_Ac = (2, 6) if self.DDF_major == '4' else (2, 6)

        line         = self._readline()     # <x0>,<y0>,<x1>,<y1>,<default grid step>,<swap level>[,<routing layers>],<max layers>;
        board_params = list(map(int, line[:-1].split(',')))
        # board_params[0..3] = board outline extents; used to select the smallest paper size that fits.

        # Board outline extents → paper size selection
        ext_x0 = self.units_to_mm(board_params[0])
        ext_y0 = -self.units_to_mm(board_params[1])
        ext_x1 = self.units_to_mm(board_params[2])
        ext_y1 = -self.units_to_mm(board_params[3])
        board_center: Point2D = ((ext_x0 + ext_x1) / 2, (ext_y0 + ext_y1) / 2)  # Centre of board extents.

        board_h = abs(ext_y0) + abs(ext_y1) + 90   # Add margin for frame + title block.
        board_w = abs(ext_x0) + abs(ext_x1) + 30

        sheet_sizes: dict[str, tuple[int, int]] = {
            "A5": (148, 210), "A4": (210, 297), "A3": (297, 420),
            "A2": (420, 594), "A1": (594, 841), "A0": (841, 1189),
        }
        sheetSize    = "A0"   # Default to largest sheet; shrink to smallest that fits.
        frame_center: Point2D = (
            sheet_sizes["A0"][1] / 2,
            (sheet_sizes["A0"][0] - 45) / 2 + 7.5,   # Centre of frame, above the title block.
        )
        for sheet_name, (sh, sw) in sheet_sizes.items():
            if board_h <= sh and board_w <= sw:
                sheetSize    = sheet_name
                frame_center = (sw / 2, (sh - 45) / 2 + 7.5)
                break

        self.offsetX   = frame_center[0] - board_center[0]  # Offset to centre PCB on frame.
        self.offsetY   = frame_center[1] - board_center[1]
        maxLayers      = board_params[-1]   # Number of copper layers (always even, 2–32).
        self.layerMask = hex((2 ** maxLayers) - 1)

        mask_bits = bin(int(self.layerMask, 16))[2:].zfill(32)[::-1]
        layers_t: list[LayerEntry] = sorted(
            compress(layersCu, (int(x) for x in mask_bits))
        )
        # With canonical KiCad 6+ ordinals (F.Cu=0, In1..In30=1..30, B.Cu=31),
        # sorted() already places B.Cu last — no manual reordering needed.

        layers_p = "\n".join(
            f'\t({entry[0]} "{entry[1]}" signal)'
            for entry in layers_t[:maxLayers]
        )
        if self.args.verbose:
            print(f"\n{maxLayers} layers:\n{layers_p}\n")

        header = HEADER_TEMPLATE.format(
            PCBlayers=layers_p,
            papersize=f'"{sheetSize}"',
            board_thickness=BOARD_THICKNESS_MM,
            pad_to_mask_clearance=getattr(self.args, "pad_to_mask_clearance",
                                          get_board_default("pad_to_mask_clearance")),
            solder_mask_min_width=getattr(self.args, "solder_mask_min_width",
                                           get_board_default("solder_mask_min_width")),
            pad_to_paste_clearance=getattr(self.args, "pad_to_paste_clearance",
                                           get_board_default("pad_to_paste_clearance")),
            pad_to_paste_clearance_ratio=getattr(self.args, "pad_to_paste_clearance_ratio",
                                                 get_board_default("pad_to_paste_clearance_ratio")),
        )
        if getattr(self.args, 'all_shapes', False):
            # Name the layer that carries the check marks (see CHECK_LAYER).
            header = header.replace(f'(50 "{CHECK_LAYER}" user)',
                                    f'(50 "{CHECK_LAYER}" user "{CHECK_LAYER_NAME}")', 1)
        self.kicad.write(header)
        self.kicad.write('  (net 0 "")\n')   # KiCad net 0 must always be an empty (unconnected) net.

        for _ in range(3):
            self.ddf.readline()     # <layer lamination sequence> / <reference point x,y> / <router options + user settings>
        self.ddf.readline()         # <layer direction>

        # Power-plane net numbers, spread over 6 lines, one per pair of layers.
        # Each position maps to a copper layer (65535 = no power plane on that layer).
        # Layer order: <Top> <Bot> <In2> <In1> <In4> <In3> ... <In30> <In29>
        pwr_raw   = " ".join(l.decode("CP437").strip() for l in islice(self.ddf, 6))
        pwrPlanes: list[tuple[str, int]] = [
            (layersCu[i][1], net + 1)           # +1: KiCad net 0 must remain empty.
            for i, net in enumerate(int(x) for x in pwr_raw.split())
            if int(net) != 65535
        ]
        self.zoneParams["pwrPlanes"] = pwrPlanes
        self.zoneParams["extX0"]     = round(self.units_to_mm(board_params[0]) + self.offsetX, self.di_Ac)
        self.zoneParams["extY0"]     = round(-self.units_to_mm(board_params[1]) + self.offsetY, self.di_Ac)
        self.zoneParams["extX1"]     = round(self.units_to_mm(board_params[2]) + self.offsetX, self.di_Ac)
        self.zoneParams["extY1"]     = round(-self.units_to_mm(board_params[3]) + self.offsetY, self.di_Ac)

    # -----------------------------------------------------------------------
    # *S – Shape definition
    # -----------------------------------------------------------------------

    def _handle_shape(self, line: str) -> None:
        sName = line[2:]

        if sName.endswith('.BAK'):
            if self.args.verbose:
                if not self._shapes_header_printed:
                    print("Shapes:")
                    self._shapes_header_printed = True
                print(f"  {sName} \033[2;31;43m SKIPPED \033[0;0m")
            return

        if self.args.verbose:
            if not self._shapes_header_printed:
                print("Shapes:")
                self._shapes_header_printed = True
            print(f"  {sName}")

        # Reference text descriptor
        # Reference text descriptor: <rel_x> <rel_y> <height> <rotation> <width> <thickness>
        lname     = [int(i) for i in self._readline().split()]
        sn_rel_x  = self.units_to_mm(lname[0])
        sn_rel_y  = -self.units_to_mm(lname[1])
        sn_height = self.units_to_mm(lname[2])
        sn_rot    = lname[3] / 64
        sn_width  = self.units_to_mm(lname[4])
        sn_thick  = lname[5] * sn_height / self.fontThickRatio

        # Alias text descriptor (same field order as the reference text): not
        # used -- a placed component's Value text takes its position and size
        # from the *C record (Section 5 of FILEFORMAT-DDF.md).
        self._readline()

        # Build the footprint template; {…} placeholders are filled per component.
        # The library item is "Ultiboard:<footprint>", a nickname no
        # fp-lib-table lists (CORRECTED_FOLDER.md, section 8): the board keeps
        # the footprint it embeds, and the link equals the Footprint field KIUC
        # writes for the part, so Update PCB from Schematic finds nothing to
        # change. The footprint library <name>.pretty, listed under the
        # nickname <name>, is for browsing and placing new parts.
        fp_lib = FOOTPRINT_LINK_NICKNAME
        fp_name = (getattr(self.args, 'footprint_names', None) or {}).get(sName, sName)

        def template_text(text: str) -> str:   # a KiCad string inside the .format() template
            return kicad_string(text).replace("{", "{{").replace("}", "}}")
        shapeStr = (
            f'  (footprint "{template_text(fp_lib)}:{template_text(fp_name)}"\n'
            f'     (layer "{{fp_side}}.Cu")\n'
            f'     (at {{shapePos}})\n'
            f'     {{r_block}}{{v_block}}'
            f'     (fp_text user "${template_text(sName)}"\n'
            f'        (at {self._f(sn_rel_x)} {self._f(sn_rel_y)} {self._f(sn_rot)})\n'
            f'        (unlocked yes)\n'
            f'        (layer "{{fp_side}}.Fab")\n'
            f'        (effects\n'
            f'           (font\n'
            f'              (face "{self.args.font}")\n'
            f'              (size {self._f(sn_height)} {self._f(sn_width)})\n'
            f'              (thickness {self._f(sn_thick)})\n'
            f'           )\n{{mir}}'
            f'        )\n'
            f'     )\n'
        )

        self.ddf.readline()  # <Rth_junc_board> – not used

        board_lines = self._read_shape_lines(sName)
        pinDescr    = self._read_pad_descriptors()
        board_arcs  = self._read_shape_arcs(sName)

        if sName == 'BOARD':
            # Snap adjacent board-outline endpoints before writing, so KiCad
            # considers the outline closed and valid for DRC and zone-fill.
            self._snap_and_write_board_outline(board_lines, board_arcs)
        else:
            shapeStr += board_lines + board_arcs   # both are strings for non-BOARD shapes
            self.Shapes[sName] = {'str': shapeStr, 'pinDescr': pinDescr}

    def _read_shape_lines(self, sName: str) -> list[tuple[float, float, float, float]] | str:
        """Read outline line segments.

        For BOARD: returns a list of (x1, y1, x2, y2) tuples in mm (offsets not
        yet applied) so that _snap_and_write_board_outline can adjust endpoints
        before writing.
        For footprints: returns the KiCad S-expression string as before.
        """
        sline = ''
        while True:
            seg_line = self._readline()
            if seg_line.startswith(';'):
                break
            sline += seg_line[:-1] if seg_line.endswith(';') else seg_line
            if ';' in seg_line:
                break

        if not sline:
            return [] if sName == 'BOARD' else ''

        seg_coords = [int(i) for i in sline.split(',')]
        # Build a corrected list of shape line endpoints.
        # DDF outline lines are stored as a flat list of (x,y) pairs with odd/even
        # encoding to mark segment breaks.  Remove duplicate and invalid pairs.
        p = 2
        while p < len(seg_coords):
            if seg_coords[p] % 2 == 0:
                if seg_coords[p - 2] % 2 == 0:
                    seg_coords[p:p] = [seg_coords[p - 2], seg_coords[p - 1]]
                    p += 2
                else:
                    seg_coords[p - 2] -= 1
            elif seg_coords[p - 2] % 2 != 0:  # Remove previous (x,y) pair when both x values are odd.
                del seg_coords[p - 2:p]
                p -= 2
            p += 2

        is_board = sName == 'BOARD'
        li_type  = "gr_line"   if is_board else "fp_line"
        fp_layer = "Edge.Cuts" if is_board else "{fp_side}.SilkS"
        ox = self.offsetX if is_board else 0.0
        oy = self.offsetY if is_board else 0.0

        if is_board:
            # Return raw mm coordinates (without offset) for snapping.
            # The offset is applied in _snap_and_write_board_outline after snapping.
            board_segs: list[tuple[float, float, float, float]] = []
            for p in range(0, len(seg_coords) - 3, 4):
                board_segs.append((
                    self.units_to_mm(seg_coords[p]),
                    -self.units_to_mm(seg_coords[p + 1]),
                    self.units_to_mm(seg_coords[p + 2]),
                    -self.units_to_mm(seg_coords[p + 3]),
                ))
            return board_segs

        # Footprint path: build and return the KiCad string directly.
        result = ''
        for p in range(0, len(seg_coords) - 3, 4):
            result += (
                f'     ({li_type}\n'
                f'        (start {self._f(self.units_to_mm(seg_coords[p])     + ox)}'
                f' {self._f(-self.units_to_mm(seg_coords[p + 1]) + oy)})\n'
                f'        (end   {self._f(self.units_to_mm(seg_coords[p + 2]) + ox)}'
                f' {self._f(-self.units_to_mm(seg_coords[p + 3]) + oy)})\n'
                f'        (layer "{fp_layer}")\n'
                f'        (width {self.lineWidth})\n'
                f'     )\n'
            )
        return result

    def _read_pad_descriptors(self) -> list[PadDescriptor]:
        """Read and return pad descriptor records for a shape."""
        pinDescr: list[PadDescriptor] = []
        while True:
            pad_line = self._readline()
            if len(pad_line) > 1:
                parts      = pad_line[:-1].split(',', 5)
                layer_bits = bin(
                    int(parts[2], 16) & int(self.layerMask, 16)
                )[2:].zfill(32)[::-1]
                pinDescr.append({
                    'code':   int(parts[0]),
                    'rot':    float(parts[1]) / 64,
                    'layers': sorted(compress(layersCu, (int(x) for x in layer_bits))),
                    'relx':   self.units_to_mm(int(parts[3])),
                    'rely':   -self.units_to_mm(int(parts[4])),
                    'name':   parts[5],
                })
            if ';' in pad_line:
                break
        return pinDescr

    def _read_shape_arcs(self, sName: str) -> list[dict] | str:
        """Read outline arc/circle records.

        Format per line: <cx>,<cy>,<radius>,<ang1>,<ang2>
        ang1 and ang2 are in degrees × 64.

        For BOARD: returns a list of geometry dicts (without offset applied) so
        that _snap_and_write_board_outline can adjust arc endpoints before writing.
        Each dict has keys: 'type' ('arc' or 'circle'), 'cx', 'cy', 'radius',
        and for arcs: 'dx_start','dy_start','dx_mid','dy_mid','dx_end','dy_end'.
        For footprints: returns the KiCad S-expression string as before.
        """
        result   = ''
        is_board = sName == 'BOARD'
        ox = self.offsetX if is_board else 0.0
        oy = self.offsetY if is_board else 0.0
        board_arcs: list[dict] = []

        while True:
            arc_line = self._readline()
            if len(arc_line) > 1:
                parts       = arc_line[:-1].split(',')
                centre_x    = self.units_to_mm(int(parts[0]))
                centre_y    = self.units_to_mm(int(parts[1]))
                radius      = self.units_to_mm(int(parts[2]))
                start_angle = int(parts[3]) / 64    # degrees
                span_angle  = int(parts[4]) / 64    # degrees, always > 0

                if span_angle == 360:
                    li_type  = "gr_circle" if is_board else "fp_circle"
                    fp_layer = "Edge.Cuts" if is_board else "{fp_side}.SilkS"
                    if is_board:
                        # Full circles have no open endpoints; store for writing only.
                        board_arcs.append({
                            'type':   'circle',
                            'cx':     centre_x,
                            'cy':     centre_y,
                            'radius': radius,
                        })
                    else:
                        result += (
                            f'     ({li_type}\n'
                            f'        (center {self._f(centre_x + ox)} {self._f(-centre_y + oy)})\n'
                            f'        (end {self._f(centre_x + radius + ox)} {self._f(-centre_y + oy)})\n'
                            f'        (layer "{fp_layer}")\n'
                            f'        (width {self.lineWidth})\n'
                            f'     )\n'
                        )
                else:
                    li_type  = "gr_arc"    if is_board else "fp_arc"
                    fp_layer = "Edge.Cuts" if is_board else "{fp_side}.SilkS"
                    (dx_start, dy_start), \
                    (dx_mid,   dy_mid),   \
                    (dx_end,   dy_end)    = calc_arc_points(
                        radius, start_angle, span_angle)
                    if is_board:
                        # Store raw mm coordinates (without offset) for snapping.
                        board_arcs.append({
                            'type':     'arc',
                            'cx':       centre_x,
                            'cy':       centre_y,
                            'radius':   radius,
                            'dx_start': dx_start, 'dy_start': dy_start,
                            'dx_mid':   dx_mid,   'dy_mid':   dy_mid,
                            'dx_end':   dx_end,   'dy_end':   dy_end,
                        })
                    else:
                        result += (
                            f'     ({li_type}\n'
                            f'        (start {self._f(centre_x + dx_start + ox)}'
                            f' {self._f(-centre_y - dy_start + oy)})\n'
                            f'        (mid   {self._f(centre_x + dx_mid   + ox)}'
                            f' {self._f(-centre_y + dy_mid   + oy)})\n'
                            f'        (end   {self._f(centre_x + dx_end   + ox)}'
                            f' {self._f(-centre_y - dy_end   + oy)})\n'
                            f'        (layer "{fp_layer}")\n'
                            f'        (width {self.lineWidth})\n'
                            f'     )\n'
                        )

            if ';' in arc_line:
                break

        return board_arcs if is_board else result

    def _snap_and_write_board_outline(
        self,
        lines: list[tuple[float, float, float, float]],
        arcs:  list[dict],
    ) -> None:
        """Snap adjacent endpoints, separate the closed contour from extra lines,
        and write everything to the KiCad file.

        KiCad requires that the board outline on Edge.Cuts forms a perfectly closed
        contour.  Ultiboard boards can contain lines that are *inside* the outline
        (partition/divider lines, like a line separating two board sections).  Such
        lines must not go on Edge.Cuts — KiCad will report a malformed outline.

        This method performs three passes:

        Pass 1 – Snap:
          Collect all open endpoints (start/end of lines and arcs; circles have none).
          Any pair of endpoints from different elements that are closer than
          snapTolerance are moved to their midpoint.  This closes the tiny gaps that
          arise from the 1/1200-inch DDF unit conversion.

        Pass 2 – Separate contour from extras (iterative degree-pruning):
          Build an adjacency degree count for every snapped endpoint.
          In a pure closed contour every vertex has exactly degree 2
          (one incoming segment, one outgoing segment).
          Any vertex with degree ≠ 2 (dead end = 1, branch/T-junction = 3+)
          belongs to a non-contour element.  Iteratively remove segments that
          touch a non-degree-2 vertex and decrement their neighbours' degrees,
          repeating until stable.  The surviving segments form the closed contour;
          the removed segments are extras.

          This correctly handles:
          - Floating lines (both endpoints degree 1) → F.Fab
          - Pink-style dividers (endpoints snapped to contour → degree 3) → F.Fab
          - Lines intersecting the outline at a non-vertex point → F.Fab
            (their snapped endpoints give the contour vertex degree 3)

        Pass 3 – Write:
          Contour elements → Edge.Cuts.
          Extra lines      → F.Fab  (visible reference, no DRC impact).
          Circles always   → Edge.Cuts  (closed by definition, no open endpoints).
          Arcs always      → Edge.Cuts  (only arcs forming part of the contour
                                         survive pass 2; extra arcs are uncommon
                                         in practice but handled correctly).
        """
        # ── Pass 1: Build mutable endpoint lists and snap ────────────────────

        # Lines: each stored as [[x1,y1],[x2,y2]] (mutable so snap modifies in place).
        line_pts: list[list[list[float]]] = [
            [[x1, y1], [x2, y2]] for x1, y1, x2, y2 in lines
        ]

        # Arcs: start/end as mutable [x,y]; circles have no open endpoints.
        arc_starts: list[list[float] | None] = []
        arc_ends:   list[list[float] | None] = []
        for a in arcs:
            if a['type'] == 'arc':
                arc_starts.append([a['cx'] + a['dx_start'], -a['cy'] - a['dy_start']])
                arc_ends.append(  [a['cx'] + a['dx_end'],   -a['cy'] - a['dy_end']])
            else:
                arc_starts.append(None)   # circle – no open endpoints
                arc_ends.append(None)

        # Flat list of all mutable [x, y] endpoint references.
        endpoints: list[list[float]] = []
        for pair in line_pts:
            endpoints.append(pair[0])
            endpoints.append(pair[1])
        for i in range(len(arcs)):
            if arc_starts[i] is not None:
                endpoints.append(arc_starts[i])
                endpoints.append(arc_ends[i])

        # Snap pairs within snapTolerance to their midpoint (each endpoint moves once).
        snapped: set[int] = set()
        n = len(endpoints)
        for i in range(n):
            if i in snapped:
                continue
            for j in range(i + 1, n):
                if j in snapped:
                    continue
                dx = endpoints[i][0] - endpoints[j][0]
                dy = endpoints[i][1] - endpoints[j][1]
                if math.sqrt(dx * dx + dy * dy) <= self.snapTolerance:
                    mid_x = (endpoints[i][0] + endpoints[j][0]) / 2
                    mid_y = (endpoints[i][1] + endpoints[j][1]) / 2
                    endpoints[i][0] = mid_x;  endpoints[i][1] = mid_y
                    endpoints[j][0] = mid_x;  endpoints[j][1] = mid_y
                    snapped.add(i);  snapped.add(j)
                    break   # each endpoint snaps to at most one neighbour

        # ── Pass 2: Degree-pruning to separate contour from extra lines ──────
        # Represent each endpoint as a rounded tuple for use as a dict key.
        # Rounding to di_Ac matches the precision used everywhere else in KIUB.

        def _pt(xy: list[float]) -> tuple[float, float]:
            return (round(xy[0], self.di_Ac), round(xy[1], self.di_Ac))

        # Build degree count: how many segment-ends land at each point.
        degree: dict[tuple, int] = defaultdict(int)
        for pair in line_pts:
            degree[_pt(pair[0])] += 1
            degree[_pt(pair[1])] += 1
        for i in range(len(arcs)):
            if arc_starts[i] is not None:
                degree[_pt(arc_starts[i])] += 1
                degree[_pt(arc_ends[i])]   += 1

        # Iteratively remove segments touching non-degree-2 vertices until stable.
        # 'contour_lines' / 'extra_lines' track which lines survive vs are removed.
        # Arcs are treated the same way.
        contour_line_mask: list[bool] = [True]  * len(line_pts)
        contour_arc_mask:  list[bool] = [True]  * len(arcs)

        changed = True
        while changed:
            changed = False
            # Check lines
            for i, pair in enumerate(line_pts):
                if not contour_line_mask[i]:
                    continue
                p1, p2 = _pt(pair[0]), _pt(pair[1])
                if degree[p1] != 2 or degree[p2] != 2:
                    contour_line_mask[i] = False
                    degree[p1] -= 1
                    degree[p2] -= 1
                    changed = True
            # Check arcs (never flag circles – they are always contour)
            for i, a in enumerate(arcs):
                if not contour_arc_mask[i] or a['type'] == 'circle':
                    continue
                p1, p2 = _pt(arc_starts[i]), _pt(arc_ends[i])
                if degree[p1] != 2 or degree[p2] != 2:
                    contour_arc_mask[i] = False
                    degree[p1] -= 1
                    degree[p2] -= 1
                    changed = True

        # ── Pass 3: Write ─────────────────────────────────────────────────────
        ox, oy = self.offsetX, self.offsetY

        # Lines: contour → Edge.Cuts,  extras → F.Fab
        for i, (pair, is_contour) in enumerate(zip(line_pts, contour_line_mask)):
            (x1, y1), (x2, y2) = pair
            layer = "Edge.Cuts" if is_contour else "F.Fab"
            self.kicad.write(
                f'     (gr_line\n'
                f'        (start {self._f(x1 + ox)} {self._f(y1 + oy)})\n'
                f'        (end   {self._f(x2 + ox)} {self._f(y2 + oy)})\n'
                f'        (layer "{layer}")\n'
                f'        (width {self.lineWidth})\n'
                f'     )\n'
            )

        # Arcs and circles: contour arcs → Edge.Cuts; extra arcs → F.Fab;
        # circles always → Edge.Cuts (closed by definition).
        for i, (a, is_contour) in enumerate(zip(arcs, contour_arc_mask)):
            if a['type'] == 'circle':
                self.kicad.write(
                    f'     (gr_circle\n'
                    f'        (center {self._f(a["cx"] + ox)} {self._f(-a["cy"] + oy)})\n'
                    f'        (end {self._f(a["cx"] + a["radius"] + ox)} {self._f(-a["cy"] + oy)})\n'
                    f'        (layer "Edge.Cuts")\n'
                    f'        (width {self.lineWidth})\n'
                    f'     )\n'
                )
            else:
                # Use snapped start/end; mid is interior and is not snapped.
                sx, sy = arc_starts[i]
                ex, ey = arc_ends[i]
                mid_x  = a['cx'] + a['dx_mid'] + ox
                mid_y  = -a['cy'] + a['dy_mid'] + oy
                layer  = "Edge.Cuts" if is_contour else "F.Fab"
                self.kicad.write(
                    f'     (gr_arc\n'
                    f'        (start {self._f(sx + ox)} {self._f(sy + oy)})\n'
                    f'        (mid   {self._f(mid_x)} {self._f(mid_y)})\n'
                    f'        (end   {self._f(ex + ox)} {self._f(ey + oy)})\n'
                    f'        (layer "{layer}")\n'
                    f'        (width {self.lineWidth})\n'
                    f'     )\n'
                )

    # -----------------------------------------------------------------------
    # *T – Technology data (dispatches internally by sub-code)
    # -----------------------------------------------------------------------

    def _handle_tech(self, line: str) -> None:
        current_subcode = line[2]  # Can be 'T', 'C', 'D', '0', '1', '2', 'P' or 'S'
        self._tech_pending = True

        match current_subcode:
            case 'P':   # *TP – default padset (hex-word bit pattern) – not used
                if self.args.verbose:
                    print(f"Default padset {line[4:]}\n")

            case 'T':   # *TT – <trace code>,<trace width>,<trace clearance>
                tl = [int(i) for i in line[4:].split(',')]
                self.traceWidth[tl[0]]     = tl[1]
                self.traceClearance[tl[0]] = tl[2]

            case 'C':   # *TC – <drill tolerance> <board clearance>
                dd = [int(i) for i in line[4:].split()]
                # boardClearance is stored in .kicad_pro and used for power-plane zones.
                self.boardClearance = self.units_to_mm(float(dd[1])) if len(dd) > 1 else self.defaultClearance
                if self.args.verbose:
                    print(f"Drill tolerance: {dd[0]}\n")

            case 'D':   # *TD – <drill code>,<drill diameter>
                dc = [int(i) for i in line[4:].split(',')]
                mm = self.units_to_mm(dc[1], self.dr_Ac)
                # Pad drill codes below dcMin are set to -1 (SMD special case: see drillcodes notes).
                # Via codes (≥240) keep their exact size to allow microvias.
                self.drillCode[dc[0]] = mm if dc[0] >= 240 else (mm if mm > self.dcMin else -1)

            case '0' | '1' | '2':  # *T0/*T1/*T2 – pad definitions for Inner(0) / Front(1) / Back(2)
                self._handle_tech_pad(line)

            case 'S':   # *TS – <direction> <top size> <bottom size> – wave solder direction, not used
                if self.args.verbose:
                    print(f"Wave solder dir {line[4:]}\n")

            case _:
                if self.args.verbose:
                    print(f"T? {line}\n")

    def _handle_tech_pad(self, line: str) -> None:
        """Process a *T0/*T1/*T2 pad definition record.

        Field order: <pad code>,<x1>,<x2>,<y>,<radius>,<clearance>,
                     <hor. aperture>,<vert. aperture>,<hor. th. aperture>,<vert. th. aperture>
        Aperture fields are not used.
        """
        pi = [int(i) for i in line[4:].split(',')]
        pc_offsetX = (
            0 if pi[1] == pi[2]                                     # Centric pads and SMD pads have no offset.
            else self.units_to_mm(pi[2] - pi[1]) / 2                # Pad-to-hole offset for non-centric pads.
        )
        roundratio = (
            round(pi[4] / min(pi[1] + pi[2], pi[3]), self.di_Ac)   # Calculate pad corner rounding ratio.
            if (pi[1] + pi[2]) != 0 and pi[3] != 0
            else 0
        )
        pi[1] = self.units_to_mm(pi[1])
        pi[2] = self.units_to_mm(pi[2])
        pi[3] = self.units_to_mm(pi[3])
        pi[5] = self.units_to_mm(pi[5], self.di_Ac)

        if pi[3] == 0 and self.drillCode[pi[0]] > 0:
            # When pad size is zero, set pad size equal to the drill size (NPTH hole).
            # Only for a real drill: -1 marks a fiducial (SMD pads front and back,
            # no hole -- see drillcodes notes), which must never become a hole.
            pi[1] = pi[2] = self.drillCode[pi[0]] / 2
            pi[3] = self.drillCode[pi[0]]

        # Swap DDF layers 0↔1 to match KiCad numbering:
        #   DDF *T0 (Inner)  → pads[0],  DDF *T1 (Top/F.Cu) → pads[1],  DDF *T2 (Bot/B.Cu) → pads[2]
        #   KiCad:            pads[0]=F.Cu,              pads[1]=Inner,             pads[2]=B.Cu
        ppos = int(line[2]) if int(line[2]) == 2 else abs(int(line[2]) - 1)
        self.pads[ppos].iloc[pi[0]] = [
            pi[1] + pi[2],
            pi[3],
            pc_offsetX,
            roundratio,
            pi[5] if pi[5] else self.NPTHclearance,   # Use NPTHclearance when clearance is zero.
        ]

    def _finish_tech(self) -> None:
        """Complete the pad tables once the technology block has been read.

        Drill-only codes -- a drill but no pad definition -- get a round pad
        exactly the drill size (an NPTH hole), so KiCad never sees a padless
        hole (a DRC violation). Via codes (240-255) are not processed here.

        This used to run on reading a code-255 pad record (*T0/*T1/*T2 255),
        but Ultiboard leaves all-zero records out of a DDF, so a file whose
        via code 255 is undefined had no such record and the fill-in never
        ran. It also indexed the tables by the DDF layer digit, without the
        0<->1 swap used everywhere else. Now it runs once, over all three
        tables, whatever records the file holds.
        """
        self._tech_pending = False
        filled: set[int] = set()
        for table in self.pads:
            for didx in range(len(self.drillCode) - 16):
                if self.drillCode[didx] > 0 and table.iloc[didx]['Xsize'] == 0:
                    table.loc[didx, 'Xsize']      = self.drillCode[didx]
                    table.loc[didx, 'Ysize']      = self.drillCode[didx]
                    table.loc[didx, 'roundratio'] = PTH_PAD_ROUND_RATIO
                    table.loc[didx, 'clearance']  = self.NPTHclearance
                    filled.add(didx)
        if filled and self.args.verbose:
            print(f"Drill-only pad codes given a drill-sized pad (NPTH): "
                  f"{', '.join(map(str, sorted(filled)))}\n")


    # -----------------------------------------------------------------------
    # *N – Netlist entry
    # -----------------------------------------------------------------------

    def _handle_netlist(self, line: str) -> None:
        # *N record format: "<netname>" <tracecode> <xlo> <xhi> <ylo> <yhi> <xsum> <ysum> <pincount>;
        fields       = line[3:-1].split()
        self.ncount += 1
        net_name     = fields[0].strip('"') or f"SB${self.ncount}"
        repl         = str.maketrans({'"': '', "'": '/', '\\': '/'})
        self.nets[self.ncount] = net_name.translate(repl)
        if self.nets[self.ncount] == 65535:
            self.nets[self.ncount] = 1

        # fields[1] is the trace code used by this net – store it for netclass assignment
        # in write_kicad_pro().  Code 0 means 'no routing constraint' and is not mapped.
        if len(fields) > 1:
            try:
                tc = int(fields[1])
                if tc != 0:
                    self.netTraceCode[self.nets[self.ncount]] = tc
            except ValueError:
                pass

        self.kicad.write(f'  (net {self.ncount} "{self.nets[self.ncount]}")\n')

    # -----------------------------------------------------------------------
    # *C – Component placement
    # -----------------------------------------------------------------------

    def _handle_component(self, line: str) -> None:
        # *C record: <name> /<alias> <shape name>. The alias never holds a
        # space; a shape name may (*S keeps it), so the shape is the rest of
        # the line, as KIUC's ddf_component_shapes reads it.
        carr   = line[3:].split(None, 2)
        cname  = carr[0]
        calias = carr[1].strip("/")
        cshape = carr[2].strip()

        # Position line: <x>,<y>,<rotation>,<name_x>,<name_y>,<name_rot>,<name_w>,<name_h>,<name_thick>,
        #                <alias_x>,<alias_y>,<alias_rot>,<alias_w>,<alias_h>,<alias_thick>
        # Rotation is in degrees × 64.
        carr   = self._readline().split(",")
        cxpos  = round(self.units_to_mm(int(carr[0])) + self.offsetX, self.di_Ac)
        cypos  = round(-self.units_to_mm(int(carr[1])) + self.offsetY, self.di_Ac)
        layerB = int(carr[2]) / 64 < 0   # Kept for speed and readability; equivalent to 'crot < 0'.
        crot_raw = int(carr[2]) / 64   # Kept unrounded: feeds cnrot/carot below as well as crot itself;
                                        # each of the three rounds independently from this raw value.
        crot   = round(crot_raw, self.di_Ac)

        cnxpos = round(-self.units_to_mm(int(carr[3])) if layerB else self.units_to_mm(int(carr[3])), self.di_Ac)
        cnypos = round(-self.units_to_mm(int(carr[4])), self.di_Ac)
        cnrot  = round(int(carr[5]) / 64 + crot_raw, self.di_Ac)
        cnwdth = round(self.units_to_mm(int(carr[6])), self.di_Ac)
        cnhght_raw = self.units_to_mm(int(carr[7]))   # Kept unrounded: feeds cnthck below as well as cnhght itself.
        cnhght = round(cnhght_raw, self.di_Ac)
        cnthck = round(int(carr[8]) * cnhght_raw / self.fontThickRatio, self.di_Ac)

        caxpos = round(-self.units_to_mm(int(carr[9])) if layerB else self.units_to_mm(int(carr[9])), self.di_Ac)
        caypos = round(-self.units_to_mm(int(carr[10])), self.di_Ac)
        carot  = round(int(carr[11]) / 64 + crot_raw, self.di_Ac)
        cawdth = round(self.units_to_mm(int(carr[12])), self.di_Ac)
        cahght_raw = self.units_to_mm(int(carr[13]))   # Kept unrounded: feeds cathck below as well as cahght itself.
        cahght = round(cahght_raw, self.di_Ac)
        cathck = round(int(carr[14]) * cahght_raw / self.fontThickRatio, self.di_Ac)

        self.ddf.readline()  # <x-force_vect>,<y-force_vect>,<Temp case>,<Temp junc>,<power>,<Rth_junc_board>,0 – not used

        padnet: list[int] = []
        while True:
            pin_line = self._readline()
            if not pin_line or pin_line[0] == ';':   # ';' line terminates the pin block (V4.80+)
                break
            if pin_line[0] == '*':                   # V4.60: no terminator – next record starts immediately
                self._pushback(pin_line)
                break
            # Each pin line: <netnr> <pad_setting> ... – take every other field (the net numbers).
            # Save net_number+1 for each pin; KiCad net 0 must remain empty.
            padnet.extend(int(i) + 1 for i in pin_line.split()[::2])

        side = 'B' if layerB else 'F'
        mir  = "                 (justify mirror)\n" if layerB else ""  # Mirror text for bottom-layer components.

        refBlock = (
            f'     (property "Reference" "{kicad_string(cname)}"\n'
            f'           (layer "{side}.SilkS")\n'
            f'           (at {cnxpos} {cnypos} {cnrot})\n'
            f'           (unlocked yes)\n'
            f'           (effects\n'
            f'                 (font\n'
            f'                       (face "{self.args.font}")\n'
            f'                       (size {cnhght} {cnwdth})\n'
            f'                       (thickness {cnthck})\n'
            f'                 )\n{mir}'
            f'           )\n'
            f'     )\n'
        )
        valBlock = (
            f'     (property "Value" "{kicad_string(calias)}"\n'
            f'           (layer "{side}.Fab")\n'
            f'           (at {caxpos} {caypos} {carot})\n'
            f'           (unlocked yes)\n'
            f'           (hide)\n'
            f'           (effects\n'
            f'                 (font\n'
            f'                       (face "{self.args.font}")\n'
            f'                       (size {cahght} {cawdth})\n'
            f'                       (thickness {cathck})\n'
            f'                 )\n{mir}'
            f'           )\n'
            f'     )\n'
        )

        shape    = self.Shapes[cshape]
        shapeStr = shape['str'].format(
            shapePos=f"{cxpos} {cypos} {crot}",
            fp_side=side,
            mir=mir,
            r_block=refBlock,
            v_block=valBlock,
        )

        if layerB:
            shapeStr = self._flip_x_coords(shapeStr)

        if shape['pinDescr']:
            self._current_shape = cshape
            shapeStr += self._build_pad_attr(shape, layerB)
            shapeStr += self._build_pads(shape, padnet, crot, layerB)
            self._report_zero_pads(cshape, cname)
            if getattr(self.args, 'all_shapes', False) and cshape in self._zero_pads:
                shapeStr += self._check_mark(shapeStr)

        self.kicad.write(shapeStr + "  )\n")

    _MARK_POINTS = re.compile(
        r'\((?:start|end|mid|center)\s+(-?[\d.]+)\s+(-?[\d.]+)\)')
    _MARK_PADS = re.compile(
        r'\(pad "[^"]*" \S+ \S+\n\s+\(at (-?[\d.]+) (-?[\d.]+)[^)]*\)\n'
        r'\s+\(size (-?[\d.]+) (-?[\d.]+)\)')

    def _check_mark(self, footprint: str) -> str:
        """A rectangle with both diagonals on CHECK_LAYER around a
        footprint's outline, arcs and pads (not its texts), in footprint
        coordinates -- the visual flag for a shape whose pads came out size
        0. Built from the footprint's own text, so it fits whatever the shape
        holds."""
        xs: list[float] = []
        ys: list[float] = []
        graphics = footprint.split('     (pad ', 1)[0]     # outline and arcs come first
        for m in self._MARK_POINTS.finditer(graphics):        # texts use (at ...): not matched
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
        for m in re.finditer(r'\(fp_circle\n\s+\(center (-?[\d.]+) (-?[\d.]+)\)\n'
                             r'\s+\(end (-?[\d.]+) (-?[\d.]+)\)', footprint):
            cx, cy, ex, ey = map(float, m.groups())
            r = math.hypot(ex - cx, ey - cy)
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        for m in self._MARK_PADS.finditer(footprint):
            x, y, w, h = map(float, m.groups())
            half = max(abs(w), abs(h)) / 2          # covers any pad rotation
            xs += [x - half, x + half]
            ys += [y - half, y + half]
        if not xs:
            xs, ys = [0.0], [0.0]
        m = CHECK_MARK_MARGIN_MM
        x0, y0 = self._f(min(xs) - m), self._f(min(ys) - m)
        x1, y1 = self._f(max(xs) + m), self._f(max(ys) + m)
        stroke = (f'        (stroke (width {CHECK_MARK_WIDTH_MM}) (type solid))\n'
                  f'        (layer "{CHECK_LAYER}")\n')
        return (f'     (fp_rect\n        (start {x0} {y0})\n        (end {x1} {y1})\n'
                f'{stroke}        (fill none)\n     )\n'
                f'     (fp_line\n        (start {x0} {y0})\n        (end {x1} {y1})\n{stroke}     )\n'
                f'     (fp_line\n        (start {x0} {y1})\n        (end {x1} {y0})\n{stroke}     )\n')

    def _report_zero_pads(self, cshape: str, cname: str) -> None:
        """Warn once per shape whose pads came out size 0 because their pad
        code has no size in the technology tables (typically a code the
        DDF's technology never defines)."""
        codes = self._zero_pads.get(cshape)
        if not codes or cshape in self._zero_pads_reported:
            return
        self._zero_pads_reported.add(cshape)
        count = sum(codes.values())
        listed = ', '.join(str(c) for c in sorted(codes))
        print(f"Warning: shape {cshape} (first placed as {cname}): pad code"
              f"{'s' if len(codes) > 1 else ''} {listed} "
              f"{'have' if len(codes) > 1 else 'has'} no pad size in the technology "
              f"tables -- {count} pad{'s' if count > 1 else ''} written with size 0 "
              f"(KiCad pins {'them' if count > 1 else 'it'} to 1 µm on load)"
              + (f"; marked on layer {CHECK_LAYER_NAME}"
                 if getattr(self.args, 'all_shapes', False) else ""))

    def _report_via_fixes(self) -> None:
        """One summary line per kind of via fix made in _handle_via (each via
        was also reported on its own)."""
        def codes(d: dict[int, int]) -> str:
            return ', '.join(str(c) for c in sorted(d))
        n = sum(self._via_microdrill.values())
        if n:
            print(f"Warning: {n} via{'s' if n > 1 else ''} (via code"
                  f"{'s' if len(self._via_microdrill) > 1 else ''} {codes(self._via_microdrill)}) "
                  f"had no drill and got a {VIA_MICRODRILL_MM} mm drill to keep their layers "
                  f"connected -- inform the manufacturer that these holes are not to be drilled")
        n = sum(self._via_min_ring.values())
        if n:
            print(f"Warning: {n} via{'s' if n > 1 else ''} (via code"
                  f"{'s' if len(self._via_min_ring) > 1 else ''} {codes(self._via_min_ring)}) "
                  f"had no pad and got a pad {PTH_PAD_MIN_ANNULAR_MM} mm larger than the drill "
                  f"(minimum ring); the board's minimum via annular width allows for it")
        n = sum(self._via_skipped.values())
        if n:
            print(f"Warning: {n} via{'s' if n > 1 else ''} (via code"
                  f"{'s' if len(self._via_skipped) > 1 else ''} {codes(self._via_skipped)}) "
                  f"skipped: neither a drill nor a pad")

    def _flip_x_coords(self, shapeStr: str) -> str:
        """Invert all x-coordinates in geometric entries for bottom-layer components."""
        # The shape outline is already baked into shapeStr; use regex to negate each x value.
        regex    = r"(?:\bstart|center|mid|end\b)\s+(-?[\d.]+)\s+(-?[\d.]+)"
        new_str  = ""
        prev_end = 0
        for m in re.finditer(regex, shapeStr, re.MULTILINE | re.UNICODE):
            flipped_x = -float(m.group(1))
            new_str  += shapeStr[prev_end:m.start(1)] + f"{flipped_x:.{self.di_Ac}f}"
            prev_end  = m.end(1)
        return new_str + shapeStr[prev_end:]

    def _build_pad_attr(self, shape: dict[str, Any], layerB: bool) -> str:
        """Return the (attr smd) or (attr through_hole) line based on drill size.

        drillCode ≤ 0: SMD pad.  0 = no drill; -1 = drill below dcMin (special case:
        an SMD pad placed on both F.Cu and B.Cu – see drillcodes notes).
        drillCode > 0: through-hole pad.
        """
        first_code = shape['pinDescr'][0]['code']
        return (
            "     (attr smd)\n"
            if self.drillCode[first_code] <= 0
            else "     (attr through_hole)\n"
        )

    def _build_pads(self, shape: dict[str, Any], padnet: list[int],
                    crot: float, layerB: bool) -> str:
        """Return KiCad pad strings for all pins of a component."""
        result = ''
        for pidx, pad in enumerate(shape['pinDescr']):
            pinLayer: list[str] = ["", "", ""]   # [0]=Front pad, [1]=all Inner pads, [2]=Back pad
            fpaste = bpaste     = ""

            for layer_entry in pad['layers']:
                # Collect pad layer strings; swap F.Cu↔B.Cu for bottom-layer components.
                pin_id = layer_entry[1]
                if layerB and pin_id in ('F.Cu', 'B.Cu'):
                    pin_id = {'F.Cu': 'B.Cu', 'B.Cu': 'F.Cu'}[pin_id]
                pinLayer[{'F': 0, 'B': 2, 'I': 1}[pin_id[0]]] += f' "{pin_id}"'

            if self.drillCode[pad['code']] == 0:
                bpaste = ' "B.Paste"'
                fpaste = ' "F.Paste"'

            paste_map = {'"F.Cu"': '{fp} "F.Mask"', '"B.Cu"': '{bp} "B.Mask"'}
            if pinLayer[0]:
                pinLayer[0] += paste_map[pinLayer[0].strip()].format(fp=fpaste, bp=bpaste)
            if pinLayer[2]:
                pinLayer[2] += paste_map[pinLayer[2].strip()].format(fp=fpaste, bp=bpaste)

            dc = self.drillCode[pad['code']]
            if dc <= 0:
                # SMD pad. dc=-1 means an SMD pad intentionally placed on both F.Cu and B.Cu.
                if pinLayer[0]:
                    result += self._add_pad(padnet[pidx], pad, "smd roundrect",
                                            crot, "", pinLayer[0], layerB)
                if pinLayer[2]:
                    result += self._add_pad(padnet[pidx], pad, "smd roundrect",
                                            crot, "", pinLayer[2], layerB)
            elif abs(self.pads[2].at[pad['code'], 'Ysize']) == dc:
                # NPTH: pad size equals drill size → single pad, no annular ring.
                result += self._add_pad(padnet[pidx], pad, "np_thru_hole circle",
                                        crot, dc, ' "*.Cu" "*.Mask"', layerB)
            else:
                # PTH: first add the drilled hole (pad size ≈ drill diameter, set in _add_pad),
                # then add Front, Inner, and Back pads individually as SMD pads.
                result += self._add_pad(padnet[pidx], pad, "thru_hole roundrect",
                                        crot, dc, ' "*.Cu" "*.Mask"', layerB)
                for pl in pinLayer:
                    if pl:
                        result += self._add_pad(padnet[pidx], pad, "smd roundrect",
                                                crot, "", pl, layerB)
        return result

    def _add_pad(self, padnet: int, pad: PadDescriptor, padshape: str,
                 crot: float, drCode: float | str, pinLayer: str,
                 layerB: bool) -> str:
        """Return a KiCad pad S-expression string."""
        layer_idx  = {'*': 0, 'F': 0, 'B': 2, 'I': 1}.get(pinLayer[2:3])
        net_num    = padnet if padnet != 65536 else 0   # 65536 = unconnected; map to KiCad net 0.
        pad_x      = -pad['relx'] if layerB else pad['relx']   # Invert x for bottom-layer pads.

        if padshape == "thru_hole roundrect":
            # PTH drill pad: create a pad slightly larger than the drill hole so KiCad
            # has a copper annulus to attach the net to (deliberate minimum annular
            # ring -- see PTH_PAD_MIN_ANNULAR_MM; this is NOT a float-rounding fudge).
            pad_w = pad_h = round(drCode + PTH_PAD_MIN_ANNULAR_MM, self.di_Ac)
            pad_offset    = 0
            pad_rr        = PTH_PAD_ROUND_RATIO
        else:
            pad_w      = self.pads[layer_idx].at[pad['code'], 'Xsize']
            pad_h      = self.pads[layer_idx].at[pad['code'], 'Ysize']
            pad_offset = self.pads[layer_idx].at[pad['code'], 'Xoffset']
            if layerB and pad['rot'] in (0, 180):
                pad_offset = -pad_offset   # Invert offset for bottom-layer pads.
            pad_rr     = self.pads[layer_idx].at[pad['code'], 'roundratio']
            if pad_w == 0 or pad_h == 0:
                # No size for this pad code in the technology tables: written
                # as is (KiCad pins it to 1 µm on load), reported per shape.
                codes = self._zero_pads.setdefault(self._current_shape, {})
                codes[pad['code']] = codes.get(pad['code'], 0) + 1

        return (
            f'     (pad "{pad["name"]}" {padshape}\n'
            f'           (at {self._f(pad_x)} {self._f(pad["rely"])} {self._f(pad["rot"] + crot)})\n'
            f'           (size {self._f(pad_w)} {self._f(pad_h)})\n'
            f'           (drill {drCode}\n'
            f'                   (offset {self._f(pad_offset)} 0)\n'
            f'           )\n'
            f'           (layers{pinLayer})\n'
            f'           (roundrect_rratio {pad_rr})\n'
            f'           (net {net_num} "{self.nets[net_num]}")\n'
            f'           (clearance {self.pads[layer_idx].at[pad["code"], "clearance"]})\n'
            f'     )\n'
        )

    # -----------------------------------------------------------------------
    # *L – Subrecords (traces, arcs, polygons)
    # -----------------------------------------------------------------------

    def _handle_subrecord(self, line: str) -> None:
        match line[2]:
            case 'T': self._handle_trace(line)
            case 'V': self._handle_vector(line)
            case 'A': self._handle_arc_trace(line)
            case 'P': self._handle_polygon(line)
            case _:
                if self.args.verbose:
                    print(line[2]+"\n")

    def _handle_trace(self, line: str) -> None:
        """*LT – Horizontal, vertical, and 45° traces.

        Header: <layer> <coord1>
        Data lines: <coord2> <coord3> <netnr> <trace_code> <trace_type> <orientation>
          trace_type:  0=fixed, 128=variable
          orientation: 1=horizontal, 2=vertical, 4=north-east diagonal, 8=south-east diagonal
        Last data line ends with ';'.
        """
        tline  = [int(i) for i in line[4:].split()]
        tlayer = layer_from_bit(tline[0] - 1)[1]
        coord1 = self.units_to_mm(int(tline[1]))

        while True:
            trace_line = self._readline()
            if len(trace_line) > 1:
                tarr   = trace_line.split()
                coord2 = self.units_to_mm(int(tarr[0]))
                coord3 = self.units_to_mm(int(tarr[1]))
                netnr  = self._map_ddf_to_kicad_net(int(tarr[2]))
                width  = self.units_to_mm(self.traceWidth[int(tarr[3])])
                orient = int(tarr[5][0])

                # Track the tightest clearance seen on any segment for this net.
                tcode = int(tarr[3])
                clr   = self.traceClearance.get(tcode, 0)
                if clr > 0 and netnr > 0:
                    self.netMinClearance[netnr] = min(
                        self.netMinClearance.get(netnr, clr), clr)

                match orient:
                    case 1:     # horizontal
                        x1, y1 = self._f(coord2 + self.offsetX), self._f(-coord1 + self.offsetY)
                        x2, y2 = self._f(coord3 + self.offsetX), self._f(-coord1 + self.offsetY)
                    case 2:     # vertical
                        x1, y1 = self._f(coord1 + self.offsetX), self._f(-coord2 + self.offsetY)
                        x2, y2 = self._f(coord1 + self.offsetX), self._f(-coord3 + self.offsetY)
                    case 4:     # north-east diagonal
                        half1 = (coord1 - coord2) / 2
                        half2 = (coord1 - coord3) / 2
                        x1 = self._f(coord2 + half1 + self.offsetX)
                        y1 = self._f(half1           + self.offsetY)
                        x2 = self._f(coord3 + half2 + self.offsetX)
                        y2 = self._f(half2           + self.offsetY)
                    case 8:     # south-east diagonal
                        half1 = (coord2 - coord1) / 2
                        half2 = (coord3 - coord1) / 2
                        x1 = self._f(coord2 - half1 + self.offsetX)
                        y1 = self._f(half1           + self.offsetY)
                        x2 = self._f(coord3 - half2 + self.offsetX)
                        y2 = self._f(half2           + self.offsetY)

                self.kicad.write(
                    f'  (segment\n'
                    f'        (start {x1} {y1})\n'
                    f'        (end {x2} {y2})\n'
                    f'        (width {self._f(width)})\n'
                    f'        (layer "{tlayer}")\n'
                    f'        (net {netnr})\n'
                    f'  )\n'
                )
            if ';' in trace_line:
                break

    def _handle_vector(self, line: str) -> None:
        """*LV – Arbitrary-angle vector traces.

        Format: <layer> <x1> <y1> <x2> <y2> <netnr> <trace_code> <trace_type>
        """
        vline  = [int(i) for i in line[4:].split()]
        vlayer = layer_from_bit(vline[0] - 1)[1]
        vnetnr = self._map_ddf_to_kicad_net(vline[5])
        vtcode = vline[6]
        vclr   = self.traceClearance.get(vtcode, 0)
        if vclr > 0 and vnetnr > 0:
            self.netMinClearance[vnetnr] = min(
                self.netMinClearance.get(vnetnr, vclr), vclr)
        self.kicad.write(
            f'  (segment\n'
            f'        (start {self._f(self.units_to_mm(vline[1]) + self.offsetX)}'
            f' {self._f(-self.units_to_mm(vline[2]) + self.offsetY)})\n'
            f'        (end {self._f(self.units_to_mm(vline[3]) + self.offsetX)}'
            f' {self._f(-self.units_to_mm(vline[4]) + self.offsetY)})\n'
            f'        (width {self._f(self.units_to_mm(self.traceWidth[vtcode]))})\n'
            f'        (layer "{vlayer}")\n'
            f'        (net {vnetnr})\n'
            f'  )\n'
        )

    def _handle_arc_trace(self, line: str) -> None:
        """*LA – Arc trace."""
        # <layer> <cx> <cy> <radius> <arc1> <arc2> <netnr> <trace code> <trace type>
        aline       = [int(i) for i in line[4:].split()]
        alayer      = layer_from_bit(aline[0] - 1)[1]
        centre_x    = self.units_to_mm(aline[1])
        centre_y    = self.units_to_mm(aline[2])
        radius      = self.units_to_mm(aline[3])
        start_angle = aline[4] / 64
        span_angle  = aline[5] / 64
        anetnr      = self._map_ddf_to_kicad_net(aline[6])
        atcode      = aline[7]
        # Zero width causes a KiCad import bug: it is silently changed to 0.1mm.
        # Use _KICAD_NEAR_ZERO_MM as a near-zero sentinel that KiCad accepts without altering.
        atWidth: float = (
            self.units_to_mm(self.traceWidth[atcode]) if atcode != 65535 else _KICAD_NEAR_ZERO_MM
        ) or _KICAD_NEAR_ZERO_MM

        aclr = self.traceClearance.get(atcode, 0)
        if aclr > 0 and anetnr > 0 and atcode != 65535:
            self.netMinClearance[anetnr] = min(
                self.netMinClearance.get(anetnr, aclr), aclr)

        (dx_start, dy_start), \
        (dx_mid,   dy_mid),   \
        (dx_end,   dy_end)    = calc_arc_points(radius, start_angle, span_angle)

        self.kicad.write(
            f'  (arc\n'
            f'        (start {self._f(centre_x + dx_start + self.offsetX)}'
            f' {self._f(-(centre_y + dy_start) + self.offsetY)})\n'
            f'        (mid   {self._f(centre_x + dx_mid   + self.offsetX)}'
            f' {self._f(-(centre_y - dy_mid)   + self.offsetY)})\n'
            f'        (end   {self._f(centre_x + dx_end   + self.offsetX)}'
            f' {self._f(-(centre_y + dy_end)   + self.offsetY)})\n'
            f'        (width {self._f(atWidth)})\n'
            f'        (layer "{alayer}")\n'
            f'        (net {anetnr})\n'
            f'  )\n'
        )

    def _handle_polygon(self, line: str) -> None:
        """*LP – Polygon fill zone."""
        lpline  = [int(i) for i in line[4:].split()]
        lplayer = layer_from_bit(lpline[0] - 1)[1]
        lpnetnr = self._map_ddf_to_kicad_net(lpline[1])
        lppat   = lpline[2]
        lpdist  = lpline[3] - self.traceWidth[lpline[4]]  # Hatch gap: Ultiboard stores centre-to-centre
        lptcode = lpline[4]                                #   distance; KiCad needs gap = c-to-c minus trace width.
        lpclear = self._f(self.units_to_mm(lpline[5]))
        width   = self._f(self.units_to_mm(self.traceWidth[lptcode]))

        if lppat in (3, 12):
            # Hatch fill: code 3 = 0°, code 12 = 45° → angle = (code−3) × 5
            lpHatch = (
                f'                (mode hatch)\n'
                f'                (hatch_thickness {width})\n'
                f'                (hatch_gap {self._f(self.units_to_mm(lpdist))})\n'
                f'                (hatch_orientation {(lppat - 3) * 5})\n'
            )
        else:
            lpHatch = ""

        self.kicad.write(
            f'  (zone\n'
            f'        (net {lpnetnr})\n'
            f'        (net_name "{self.nets[lpnetnr]}")\n'
            f'        (layer "{lplayer}")\n'
            f'        (hatch edge {width})\n'
            f'        (connect_pads\n'
            f'                (clearance {lpclear})\n'
            f'        )\n'
            f'        (min_thickness {width})\n'
            f'        (fill yes\n'
            f'                (thermal_gap {lpclear})\n'
            f'                (thermal_bridge_width {width})\n'
            f'{lpHatch}'
            f'        )\n'
            f'        (polygon\n'
            f'            (pts\n'
        )

        while True:
            poly_line = self._readline()
            if poly_line[0] == ';':
                break
            coords = [self.units_to_mm(int(i)) for i in poly_line.strip(':;').split()]
            for px, py in zip(coords[::2], coords[1::2]):
                self.kicad.write(
                    f'                (xy {self._f(px + self.offsetX)} {self._f(-py + self.offsetY)})\n')
            if ':' in poly_line or ';' in poly_line:
                break

        self.kicad.write("            )\n        )\n  )\n")

    # -----------------------------------------------------------------------
    # *V – Vias
    # -----------------------------------------------------------------------

    def _handle_via(self, line: str) -> None:
        # *V header: <x-pos>
        vxpos = self._f(self.units_to_mm(int(line[3:].split()[0])) + self.offsetX)

        while True:
            via_line = self._readline()
            if via_line[0] == ';':   # Last record ends with ';'
                break
            # Per-via line: <y-pos> <netnr> <pad_code> <pad_layerset> <pad_rot> <pad_shift> <via_index> <glue_flag>
            vf        = via_line.split()
            vypos     = self._f(-self.units_to_mm(int(vf[0])) + self.offsetY)
            vnetnr    = self._map_ddf_to_kicad_net(int(vf[1]))
            vpcode    = int(vf[2])
            mask_bits = bin(int(vf[3], 16) & int(self.layerMask, 16))[2:].zfill(32)[::-1]
            vlayers   = sorted(compress(layersCu, (int(x) for x in mask_bits)))

            if 'F.Cu' in str(vlayers) and 'B.Cu' in str(vlayers):
                via_type    = ""                                         # Full through-hole via (F.Cu to B.Cu).
                vlayers_str = '"F.Cu" "B.Cu"'
            else:
                via_type    = "blind"                                    # Blind or buried via: use outermost layers.
                vlayers_str = " ".join(f'"{item[1]}"' for item in (vlayers[0], vlayers[-1]))

            f_size = abs(self.pads[0].at[vpcode, "Ysize"])
            i_size = abs(self.pads[1].at[vpcode, "Ysize"])
            b_size = abs(self.pads[2].at[vpcode, "Ysize"])
            v_drill: float = self.drillCode[vpcode]
            has_pad = f_size > 0 or i_size > 0 or b_size > 0

            # Via codes lacking a drill and/or a pad (Ultiboard accepts them;
            # KiCad needs both, with the pad larger than the drill):
            #   pad, no drill  -> a 0.001 mm drill (KiCad silently replaces a
            #                     zero drill with 0.3 mm); keeps the layers
            #                     connected, the hole is not to be drilled
            #   drill, no pad  -> pad = drill + PTH_PAD_MIN_ANNULAR_MM, the
            #                     same minimum ring as padless PTH holes
            #   neither        -> nothing physical to write: skipped
            if v_drill <= 0 and not has_pad:
                self._via_skipped[vpcode] = self._via_skipped.get(vpcode, 0) + 1
                print(f"Warning: via at ({vxpos}, {vypos}) skipped: via code {vpcode} "
                      f"has neither a drill nor a pad in the technology tables")
                if ';' in via_line:
                    break
                continue
            if v_drill <= 0:
                v_drill = VIA_MICRODRILL_MM
                self._via_microdrill[vpcode] = self._via_microdrill.get(vpcode, 0) + 1
                print(f"Warning: via at ({vxpos}, {vypos}), net "
                      f"\"{self.nets.get(vnetnr, '')}\": via code {vpcode} has no drill -- "
                      f"written with a {VIA_MICRODRILL_MM} mm drill")
            elif not has_pad:
                f_size = i_size = b_size = round(v_drill + PTH_PAD_MIN_ANNULAR_MM, self.di_Ac)
                self._via_min_ring[vpcode] = self._via_min_ring.get(vpcode, 0) + 1
                print(f"Warning: via at ({vxpos}, {vypos}), net "
                      f"\"{self.nets.get(vnetnr, '')}\": via code {vpcode} has no pad -- "
                      f"written {f_size} mm over its {v_drill} mm drill (minimum ring)")
            # Only emit a padstack block when inner or back-copper annular ring
            # differs from the front-copper size (KiCad 9 padstack feature).
            # A via's padstack size is one diameter, as its own (size):
            # KiCad refuses a second value ("Expecting ')'").
            if i_size != f_size or b_size != f_size:
                padstack_str = (
                    f'        (padstack\n'
                    f'          (mode front_inner_back)\n'
                    f'          (layer "Inner"\n'
                    f'            (size {i_size})\n'
                    f'          )\n'
                    f'          (layer "B.Cu"\n'
                    f'            (size {b_size})\n'
                    f'          )\n'
                    f'        )\n'
                )
            else:
                padstack_str = ""
            self.kicad.write(
                f'  (via{" " + via_type if via_type else ""}\n'
                f'        (at {vxpos} {vypos})\n'
                f'        (size {f_size})\n'
                f'        (drill {v_drill})\n'
                f'        (layers {vlayers_str})\n'
                f'        (remove_unused_layers)\n'
                f'        (keep_end_layers)\n'
                f'{padstack_str}'
                f'        (net {vnetnr})\n'
                f'  )\n'
            )
            if ';' in via_line:
                break

    # -----------------------------------------------------------------------
    # *X – Text
    # -----------------------------------------------------------------------

    def _handle_text(self, line: str) -> None:
        # *X format: <x-pos> <y-pos> <height> <width> <thickness> <rotation> <layer> <text string>
        # Notes: rotation is in degrees×64.
        #        The text string is extracted as raw bytes at the top of the main loop
        #        (stored in self._line_b) for correct CP437/CP850→Unicode mapping.
        xfields  = line[3:].split(None, 7)[:7]
        text_x   = round(self.units_to_mm(int(xfields[0])) + self.offsetX, self.di_Ac)
        text_y   = round(-self.units_to_mm(int(xfields[1])) + self.offsetY, self.di_Ac)
        text_h_raw = self.units_to_mm(int(xfields[2])) / self.fontHeightRatio  # Empirical scale; kept
                                                                                 # unrounded: feeds text_t below
                                                                                 # as well as text_h itself.
        text_h   = round(text_h_raw, self.di_Ac)
        text_w   = round(self.units_to_mm(int(xfields[3])) * self.fontWidthRatio,  self.di_Ac)
        text_t   = round(int(xfields[4]) * text_h_raw / self.fontThickRatio, self.di_Ac)
        text_bot = int(xfields[5]) / 64 < 0
        text_rot = int(xfields[5]) / 64
        text_lay = int(xfields[6])
        text_mir = ""

        if text_bot or (text_lay % 2 == 0 and text_lay > 0):
            text_mir = "                (justify mirror)\n"   # Mirror when layer is even and non-zero, or bottom layer.
            text_rot = -text_rot                               # Invert rotation for mirrored text.

        if text_lay == 0:
            # Layer 0: positive rotation → F.SilkS, negative rotation → B.SilkS.
            real_layer = 'B.SilkS' if text_bot else 'F.SilkS'
        else:
            real_layer = layer_from_bit(text_lay - 1)[1]   # Layers 1–32: copper layers.

        self.kicad.write(
            f'  (gr_text "{ubfont(self._line_b) if self._line_b else self._line_b}"\n'
            f'        (at {text_x} {text_y} {text_rot})\n'
            f'        (layer "{real_layer}")\n'
            f'        (effects\n'
            f'                (font\n'
            f'                        (face "{self.args.font}")\n'
            f'                        (size {text_h} {text_w})\n'
            f'                        (thickness {text_t})\n'
            f'                )\n{text_mir}'
            f'        )\n'
            f'  )\n'
        )

    # -----------------------------------------------------------------------
    # Post-loop: power-plane zones
    # -----------------------------------------------------------------------

    def _write_power_plane_zones(self) -> None:
        """Write any power-plane copper zones collected during header parsing."""
        for pwr_layer, pwr_net in self.zoneParams.get('pwrPlanes', []):
            self.kicad.write(
                f'  (zone\n'
                f'        (net {pwr_net})\n'
                f'        (net_name "{self.nets[pwr_net]}")\n'
                f'        (layer "{pwr_layer}")\n'
                f'        (hatch edge {self._f(self.defaultWidth)})\n'
                f'        (connect_pads\n'
                f'                (clearance {self._f(self.boardClearance)})\n'
                f'        )\n'
                f'        (min_thickness {self._f(self.defaultWidth)})\n'
                f'        (fill yes\n'
                f'                (thermal_gap {self._f(self.defaultThermalGap)})\n'
                f'                (thermal_bridge_width {self._f(self.defaultThermalWidth)})\n'
                f'        )\n'
                f'        (polygon\n'
                f'            (pts\n'
                f'                (xy {self.zoneParams["extX0"]} {self.zoneParams["extY0"]})\n'
                f'                (xy {self.zoneParams["extX1"]} {self.zoneParams["extY0"]})\n'
                f'                (xy {self.zoneParams["extX1"]} {self.zoneParams["extY1"]})\n'
                f'                (xy {self.zoneParams["extX0"]} {self.zoneParams["extY1"]})\n'
                f'            )\n'
                f'        )\n'
                f'  )\n'
            )

    # -----------------------------------------------------------------------
    # KiCad project file (.kicad_pro) generator
    # -----------------------------------------------------------------------

    def write_kicad_pro(self, pro_path: str) -> None:
        """Write a .kicad_pro JSON file derived from the DDF technology data.

        The file captures:
        - One net class per DDF trace code (all 32 codes, track_width + clearance).
          The Default net class uses the smallest trace clearance found.
        - DRC constraint values: smallest pad clearance, smallest trace
          clearance, smallest drill, smallest via size, and board clearance.
        - The full rule_severities table as specified.
        """
        # ── Derived values ──────────────────────────────────────────────────

        # All 32 DDF trace codes are always emitted.  Codes with width = 0
        # (unused in this design) keep width = 0 and fall back to defaultClearance.
        active_trace_codes = {
            code: (
                self.units_to_mm(self.traceWidth.get(code, 0)),
                self.units_to_mm(self.traceClearance[code])
                    if self.traceWidth.get(code, 0) > 0
                       and self.traceClearance.get(code, 0) > 0
                    else self.defaultClearance,
            )
            for code in range(32)
        }

        # Smallest trace clearance across all codes (including code 0 sentinel)
        all_clearances_mm = [
            self.units_to_mm(v) for v in self.traceClearance.values() if v > 0
        ]
        min_trace_clearance = round(min(all_clearances_mm), self.di_Ac) if all_clearances_mm else self.defaultClearance

        # Smallest and default track width (ignore zero-width sentinel codes)
        all_widths_mm = [w for w, _ in active_trace_codes.values() if w > 0]
        min_track_width = round(min(all_widths_mm), self.di_Ac) if all_widths_mm else self.defaultWidth

        # Smallest pad clearance: take the minimum across all three pad layers
        pad_clearances: list[float] = []
        for layer_idx in range(3):
            for code in range(256):
                c = self.pads[layer_idx].at[code, 'clearance']
                if c > 0:
                    pad_clearances.append(c)
        min_pad_clearance = round(min(pad_clearances), self.di_Ac) if pad_clearances else self.NPTHclearance

        # Smallest through-hole diameter: Ysize of PTH pad codes (0–239) where a real
        # drill exists, plus all via drill codes (240–255).  This becomes
        # min_through_hole_diameter, which KiCad applies to both pads and vias.
        pth_drills: list[float] = []
        for layer_idx in range(3):
            for code in range(240):
                ysize = self.pads[layer_idx].at[code, 'Ysize']
                if ysize > 0 and self.drillCode[code] > 0:   # real PTH, not SMD/NPTH
                    pth_drills.append(ysize)
        via_drills_all: list[float] = [
            self.drillCode[code] for code in range(240, 256) if self.drillCode[code] > 0
        ]
        # The default via (size and drill) comes from complete via codes only
        # -- a drill AND a front pad -- so its drill is always smaller than
        # the via. A drill-only code is still a real hole (written with the
        # minimum ring, see _handle_via) and counts for the smallest hole
        # above; a pad-only code (written with VIA_MICRODRILL_MM) has no
        # drill in the table and counts for neither.
        complete_via_codes = [
            code for code in range(240, 256)
            if self.drillCode[code] > 0 and self.pads[0].at[code, 'Ysize'] > 0
        ]
        all_drills = pth_drills + via_drills_all
        min_through_hole = round(min(all_drills), self.dr_Ac) if all_drills else 0.0

        # Smallest via: prefer pad Ysize (copper diameter) for codes 240–255.
        via_sizes: list[float] = []
        via_drills: list[float] = []
        for code in complete_via_codes:
            via_drills.append(self.drillCode[code])
            via_sizes.append(self.pads[0].at[code, 'Ysize'])   # layer 0 = Front
        min_via_drill    = round(min(via_drills), self.dr_Ac) if via_drills  else min_through_hole
        min_via_diameter = round(min(via_sizes),  self.di_Ac) if via_sizes   else min_via_drill * 2

        # Smallest annular width across ALL PTH pads and vias.
        # For PTH pads: ann = (Xsize - Ysize) / 2 - abs(Xoffset)
        #   Xsize   = total copper width (left half + right half of pad)
        #   Ysize   = drill diameter (= pad height for PTH)
        #   Xoffset = shift of pad centre relative to hole centre (non-centric pads)
        # For vias:   ann = (pad_copper_diameter - drill_diameter) / 2
        # Only include pads where drillCode > 0 (excludes SMD/NPTH) and ann > 0.
        annular_widths: list[float] = []
        for layer_idx in range(3):
            for code in range(240):
                if self.drillCode[code] <= 0:
                    continue                                    # SMD or NPTH – skip
                xsize   = self.pads[layer_idx].at[code, 'Xsize']
                ysize   = self.pads[layer_idx].at[code, 'Ysize']
                xoffset = self.pads[layer_idx].at[code, 'Xoffset']
                if xsize <= 0 or ysize <= 0:
                    continue
                ann = (xsize - ysize) / 2 - abs(xoffset)
                if ann > _ANNULAR_WIDTH_NOISE_EPS_MM:               # ignore float-rounding noise
                    annular_widths.append(round(ann, self.di_Ac))
        for code in range(240, 256):
            pad_y = self.pads[0].at[code, 'Ysize']
            drill = self.drillCode[code]
            if pad_y > 0 and drill > 0:
                ann = (pad_y - drill) / 2
                if ann > _ANNULAR_WIDTH_NOISE_EPS_MM:
                    annular_widths.append(round(ann, self.di_Ac))
        if self._via_min_ring:
            # Padless vias written with the minimum ring (see _handle_via):
            # Ultiboard accepted them, so the board minimum allows for them.
            annular_widths.append(round(PTH_PAD_MIN_ANNULAR_MM / 2, self.di_Ac))
        min_annular_width = round(min(annular_widths), self.di_Ac) if annular_widths \
                            else round((min_via_diameter - min_via_drill) / 2, self.di_Ac)

        # Build a reverse lookup: net name → KiCad net number, needed to find
        # which netMinClearance entries belong to each TraceCode_N class.
        name_to_kicad_net: dict[str, int] = {
            name: num for num, name in self.nets.items() if isinstance(name, str)
        }

        # ── Net classes ─────────────────────────────────────────────────────

        # Build one netclass entry for all 32 DDF trace codes.
        # The clearance for each class starts from the DDF traceClearance value,
        # then is tightened to the smallest clearance actually seen on any
        # routed segment (LT/LV/LA) belonging to any net assigned to that class.
        netclass_entries: list[dict] = []

        # Default net class (always first, no pattern restrictions)
        netclass_entries.append({
            "bus_width":         12,
            "clearance":         min_trace_clearance,
            "diff_pair_gap":     round(min_trace_clearance * DIFF_PAIR_GAP_RATIO, self.di_Ac),
            "diff_pair_via_gap": round(min_trace_clearance * DIFF_PAIR_GAP_RATIO, self.di_Ac),
            "diff_pair_width":   min_track_width,
            "line_style":        0,
            "microvia_diameter": min_via_diameter,
            "microvia_drill":    min_via_drill,
            "name":              "Default",
            "pcb_color":         "rgba(0, 0, 0, 0.000)",
            "schematic_color":   "rgba(0, 0, 0, 0.000)",
            "track_width":       min_track_width,
            "via_diameter":      min_via_diameter,
            "via_drill":         min_via_drill,
            "wire_width":        6,
        })

        # One class per DDF trace code (all 32)
        for code in range(32):
            width_mm, clearance_mm = active_trace_codes[code]

            # Find the tightest clearance seen on any routed segment for any net
            # assigned to this trace code, and apply it if smaller than the DDF value.
            nets_in_class = [
                name_to_kicad_net[name]
                for name, tc in self.netTraceCode.items()
                if tc == code and name in name_to_kicad_net
            ]
            seg_clears_du = [
                self.netMinClearance[kn]
                for kn in nets_in_class
                if kn in self.netMinClearance
            ]
            if seg_clears_du:
                # Convert the tightest segment clearance to mm and take the minimum.
                effective_clearance_mm = min(
                    clearance_mm,
                    self.units_to_mm(min(seg_clears_du))
                )
            else:
                effective_clearance_mm = clearance_mm

            netclass_entries.append({
                "bus_width":         12,
                "clearance":         round(effective_clearance_mm, self.di_Ac),
                "diff_pair_gap":     round(effective_clearance_mm * DIFF_PAIR_GAP_RATIO, self.di_Ac),
                "diff_pair_via_gap": round(effective_clearance_mm * DIFF_PAIR_GAP_RATIO, self.di_Ac),
                "diff_pair_width":   round(width_mm, self.di_Ac),
                "line_style":        0,
                "microvia_diameter": min_via_diameter,
                "microvia_drill":    min_via_drill,
                "name":              f"TraceCode_{code}",
                "pcb_color":         "rgba(0, 0, 0, 0.000)",
                "schematic_color":   "rgba(0, 0, 0, 0.000)",
                "track_width":       round(width_mm, self.di_Ac),
                "via_diameter":      min_via_diameter,
                "via_drill":         min_via_drill,
                "wire_width":        6,
            })

        # ── Net-to-class pattern assignments ────────────────────────────────
        # Each *N record in the DDF carries the trace code used by that net
        # (field index 1 in the netlist line).  Map every net name to its
        # corresponding TraceCode_N class.  Nets with trace code 0 are excluded
        # (code 0 is the DDF sentinel for "no routing constraint").
        netclass_patterns: list[dict] = [
            {"netclass": f"TraceCode_{tc}", "pattern": net_name}
            for net_name, tc in sorted(self.netTraceCode.items())
            if tc != 0
        ]

        if self.args.verbose:
            print(f"\n.kicad_pro  →  {pro_path}")
            print(f"  Net classes:          {len(netclass_entries)} "
                  f"(Default + {len(netclass_entries) - 1} trace codes)")
            print(f"  Net assignments:      {len(netclass_patterns)} nets mapped to trace-code classes")
            print(f"  Min trace clearance:  {min_trace_clearance:.6f} mm")
            print(f"  Min pad clearance:    {min_pad_clearance:.6f} mm")
            print(f"  Min annular width:    {min_annular_width:.6f} mm")
            print(f"  Min through-hole:     {min_through_hole:.6f} mm")
            print(f"  Min via diameter:     {min_via_diameter:.6f} mm")
            print(f"  Min via drill:        {min_via_drill:.6f} mm")
            print(f"  Board clearance:      {self.boardClearance:.6f} mm\n")

        # ── Assemble JSON ────────────────────────────────────────────────────

        board_section: dict = {
            "design_settings": {
                    "defaults": {
                        "board_outline_line_width": self.lineWidth,
                        "copper_line_width":        self.lineWidth,
                        "copper_text_size_h":       1.5,
                        "copper_text_size_v":       1.5,
                        "copper_text_thickness":    0.3,
                        "other_line_width":         self.lineWidth,
                        "silk_line_width":          self.lineWidth,
                        "silk_text_size_h":         1.5,
                        "silk_text_size_v":         1.5,
                        "silk_text_thickness":      0.3,
                    },
                    "rules": {
                        "max_error":                      0.005,
                        "min_clearance":                  min_trace_clearance,
                        "min_connection":                 0.0,
                        "min_copper_edge_clearance":      round(self.boardClearance, self.di_Ac),
                        "min_groove_width":               0.0,
                        "min_hole_clearance":             min_pad_clearance,
                        "min_hole_to_hole":               min_through_hole,
                        "min_microvia_diameter":          min_via_diameter,
                        "min_microvia_drill":             min_via_drill,
                        "min_resolved_spokes":            2,
                        "min_silk_clearance":             0.0,
                        "min_text_height":                0.8,
                        "min_text_thickness":             0.08,
                        "min_through_hole_diameter":      min_through_hole,
                        "min_track_width":                min_track_width,
                        "min_via_annular_width":          min_annular_width,
                        "min_via_diameter":               min_via_diameter,
                        "solder_mask_to_copper_clearance": getattr(
                            self.args, "solder_mask_to_copper_clearance",
                            get_board_default("solder_mask_to_copper_clearance")),
                        "use_height_for_length_calcs":    True,
                    },
                    "rule_severities": {
                        "annular_width":                       "error",
                        "clearance":                           "error",
                        "connection_width":                    "warning",
                        "copper_edge_clearance":               "warning",
                        "copper_sliver":                       "warning",
                        "courtyards_overlap":                  "ignore",
                        "creepage":                            "error",
                        "diff_pair_gap_out_of_range":          "error",
                        "diff_pair_uncoupled_length_too_long": "error",
                        "drill_out_of_range":                  "warning",
                        "duplicate_footprints":                "warning",
                        "extra_footprint":                     "warning",
                        "footprint":                           "error",
                        "footprint_filters_mismatch":          "ignore",
                        "footprint_symbol_mismatch":           "warning",
                        "footprint_type_mismatch":             "ignore",
                        "hole_clearance":                      "error",
                        "hole_to_hole":                        "warning",
                        "holes_co_located":                    "warning",
                        "invalid_outline":                     "error",
                        "isolated_copper":                     "warning",
                        "item_on_disabled_layer":              "error",
                        "items_not_allowed":                   "error",
                        "length_out_of_range":                 "error",
                        "lib_footprint_issues":                "ignore",
                        "lib_footprint_mismatch":              "ignore",
                        "malformed_courtyard":                 "error",
                        "microvia_drill_out_of_range":         "error",
                        "mirrored_text_on_front_layer":        "warning",
                        "missing_courtyard":                   "ignore",
                        "missing_footprint":                   "warning",
                        "net_conflict":                        "warning",
                        "nonmirrored_text_on_back_layer":      "warning",
                        "npth_inside_courtyard":               "ignore",
                        "padstack":                            "ignore",
                        "pth_inside_courtyard":                "ignore",
                        "shorting_items":                      "error",
                        "silk_edge_clearance":                 "ignore",
                        "silk_over_copper":                    "ignore",
                        "silk_overlap":                        "ignore",
                        "skew_out_of_range":                   "error",
                        "solder_mask_bridge":                  "warning",
                        "starved_thermal":                     "error",
                        "text_height":                         "ignore",
                        "text_on_edge_cuts":                   "error",
                        "text_thickness":                      "ignore",
                        "through_hole_pad_without_hole":       "error",
                        "too_many_vias":                       "error",
                        "track_angle":                         "error",
                        "track_dangling":                      "warning",
                        "track_segment_length":                "error",
                        "track_width":                         "error",
                        "tracks_crossing":                     "error",
                        "unconnected_items":                   "error",
                        "unresolved_variable":                 "error",
                        "via_dangling":                        "warning",
                        "zones_intersect":                     "error",
                    },
                },
        }

        net_settings_section: dict = {
            "classes":              netclass_entries,
            "meta":                 {"version": 3},
            "net_colors":           None,
            "netclass_assignments": None,
            "netclass_patterns":    netclass_patterns,
        }

        pcbnew_last_paths: dict = {
            "gencad":       "",
            "idf":          "",
            "netlist":      "",
            "plot":         "",
            "pos_files":    "",
            "specctra_dsn": "",
            "step":         "",
            "svg":          "",
            "vrml":         "",
        }

        # ── Create-or-update ─────────────────────────────────────────────────
        # KIUB owns "board", "net_settings", and pcbnew.last_paths. If a
        # .kicad_pro already exists at this path -- most commonly because
        # KIUC created one first from the matching schematic -- it is loaded
        # and only KIUB's own keys are touched; every KIUC-owned key (erc,
        # schematic.*, sheets, libraries, cvpcb, text_variables, ...) is left
        # exactly as found. This mirrors KIUC's own write_kicad_pro() merge
        # logic (kiuc_writer.py) so both tools can write into the same
        # project file without clobbering each other's settings, regardless
        # of which one runs first.
        if os.path.exists(pro_path):
            try:
                with open(pro_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                # Corrupt or unreadable existing file -- do not attempt to
                # patch something that can't be parsed; start from a clean,
                # minimal structure instead of silently destroying whatever
                # was there.
                data = {}
                if self.args.verbose:
                    print(f"  Warning: existing {pro_path} could not be "
                          f"parsed; rebuilding it from scratch.")

            data["board"] = board_section
            data.setdefault("meta", {})
            data["meta"]["filename"] = os.path.basename(pro_path)
            data["meta"].setdefault("version", 1)
            data["net_settings"] = net_settings_section
            data.setdefault("pcbnew", {})
            data["pcbnew"].setdefault("last_paths", {})
            data["pcbnew"]["last_paths"].update(pcbnew_last_paths)
            data["pcbnew"].setdefault("page_layout_descr_file", "")

            if self.args.verbose:
                print(f"  Existing .kicad_pro found at {pro_path} -- "
                      f"updating board/net_settings/pcbnew.last_paths only.")
        else:
            data = {
                "board":        board_section,
                "meta":         {"filename": os.path.basename(pro_path), "version": 1},
                "net_settings": net_settings_section,
                "pcbnew":       {"last_paths": pcbnew_last_paths, "page_layout_descr_file": ""},
            }

        with open(pro_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')

    # -----------------------------------------------------------------------
    # Internal utility
    # -----------------------------------------------------------------------

    def _readline(self) -> str:
        """Read and decode one line from the DDF file."""
        return self.ddf.readline().decode("CP437").strip()

    def _pushback(self, line: str) -> None:
        """Push a decoded record line back into the main convert() loop.
        The line is re-encoded so convert()'s raw-bytes branch will pick
        it up and re-dispatch it as the next top-level record."""
        self._pushback_raw = (line + "\n").encode("CP437")

# ---------------------------------------------------------------------------
# open_ddf() – shared helper used by both the CLI block below and kiub_gui.py
# ---------------------------------------------------------------------------

def _peek_ddf_version(path: str) -> int:
    """Peek at the *P record to read the DDF major version number,
    without opening the whole file. The version line immediately
    follows *P and looks like "4 60" or "3 3". Only the first 5 lines
    are read, so this is effectively free."""
    ddf_version = 4   # safe default
    with open(path, 'rb') as _f:
        for _ in range(5):
            raw = _f.readline()
            if not raw:
                break
            line = raw.decode('CP437', errors='replace').strip()
            if line.startswith('*P'):
                ver_line = _f.readline().decode('CP437', errors='replace').strip()
                try:
                    ddf_version = int(ver_line.split()[0])
                except (ValueError, IndexError):
                    pass
                break
    return ddf_version


def ensure_trailing_blank_line(text: str) -> str:
    """Ultiboard requires a DDF file's very last line to be blank to
    open it correctly -- confirmed necessary for any DDF file this tool
    writes or rewrites, not just fresh conversions. Strips whatever
    trailing whitespace/newlines *text* already has and appends exactly
    two newlines, guaranteeing one genuinely blank final line regardless
    of what was there before (a single trailing newline only terminates
    the last content line; it does not itself create a blank line after
    it -- that needs a second one).
    """
    return text.rstrip() + "\n\n"


def legacy_v3_conversion(path: str) -> "pathlib.Path | None":
    """For '<name>_V3.DDF', a V2/V3 file next to '<name>.DDF': that
    '<name>.DDF', which a KIUB before 2026-10-27 wrote as the V4.60
    conversion of this file, keeping this one as the original. None for any
    other file. Such a folder has no <name>_corrected manifest to tell, so
    the CLI and the GUI ask before converting '<name>_V3.DDF' as a design of
    its own."""
    import pathlib as _pathlib
    p = _pathlib.Path(path)
    if not p.stem.lower().endswith("_v3") or len(p.stem) <= 3:
        return None
    try:
        if _peek_ddf_version(path) not in (2, 3):
            return None
    except (OSError, ValueError):
        return None
    found = _find_by_name(str(p.parent), p.stem[:-3], (".ddf",))
    return _pathlib.Path(found) if found else None


def legacy_v3_question(path: str) -> str | None:
    """The question for legacy_v3_conversion, or None."""
    converted = legacy_v3_conversion(path)
    if converted is None:
        return None
    name = os.path.basename(path)
    return (f"{name} looks like the original kept by an earlier KIUB: {converted.name} next to it "
            f"is its V4.60 conversion, and the design is {converted.stem}.\n\n"
            f"To convert the design from this original, copy it over {converted.name} and convert "
            f"{converted.name}. Converting {name} itself makes it a design of its own, "
            f"{os.path.splitext(name)[0]}, whose board does not pair with {converted.stem}'s "
            f"schematic.")


def _load_kiub_v2v3_module():
    """Dynamically import kiub_v2v3.py from alongside this file. Raises
    SystemExit (after printing a message) if it isn't found -- matches
    open_ddf()'s existing behaviour."""
    _here = os.path.dirname(os.path.abspath(__file__))
    _v2v3_path = os.path.join(_here, "kiub_v2v3.py")
    if not os.path.exists(_v2v3_path):
        print(
            "Error: kiub_v2v3.py not found.\n"
            "Place kiub_v2v3.py in the same folder as kiub.py to convert V2/V3 files."
        )
        sys.exit(1)

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("kiub_v2v3", _v2v3_path)
    _mod  = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod


_kiub_library_module = None


def _load_kiub_library_module():
    """Import kiub_library.py from alongside this file (library board, footprint library),
    once: every caller gets the same module, so its LibraryError is the
    class callers catch. Raises SystemExit (after printing a message) if it
    isn't found, as _load_kiub_v2v3_module does."""
    global _kiub_library_module
    if _kiub_library_module is not None:
        return _kiub_library_module
    _here = os.path.dirname(os.path.abspath(__file__))
    _lib_path = os.path.join(_here, "kiub_library.py")
    if not os.path.exists(_lib_path):
        print(
            "Error: kiub_library.py not found.\n"
            "Place kiub_library.py in the same folder as kiub.py."
        )
        sys.exit(1)

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("kiub_library", _lib_path)
    _mod  = _ilu.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod      # its dataclasses look themselves up there
    _spec.loader.exec_module(_mod)
    _kiub_library_module = _mod
    return _mod


KICAD_FOLDER_SUFFIX = "_KiCad"     # <name>_KiCad: the KiCad project folder KIUB and KIUC share
FOOTPRINT_LINK_NICKNAME = "Ultiboard"   # the boards' footprint links, Ultiboard:<footprint>


_kiub_corrected_module = None


def _load_kiub_corrected_module():
    """Import kiub_corrected.py (the design's corrected folder, shared with
    KIUC as kiuc_corrected.py) from alongside this file, once, as
    _load_kiub_library_module does."""
    global _kiub_corrected_module
    if _kiub_corrected_module is not None:
        return _kiub_corrected_module
    _here = os.path.dirname(os.path.abspath(__file__))
    _path = os.path.join(_here, "kiub_corrected.py")
    if not os.path.exists(_path):
        print(
            "Error: kiub_corrected.py not found.\n"
            "Place kiub_corrected.py in the same folder as kiub.py."
        )
        sys.exit(1)

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("kiub_corrected", _path)
    _mod  = _ilu.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod      # its dataclasses look themselves up there
    _spec.loader.exec_module(_mod)
    _kiub_corrected_module = _mod
    return _mod


def locate(path: str):
    """Where the design of an input lives (kiub_corrected.locate): a file
    inside <name>_corrected belongs to the design <name>, whose originals
    are in the parent folder; any other file is its own design, named after
    it."""
    return _load_kiub_corrected_module().locate(os.path.abspath(path))


def design_name(path: str) -> str:
    """The name of the design a DDF (or library) belongs to, which names
    its KiCad output: the file's own name, or -- for a file in
    <name>_corrected -- <name>."""
    return locate(path).name


LIBRARY_FOLDER_SUFFIX = "_lib_KiCad"   # <name>_lib_KiCad: the output of a library-only input
LIBRARY_BOARD_SUFFIX = "_lib"          # <name>_lib.kicad_pcb: the library board
FOOTPRINT_LIBRARY_SUFFIX = ".pretty"
KICAD_MOD_VERSION = "20241229"          # the file format version kiub.py writes boards in
KIUB_GENERATOR_VERSION = "1.1.0"


DDF_LIBRARY, DDF_DESIGN, DDF_UNSURE = 'library', 'design', 'unsure'


def existing_outputs(plan: "OutputPlan") -> list[str]:
    """The boards of this conversion that already exist and would be
    replaced (asked first: they may hold work done in KiCad). The
    .kicad_pro is updated, not replaced, and the footprint library's
    files are replaced without asking."""
    return [p for p in (plan.board, plan.library_board) if p and os.path.exists(p)]


OVERWRITE_QUESTION = ("These files already exist and will be replaced -- changes made to them "
                      "in KiCad are lost:")


def ddf_kind(ddf_bytes: bytes) -> str:
    """What a DDF holds: DDF_LIBRARY -- shapes only, no components, nets,
    copper or vias (*C, *N, *L, *V; texts do not count) -- converts to a
    library only; DDF_DESIGN -- nets, copper or vias -- to a design and a
    library; DDF_UNSURE -- components but no nets, copper or vias, as
    Ultiboard's library DDFs place their shapes -- is the user's call."""
    if re.search(rb'(?m)^\r*\*[NLV]', ddf_bytes):
        return DDF_DESIGN
    if re.search(rb'(?m)^\r*\*C ', ddf_bytes):
        return DDF_UNSURE
    return DDF_LIBRARY


def input_kind(path: str) -> str:
    """ddf_kind of an input; an Ultiboard library (.LIB/.L55) is
    DDF_LIBRARY. DDF_DESIGN when it cannot be read (the conversion reports
    that)."""
    lib = _load_kiub_library_module()
    if lib.library_kind(path) == 'ultiboard':
        return DDF_LIBRARY
    try:
        with open(path, 'rb') as f:
            return ddf_kind(f.read())
    except OSError:
        return DDF_DESIGN


UNSURE_QUESTION = ("This DDF places components but holds no nets, copper or vias: it may be a "
                   "shape library, as Ultiboard's library DDFs place their shapes.")


def default_output_dir(path: str, library_only: bool = False) -> str:
    """The output folder, next to the design's originals and not to be
    chosen (CORRECTED_FOLDER.md, section 1): '<name>_KiCad' for a design --
    the KiCad project folder KIUB and KIUC share -- and '<name>_lib_KiCad'
    for a library-only input. An input inside <name>_corrected converts into
    the parent folder's, never into the corrected folder."""
    loc = locate(path)
    return str(loc.lib_kicad_dir if library_only else loc.kicad_dir)


def default_output_path(path: str, library_only: bool = False) -> str:
    """The output, '<name>.kicad_pcb' in default_output_dir. Its folder and
    name stem name every output file (see OutputPlan)."""
    return os.path.join(default_output_dir(path, library_only), f"{design_name(path)}.kicad_pcb")


class OutputPlan:
    """The files one conversion writes, all in one folder and named after
    one stem: a design gives <stem>.kicad_pcb/.kicad_pro (the design), and
    every conversion <stem>_lib.kicad_pcb/.kicad_pro (the library board:
    every shape laid out once), the footprint library <stem>.pretty and its
    fp-lib-table entry, and (GUI) <stem>_log.txt."""

    def __init__(self, outfile: str, library_only: bool) -> None:
        self.folder = os.path.dirname(os.path.abspath(outfile))
        base = os.path.basename(outfile)
        self.stem = base[:-len(".kicad_pcb")] if base.lower().endswith(".kicad_pcb") else base
        self.library_only = library_only
        lib = _load_kiub_library_module()
        self.nickname = lib.kicad_name(self.stem)

    def _path(self, suffix: str) -> str:
        return os.path.join(self.folder, self.stem + suffix)

    @property
    def board(self) -> str | None:
        return None if self.library_only else self._path(".kicad_pcb")

    @property
    def library_board(self) -> str:
        return self._path(LIBRARY_BOARD_SUFFIX + ".kicad_pcb")

    @property
    def project(self) -> str:
        """The project to open: the design's, or the library board's."""
        return os.path.splitext(self.board or self.library_board)[0] + ".kicad_pro"

    @property
    def log(self) -> str:
        return self._path("_log.txt")

    @property
    def pretty(self) -> str:
        return os.path.join(self.folder, self.nickname + FOOTPRINT_LIBRARY_SUFFIX)


def plan_footprint_library(args: argparse.Namespace, ddf_bytes: bytes,
                           plan: OutputPlan) -> list[str]:
    """Name the footprints on args (footprint name per shape): the boards
    link each footprint as Ultiboard:<footprint> and the footprint library
    holds it under that name. The shared naming rule (CORRECTED_FOLDER.md,
    section 8): KIUC's ddf_footprint_names gives the same names, so a change
    here is made there too. Returns report lines for renamed shapes.
    ddf_bytes is the DDF as converted (V2/V3 pre-converted)."""
    lib = _load_kiub_library_module()
    shapes = lib.shape_names(ddf_bytes.decode("CP437"))
    names, renamed = lib.footprint_names(shapes)
    args.footprint_names = names
    return [f"Footprint library: shape {line} (a character KiCad does not allow in a "
            f"footprint name)" for line in renamed]


def write_footprint_library(board_text: str, plan: OutputPlan,
                            report_marks: bool = True) -> list[str]:
    """Write the plan's .pretty -- one .kicad_mod per footprint of the
    library board (check marks included) -- and add it to the folder's
    fp-lib-table. Returns the report lines."""
    lib = _load_kiub_library_module()
    os.makedirs(plan.pretty, exist_ok=True)
    replaced = 0
    marked: list[str] = []
    footprints = lib.board_footprints(board_text)
    for name, block in footprints:
        path = os.path.join(plan.pretty, name + ".kicad_mod")
        replaced += os.path.exists(path)
        if f'(layer "{CHECK_LAYER}")' in block:
            marked.append(name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(lib.kicad_mod(block, name, KICAD_MOD_VERSION, KIUB_GENERATOR_VERSION))
    report = [f"Footprint library: {plan.pretty} ({len(footprints)} footprint"
              f"{'s' if len(footprints) != 1 else ''}"
              + (f", {replaced} replaced" if replaced else "") + ")"]
    if marked and report_marks:
        report.append(f"Footprint library: marked on layer {CHECK_LAYER} ({CHECK_LAYER_NAME} "
                      f"on the board), pads without size: {', '.join(marked)}")
    table = os.path.join(plan.folder, lib.FP_LIB_TABLE)
    try:
        with open(table, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = None
    text, done = lib.fp_lib_table(existing, plan.nickname,
                                  f"${{KIPRJMOD}}/{plan.nickname}{FOOTPRINT_LIBRARY_SUFFIX}",
                                  "Footprints converted by KIUB")
    if text is not None and text != existing:
        with open(table, "w", encoding="utf-8") as f:
            f.write(text)
    report.append(f"Footprint library: {table} {done}"
                  + (f" (library \"{plan.nickname}\")" if text is not None else ""))
    return report


def convert_input(args: argparse.Namespace, ddf_bytes: bytes, plan: OutputPlan) -> list[str]:
    """Write everything one input converts to (see OutputPlan): for a
    design the design board first, then -- for every input -- the library
    board, the footprint library and its fp-lib-table entry. ddf_bytes is
    the DDF as converted (V2/V3 pre-converted; a library already turned
    into DDF text). Prints progress; returns the files written. Raises
    kiub_library.LibraryError, before anything is written, for a
    library-only input without shapes; a design without shapes gets its
    board and a note."""
    import contextlib as _contextlib
    import copy as _copy
    import io as _io
    lib = _load_kiub_library_module()
    text = ddf_bytes.decode("CP437")
    try:
        layout = lib.layout_library(text)
    except lib.LibraryError:
        if plan.library_only:
            raise
        layout = None
    os.makedirs(plan.folder, exist_ok=True)
    for line in plan_footprint_library(args, ddf_bytes, plan):
        print(f"Note: {line}")
    written: list[str] = []
    if plan.board:
        with open(plan.board, 'w', encoding='utf-8', errors='replace') as kicad:
            converter = Converter(_io.BytesIO(ddf_bytes), kicad, args)
            converter.convert()
        pro = os.path.splitext(plan.board)[0] + '.kicad_pro'
        converter.write_kicad_pro(pro)
        written += [plan.board, pro]
    if layout is None:
        print("Note: the DDF defines no shapes: no library board and no footprint library.")
        return written
    print(f"Library board: {plan.library_board}")
    for line in layout.report:
        print(line)
    for line in layout.warnings:
        print(f"Warning: {line}")
    largs = _copy.copy(args)
    largs.all_shapes = True                 # check marks, layer KIUB.Check
    board = _io.StringIO()
    if plan.board:                          # the design's own pass already reported
        largs.verbose = False
        quiet = _contextlib.redirect_stdout(_io.StringIO())
    else:
        quiet = _contextlib.nullcontext()
    with quiet:
        converter = Converter(_io.BytesIO(layout.text.encode("CP437")), board, largs)
        converter.convert()
    with open(plan.library_board, 'w', encoding='utf-8', errors='replace') as kicad:
        kicad.write(board.getvalue())
    pro = os.path.splitext(plan.library_board)[0] + '.kicad_pro'
    with quiet:
        converter.write_kicad_pro(pro)
    written += [plan.library_board, pro]
    try:
        for line in write_footprint_library(board.getvalue(), plan, report_marks=bool(plan.board)):
            print(line)
        written.append(plan.pretty)
    except OSError as e:
        print(f"Warning: no footprint library written: {e}.")
    return written


def technology_from_args(args):
    """The technology (.TEC data) a library conversion was given: --tec
    FILE, or --tec-split TOP BOTTOM INNER DRILLS [TRACES] (the GUI fills
    the same attributes). Raises kiub_library.LibraryError when a file
    does not fit its role or none was given."""
    lib = _load_kiub_library_module()
    single = getattr(args, 'tec', None)
    split = getattr(args, 'tec_split', None)
    if not single and not split:
        raise lib.LibraryError('an Ultiboard library holds pad codes only: give its technology '
                               'with --tec FILE or --tec-split TOP BOTTOM INNER DRILLS [TRACES]')
    if single:
        return lib.load_technology(single=single)
    if not 4 <= len(split) <= 5:
        raise lib.LibraryError('--tec-split takes four or five files: TOP BOTTOM INNER DRILLS '
                               '[TRACES]')
    return lib.load_technology(top=split[0], bottom=split[1], inner=split[2], drills=split[3],
                               traces=split[4] if len(split) == 5 else None)


def open_library(lib_path: str, tech):
    """Read an Ultiboard library (.LIB/.L55) and turn it, with its
    technology converted to the library's units, into DDF text that
    apply_library_layout lays out. Prints what was read; returns a handle
    on the DDF. Raises kiub_library.LibraryError for a library without
    readable shapes."""
    import io as _io
    lib = _load_kiub_library_module()
    library = lib.read_library(lib_path)
    print(f"Library: {lib_path} (V{library.units} units, {len(library.shapes)} shapes)")
    for line in library.skipped:
        print(f"Warning: library slot {line} -- left out")
    if library.full_length_names:
        n = len(library.full_length_names)
        print(f"Library: {n} shape name{'s are' if n > 1 else ' is'} 12 characters long, the "
              f"most a library holds -- a longer name was shortened when the library was compiled")
    for line in tech.sources:
        print(f"Technology: {line}")
    tech, note = lib.convert_technology(tech, library.units)
    if note:
        print(f"Technology: {note}")
    return _io.BytesIO(lib.library_ddf(library, tech).encode("CP437"))


def check_v2v3_staircases(path: str) -> dict | None:
    """For a V2/V3 DDF file, check whether it contains any staircase-
    represented diagonal traces (see kiub_v2v3.detect_staircases()) --
    intended for a GUI to decide whether to prompt the user, before
    committing to a real conversion, for a staircase-limit override or
    to disable staircase recovery entirely.

    Returns None if the file isn't V2/V3 (nothing to prompt about) or if
    kiub_v2v3.py can't be found/loaded; otherwise returns
    kiub_v2v3.detect_staircases()'s own result dict.
    """
    if _peek_ddf_version(path) not in (2, 3):
        return None
    try:
        _mod = _load_kiub_v2v3_module()
        with open(path, 'r', encoding='CP437', errors='replace') as _src:
            return _mod.detect_staircases(_src.read())
    except Exception:
        return None


def convert_v2v3_text(path: str, args: argparse.Namespace | None = None,
                      verbose: bool = False) -> str:
    """A V2/V3 DDF converted to V4.60 by kiub_v2v3, in memory.

    If *args* carries 'v2v3_text_width_ratio'/'v2v3_text_thickness_ratio'
    (see FINE_TUNING_SPEC), these override kiub_v2v3's own built-in
    defaults for this conversion; otherwise kiub_v2v3's defaults apply.
    Likewise 'v2v3_staircase_limit_mil' (a number, including 0 to disable
    staircase-to-diagonal recovery entirely) overrides kiub_v2v3's own
    default -- the file's own declared grid step, capped at its fixed
    ceiling (see kiub_v2v3.STAIRCASE_CEILING_MIL); if absent or None,
    kiub_v2v3 decides for itself. 'v2v3_corner_slant_limit_mil' works the
    same way but independently for corner-slanting -- absent or None
    means it tracks whatever the staircase limit resolved to (the
    default relationship), not necessarily kiub_v2v3's own module
    default; a number (including 0 to disable corner-slanting entirely)
    sets it independently of the staircase limit, e.g. to keep chamfering
    ordinary corners while staircase merging is disabled, or vice versa.

    Raises SystemExit if kiub_v2v3.py is not found.
    """
    _mod = _load_kiub_v2v3_module()

    # Propagate fine-tuning overrides (if any) into kiub_v2v3's own
    # module-level constants before conversion.
    if args is not None:
        _mod.TEXT_WIDTH_RATIO = getattr(
            args, 'v2v3_text_width_ratio', getattr(_mod, 'TEXT_WIDTH_RATIO', 0.8))
        _mod.TEXT_THICKNESS_RATIO = getattr(
            args, 'v2v3_text_thickness_ratio', getattr(_mod, 'TEXT_THICKNESS_RATIO', 0.1667))
        # v2v3_staircase_limit_mil: None/absent = auto (the file's own
        # declared grid step, capped at STAIRCASE_CEILING_MIL); a
        # number (including 0, meaning disabled) is an explicit
        # override -- see kiub_v2v3's own CLI flags
        # --staircase-limit / --no-staircase-merge for the equivalent
        # standalone behaviour. Still subject to the ceiling either way.
        _staircase_limit = getattr(args, 'v2v3_staircase_limit_mil', None)
        if _staircase_limit is not None:
            _mod.STAIRCASE_LIMIT_MIL = _staircase_limit
            _mod.STAIRCASE_LIMIT_EXPLICIT = True
        # v2v3_corner_slant_limit_mil: None/absent = track the
        # staircase limit above (kiub_v2v3's own default relationship);
        # a number (including 0, meaning disabled) sets it
        # independently -- needed whenever corner-slanting should
        # behave differently from staircase merging in the same
        # conversion, e.g. the GUI's staircase prompt keeping
        # chamfering on even when the user disables staircase
        # recovery itself.
        _corner_slant_limit = getattr(args, 'v2v3_corner_slant_limit_mil', None)
        if _corner_slant_limit is not None:
            _mod.CORNER_SLANT_LIMIT_MIL = _corner_slant_limit
            _mod.CORNER_SLANT_LIMIT_EXPLICIT = True

    if verbose:
        print(f"DDF version {_peek_ddf_version(path)} detected – pre-converting via kiub_v2v3…")

    with open(path, 'r', encoding='CP437', errors='replace') as _src:
        text, corners = _mod.convert_str_counted(_src.read())
    if corners:
        print(f"V2/V3: {corners} ordinary 90-degree trace corner{'s' if corners != 1 else ''} "
              f"chamfered.")
    return text


def open_ddf(path: str, verbose: bool = False, args: argparse.Namespace | None = None):
    """Open a DDF file for reading, transparently pre-converting V2/V3 to
    V4.60 in memory (convert_v2v3_text). Nothing is written: a design's
    V4.60 copy goes into <name>_corrected through read_design and
    write_v460_copy.

    Returns an open binary file-like object that Converter accepts as *ddf*.
    The caller is responsible for closing it (use as a context manager or
    call .close() explicitly).
    """
    import io as _io
    ddf_version = _peek_ddf_version(path)
    if ddf_version in (2, 3):
        return _io.BytesIO(convert_v2v3_text(path, args, verbose).encode('CP437'))
    if verbose and ddf_version != 4:
        print(f"DDF version {ddf_version} detected.")
    return open(path, 'rb')


# ---------------------------------------------------------------------------
# A design's working set (CORRECTED_FOLDER.md): each file read from
# <name>_corrected when a copy is there, else the original; KIUB's one
# correction, the V4.60 DDF of a V2/V3 original, written there.
# ---------------------------------------------------------------------------

V460_CORRECTION = 'v460'


def prepare_design(path: str):
    """The corrected-folder preparation of a design DDF (a
    kiub_corrected.Preparation): its location, its corrected folder and what
    check() found, or `error` set when it can't be converted safely -- an
    input in a corrected folder whose original is gone, or a folder of
    originals that can't be written. Every original the manifest records is
    checked, also the schematic sheets KIUB doesn't read."""
    kcor = _load_kiub_corrected_module()
    loc = locate(path)
    prep = kcor.Preparation(location=loc, folder=kcor.CorrectedFolder(loc, 'KIUB'))
    if not loc.original.is_file():
        prep.error = (f'The original of {loc.original.name} is not in\n{loc.home}\n\n'
                      f'Convert the design from its original files: the corrected folder only '
                      f'holds copies of them.')
        return prep
    if not kcor.is_writable(loc.home):
        prep.error = kcor.READ_ONLY_MESSAGE.format(folder=loc.home)
        return prep
    prep.originals = [loc.original]
    prep.status = prep.folder.check(prep.originals)
    return prep


def check_writable(path: str) -> str:
    """For a library-only input, which writes no corrected copy: the
    read-only refusal (as prepare_design's), or ''."""
    kcor = _load_kiub_corrected_module()
    home = locate(path).home
    return '' if kcor.is_writable(home) else kcor.READ_ONLY_MESSAGE.format(folder=home)


def redo_from_originals(prep) -> str:
    """Empty the corrected folder for a redo from the originals (the user
    said so): every file the manifest lists goes, KIUC redoes its own
    corrections on its next run. Returns the line for the log."""
    prep.folder.reset(prep.originals)
    prep.status = prep.folder.check(prep.originals)
    return f'Corrected copies removed from {prep.folder.path}; converting the original files.'


def working_ddf(prep) -> "pathlib.Path":
    """The DDF to convert: its corrected copy when there is one, else the
    original."""
    return prep.folder.working(prep.location.original)


def working_notes(prep) -> list[str]:
    """Log lines about the working set: copies edited by hand, and the
    corrected copy used with its corrections."""
    kcor = _load_kiub_corrected_module()
    lines = [f'Note: {line}' for line in kcor.describe(prep.status)] if prep.status.edited else []
    working = working_ddf(prep)
    if working != prep.location.original:
        try:
            done = prep.folder.manifest().files.get(working.name, {}).get('corrections', [])
        except (OSError, ValueError):
            done = []
        lines.append(f'Using the corrected copy {working}'
                     + (f' ({", ".join(done)})' if done else ''))
        if V460_CORRECTION in done:
            lines.append(f'Note: the V2/V3 original was converted to V4.60 when this copy was made; '
                         f'the V2/V3 settings of that conversion stay until the copies are redone '
                         f'from the originals (remove {prep.folder.path.name} to do so).')
    return lines


class DesignDDF:
    """A design's DDF as read for conversion (read_design): `data` the bytes
    to convert, `working` the file they came from, `version` its DDF
    version, and `copy` the V4.60 DDF to write into <name>_corrected
    (write_v460_copy), for a V2/V3 working file -- else None."""

    def __init__(self, data: bytes, working, version: int, copy: bytes | None) -> None:
        self.data, self.working, self.version, self.copy = data, working, version, copy


def read_design(prep, args: argparse.Namespace | None = None,
                verbose: bool = False) -> DesignDDF:
    """Read the design's working DDF (working_ddf); a V2/V3 one is
    converted to V4.60 in memory. Writes nothing."""
    working = working_ddf(prep)
    version = _peek_ddf_version(str(working))
    if version in (2, 3):
        text = convert_v2v3_text(str(working), args, verbose)
        return DesignDDF(text.encode('CP437'), working, version,
                         ensure_trailing_blank_line(text).encode('CP437', errors='replace'))
    if verbose and version != 4:
        print(f"DDF version {version} detected.")
    with open(working, 'rb') as f:
        return DesignDDF(f.read(), working, version, None)


def write_v460_copy(prep, design: DesignDDF) -> str | None:
    """Write a V2/V3 design's V4.60 DDF as <name>_corrected/<name>.DDF,
    recorded as correction 'v460' (the original stays as it is), once the
    conversion goes ahead. Returns the line for the log, or None when there
    is nothing to write."""
    if design.copy is None:
        return None
    copy = prep.folder.write(prep.location.original, design.copy, V460_CORRECTION,
                             f'V{design.version} DDF converted to V4.60')
    return (f"Note: the V{design.version} DDF was converted to V4.60 as {copy} -- the original "
            f"{prep.location.original.name} is left as it is.")


# ---------------------------------------------------------------------------
# Non-digit-ending refdes pre-scan (lightweight, read-only – see KIUC's
# kiuc_refdes.py "Refdes Reannotate" tool for the actual renaming logic,
# which is out of scope for KIUB).
# ---------------------------------------------------------------------------

# Matches a DDF '*C <refdes> ...' component-definition line. Confirmed stable
# across DDF V2.x-V5.x by KIUC's kiuc_refdes.py, which uses the same shape.
_REFDES_LINE_RE = re.compile(r'^\r*\*C (\S+) ', re.MULTILINE)


def scan_non_digit_refdes(ddf_text: str) -> list[str]:
    """Return every unique component reference designator (from '*C' records)
    that does NOT end in a digit, in first-seen order.

    This is a simple "ends in a digit" check -- unlike KIUC's schematic-side
    Refdes Reannotate tool, a DDF/PCB component has no equivalent of a
    multi-unit symbol (e.g. gates U2A/U2B of one physical IC), so there is
    no case here where a trailing letter after a digit should be treated as
    already-annotated.

    Read-only: does not rename anything. KIUB never edits reference
    designators itself -- if a schematic exists for this board (whether or
    not it's found sitting next to the DDF), KIUC's Refdes Reannotate tool
    is the only place renaming should happen, since it's what keeps the
    schematic and PCB in sync; renaming independently of it breaks that
    sync.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _REFDES_LINE_RE.finditer(ddf_text):
        rd = m.group(1)
        if rd and not rd[-1].isdigit() and rd not in seen_set:
            seen_set.add(rd)
            seen.append(rd)
    return seen


def _design_name_and_folders(ddf_path: str) -> tuple[str, list[str]]:
    """The design name and the folders where that design's schematic or
    its KIUC output may live: the folder of the originals first (also for
    an input inside <name>_corrected), then <name>_KiCad."""
    loc = locate(ddf_path)
    return loc.name, [str(loc.home), str(loc.kicad_dir)]


def _find_by_name(folder: str, stem: str, exts: tuple[str, ...]) -> str | None:
    """A file '<stem><ext>' in folder, stem and extension matched without
    regard to case (Ulticap/Ultiboard files come in any case)."""
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return None
    for ext in exts:
        for entry in entries:
            base, e = os.path.splitext(entry)
            if base.lower() == stem.lower() and e.lower() == ext:
                path = os.path.join(folder, entry)
                if os.path.isfile(path):
                    return path
    return None


def find_sibling_schematic(ddf_path: str) -> str | None:
    """Return the path of the schematic belonging to this DDF, if any:
    '<name>.SCH' or '<name>.UTSCH' (an Ulticap original) or
    '<name>.kicad_sch' next to the DDF, or '<name>.kicad_sch' in one of
    KIUC's output folders. Names and extensions are matched without regard
    to case. Returns None if there is none."""
    name, folders = _design_name_and_folders(ddf_path)
    found = _find_by_name(folders[0], name, ('.sch', '.utsch', '.kicad_sch'))
    if found:
        return found
    for folder in folders[1:]:
        found = _find_by_name(folder, name, ('.kicad_sch',))
        if found:
            return found
    return None


def refdes_advice(ddf_path: str) -> tuple[str, bool]:
    """The advice shown (CLI and GUI alike) when the DDF to convert -- the
    design's working DDF -- holds reference designators that do not end in
    a digit, and whether a schematic belongs to the design, in which case
    the conversion should stop: KIUC's refdes fix (or the standalone Refdes
    Reannotate) renames them in the schematic and writes a matching copy of
    the DDF into <name>_corrected, which KIUB then converts by itself."""
    sibling = find_sibling_schematic(ddf_path)
    if not sibling:
        return ("No schematic (.SCH, .UTSCH or .kicad_sch) was found for this "
                "DDF.\n\nKiCad's PCB editor accepts references that do not end "
                "in a digit, so no action is needed if this board has no "
                "schematic. If a schematic for this board exists elsewhere, run "
                "KIUC's Refdes Reannotate on it first -- renaming these "
                "references independently of the schematic breaks their sync."), False
    loc = locate(ddf_path)
    text = (f"A schematic belongs to this design:\n  {sibling}\n\n"
            f"Convert it with KIUC first and let it fix the reference designators "
            f"(or run Refdes Reannotate on it): that renames these references in the "
            f"schematic and writes a matching copy of the DDF into "
            f"{loc.name}_corrected. KIUB then converts that copy by itself -- converting "
            f"this DDF now gives a board whose references don't match the schematic.")
    return text, True


# ---------------------------------------------------------------------------
# CLI and entry point
# ---------------------------------------------------------------------------

def ask(question: str, default: bool) -> bool:
    """A yes/no question on the console, [Y/n] or [y/N] after the default
    (an empty answer). Without an answer to read (input closed, e.g. a
    scheduled run) the default is taken, and said."""
    hint = '[Y/n]' if default else '[y/N]'
    try:
        answer = input(f"{question} {hint}: ").strip().lower()
    except EOFError:
        print(f"\n  No answer to read: {'yes' if default else 'no'} (the default).")
        return default
    return default if not answer else answer in ('y', 'yes')


parser = argparse.ArgumentParser(description='Convert UltiBoard V2/V3/V4/V5 DDFs to KiCad pcb.')
parser.add_argument('infile', help='Ultiboard DDF file (with or without the .DDF file extension), '
                                   'or an Ultiboard library (.LIB, .L55) with --tec/--tec-split.')
parser.add_argument('-f', '--font', default='KiCad Font', help='use a different font.')
parser.add_argument('-v', '--verbose', action='store_true', help='Display conversion information.')
for _name, _default, _lo, _hi, _desc, _target in BOARD_DEFAULTS_SPEC:
    parser.add_argument(
        f'--{_name.replace("_", "-")}', type=float, default=_default, dest=_name,
        help=f'{_desc} (written to {_target}). Default: {_default}, suggested range {_lo}-{_hi}.')
for _name, _default, _lo, _hi, _desc, _category in FINE_TUNING_SPEC:
    parser.add_argument(
        f'--{_name.replace("_", "-")}', type=float, default=_default, dest=_name,
        help=f'[fine-tuning/{_category}] {_desc} Default: {_default:.6g}, suggested range {_lo}-{_hi}.')
parser.add_argument(
    '--v2v3-staircase-limit-mil', type=float, default=None, dest='v2v3_staircase_limit_mil',
    metavar='MIL',
    help='DDF V2/V3 pre-conversion only: maximum length (mil) for a single staircase '
         "grid step recovered as a diagonal trace. Default: the file's own declared "
         'default grid step. Always capped at 25 mil regardless of this file\'s '
         'declared grid or this override (see kiub_v2v3.STAIRCASE_CEILING_MIL).')
parser.add_argument(
    '--v2v3-no-staircase-merge', action='store_true',
    help='DDF V2/V3 pre-conversion only: disable staircase-to-diagonal recovery '
         'entirely (default: enabled).')
parser.add_argument(
    '--v2v3-corner-slant-limit-mil', type=float, default=None, dest='v2v3_corner_slant_limit_mil',
    metavar='MIL',
    help='DDF V2/V3 pre-conversion only: maximum length (mil) to trim off each leg of '
         'an ordinary 90-degree trace corner when chamfering it. Default: tracks '
         '--v2v3-staircase-limit-mil (or its own auto default, if that is not set '
         'either) -- set this independently to make corner-slanting behave differently '
         'from staircase merging in the same conversion.')
parser.add_argument(
    '--v2v3-no-chamfer', action='store_true',
    help='DDF V2/V3 pre-conversion only: disable corner-slanting (chamfering ordinary '
         '90-degree trace corners) entirely (default: enabled).')
parser.add_argument('--library-only', action='store_true', dest='library_only',
                    help='Convert a DDF as a shape library only: the library board and the '
                         'footprint library, no design board. Without it, a DDF that places '
                         'components but holds no nets, copper or vias gets this question.')
parser.add_argument('--tec', metavar='FILE',
                    help='Ultiboard library input: its technology as one .TEC file (Ultiboard '
                         'V4.60 and later).')
parser.add_argument('--tec-split', nargs='+', metavar='FILE', dest='tec_split',
                    help='Ultiboard library input: its technology as a multi-file set (older '
                         'Ultiboard versions), in this order: TOP BOTTOM INNER DRILLS [TRACES] -- '
                         'top, bottom and inner pad tables, the drill table, optionally the trace '
                         'table. The same file may serve several pad tables.')
parser.add_argument('--yes', action='store_true',
                    help='Do not prompt; continue automatically (non-digit-ending reference designators, '
                         'incomplete technology tables, existing boards replaced; a DDF that may be '
                         'a design or a library converts as a design; an earlier KIUB\'s <name>_V3.DDF '
                         'converts as a design of its own). Corrected copies out of date: the '
                         'default answer is taken -- redo from the originals, or cancel when copies '
                         'edited by hand would be lost.')
parser.epilog = ('The output goes next to the design\'s files and can\'t be chosen: <name>_KiCad (a '
                 'design) or <name>_lib_KiCad (a library-only input). A V2/V3 DDF is converted to '
                 'V4.60 into <name>_corrected; the original is never written. See CORRECTED_FOLDER.md.')

args = parser.parse_args()

if args.verbose:
    os.system('cls' if os.name == 'nt' else 'clear')

if args.v2v3_no_staircase_merge:
    args.v2v3_staircase_limit_mil = 0

if args.v2v3_no_chamfer:
    args.v2v3_corner_slant_limit_mil = 0

if (os.path.splitext(args.infile)[1].lower() not in ('.ddf', '.lib', '.l55')
        and not os.path.exists(args.infile)):
    args.infile += ".ddf"
if not os.path.exists(args.infile):
    print(f"Error: File '{args.infile}' does not exist.")
    sys.exit(1)

# An Ultiboard library (.LIB/.L55, told apart by its signature) converts
# only with its technology (.TEC data), and -- like a DDF holding shapes
# only -- to a library only (no design board).
_kind = _load_kiub_library_module().library_kind(args.infile)
if _kind == 'ulticap':
    print("Error: this is an Ulticap symbol library, not an Ultiboard one: convert it with KIUC.")
    sys.exit(1)
_is_library = _kind == 'ultiboard'
if not _is_library and (args.tec or args.tec_split):
    print("Error: --tec and --tec-split are for an Ultiboard library (.LIB, .L55) only.")
    sys.exit(1)
_input_kind = input_kind(args.infile)
_library_only = _input_kind == DDF_LIBRARY or args.library_only
if _input_kind == DDF_UNSURE and not args.library_only and not args.yes:
    print(f"\n{UNSURE_QUESTION}")
    _library_only = ask("Convert it as a library only (no design board)?", False)

# The design's corrected folder (CORRECTED_FOLDER.md): a design reads its
# working DDF -- <name>_corrected/<name>.DDF when there is one -- after the
# checks; a library-only input writes no copy and is read as it is.
_prep = None
if _library_only:
    _refusal = check_writable(args.infile)
    if _refusal:
        for _line in _refusal.splitlines():
            print(f"Error: {_line}" if _line else "")
        sys.exit(1)
else:
    _legacy = legacy_v3_question(args.infile)
    if _legacy:
        print(f"\nWarning: {_legacy}")
        if not args.yes and not ask("\nConvert it as a design of its own anyway?", False):
            print("Conversion aborted. Nothing written.")
            sys.exit(1)
    _prep = prepare_design(args.infile)
    if _prep.error:
        for _line in _prep.error.splitlines():
            print(f"Error: {_line}" if _line else "")
        sys.exit(1)
    _question = _prep.redo_question()
    if _question:
        _text, _default = _question
        print()
        for _line in _text.splitlines():
            print(f"Warning: {_line}" if _line else "")
        if args.yes:
            _redo = _default
            print(f"Redo from the original files? -- {'yes' if _redo else 'no'} (the default, --yes)")
        else:
            _redo = ask("\nRedo from the original files?", _default)
        if not _redo:
            print("Conversion cancelled -- nothing changed.")
            sys.exit(1)
        print(redo_from_originals(_prep))
    for _line in working_notes(_prep):
        print(_line)

_plan = OutputPlan(default_output_path(args.infile, _library_only), _library_only)
args.outfile = _plan.board or _plan.library_board

if args.verbose:
    print(f"Ultiboard file: {args.infile}\nKiCad folder:   {_plan.folder}\n"
          f"Mode:           {'Library' if _library_only else 'Design and library'}")

if _is_library:
    _libmod = _load_kiub_library_module()
    try:
        _tech = technology_from_args(args)
    except _libmod.LibraryError as _e:
        print(f"Error: {_e}. Nothing written.")
        sys.exit(1)
    if _tech.incomplete:
        for _line in _tech.incomplete:
            print(f"Warning: incomplete technology table -- {_line}; the missing codes count "
                  f"as undefined")
        if not args.yes:
            if not ask("\nContinue with the conversion anyway?", False):
                print("Conversion aborted.")
                sys.exit(1)
    try:
        ddf_handle = open_library(args.infile, _tech)
    except _libmod.LibraryError as _e:
        print(f"Error: {_e}. Nothing written.")
        sys.exit(1)
    try:
        _ddf_bytes = ddf_handle.read()
    finally:
        ddf_handle.close()
    _design = None
elif _library_only:
    # A library-only DDF is read as it is: no V4.60 copy.
    ddf_handle = open_ddf(args.infile, verbose=args.verbose, args=args)
    try:
        _ddf_bytes = ddf_handle.read()
    finally:
        ddf_handle.close()
    _design = None
else:
    _design = read_design(_prep, args, verbose=args.verbose)
    _ddf_bytes = _design.data

# ── Non-digit-ending refdes pre-scan (read-only; see scan_non_digit_refdes) ──
_offending_refs = ([] if _library_only
                   else scan_non_digit_refdes(_ddf_bytes.decode('CP437', errors='replace')))
if _offending_refs:
    print("\nWarning: the following component reference designators do not end in a digit:")
    for _rd in _offending_refs:
        print(f"  {_rd}")
    _advice, _has_schematic = refdes_advice(args.infile)
    print("\n" + _advice)
    if not args.yes:
        _question = ("Continue with this file anyway (not recommended)?" if _has_schematic
                     else "Continue with the conversion anyway?")
        if not ask(f"\n{_question}", False):
            print("Conversion aborted.")
            sys.exit(1)

_existing = existing_outputs(_plan)
if _existing and not args.yes:
    print(f"\n{OVERWRITE_QUESTION}")
    for _p in _existing:
        print(f"  {_p}")
    if not ask("\nReplace them?", False):
        print("Conversion aborted. Nothing written.")
        sys.exit(1)

if _design is not None:
    _line = write_v460_copy(_prep, _design)
    if _line:
        print(_line)

try:
    convert_input(args, _ddf_bytes, _plan)
except _load_kiub_library_module().LibraryError as _e:
    print(f"Error: {_e}. Nothing written.")
    sys.exit(1)
if args.verbose:
    print("Done.")
