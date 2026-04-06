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
Version: 1.1.0
"""
from __future__ import annotations

import sys
import os
import math
import argparse
import re
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

    class AtIndexer:
        def __init__(self, outer: SimpleDataFrame) -> None:
            self.outer = outer

        def __getitem__(self, key: tuple[int, str]) -> float:
            row, col = key
            return self.outer.data[row][self.outer.col_index[col]]

        def __setitem__(self, key: tuple[int, str], value: float) -> None:
            row, col = key
            self.outer.data[row][self.outer.col_index[col]] = value

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

layersCu: list[LayerEntry] = [
    [0,  "F.Cu"],   [2,  "B.Cu"],   [6,  "In2.Cu"],  [4,  "In1.Cu"],
    [10, "In4.Cu"], [8,  "In3.Cu"], [14, "In6.Cu"],  [12, "In5.Cu"],
    [18, "In8.Cu"], [16, "In7.Cu"], [22, "In10.Cu"], [20, "In9.Cu"],
    [26, "In12.Cu"],[24, "In11.Cu"],[30, "In14.Cu"], [28, "In13.Cu"],
    [34, "In16.Cu"],[32, "In15.Cu"],[38, "In18.Cu"], [36, "In17.Cu"],
    [42, "In20.Cu"],[40, "In19.Cu"],[46, "In22.Cu"], [44, "In21.Cu"],
    [50, "In24.Cu"],[48, "In23.Cu"],[54, "In26.Cu"], [52, "In25.Cu"],
    [58, "In28.Cu"],[56, "In27.Cu"],[62, "In30.Cu"], [60, "In29.Cu"],
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
HEADER_TEMPLATE: str = """\
(kicad_pcb (version 20221018) (generator KIUB)

  (general
    (thickness 1.6)
  )

  (paper {papersize})
  (layers\n{PCBlayers}
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(21 "Eco1.User" user "User.Eco1")
\t\t(23 "Eco2.User" user "User.Eco2")
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t\t(39 "User.1" user)
\t\t(41 "User.2" user)
\t\t(43 "User.3" user)
\t\t(45 "User.4" user)
  )

  (setup
    (pad_to_mask_clearance 0.051)
    (solder_mask_min_width 0.15)
    (allow_soldermask_bridges_in_footprints yes)
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
        if b == 94:  # '^' toggles overline mode
            overline_active = not overline_active
            continue
        ch = ub_fontmap.get(bytes([b]), "?")
        if overline_active:
            chars.append("\u0305")  # combining overline (must precede the character in KiCad)
        chars.append("\\" + ch if ch in ('"', "\\") else ch)
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
    NPTHclearance:       float = 0.15
    dcMin:               float = 0.05
    lineWidth:           float = 0.075
    defaultClearance:    float = 0.254
    defaultWidth:        float = 0.254
    defaultThermalGap:   float = 0.254
    defaultThermalWidth: float = 0.254
    fontThickRatio:      int   = 1000
    fontHeightRatio:     float = 1.208
    fontWidthRatio:      float = 1.186

    def __init__(self, ddf: IO[bytes], kicad: IO[str],
                 args: argparse.Namespace) -> None:
        self.ddf   = ddf
        self.kicad = kicad
        self.args  = args

        # DDF version and numeric precision – updated in _handle_header
        self.DDF_major: int | str = 4
        self.dr_Ac: int = 2
        self.di_Ac: int = 6

        # Layout offsets – set in _handle_header
        self.offsetX:   float = 0.0
        self.offsetY:   float = 0.0
        self.layerMask: str   = "0x3"

        # Technology tables
        self.traceWidth:     dict[int, int]   = {}
        self.traceClearance: dict[int, int]   = {}
        self.drillCode:      list[int | float] = [0] * 256

        padColumns = ["Xsize", "Ysize", "Xoffset", "roundratio", "clearance"]
        self.pads: list[SimpleDataFrame] = [
            SimpleDataFrame(256, 5, padColumns) for _ in range(3)
        ]

        # Net / shape registries
        self.nets:   dict[int, str | int]          = {0: ''}
        self.ncount: int                           = 0
        self.Shapes: dict[str, dict[str, Any]]     = {}

        # Board clearance – set in _handle_tech
        self.boardClearance: float = 0.0

        # Power-plane zone data – set in _handle_header
        self.zoneParams: dict[str, Any] = {
            key: [] for key in ["pwrPlanes", "extX0", "extY0", "extX1", "extY1"]
        }

        # Raw binary text payload for the current *X record
        self._line_b: bytes = b""

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
        for raw_line in self.ddf:
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
        line           = self._readline()
        self.DDF_major = line[0]    # '4' or '5'
        # Both versions currently use the same accuracy; kept explicit
        # for forward-compatibility.
        self.dr_Ac, self.di_Ac = (2, 6) if self.DDF_major == '4' else (2, 6)

        line         = self._readline()
        board_params = list(map(int, line[:-1].split(',')))

        # Board outline extents → paper size selection
        ext_x0 = self.units_to_mm(board_params[0])
        ext_y0 = -self.units_to_mm(board_params[1])
        ext_x1 = self.units_to_mm(board_params[2])
        ext_y1 = -self.units_to_mm(board_params[3])
        board_center: Point2D = ((ext_x0 + ext_x1) / 2, (ext_y0 + ext_y1) / 2)

        board_h = abs(ext_y0) + abs(ext_y1) + 90   # add margin for frame + title block
        board_w = abs(ext_x0) + abs(ext_x1) + 30

        sheet_sizes: dict[str, tuple[int, int]] = {
            "A5": (148, 210), "A4": (210, 297), "A3": (297, 420),
            "A2": (420, 594), "A1": (594, 841), "A0": (841, 1189),
        }
        sheetSize    = "A0"
        frame_center: Point2D = (
            sheet_sizes["A0"][1] / 2,
            (sheet_sizes["A0"][0] - 45) / 2 + 7.5,
        )
        for sheet_name, (sh, sw) in sheet_sizes.items():
            if board_h <= sh and board_w <= sw:
                sheetSize    = sheet_name
                frame_center = (sw / 2, (sh - 45) / 2 + 7.5)
                break

        self.offsetX   = round(frame_center[0] - board_center[0], self.di_Ac)
        self.offsetY   = round(frame_center[1] - board_center[1], self.di_Ac)
        maxLayers      = board_params[-1]
        self.layerMask = hex((2 ** maxLayers) - 1)

        mask_bits = bin(int(self.layerMask, 16))[2:].zfill(32)[::-1]
        layers_t: list[LayerEntry] = sorted(
            compress(layersCu, (int(x) for x in mask_bits))
        )
        # B.Cu must follow the inner layers, not sit at index 1.
        layers_t.append(layers_t[1])
        layers_t.remove(layers_t[1])

        layers_p = "\n".join(
            f'\t({entry[0]} "{entry[1]}" signal)'
            for entry in layers_t[:maxLayers]
        )
        if self.args.verbose:
            print(f"{maxLayers} layers:\n{layers_p}")

        self.kicad.write(HEADER_TEMPLATE.format(PCBlayers=layers_p,
                                                papersize=f'"{sheetSize}"'))
        self.kicad.write('  (net 0 "")\n')

        for _ in range(3):
            self.ddf.readline()     # lamination sequence / reference point / router options
        self.ddf.readline()         # layer direction

        # Power-plane net numbers (spread over 6 lines).
        pwr_raw   = " ".join(l.decode("CP437").strip() for l in islice(self.ddf, 6))
        pwrPlanes: list[tuple[str, int]] = [
            (layersCu[i][1], net + 1)
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
                print(f"Shape {sName} \033[2;31;43m SKIPPED \033[0;0m")
            return

        if self.args.verbose:
            print(f"Shape {sName}")

        # Reference text descriptor
        lname     = [int(i) for i in self._readline().split()]
        sn_rel_x  = self.units_to_mm(lname[0])
        sn_rel_y  = -self.units_to_mm(lname[1])
        sn_height = self.units_to_mm(lname[2])
        sn_rot    = lname[3] / 64
        sn_width  = self.units_to_mm(lname[4])
        sn_thick  = round(lname[5] * sn_height / self.fontThickRatio, self.di_Ac)

        # Alias text descriptor
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

        shapeStr += self._read_shape_lines(sName)
        pinDescr  = self._read_pad_descriptors()
        shapeStr += self._read_shape_arcs(sName)

        if sName != 'BOARD':
            self.Shapes[sName] = {'str': shapeStr, 'pinDescr': pinDescr}

    def _read_shape_lines(self, sName: str) -> str:
        """Read outline line segments and return their KiCad string (or write directly for BOARD)."""
        sline = ''
        while True:
            seg_line = self._readline()
            if seg_line.startswith(';'):
                break
            sline += seg_line[:-1] if seg_line.endswith(';') else seg_line
            if ';' in seg_line:
                break

        if not sline:
            return ''

        seg_coords = [int(i) for i in sline.split(',')]
        # Correct the coordinate list (see project notes on shape lines).
        p = 2
        while p < len(seg_coords):
            if seg_coords[p] % 2 == 0:
                if seg_coords[p - 2] % 2 == 0:
                    seg_coords[p:p] = [seg_coords[p - 2], seg_coords[p - 1]]
                    p += 2
                else:
                    seg_coords[p - 2] -= 1
            elif seg_coords[p - 2] % 2 != 0:
                del seg_coords[p - 2:p]
                p -= 2
            p += 2

        is_board = sName == 'BOARD'
        li_type  = "gr_line"   if is_board else "fp_line"
        fp_layer = "Edge.Cuts" if is_board else "{fp_side}.SilkS"
        ox = self.offsetX if is_board else 0.0
        oy = self.offsetY if is_board else 0.0
        result   = ''

        for p in range(0, len(seg_coords) - 3, 4):
            seg = (
                f'     ({li_type}\n'
                f'        (start {self._f(self.units_to_mm(seg_coords[p])     + ox)}'
                f' {self._f(-self.units_to_mm(seg_coords[p + 1]) + oy)})\n'
                f'        (end   {self._f(self.units_to_mm(seg_coords[p + 2]) + ox)}'
                f' {self._f(-self.units_to_mm(seg_coords[p + 3]) + oy)})\n'
                f'        (width {self.lineWidth})\n'
                f'        (layer "{fp_layer}")\n'
                f'     )\n'
            )
            if is_board:
                self.kicad.write(seg)
            else:
                result += seg
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

    def _read_shape_arcs(self, sName: str) -> str:
        """Read outline arc/circle records and return their KiCad string (or write for BOARD).

        Format per line: <cx>,<cy>,<radius>,<ang1>,<ang2>
        ang1 and ang2 are in degrees × 64.
        """
        result   = ''
        is_board = sName == 'BOARD'
        ox = self.offsetX if is_board else 0.0
        oy = self.offsetY if is_board else 0.0

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
                    shape_t  = (
                        f'     ({li_type}\n'
                        f'        (center {self._f(centre_x + ox)} {self._f(-centre_y + oy)})\n'
                        f'        (end {self._f(centre_x + radius + ox)} {self._f(-centre_y + oy)})\n'
                        f'        (width {self.lineWidth})\n'
                        f'        (layer "{fp_layer}")\n'
                        f'     )\n'
                    )
                else:
                    li_type  = "gr_arc"    if is_board else "fp_arc"
                    fp_layer = "Edge.Cuts" if is_board else "{fp_side}.SilkS"
                    (dx_start, dy_start), \
                    (dx_mid,   dy_mid),   \
                    (dx_end,   dy_end)    = calc_arc_points(
                        radius, start_angle, span_angle, self.di_Ac)
                    shape_t = (
                        f'     ({li_type}\n'
                        f'        (start {self._f(centre_x + dx_start + ox)}'
                        f' {self._f(-centre_y - dy_start + oy)})\n'
                        f'        (mid   {self._f(centre_x + dx_mid   + ox)}'
                        f' {self._f(-centre_y + dy_mid   + oy)})\n'
                        f'        (end   {self._f(centre_x + dx_end   + ox)}'
                        f' {self._f(-centre_y - dy_end   + oy)})\n'
                        f'        (width {self.lineWidth})\n'
                        f'        (layer "{fp_layer}")\n'
                        f'     )\n'
                    )

                if is_board:
                    self.kicad.write(shape_t)
                else:
                    result += shape_t

            if ';' in arc_line:
                break
        return result

    # -----------------------------------------------------------------------
    # *T – Technology data (dispatches internally by sub-code)
    # -----------------------------------------------------------------------

    def _handle_tech(self, line: str) -> None:
        match line[2]:
            case 'P':   # *TP – default padset (not used)
                if self.args.verbose:
                    print(f"Default padset {line[4:]}")

            case 'T':   # *TT – trace code, width, clearance
                tl = [int(i) for i in line[4:].split(',')]
                self.traceWidth[tl[0]]     = tl[1]
                self.traceClearance[tl[0]] = tl[2]

            case 'C':   # *TC – drill tolerance, board clearance
                dd = [int(i) for i in line[4:].split()]
                if self.args.verbose:
                    print(f"Drill tolerance {dd[0]}")
                self.boardClearance = self.units_to_mm(float(dd[1]))

            case 'D':   # *TD – drill code, drill diameter
                dc = [int(i) for i in line[4:].split(',')]
                mm = self.units_to_mm(dc[1], self.dr_Ac)
                # Via codes (≥240) keep their size to allow microvias;
                # pad codes below dcMin are set to -1 (SMD special case).
                self.drillCode[dc[0]] = mm if dc[0] >= 240 else (mm if mm > self.dcMin else -1)

            case '0' | '1' | '2':  # *T0/*T1/*T2 – pad definitions
                self._handle_tech_pad(line)

            case 'S':   # *TS – wave solder direction (not used)
                if self.args.verbose:
                    print(f"Wave solder dir {line[4:]}")

            case _:
                if self.args.verbose:
                    print(f"T? {line}")

    def _handle_tech_pad(self, line: str) -> None:
        """Process a *T0/*T1/*T2 pad definition record."""
        pi = [int(i) for i in line[4:].split(',')]
        pc_offsetX = (
            0 if pi[1] == pi[2]
            else round(self.units_to_mm(pi[2] - pi[1]) / 2, self.di_Ac)
        )
        roundratio = (
            round(pi[4] / min(pi[1] + pi[2], pi[3]), self.di_Ac)
            if (pi[1] + pi[2]) != 0 and pi[3] != 0
            else 0
        )
        pi[1] = self.units_to_mm(pi[1])
        pi[2] = self.units_to_mm(pi[2])
        pi[3] = self.units_to_mm(pi[3])
        pi[5] = self.units_to_mm(pi[5], self.di_Ac)

        if pi[3] == 0 and self.drillCode[pi[0]] != 0:
            # NPTH hole: set pad size equal to drill size.
            pi[1] = pi[2] = self.drillCode[pi[0]] / 2
            pi[3] = self.drillCode[pi[0]]

        # Swap layers 0↔1 to match KiCad numbering.
        ppos = int(line[2]) if int(line[2]) == 2 else abs(int(line[2]) - 1)
        self.pads[ppos].iloc[pi[0]] = [
            round(pi[1] + pi[2], self.di_Ac),
            pi[3],
            pc_offsetX,
            roundratio,
            pi[5] if pi[5] else self.NPTHclearance,
        ]

        if int(line[2]) in (0, 1, 2) and pi[0] == 255:
            # Fill pad size for drill-only (NPTH) codes.
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
        fields       = line[3:-1].split()
        self.ncount += 1
        net_name     = fields[0].strip('"') or f"SB${self.ncount}"
        repl         = str.maketrans({'"': '', "'": '/', '\\': '/'})
        self.nets[self.ncount] = net_name.translate(repl)
        if self.nets[self.ncount] == 65535:
            self.nets[self.ncount] = 1
        self.kicad.write(f'  (net {self.ncount} "{self.nets[self.ncount]}")\n')

    # -----------------------------------------------------------------------
    # *C – Component placement
    # -----------------------------------------------------------------------

    def _handle_component(self, line: str) -> None:
        carr   = line[3:].split()
        cname  = carr[0]
        calias = carr[1].strip("/")
        cshape = carr[2]

        carr   = self._readline().split(",")
        cxpos  = round(self.units_to_mm(int(carr[0])) + self.offsetX, self.di_Ac)
        cypos  = round(-self.units_to_mm(int(carr[1])) + self.offsetY, self.di_Ac)
        layerB = int(carr[2]) / 64 < 0
        crot   = round(int(carr[2]) / 64, self.di_Ac)

        cnxpos = round(-self.units_to_mm(int(carr[3])) if layerB else self.units_to_mm(int(carr[3])), self.di_Ac)
        cnypos = round(-self.units_to_mm(int(carr[4])), self.di_Ac)
        cnrot  = round(int(carr[5]) / 64 + crot, self.di_Ac)
        cnhght = round(self.units_to_mm(int(carr[6])), self.di_Ac)
        cnwdth = round(self.units_to_mm(int(carr[7])),  self.di_Ac)
        cnthck = round(int(carr[8]) * cnhght / self.fontThickRatio, self.di_Ac)

        caxpos = round(-self.units_to_mm(int(carr[9])) if layerB else self.units_to_mm(int(carr[9])), self.di_Ac)
        caypos = round(-self.units_to_mm(int(carr[10])), self.di_Ac)
        carot  = round(int(carr[11]) / 64 + crot, self.di_Ac)
        cahght = round(self.units_to_mm(int(carr[12])), self.di_Ac)
        cawdth = round(self.units_to_mm(int(carr[13])),  self.di_Ac)
        cathck = round(int(carr[14]) * cahght / self.fontThickRatio, self.di_Ac)

        self.ddf.readline()  # thermal/force vector line – not used

        padnet: list[int] = []
        while True:
            pin_line = self._readline()
            if not pin_line or pin_line[0] == ';':
                break
            padnet.extend(int(i) + 1 for i in pin_line.split()[::2])

        side = 'B' if layerB else 'F'
        mir  = "                 (justify mirror)\n" if layerB else ""

        refBlock = (
            f'     (property "Reference" "{cname}"\n'
            f'           (layer "{side}.SilkS")\n'
            f'           (at {cnxpos} {cnypos} {cnrot})\n'
            f'           (unlocked yes)\n'
            f'           (hide no)\n'
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
            f'           (hide yes)\n'
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
        """Invert all x-coordinates in geometric entries (for bottom-layer components)."""
        regex    = r"(?:\bstart|center|mid|end\b)\s+(-?[\d.]+)\s+(-?[\d.]+)"
        new_str  = ""
        prev_end = 0
        for m in re.finditer(regex, shapeStr, re.MULTILINE | re.UNICODE):
            flipped_x = -float(m.group(1))
            new_str  += shapeStr[prev_end:m.start(1)] + f"{flipped_x:.{self.di_Ac}f}"
            prev_end  = m.end(1)
        return new_str + shapeStr[prev_end:]

    def _build_pad_attr(self, shape: dict[str, Any], layerB: bool) -> str:
        """Return the (attr smd) or (attr through_hole) line."""
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
            pinLayer: list[str] = ["", "", ""]
            fpaste = bpaste     = ""

            for layer_entry in pad['layers']:
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
                if pinLayer[0]:
                    result += self._add_pad(padnet[pidx], pad, "smd roundrect",
                                            crot, "", pinLayer[0], layerB)
                if pinLayer[2]:
                    result += self._add_pad(padnet[pidx], pad, "smd roundrect",
                                            crot, "", pinLayer[2], layerB)
            elif abs(self.pads[2].at[pad['code'], 'Ysize']) == dc:
                result += self._add_pad(padnet[pidx], pad, "np_thru_hole circle",
                                        crot, dc, ' "*.Cu" "*.Mask"', layerB)
            else:
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
        net_num    = padnet if padnet != 65536 else 0
        pad_x      = -pad['relx'] if layerB else pad['relx']

        if padshape == "thru_hole roundrect":
            # drCode is a float from units_to_mm; adding 0.01 can produce IEEE noise.
            pad_w = pad_h = round(drCode + 0.01, self.di_Ac)
            pad_offset    = 0
            pad_rr        = 0.5
        else:
            pad_w      = self.pads[layer_idx].at[pad['code'], 'Xsize']
            pad_h      = self.pads[layer_idx].at[pad['code'], 'Ysize']
            pad_offset = self.pads[layer_idx].at[pad['code'], 'Xoffset']
            if layerB and pad['rot'] in (0, 180):
                pad_offset = -pad_offset
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
                    print(line[2])

    def _handle_trace(self, line: str) -> None:
        """*LT – Horizontal, vertical, and 45° traces."""
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
        """*LV – Arbitrary-angle vector traces."""
        vline  = [int(i) for i in line[4:].split()]
        vlayer = layer_from_bit(vline[0] - 1)[1]
        self.kicad.write(
            f'  (segment\n'
            f'        (start {self._f(self.units_to_mm(vline[1]) + self.offsetX)}'
            f' {self._f(-self.units_to_mm(vline[2]) + self.offsetY)})\n'
            f'        (end {self._f(self.units_to_mm(vline[3]) + self.offsetX)}'
            f' {self._f(-self.units_to_mm(vline[4]) + self.offsetY)})\n'
            f'        (width {self.units_to_mm(self.traceWidth[vline[6]])})\n'
            f'        (layer "{vlayer}")\n'
            f'        (net {self._map_ddf_to_kicad_net(vline[5])})\n'
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
        # Zero width causes a KiCad import bug (gets silently set to 0.1).
        atWidth: float = (
            self.units_to_mm(self.traceWidth[atcode]) if atcode != 65535 else 0.000001
        ) or 0.000001

        (dx_start, dy_start), \
        (dx_mid,   dy_mid),   \
        (dx_end,   dy_end)    = calc_arc_points(radius, start_angle, span_angle, self.di_Ac)

        self.kicad.write(
            f'  (gr_arc\n'
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
        lpdist  = lpline[3] - self.traceWidth[lpline[4]]
        lptcode = lpline[4]
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
        vxpos = self._f(self.units_to_mm(int(line[3:].split()[0])) + self.offsetX)

        while True:
            via_line = self._readline()
            if via_line[0] == ';':
                break
            vf        = via_line.split()
            vypos     = self._f(-self.units_to_mm(int(vf[0])) + self.offsetY)
            vnetnr    = self._map_ddf_to_kicad_net(int(vf[1]))
            vpcode    = int(vf[2])
            mask_bits = bin(int(vf[3], 16) & int(self.layerMask, 16))[2:].zfill(32)[::-1]
            vlayers   = sorted(compress(layersCu, (int(x) for x in mask_bits)))

            if 'F.Cu' in str(vlayers) and 'B.Cu' in str(vlayers):
                via_type    = ""
                vlayers_str = '"F.Cu" "B.Cu"'
            else:
                via_type    = "blind"
                vlayers_str = " ".join(f'"{item[1]}"' for item in (vlayers[0], vlayers[-1]))

            self.kicad.write(
                f'  (via {via_type}\n'
                f'        (at {vxpos} {vypos})\n'
                f'        (size {abs(self.pads[0].at[vpcode, "Ysize"])})\n'
                f'        (drill {self.drillCode[vpcode]})\n'
                f'        (layers {vlayers_str})\n'
                f'        (remove_unused_layers yes)\n'
                f'        (keep_end_layers yes)\n'
                f'        (zone_layer_connections)\n'
                f'        (padstack\n'
                f'        \t(mode front_inner_back)\n'
                f'        \t(layer "Inner"\n'
                f'        \t\t(size {abs(self.pads[1].at[vpcode, "Ysize"])})\n'
                f'        \t)\n'
                f'        \t(layer "B.Cu"\n'
                f'        \t\t(size {abs(self.pads[2].at[vpcode, "Ysize"])})\n'
                f'        \t)\n'
                f'        )\n'
                f'        (net {vnetnr})\n'
                f'  )\n'
            )
            if ';' in via_line:
                break

    # -----------------------------------------------------------------------
    # *X – Text
    # -----------------------------------------------------------------------

    def _handle_text(self, line: str) -> None:
        xfields  = line[3:].split(None, 7)[:7]
        text_x   = round(self.units_to_mm(int(xfields[0])) + self.offsetX, self.di_Ac)
        text_y   = round(-self.units_to_mm(int(xfields[1])) + self.offsetY, self.di_Ac)
        text_h   = round(self.units_to_mm(int(xfields[2])) / self.fontHeightRatio, self.di_Ac)
        text_w   = round(self.units_to_mm(int(xfields[3])) * self.fontWidthRatio,  self.di_Ac)
        text_t   = round(int(xfields[4]) * text_h / self.fontThickRatio, self.di_Ac)
        text_bot = int(xfields[5]) / 64 < 0
        text_rot = int(xfields[5]) / 64
        text_lay = int(xfields[6])
        text_mir = ""

        if text_bot or (text_lay % 2 == 0 and text_lay > 0):
            text_mir = "                (justify mirror)\n"
            text_rot = -text_rot

        if text_lay == 0:
            real_layer = 'B.SilkS' if text_bot else 'F.SilkS'
        else:
            real_layer = layer_from_bit(text_lay - 1)[1]

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
    # Internal utility
    # -----------------------------------------------------------------------

    def _readline(self) -> str:
        """Read and decode one line from the DDF file."""
        return self.ddf.readline().decode("CP437").strip()

# ---------------------------------------------------------------------------
# CLI and entry point
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description='Convert UltiBoard V4 and V5 DDFs to KiCad pcb.')
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

with open(args.infile, 'rb') as ddf, \
     open(args.outfile, 'w', encoding='utf-8', errors='replace') as kicad:
    Converter(ddf, kicad, args).convert()
