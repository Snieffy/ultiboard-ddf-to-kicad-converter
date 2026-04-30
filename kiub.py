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
(kicad_pcb (version 20241029) (generator "KIUB") (generator_version "1.1.0")

  (general
    (thickness 1.6)
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
    (pad_to_mask_clearance 0.051)
    (solder_mask_min_width 0.15)
  )

"""

# ---------------------------------------------------------------------------
# Pure helper functions (no state)
# ---------------------------------------------------------------------------

def ubfont(payload: bytes) -> str:
    """Convert CP437/CP850 Ultiboard font bytes to a Unicode string."""
    chars: list[str] = []
    overline_active = False
    for b in payload:
        if b == 94:  # Ultiboard uses '^' as start/end toggle for overline text.
            overline_active = not overline_active
            continue
        ch = ub_fontmap.get(bytes([b]), "?")
        if overline_active:
            chars.append("\u0305")  # KiCad needs the combining overline character before the base character.
        chars.append("\\" + ch if ch in ('"', "\\") else ch)  # Escape double-quotes and backslashes.
    return "".join(chars)


def layer_from_bit(bit_index: int) -> LayerEntry:
    """Return the layersCu entry for a given zero-based bit position."""
    mask_bits = bin(1 << bit_index)[2:].zfill(32)[::-1]
    return list(compress(layersCu, (int(x) for x in mask_bits)))[0]


def calc_arc_points(
    radius: float,
    start_angle_deg: float,
    span_deg: float,
    accuracy: int = 6,
) -> tuple[Point2D, Point2D, Point2D]:
    """Return centre-relative (x, y) offsets for the start, mid-point, and
    end of an arc.

    Parameters
    ----------
    radius          : arc radius in mm
    start_angle_deg : arc start angle in degrees (DDF *arc1* value / 64)
    span_deg        : arc angular span in degrees (DDF *arc2* value / 64);
                      always positive
    accuracy        : decimal places for rounding

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

    start_pt: Point2D = (round(radius * math.cos(deg_to_rad * ub_start), accuracy),
                         round(radius * math.sin(deg_to_rad * ub_start), accuracy))
    mid_pt: Point2D   = (round(radius * math.cos(deg_to_rad * ub_mid),   accuracy),
                         round(radius * math.sin(deg_to_rad * ub_mid),   accuracy))
    end_pt: Point2D   = (round(radius * math.cos(deg_to_rad * ub_end),   accuracy),
                         round(radius * math.sin(deg_to_rad * ub_end),   accuracy))

    return start_pt, mid_pt, end_pt



# ---------------------------------------------------------------------------
# Converter class
# ---------------------------------------------------------------------------

class Converter:
    """Holds all conversion state and one handler method per DDF record type."""

    # Default settings
    NPTHclearance:       float = 0.15    # Hole clearance for NPTH pads – used when pad clearance is zero.
    dcMin:               float = 0.05   # Smallest drill diameter (mm); drills below this are set to -1 (SMD special case).
    lineWidth:           float = 0.075  # Default width for lines, arcs and circles.
    defaultClearance:    float = 0.254  # Default clearance.
    defaultWidth:        float = 0.254  # Default copper width.
    defaultThermalGap:   float = 0.254  # Default thermal gap.
    defaultThermalWidth: float = 0.254  # Default thermal bridge width.
    fontThickRatio:      int   = 1000   # Font thickness divisor: thickness = value × height / 1000 (Ultiboard definition).
    fontHeightRatio:     float = 1.208  # Text height scale: KiCad height = Ultiboard height / fontHeightRatio.
    fontWidthRatio:      float = 1.186  # Text width  scale: KiCad width  = Ultiboard width  × fontWidthRatio.
    snapTolerance:       float = 0.1    # Maximum gap (mm) between adjacent board-outline endpoints to snap closed.

    def __init__(self, ddf: IO[bytes], kicad: IO[str],
                 args: argparse.Namespace) -> None:
        self.ddf   = ddf
        self.kicad = kicad
        self.args  = args

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

        NOTE: accuracy defaults to self.di_Ac rather than being fixed at
        definition time, because di_Ac is updated when the header is read.
        """
        if accuracy is None:
            accuracy = self.di_Ac
        if self.DDF_major == '4':
            return round((val / 1.2) * 0.0254, accuracy)   # 1/1200 inch
        return round(val / 1_000_000, accuracy)             # nanometres

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

            line = str(line.strip())[2:-1]

            handler = self._dispatch.get(line[1])
            if handler:
                handler(line)

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

        self.offsetX   = round(frame_center[0] - board_center[0], self.di_Ac)  # Offset to centre PCB on frame.
        self.offsetY   = round(frame_center[1] - board_center[1], self.di_Ac)
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

        self.kicad.write(HEADER_TEMPLATE.format(PCBlayers=layers_p,
                                                papersize=f'"{sheetSize}"'))
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
        sn_thick  = round(lname[5] * sn_height / self.fontThickRatio, self.di_Ac)

        # Alias text descriptor: same field order as reference text
        lalias    = [int(i) for i in self._readline().split()]
        sa_height = self.units_to_mm(lalias[2])
        sa_width  = self.units_to_mm(lalias[4])
        sa_thick  = round(lalias[5] * sa_height / self.fontThickRatio, self.di_Ac)

        # Build the footprint template; {…} placeholders are filled per component.
        shapeStr = (
            f'  (footprint "library:{sName}"\n'
            f'     (layer "{{fp_side}}.Cu")\n'
            f'     (at {{shapePos}})\n'
            f'     {{r_block}}{{v_block}}'
            f'     (fp_text user "${sName}"\n'
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
                        radius, start_angle, span_angle, self.di_Ac)
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
            0 if pi[1] == pi[2]                                             # Centric pads and SMD pads have no offset.
            else round(self.units_to_mm(pi[2] - pi[1]) / 2, self.di_Ac)   # Pad-to-hole offset for non-centric pads.
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

        if pi[3] == 0 and self.drillCode[pi[0]] != 0:
            # When pad size is zero, set pad size equal to the drill size (NPTH hole).
            pi[1] = pi[2] = self.drillCode[pi[0]] / 2
            pi[3] = self.drillCode[pi[0]]

        # Swap DDF layers 0↔1 to match KiCad numbering:
        #   DDF *T0 (Inner)  → pads[0],  DDF *T1 (Top/F.Cu) → pads[1],  DDF *T2 (Bot/B.Cu) → pads[2]
        #   KiCad:            pads[0]=F.Cu,              pads[1]=Inner,             pads[2]=B.Cu
        ppos = int(line[2]) if int(line[2]) == 2 else abs(int(line[2]) - 1)
        self.pads[ppos].iloc[pi[0]] = [
            round(pi[1] + pi[2], self.di_Ac),
            pi[3],
            pc_offsetX,
            roundratio,
            pi[5] if pi[5] else self.NPTHclearance,   # Use NPTHclearance when clearance is zero.
        ]

        if int(line[2]) in (0, 1, 2) and pi[0] == 255:
            # Final pass after the last pad definition (*T_/255): fill in pad sizes for
            # drill-only codes (NPTH) that have no corresponding pad code in the DDF.
            # KiCad requires pad size ≥ drill size; via pad codes (≥240) are NOT processed here.
            for didx in range(len(self.drillCode) - 16):
                if self.drillCode[didx] > 0 and \
                   self.pads[int(line[2])].iloc[didx]['Xsize'] == 0:
                    self.pads[int(line[2])].loc[didx, 'Xsize']      = self.drillCode[didx]
                    self.pads[int(line[2])].loc[didx, 'Ysize']      = self.drillCode[didx]
                    self.pads[int(line[2])].loc[didx, 'roundratio'] = 0.5
                    self.pads[int(line[2])].loc[didx, 'clearance']  = self.NPTHclearance


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
        # *C record: <name> /<alias> <shape name>
        carr   = line[3:].split()
        cname  = carr[0]
        calias = carr[1].strip("/")
        cshape = carr[2]

        # Position line: <x>,<y>,<rotation>,<name_x>,<name_y>,<name_rot>,<name_w>,<name_h>,<name_thick>,
        #                <alias_x>,<alias_y>,<alias_rot>,<alias_w>,<alias_h>,<alias_thick>
        # Rotation is in degrees × 64.
        carr   = self._readline().split(",")
        cxpos  = round(self.units_to_mm(int(carr[0])) + self.offsetX, self.di_Ac)
        cypos  = round(-self.units_to_mm(int(carr[1])) + self.offsetY, self.di_Ac)
        layerB = int(carr[2]) / 64 < 0   # Kept for speed and readability; equivalent to 'crot < 0'.
        crot   = round(int(carr[2]) / 64, self.di_Ac)

        cnxpos = round(-self.units_to_mm(int(carr[3])) if layerB else self.units_to_mm(int(carr[3])), self.di_Ac)
        cnypos = round(-self.units_to_mm(int(carr[4])), self.di_Ac)
        cnrot  = round(int(carr[5]) / 64 + crot, self.di_Ac)
        cnwdth = round(self.units_to_mm(int(carr[6])), self.di_Ac)
        cnhght = round(self.units_to_mm(int(carr[7])),  self.di_Ac)
        cnthck = round(int(carr[8]) * cnhght / self.fontThickRatio, self.di_Ac)

        caxpos = round(-self.units_to_mm(int(carr[9])) if layerB else self.units_to_mm(int(carr[9])), self.di_Ac)
        caypos = round(-self.units_to_mm(int(carr[10])), self.di_Ac)
        carot  = round(int(carr[11]) / 64 + crot, self.di_Ac)
        cawdth = round(self.units_to_mm(int(carr[12])), self.di_Ac)
        cahght = round(self.units_to_mm(int(carr[13])),  self.di_Ac)
        cathck = round(int(carr[14]) * cahght / self.fontThickRatio, self.di_Ac)

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
            f'     (property "Reference" "{cname}"\n'
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
            f'     (property "Value" "{calias}"\n'
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
            shapeStr += self._build_pad_attr(shape, layerB)
            shapeStr += self._build_pads(shape, padnet, crot, layerB)

        self.kicad.write(shapeStr + "  )\n")

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
            # has a copper annulus to attach the net to.
            pad_w = pad_h = round(drCode + 0.01, self.di_Ac)   # +0.01 rounded to avoid IEEE noise.
            pad_offset    = 0
            pad_rr        = 0.5
        else:
            pad_w      = self.pads[layer_idx].at[pad['code'], 'Xsize']
            pad_h      = self.pads[layer_idx].at[pad['code'], 'Ysize']
            pad_offset = self.pads[layer_idx].at[pad['code'], 'Xoffset']
            if layerB and pad['rot'] in (0, 180):
                pad_offset = -pad_offset   # Invert offset for bottom-layer pads.
            pad_rr     = self.pads[layer_idx].at[pad['code'], 'roundratio']

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
                        x1 = self._f(round(coord2 + half1, self.di_Ac) + self.offsetX)
                        y1 = self._f(round(half1,           self.di_Ac) + self.offsetY)
                        x2 = self._f(round(coord3 + half2, self.di_Ac) + self.offsetX)
                        y2 = self._f(round(half2,           self.di_Ac) + self.offsetY)
                    case 8:     # south-east diagonal
                        half1 = (coord2 - coord1) / 2
                        half2 = (coord3 - coord1) / 2
                        x1 = self._f(round(coord2 - half1, self.di_Ac) + self.offsetX)
                        y1 = self._f(round(half1,           self.di_Ac) + self.offsetY)
                        x2 = self._f(round(coord3 - half2, self.di_Ac) + self.offsetX)
                        y2 = self._f(round(half2,           self.di_Ac) + self.offsetY)

                self.kicad.write(
                    f'  (segment\n'
                    f'        (start {x1} {y1})\n'
                    f'        (end {x2} {y2})\n'
                    f'        (width {width})\n'
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
            f'        (width {self.units_to_mm(self.traceWidth[vtcode])})\n'
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
        # Use 0.000001 as a near-zero sentinel that KiCad accepts without altering.
        atWidth: float = (
            self.units_to_mm(self.traceWidth[atcode]) if atcode != 65535 else 0.000001
        ) or 0.000001

        aclr = self.traceClearance.get(atcode, 0)
        if aclr > 0 and anetnr > 0 and atcode != 65535:
            self.netMinClearance[anetnr] = min(
                self.netMinClearance.get(anetnr, aclr), aclr)

        (dx_start, dy_start), \
        (dx_mid,   dy_mid),   \
        (dx_end,   dy_end)    = calc_arc_points(radius, start_angle, span_angle, self.di_Ac)

        self.kicad.write(
            f'  (arc\n'
            f'        (start {self._f(centre_x + dx_start + self.offsetX)}'
            f' {self._f(-(centre_y + dy_start) + self.offsetY)})\n'
            f'        (mid   {self._f(centre_x + dx_mid   + self.offsetX)}'
            f' {self._f(-(centre_y - dy_mid)   + self.offsetY)})\n'
            f'        (end   {self._f(centre_x + dx_end   + self.offsetX)}'
            f' {self._f(-(centre_y + dy_end)   + self.offsetY)})\n'
            f'        (width {atWidth})\n'
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
        lpclear = self.units_to_mm(lpline[5])
        width   = self.units_to_mm(self.traceWidth[lptcode])

        if lppat in (3, 12):
            # Hatch fill: code 3 = 0°, code 12 = 45° → angle = (code−3) × 5
            lpHatch = (
                f'                (mode hatch)\n'
                f'                (hatch_thickness {self.units_to_mm(self.traceWidth[lptcode])})\n'
                f'                (hatch_gap {self.units_to_mm(lpdist)})\n'
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
            # Only emit a padstack block when inner or back-copper annular ring
            # differs from the front-copper size (KiCad 9 padstack feature).
            if i_size != f_size or b_size != f_size:
                padstack_str = (
                    f'        (padstack\n'
                    f'          (mode front_inner_back)\n'
                    f'          (layer "Inner"\n'
                    f'            (size {i_size} {i_size})\n'
                    f'          )\n'
                    f'          (layer "B.Cu"\n'
                    f'            (size {b_size} {b_size})\n'
                    f'          )\n'
                    f'        )\n'
                )
            else:
                padstack_str = ""
            self.kicad.write(
                f'  (via{" " + via_type if via_type else ""}\n'
                f'        (at {vxpos} {vypos})\n'
                f'        (size {f_size})\n'
                f'        (drill {self.drillCode[vpcode]})\n'
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
        text_h   = round(self.units_to_mm(int(xfields[2])) / self.fontHeightRatio, self.di_Ac)  # Empirical scale.
        text_w   = round(self.units_to_mm(int(xfields[3])) * self.fontWidthRatio,  self.di_Ac)
        text_t   = round(int(xfields[4]) * text_h / self.fontThickRatio, self.di_Ac)
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
                f'        (hatch edge {self.defaultWidth})\n'
                f'        (connect_pads\n'
                f'                (clearance {self.boardClearance})\n'
                f'        )\n'
                f'        (min_thickness {self.defaultWidth})\n'
                f'        (fill yes\n'
                f'                (thermal_gap {self.defaultThermalGap})\n'
                f'                (thermal_bridge_width {self.defaultThermalWidth})\n'
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
        all_drills = pth_drills + via_drills_all
        min_through_hole = round(min(all_drills), self.dr_Ac) if all_drills else 0.0

        # Smallest via: prefer pad Ysize (copper diameter) for codes 240–255.
        via_sizes: list[float] = []
        via_drills: list[float] = []
        for code in range(240, 256):
            dc = self.drillCode[code]
            if dc > 0:
                via_drills.append(dc)
            pad_y = self.pads[0].at[code, 'Ysize']   # layer 0 = Front
            if pad_y > 0:
                via_sizes.append(pad_y)
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
                if ann > 1e-4:                                 # ignore float-rounding noise
                    annular_widths.append(round(ann, self.di_Ac))
        for code in range(240, 256):
            pad_y = self.pads[0].at[code, 'Ysize']
            drill = self.drillCode[code]
            if pad_y > 0 and drill > 0:
                ann = (pad_y - drill) / 2
                if ann > 1e-4:
                    annular_widths.append(round(ann, self.di_Ac))
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
            "diff_pair_gap":     round(min_trace_clearance * 1.5, self.di_Ac),
            "diff_pair_via_gap": round(min_trace_clearance * 1.5, self.di_Ac),
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
                "diff_pair_gap":     round(effective_clearance_mm * 1.5, self.di_Ac),
                "diff_pair_via_gap": round(effective_clearance_mm * 1.5, self.di_Ac),
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

        pro: dict = {
            "board": {
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
                        "min_copper_edge_clearance":      self.boardClearance,
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
                        "solder_mask_to_copper_clearance": 0.0,
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
                        "drill_out_of_range":                  "error",
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
            },
            "meta": {
                "filename": os.path.basename(pro_path),
                "version":  1,
            },
            "net_settings": {
                "classes":              netclass_entries,
                "meta":                 {"version": 3},
                "net_colors":           None,
                "netclass_assignments": None,
                "netclass_patterns":    netclass_patterns,
            },
            "pcbnew": {
                "last_paths": {
                    "gencad":      "",
                    "idf":         "",
                    "netlist":     "",
                    "plot":        "",
                    "pos_files":   "",
                    "specctra_dsn": "",
                    "step":        "",
                    "svg":         "",
                    "vrml":        "",
                },
                "page_layout_descr_file": "",
            },
            "schematic": {
                "meta": {"version": 1},
            },
        }

        with open(pro_path, 'w', encoding='utf-8') as f:
            json.dump(pro, f, indent=2)
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

def open_ddf(path: str, verbose: bool = False):
    """Open a DDF file for reading, transparently pre-converting V2/V3 to V4.

    Returns an open binary file-like object that Converter accepts as *ddf*.
    The caller is responsible for closing it (use as a context manager or
    call .close() explicitly).

    For V4/V5 files a regular binary file handle is returned.
    For V2/V3 files the conversion is performed in memory and an io.BytesIO
    handle is returned – no intermediate file is written to disk.

    Raises SystemExit if the file is V2/V3 but kiub_v2v3.py is not found.
    """
    import io as _io

    # ------------------------------------------------------------------
    # Peek at the *P record to read the DDF major version number.
    # The version line immediately follows *P and looks like "4 60" or "3 3".
    # We only read the first 10 lines so this is effectively free.
    # ------------------------------------------------------------------
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

    if ddf_version in (2, 3):
        # ── V2/V3: import kiub_v2v3 and pre-convert in memory ────────────
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

        if verbose:
            print(f"DDF version {ddf_version} detected – pre-converting via kiub_v2v3…")

        with open(path, 'r', encoding='CP437', errors='replace') as _src:
            _v4_str = _mod.convert_str(_src.read())

        return _io.BytesIO(_v4_str.encode('CP437'))

    else:
        # ── V4/V5: open normally ──────────────────────────────────────────
        if verbose and ddf_version != 4:
            print(f"DDF version {ddf_version} detected.")
        return open(path, 'rb')


# ---------------------------------------------------------------------------
# CLI and entry point
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description='Convert UltiBoard V2/V3/V4/V5 DDFs to KiCad pcb.')
parser.add_argument('infile', help='Ultiboard DDF file (with or without the .DDF file extension).')
parser.add_argument('-o', '--outfile', help='Kicad PCB file (with or without the .kicad_pcb file extension).')
parser.add_argument('-f', '--font', default='KiCad Font', help='use a different font.')
parser.add_argument('-v', '--verbose', action='store_true', help='Display conversion information.')

args = parser.parse_args()

if not args.infile.lower().endswith('.ddf'):
    args.infile += ".ddf"
if not os.path.exists(args.infile):
    print(f"Error: File '{args.infile}' does not exist.")
    sys.exit(1)
if not args.outfile:
    args.outfile = os.path.splitext(args.infile)[0]
if not args.outfile.lower().endswith('.kicad_pcb'):
    args.outfile += ".kicad_pcb"

if args.verbose:
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"Ultiboard file: {args.infile}\nKicad file:     {args.outfile}")

ddf_handle = open_ddf(args.infile, verbose=args.verbose)
try:
    with open(args.outfile, 'w', encoding='utf-8', errors='replace') as kicad:
        converter = Converter(ddf_handle, kicad, args)
        converter.convert()
finally:
    ddf_handle.close()

pro_path = os.path.splitext(args.outfile)[0] + '.kicad_pro'
converter.write_kicad_pro(pro_path)
if args.verbose:
    print(f"Done.")
