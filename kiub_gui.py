# KIUB_gui.py  –  Tkinter front-end for KIUB.py
# Python: V3.13
# GNU GENERAL PUBLIC LICENSE Version 3
#
# Place this file in the same directory as KIUB.py and run it directly.

from __future__ import annotations

import argparse
import configparser
import importlib.util
import io
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import Converter from KIUB.py without executing its CLI/conversion block
# ---------------------------------------------------------------------------

def _load_kiub() -> Any:
    """
    Import KIUB.py as a module without triggering its CLI / conversion block.

    KIUB.py calls argparse.parse_args() and immediately opens files at module
    level.  We intercept parse_args by temporarily replacing it with a version
    that raises a private BaseException subclass.  This aborts execution at
    exactly the point where the CLI block starts, after all class and function
    definitions have been registered, and before any file I/O takes place.
    """
    gui_dir   = Path(__file__).parent
    kiub_path = gui_dir / "kiub.py"
    if not kiub_path.exists():
        messagebox.showerror(
            "KIUB not found",
            f"Cannot find kiub.py in:\n{gui_dir}\n\n"
            "Place kiub_gui.py in the same folder as kiub.py.",
        )
        sys.exit(1)

    # Private sentinel – not catchable by KIUB code (it only catches Exception)
    class _StopCLI(BaseException):
        pass

    _orig_parse_args = argparse.ArgumentParser.parse_args

    def _patched(self: argparse.ArgumentParser,   # type: ignore[override]
                 args: Any = None, namespace: Any = None) -> Any:
        raise _StopCLI

    argparse.ArgumentParser.parse_args = _patched  # type: ignore[method-assign]

    spec   = importlib.util.spec_from_file_location("kiub", kiub_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)            # type: ignore[union-attr]
    except _StopCLI:
        pass   # CLI block intercepted – all definitions above it are loaded
    finally:
        argparse.ArgumentParser.parse_args = _orig_parse_args  # type: ignore[method-assign]

    return module


KIUB      = _load_kiub()
Converter = KIUB.Converter

# ---------------------------------------------------------------------------
# Redirect stdout into a queue so the GUI can poll it safely from the
# main thread without blocking.
# ---------------------------------------------------------------------------

class _QueueWriter(io.TextIOBase):
    """File-like object that puts every written string onto a thread-safe queue."""

    def __init__(self, q: queue.Queue[str], log_file=None) -> None:
        self._q = q
        self._log_file = log_file

    def write(self, text: str) -> int:
        if text:
            self._q.put(text)
            if self._log_file:
                # Write plain ascii output to _log.txt
                clean_text = text.replace("\x1b[2;31;43m SKIPPED \x1b[0;0m", " SKIPPED ")
                self._log_file.write(clean_text)
                self._log_file.flush()
        return len(text)


# ---------------------------------------------------------------------------
# KiCad executable config  (stored next to this script as kiub_gui.ini)
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(__file__).parent / "kiub_gui.ini"
_CONFIG_SECTION = "kicad"
_CONFIG_KEY     = "executable"


def _ini_cfg() -> configparser.ConfigParser:
    """A ConfigParser for kiub_gui.ini, pre-loaded with its current
    contents, with case-preserving option names.

    Every reader/writer of this shared file must go through this helper
    rather than instantiating configparser.ConfigParser() directly:
    several of the functions below do a read-modify-write of the *whole*
    file to persist just their own section, so a plain (lowercasing)
    ConfigParser used by any one of them would silently lowercase
    another section's option names on save -- e.g. [font_ratios]'s font
    names -- even though that function never touches [font_ratios]
    itself. Font family names aren't filenames, so unlike the
    .lower() filename/extension matching used elsewhere in KIUB, there
    is no cross-platform reason to normalise their case; it's intrinsic
    to the font itself (its own embedded name table), not the
    filesystem.
    """
    cfg = configparser.ConfigParser()
    cfg.optionxform = str      # type: ignore[method-assign]
    cfg.read(_CONFIG_FILE, encoding="utf-8")
    return cfg


def _load_kicad_exe() -> str:
    """Return the stored KiCad executable path, or '' if not set / invalid."""
    cfg = _ini_cfg()
    path = cfg.get(_CONFIG_SECTION, _CONFIG_KEY, fallback="").strip()
    return path if path and Path(path).is_file() else ""


def _save_kicad_exe(path: str) -> None:
    """Persist the KiCad executable path to the config file."""
    cfg = _ini_cfg()      # keeps any existing keys, incl. other sections
    if not cfg.has_section(_CONFIG_SECTION):
        cfg.add_section(_CONFIG_SECTION)
    cfg.set(_CONFIG_SECTION, _CONFIG_KEY, path)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


# ---------------------------------------------------------------------------
# Whether hover tooltips show (the status bar's Show tooltips, kiub_gui.ini
# [ui] show_tooltips): users who know the program can switch them off. As
# KIUC's kiuc.ini [ui].
# ---------------------------------------------------------------------------

_TOOLTIPS = {"show": True}
_UI_SECTION = "ui"
_TOOLTIPS_KEY = "show_tooltips"


def _load_show_tooltips() -> bool:
    cfg = _ini_cfg()
    try:
        return cfg.getboolean(_UI_SECTION, _TOOLTIPS_KEY, fallback=True)
    except ValueError:
        return True


def _save_show_tooltips(show: bool) -> None:
    cfg = _ini_cfg()
    if not cfg.has_section(_UI_SECTION):
        cfg.add_section(_UI_SECTION)
    cfg.set(_UI_SECTION, _TOOLTIPS_KEY, "true" if show else "false")
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


# ---------------------------------------------------------------------------
# Board-defaults config (persisted alongside the KiCad path, same ini file).
# Mirrors KIUC's [tuning] section / kiuc.ini pattern (kiuc_gui.py).
# ---------------------------------------------------------------------------

_BOARD_DEFAULTS_SECTION = "board_defaults"


def _load_board_defaults() -> dict:
    """Load saved board-default values from kiub_gui.ini. Any name not
    present in the file (fresh install, or a newly-added default) is simply
    left out, so the caller should overlay this onto KIUB.BOARD_DEFAULTS_SPEC's
    built-in defaults rather than assume every key is present."""
    cfg = _ini_cfg()
    values = {}
    if cfg.has_section(_BOARD_DEFAULTS_SECTION):
        for name, _default, _lo, _hi, _desc, _target in KIUB.BOARD_DEFAULTS_SPEC:
            if cfg.has_option(_BOARD_DEFAULTS_SECTION, name):
                try:
                    values[name] = cfg.getfloat(_BOARD_DEFAULTS_SECTION, name)
                except ValueError:
                    pass   # corrupted entry; fall back to current default
    return values


def _save_board_defaults(values: dict) -> None:
    """Persist board-default values to the config file."""
    cfg = _ini_cfg()
    if not cfg.has_section(_BOARD_DEFAULTS_SECTION):
        cfg.add_section(_BOARD_DEFAULTS_SECTION)
    for name, value in values.items():
        cfg.set(_BOARD_DEFAULTS_SECTION, name, repr(value))
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


def _fmt_mil(value: float) -> str:
    """Format a mil value to at most 2 decimal places, without trailing
    zeros -- "25" rather than "25.00", "8.33" rather than the long
    repeating decimal a raw 1/n-inch conversion (e.g. 1/1.2) produces."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _center_toplevel(win: tk.Toplevel, parent: tk.Misc) -> None:
    """Center *win* on *parent*.

    tk.Toplevel windows, unlike messagebox's built-in dialogs, don't
    center themselves automatically -- left alone they land wherever
    the window manager's default placement happens to be (typically
    near the screen's top-left corner), which reads as inconsistent
    once messagebox calls elsewhere in the same app appear centered.
    Falls back to centering on the screen if parent's own geometry
    isn't available for any reason, and always clamps the final
    position so the dialog can't end up partially off-screen -- e.g. a
    parent window that itself sits near a screen edge.
    """
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x, y = px + (pw - w) // 2, py + (ph - h) // 2
    except tk.TclError:
        x, y = 0, 0
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max(0, min(x, sw - w))
    y = max(0, min(y, sh - h))
    win.geometry(f"+{x}+{y}")


# ---------------------------------------------------------------------------
# Fine-tuning config (persisted alongside the KiCad path / board defaults,
# same ini file). Kept in its own separate ini SECTION from board_defaults
# so sensitive DRC-adjacent settings aren't mixed in with generic ones on
# disk -- even though both are now edited from one consolidated dialog
# (_ConversionSettingsDialog, below), each tab's values still persist
# through these same separate functions as before. Mirrors KIUC's own
# [tuning] section / kiuc.ini pattern (kiuc_gui.py).
# ---------------------------------------------------------------------------

_FINE_TUNING_SECTION = "fine_tuning"

# font_height_ratio/font_width_ratio are excluded from the flat
# [fine_tuning] section below -- they persist separately, per selected
# font, via [font_ratios]/[last_used] (see _load_font_ratios and
# neighbours). KIUB.FINE_TUNING_SPEC still declares both as ordinary
# tunables (CLI defaults, suggested range), just not through this path.
_FONT_RATIO_FIELDS = ('font_height_ratio', 'font_width_ratio')


def _load_fine_tuning() -> dict:
    """Load saved fine-tuning values from kiub_gui.ini. Any name not
    present in the file (fresh install, or a newly-added tunable) is simply
    left out, so the caller should overlay this onto KIUB.FINE_TUNING_SPEC's
    built-in defaults rather than assume every key is present."""
    cfg = _ini_cfg()
    values = {}
    if cfg.has_section(_FINE_TUNING_SECTION):
        for name, _default, _lo, _hi, _desc, _category in KIUB.FINE_TUNING_SPEC:
            if name in _FONT_RATIO_FIELDS:
                continue
            if cfg.has_option(_FINE_TUNING_SECTION, name):
                try:
                    values[name] = cfg.getfloat(_FINE_TUNING_SECTION, name)
                except ValueError:
                    pass   # corrupted entry; fall back to current default
    return values


def _save_fine_tuning(values: dict) -> None:
    """Persist fine-tuning values to the config file."""
    cfg = _ini_cfg()
    if not cfg.has_section(_FINE_TUNING_SECTION):
        cfg.add_section(_FINE_TUNING_SECTION)
    for name, value in values.items():
        if name in _FONT_RATIO_FIELDS:
            continue
        cfg.set(_FINE_TUNING_SECTION, name, repr(value))
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


# ---------------------------------------------------------------------------
# Per-font Height/Width ratio memory (kiub_gui.ini [font_ratios]/[last_used]).
# Mirrors the fine-tuning/board-defaults sections' own load/save pattern.
# ---------------------------------------------------------------------------

_FONT_RATIOS_SECTION = "font_ratios"
_LAST_USED_SECTION   = "last_used"
_LAST_USED_FONT_KEY  = "font"

# One-time seed for a fresh install's [font_ratios] section. This is the
# only place a font name is ever paired with a ratio in code -- once
# loaded, all font-selection auto-fill logic reads/writes the ini-backed
# table above, never a name->ratio mapping baked into the GUI itself.
# KiCad Font/DejaVu Sans Mono mirror KIUB.FINE_TUNING_SPEC's own built-in
# font_height_ratio/font_width_ratio defaults; Ultiboard Stroke's pair
# was derived from the font itself, built to match Ultiboard's native
# PCB font 1:1 (see FILEFORMAT-DDF.md Section 10.5).
_SEED_FONT_RATIOS: dict[str, tuple[float, float]] = {
    "KiCad Font":       (1.208, 1.186),
    "DejaVu Sans Mono": (1.208, 1.186),
    "Ultiboard Stroke": (1.0, 1.4),
}


def _load_font_ratios() -> dict[str, tuple[float, float]]:
    """Load the per-font Height/Width ratio table. A fresh install (no
    [font_ratios] section yet) starts from _SEED_FONT_RATIOS."""
    cfg = _ini_cfg()
    if not cfg.has_section(_FONT_RATIOS_SECTION):
        return dict(_SEED_FONT_RATIOS)
    ratios: dict[str, tuple[float, float]] = {}
    for name, raw in cfg.items(_FONT_RATIOS_SECTION):
        try:
            h_str, w_str = raw.split("|", 1)
            ratios[name] = (float(h_str), float(w_str))
        except ValueError:
            continue        # corrupted entry; skip rather than crash
    return ratios


def _save_font_ratios(ratios: dict[str, tuple[float, float]]) -> None:
    """Persist the full per-font Height/Width ratio table."""
    cfg = _ini_cfg()
    if not cfg.has_section(_FONT_RATIOS_SECTION):
        cfg.add_section(_FONT_RATIOS_SECTION)
    for name, (height, width) in ratios.items():
        cfg.set(_FONT_RATIOS_SECTION, name, f"{height}|{width}")
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


def _load_last_used_font() -> str:
    """Return the last-selected font name, or '' if none stored yet."""
    cfg = _ini_cfg()
    return cfg.get(_LAST_USED_SECTION, _LAST_USED_FONT_KEY, fallback="").strip()


def _save_last_used_font(name: str) -> None:
    cfg = _ini_cfg()
    if not cfg.has_section(_LAST_USED_SECTION):
        cfg.add_section(_LAST_USED_SECTION)
    cfg.set(_LAST_USED_SECTION, _LAST_USED_FONT_KEY, name)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


class _ConversionSettingsDialog(tk.Toplevel):
    """Consolidated pop-up combining the former separate "Board defaults"
    and "Fine-tuning" dialogs into one tabbed dialog -- three tabs total:
    "Board Defaults" (KIUB.BOARD_DEFAULTS_SPEC, kicad_pcb "setup" section
    + kicad_pro "rules" section), and "Geometry" / "Fallback" (both from
    KIUB.FINE_TUNING_SPEC, split by category exactly as this dialog's
    predecessor did standalone -- see that split's own rationale below).
    Fields on every tab are generated entirely from their spec -- adding a
    new tunable to either spec is all that's needed for it to appear here;
    no layout changes required.

    Consolidated into one dialog with one Save spanning all three tabs,
    but board defaults and fine-tuning still persist to their own separate
    kiub_gui.ini sections via the caller (see
    KiubApp._open_conversion_settings_dialog), through the same
    _save_board_defaults/_save_fine_tuning functions as before -- only the
    dialog itself is consolidated, not the underlying persistence, so nets
    the same on-disk result either way.

    Geometry/Fallback (fine-tuning) are still their own two tabs rather
    than folded into Board Defaults' own tab, or into one one another:
    'geometry' (visual/rendering fit, safe to adjust freely) and
    'clearance' (DRC-adjacent fallback values, alter cautiously) are
    different enough in risk profile to keep visually separate, matching
    this dialog's own predecessor's reasoning for splitting them to begin
    with. All three are flat, sibling tabs (not fine-tuning's own two
    nested a level under a single "Fine-tuning" tab) since nesting
    notebooks would need two levels of tab-clicking to reach some fields,
    for no real benefit over three tabs at one level.

    "Reset to defaults" resets only the currently active tab, not all
    three at once -- deliberately, so resetting one tab's values can't
    silently discard customizations on another that the user wasn't even
    looking at.

    Always opened on the main thread (button command), so no
    thread-safety concerns.
    """

    _SECTION_INTRO = {
        'geometry':  "These affect how converted geometry looks (text size, "
                     "line widths, outline snapping). Safe to adjust for "
                     "visual fit against KiCad's rendering -- they don't "
                     "affect manufacturability or DRC. Font Height/Width "
                     "ratios have moved to the main window, next to the "
                     "font selector -- they're remembered per font there.",
        'clearance': "These are fallback copper clearances/widths used only "
                     "where the DDF doesn't specify a value of its own. "
                     "Alter cautiously -- values set too aggressively can "
                     "trigger DRC clearance violations elsewhere on the "
                     "board.",
    }
    _TAB_TITLE = {
        'board_defaults': 'Board Defaults',
        'geometry':       'Geometry',
        'clearance':      'Fallback',
    }

    def __init__(self, parent: tk.Tk, board_defaults_current: dict,
                fine_tuning_current: dict) -> None:
        super().__init__(parent)
        self.title("Conversion Settings")
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False
        self.board_defaults_result: dict = {}
        self.fine_tuning_result: dict = {}

        self._board_specs = {name: (default, lo, hi, desc, target)
                             for name, default, lo, hi, desc, target in KIUB.BOARD_DEFAULTS_SPEC}
        self._fine_specs = {name: (default, lo, hi, desc, category)
                            for name, default, lo, hi, desc, category in KIUB.FINE_TUNING_SPEC
                            if name not in _FONT_RATIO_FIELDS}
        self._vars: dict[str, tk.StringVar] = {}
        self._tab_frames: dict[str, ttk.Frame] = {}
        self._tab_field_names: dict[str, list[str]] = {}

        self._notebook = ttk.Notebook(self)
        self._notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 8))

        # ── Board Defaults tab ──────────────────────────────────────────
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text=self._TAB_TITLE['board_defaults'])
        self._tab_frames['board_defaults'] = tab
        names: list[str] = []

        row = 0
        ttk.Label(
            tab,
            text="These values are written into the converted kicad_pcb's "
                 "(setup) section and/or the kicad_pro's design rules.",
            wraplength=480, justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))
        row += 1

        for name, default, lo, hi, desc, target in KIUB.BOARD_DEFAULTS_SPEC:
            ttk.Label(tab, text=name, font=("Consolas", 9, "bold")).grid(
                row=row, column=0, sticky="nw", padx=(12, 6), pady=(6, 0))
            var = tk.StringVar(value=str(board_defaults_current.get(name, default)))
            self._vars[name] = var
            ttk.Entry(tab, textvariable=var, width=10, font=("Consolas", 9)).grid(
                row=row, column=1, sticky="nw", pady=(6, 0))
            ttk.Label(tab, text=f"(default {default}, {target})",
                     foreground="#888").grid(row=row, column=2, sticky="nw",
                                             padx=(6, 12), pady=(6, 0))
            row += 1
            ttk.Label(tab, text=f"{desc} (suggested range {lo}\u2013{hi})",
                     wraplength=480, justify="left",
                     foreground="#555").grid(
                row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 12))
            row += 1
            names.append(name)
        self._tab_field_names['board_defaults'] = names

        # ── Geometry / Fallback tabs (fine-tuning) ──────────────────────
        by_category: dict[str, list] = {}
        for entry in KIUB.FINE_TUNING_SPEC:
            if entry[0] in _FONT_RATIO_FIELDS:
                continue    # shown on the main window instead -- see above
            by_category.setdefault(entry[5], []).append(entry)

        for category in ('geometry', 'clearance'):
            entries = by_category.get(category)
            if not entries:
                continue

            tab = ttk.Frame(self._notebook)
            self._notebook.add(tab, text=self._TAB_TITLE.get(category, category))
            self._tab_frames[category] = tab
            names = []

            row = 0
            ttk.Label(tab, text=self._SECTION_INTRO.get(category, ''),
                     wraplength=480, justify="left").grid(
                row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))
            row += 1

            for name, default, lo, hi, desc, _category in entries:
                ttk.Label(tab, text=name, font=("Consolas", 9, "bold")).grid(
                    row=row, column=0, sticky="nw", padx=(12, 6), pady=(6, 0))
                var = tk.StringVar(value=str(fine_tuning_current.get(name, default)))
                self._vars[name] = var
                ttk.Entry(tab, textvariable=var, width=10, font=("Consolas", 9)).grid(
                    row=row, column=1, sticky="nw", pady=(6, 0))
                ttk.Label(tab, text=f"(default {default}, range {lo}\u2013{hi})",
                         foreground="#888").grid(row=row, column=2, sticky="nw",
                                                 padx=(6, 12), pady=(6, 0))
                row += 1
                ttk.Label(tab, text=desc, wraplength=480, justify="left",
                         foreground="#555").grid(
                    row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 12))
                row += 1
                names.append(name)
            self._tab_field_names[category] = names

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        ttk.Button(frm_btns, text="Reset to defaults",
                  command=self._on_reset).pack(side="left")
        ttk.Button(frm_btns, text="Cancel",
                  command=self._on_cancel).pack(side="right")
        ttk.Button(frm_btns, text="Save",
                  command=self._on_save).pack(side="right", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_toplevel(self, parent)
        self.grab_set()

    def _current_tab_key(self) -> str:
        """Which tab key ('board_defaults' | 'geometry' | 'clearance') is
        currently selected, for "Reset to defaults" to act on only that
        one."""
        widget = self.nametowidget(self._notebook.select())
        for key, frame in self._tab_frames.items():
            if frame is widget:
                return key
        return 'board_defaults'  # unreachable in practice; a safe fallback

    def _on_reset(self) -> None:
        key = self._current_tab_key()
        specs = self._board_specs if key == 'board_defaults' else self._fine_specs
        for name in self._tab_field_names.get(key, []):
            self._vars[name].set(str(specs[name][0]))

    def _on_save(self) -> None:
        board_values: dict[str, float] = {}
        fine_values: dict[str, float] = {}
        # Board-defaults fields validated first, then fine-tuning, in each
        # spec's own declared order -- field names never collide between
        # the two specs, confirmed directly against both at implementation
        # time, so this combined iteration can't silently drop or
        # misattribute an entry either way.
        for name, (default, lo, hi, _desc, _extra) in {**self._board_specs, **self._fine_specs}.items():
            raw = self._vars[name].get().strip()
            try:
                v = float(raw)
            except ValueError:
                messagebox.showerror("Invalid value",
                    f'{name}: "{raw}" is not a number.', parent=self)
                return
            if not (lo <= v <= hi):
                ok = messagebox.askyesno("Value out of suggested range",
                    f"{name} = {v} is outside the suggested range "
                    f"{lo}\u2013{hi} (default {default}).\n\nUse it anyway?",
                    parent=self)
                if not ok:
                    return
            if name in self._board_specs:
                board_values[name] = v
            else:
                fine_values[name] = v

        self.board_defaults_result = board_values
        self.fine_tuning_result = fine_values
        self.saved = True
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()




class _StaircaseDialog(tk.Toplevel):
    """Pop-up shown when a V2/V3 DDF file's diagonal-traces-drawn-as-
    staircases are detected (see KIUB.check_v2v3_staircases /
    kiub_v2v3.detect_staircases), before conversion begins.

    The staircase step limit is no longer directly user-settable as an
    arbitrary number -- it was found to be too sensitive a value to hand
    to free-form user input: an ordinary right-angle corner with
    coincidentally equal legs is geometrically indistinguishable from a
    genuine one-step staircase, so a limit set too high risks
    misidentifying real corners as diagonals (cutting through nearby
    copper the original 90-degree corner safely cleared), while one set
    too low risks missing genuine staircases entirely -- either way,
    small input mistakes here can have an outsized effect on the whole
    conversion. Instead, what the user can actually correct is the
    file's own declared default grid step: real staircases are drawn
    along it, but the header only ever reflects whichever grid was most
    recently chosen -- if it was changed after these staircases were
    drawn, the header won't show that. detect_staircases()'s own
    most_common_step_mil (see its docstring) is a concrete, file-derived
    signal for exactly this case, shown here alongside a quick way to
    use it directly without retyping it. Whatever value ends up used,
    it's always validated against a fixed ceiling (STAIRCASE_CEILING_MIL
    in kiub_v2v3.py) rather than silently capped -- entering something
    above it is rejected with an explanation, not quietly reduced.
    """

    def __init__(self, parent: tk.Tk, detect_result: dict) -> None:
        super().__init__(parent)
        self.title("Staircase traces found")
        self.transient(parent)
        self.resizable(False, False)
        self.action: str | None = None   # 'convert' | 'disable' | None (cancelled)
        self.chamfer_enabled: bool = True

        self.ceiling_mil = detect_result.get("ceiling_mil", 25.0)
        declared_grid_mil = detect_result.get("declared_grid_mil")
        self.most_common_mil = detect_result.get("most_common_step_mil")
        most_common_count = detect_result.get("most_common_step_count", 0)
        self.grid_mil: float = declared_grid_mil if declared_grid_mil else self.ceiling_mil

        show_mismatch = (
            self.most_common_mil is not None
            and self.most_common_mil > (declared_grid_mil or 0) + 1.0
        )

        if declared_grid_mil:
            msg = f"The current default grid read from the DDF header is {_fmt_mil(declared_grid_mil)} mil."
            if show_mismatch:
                msg += (
                    f" Ultiboard uses the default grid to draw staircases, "
                    f"but the actual, most common, staircase step is "
                    f"{_fmt_mil(self.most_common_mil)} mil "
                    f"({most_common_count} occurrences). Only "
                    f"{_fmt_mil(self.most_common_mil)} mil will produce "
                    f"perfect diagonal traces, while keeping "
                    f"{_fmt_mil(declared_grid_mil)} mil, when chamfer is "
                    f"enabled, will only flatten the staircase corners."
                )
        else:
            msg = (
                "Real staircases are drawn using the default grid found "
                "in the DDF header, but this file's header doesn't "
                "declare one -- enter the grid this design was routed "
                "at below."
            )
        ttk.Label(self, text=msg, wraplength=440, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))

        row = 1
        ttk.Label(self, text="Declared grid step (mil):").grid(
            row=row, column=0, sticky="w", padx=(12, 6), pady=(0, 8))
        self._grid_var = tk.StringVar(value=_fmt_mil(self.grid_mil))
        self._grid_entry = ttk.Entry(self, textvariable=self._grid_var, width=10,
                                     font=("Consolas", 9))
        self._grid_entry.grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        self._use_staircase_step_var = tk.BooleanVar(value=show_mismatch)
        if show_mismatch:
            frm_step = ttk.Frame(self)
            frm_step.grid(row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 8))
            ttk.Checkbutton(frm_step, variable=self._use_staircase_step_var,
                           command=self._on_toggle_step_source).pack(side="left")
            ttk.Label(frm_step, text=f"Staircase step (mil): {_fmt_mil(self.most_common_mil)}").pack(side="left")
            row += 1
            self._on_toggle_step_source()  # apply initial enabled/disabled state

        self._chamfer_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Chamfer ordinary 90\u00b0 corners too",
                       variable=self._chamfer_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 12))
        row += 1

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 12))
        ttk.Button(frm_btns, text="Cancel conversion",
                  command=self._on_cancel).pack(side="left")
        ttk.Button(frm_btns, text="Disable staircase recovery",
                  command=self._on_disable).pack(side="right", padx=(8, 0))
        ttk.Button(frm_btns, text="Convert",
                  command=self._on_convert).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_toplevel(self, parent)
        self.grab_set()

    def _on_toggle_step_source(self) -> None:
        """Checking 'Staircase step' overrides the editable grid field
        entirely rather than the two competing silently -- disable the
        field while it's checked so it's visually clear which value is
        actually in effect."""
        self._grid_entry.config(
            state="disabled" if self._use_staircase_step_var.get() else "normal")

    def _resolve_grid_value(self) -> float | None:
        """Returns the effective value to use, or None (after showing an
        error) if it's invalid."""
        if self._use_staircase_step_var.get():
            return self.most_common_mil
        raw = self._grid_var.get().strip()
        try:
            v = float(raw)
        except ValueError:
            messagebox.showerror("Invalid value", f'"{raw}" is not a number.', parent=self)
            return None
        if v <= 0:
            messagebox.showerror(
                "Invalid value",
                "The grid step must be greater than 0 -- use \"Disable "
                "staircase recovery\" instead to skip it entirely.", parent=self)
            return None
        if v > self.ceiling_mil:
            messagebox.showerror(
                "Value too large",
                f"{_fmt_mil(v)} mil exceeds the fixed ceiling of {_fmt_mil(self.ceiling_mil)} mil "
                f"-- staircase recovery never uses a step larger than this "
                f"regardless of what's entered here. Enter a value at or "
                f"below {_fmt_mil(self.ceiling_mil)} mil.", parent=self)
            return None
        return v

    def _on_convert(self) -> None:
        v = self._resolve_grid_value()
        if v is None:
            return
        self.grid_mil = v
        self.chamfer_enabled = self._chamfer_var.get()
        self.action = "convert"
        self.destroy()

    def _on_disable(self) -> None:
        self.chamfer_enabled = self._chamfer_var.get()
        self.action = "disable"
        self.destroy()

    def _on_cancel(self) -> None:
        self.action = None
        self.destroy()


class _ChamferOnlyDialog(tk.Toplevel):
    """Minimal pop-up shown when a V2/V3 DDF file has no staircase-drawn
    diagonals to recover (so _StaircaseDialog never appears at all), but
    corner-slanting -- which applies to ordinary 90-degree corners
    regardless of staircase presence -- still will. Without this, a
    file in this situation would get chamfered silently, with no
    dialog and no way to opt out, for a V2/V3 file the user may not
    even know is getting this treatment at all.
    """

    def __init__(self, parent: tk.Tk, detect_result: dict) -> None:
        super().__init__(parent)
        self.title("Chamfer ordinary corners")
        self.transient(parent)
        self.resizable(False, False)
        self.action: str | None = None   # 'convert' | None (cancelled)
        self.chamfer_enabled: bool = True

        ceiling_mil = detect_result.get("ceiling_mil", 25.0)
        msg = (
            "This V2/V3 DDF file doesn't contain any staircase-drawn "
            "diagonal traces, but KIUB can still replace ordinary "
            "90-degree trace corners with 45-degree chamfers, which are "
            f"generally preferable in PCB design (capped at {_fmt_mil(ceiling_mil)} "
            "mil per corner, and never applied anywhere it would move a "
            "trace away from a via)."
        )
        ttk.Label(self, text=msg, wraplength=420, justify="left").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 8))

        self._chamfer_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Chamfer ordinary 90\u00b0 corners",
                       variable=self._chamfer_var).grid(
            row=1, column=0, sticky="w", padx=12, pady=(0, 12))

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        ttk.Button(frm_btns, text="Cancel conversion",
                  command=self._on_cancel).pack(side="left")
        ttk.Button(frm_btns, text="Convert",
                  command=self._on_convert).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_toplevel(self, parent)
        self.grab_set()

    def _on_convert(self) -> None:
        self.chamfer_enabled = self._chamfer_var.get()
        self.action = "convert"
        self.destroy()

    def _on_cancel(self) -> None:
        self.action = None
        self.destroy()


# ---------------------------------------------------------------------------
# Technology (.TEC data) for an Ultiboard library: remembered in the ini
# ---------------------------------------------------------------------------

_TECH_SECTION = "technology"
_TECH_KEYS = ("single", "top", "bottom", "inner", "drills", "traces")


def _load_technology_choice() -> dict[str, str]:
    """The last technology choice: 'mode' ('single' or 'multi') and one
    path per role ('' when not set)."""
    cfg = _ini_cfg()
    choice = {"mode": cfg.get(_TECH_SECTION, "mode", fallback="single")}
    for key in _TECH_KEYS:
        choice[key] = cfg.get(_TECH_SECTION, key, fallback="")
    if choice["mode"] not in ("single", "multi"):
        choice["mode"] = "single"
    return choice


def _save_technology_choice(choice: dict[str, str]) -> None:
    cfg = _ini_cfg()
    if not cfg.has_section(_TECH_SECTION):
        cfg.add_section(_TECH_SECTION)
    for key in ("mode",) + _TECH_KEYS:
        cfg.set(_TECH_SECTION, key, choice.get(key, ""))
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


class _TechnologyDialog(tk.Toplevel):
    """Asks for the technology of an Ultiboard library (.LIB/.L55): one
    single-file .TEC, or a multi-file set (top, bottom and inner pads,
    drills, optional traces). Each file is checked for its role as it is
    picked; OK is available only when every needed file fits. The same
    file may fill several pad roles. result is the choice dict, or None
    when cancelled."""

    _ROLES_MULTI = ("top", "bottom", "inner", "drills", "traces")

    def __init__(self, parent: tk.Tk, library_name: str, choice: dict[str, str]) -> None:
        super().__init__(parent)
        self.title("Technology")
        self.transient(parent)
        self.resizable(True, False)
        self.result: dict[str, str] | None = None
        self._lib = KIUB._load_kiub_library_module()
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text=(
            f"{library_name} is an Ultiboard library: it holds pad, drill and trace codes "
            "only. Pick the technology (.TEC data) the library was made with."),
            wraplength=640, justify="left").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12, 8))

        self._mode_var = tk.StringVar(value=choice.get("mode", "single"))
        mode_frame = ttk.Frame(self)
        mode_frame.grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 6))
        ttk.Label(mode_frame, text="Single or multi-file .TEC data:").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Single file", value="single",
                        variable=self._mode_var, command=self._update).pack(
            side="left", padx=(8, 0))
        ttk.Radiobutton(mode_frame, text="Multi-file set", value="multi",
                        variable=self._mode_var, command=self._update).pack(
            side="left", padx=(8, 0))

        lib = self._lib
        roles = {"single": lib.TEC_SINGLE, "top": lib.TEC_TOP, "bottom": lib.TEC_BOTTOM,
                 "inner": lib.TEC_INNER, "drills": lib.TEC_DRILLS, "traces": lib.TEC_TRACES}
        self._roles = roles
        self._vars: dict[str, tk.StringVar] = {}
        self._status: dict[str, ttk.Label] = {}
        self._rows: dict[str, list[tk.Widget]] = {}
        for row, key in enumerate(_TECH_KEYS, start=2):
            label = lib.TEC_ROLE_NAMES[roles[key]] + (" (optional)" if key == "traces" else "")
            var = tk.StringVar(value=choice.get(key, ""))
            var.trace_add("write", lambda *_: self._update())
            self._vars[key] = var
            widgets: list[tk.Widget] = [
                ttk.Label(self, text=label + ":"),
                ttk.Entry(self, textvariable=var, width=64),
                ttk.Button(self, text="Browse…",
                           command=lambda k=key: self._browse(k)),
                ttk.Label(self, width=2)]
            widgets[0].grid(row=row, column=0, sticky="w", padx=(12, 6), pady=2)
            widgets[1].grid(row=row, column=1, sticky="ew", pady=2)
            widgets[2].grid(row=row, column=2, padx=6, pady=2)
            widgets[3].grid(row=row, column=3, sticky="w", padx=(0, 12), pady=2)
            self._status[key] = widgets[3]
            self._rows[key] = widgets

        self._message = ttk.Label(self, wraplength=640, justify="left")
        self._message.grid(row=8, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 4))

        buttons = ttk.Frame(self)
        buttons.grid(row=9, column=0, columnspan=4, sticky="ew", padx=12, pady=(4, 12))
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right")
        self._ok = ttk.Button(buttons, text="OK", command=self._on_ok)
        self._ok.pack(side="right", padx=(0, 6))

        self._update()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_toplevel(self, parent)
        self.grab_set()

    def _active(self) -> tuple[str, ...]:
        return ("single",) if self._mode_var.get() == "single" else self._ROLES_MULTI

    def _browse(self, key: str) -> None:
        current = self._vars[key].get().strip()
        path = filedialog.askopenfilename(
            parent=self, title=f"Select {self._lib.TEC_ROLE_NAMES[self._roles[key]]}",
            initialdir=str(Path(current).parent) if current else None,
            filetypes=[("Technology files", "*.tec *.TEC"), ("All files", "*.*")])
        if path:
            self._vars[key].set(str(Path(path)))

    def _check(self, key: str) -> tuple[bool, str, list[str]]:
        """(fits, error, incomplete tables) for one role's field."""
        path = self._vars[key].get().strip()
        if not path:
            if key == "traces":
                return True, "", []
            return False, "", []
        if not Path(path).is_file():
            return False, f"{self._lib.TEC_ROLE_NAMES[self._roles[key]]}: file not found", []
        _tec, error, short = self._lib.check_tec(path, self._roles[key])
        return not error, error, short

    def _update(self) -> None:
        active = self._active()
        errors: list[str] = []
        warnings: list[str] = []
        ok = True
        for key in _TECH_KEYS:
            for w in self._rows[key]:
                w.grid() if key in active else w.grid_remove()
            if key not in active:
                continue
            fits, error, short = self._check(key)
            filled = bool(self._vars[key].get().strip())
            self._status[key].config(
                text=("✓" if fits else "✗") if filled else "",
                foreground="green" if fits else "red")
            ok = ok and fits
            if error:
                errors.append(error)
            warnings += short
        lines = errors + [f"Incomplete table: {w}; the missing codes count as undefined"
                          for w in warnings]
        self._message.config(text="\n".join(lines),
                             foreground="red" if errors else "black")
        self._message.grid() if lines else self._message.grid_remove()
        self._ok.config(state=tk.NORMAL if ok else tk.DISABLED)

    def _on_ok(self) -> None:
        self.result = {"mode": self._mode_var.get()}
        for key in _TECH_KEYS:
            self.result[key] = self._vars[key].get().strip()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


def _browse_kicad_exe(parent: tk.Misc | None = None) -> str:
    """
    Open a file-browser so the user can locate the KiCad executable.
    Returns the chosen path string, or '' if the dialog was cancelled.
    """
    if sys.platform.startswith("win"):
        filetypes = [("Executable", "*.exe"), ("All files", "*.*")]
    else:
        filetypes = [("All files", "*")]

    path = filedialog.askopenfilename(
        parent=parent,
        title="Locate the KiCad executable (kicad or kicad.exe)",
        filetypes=filetypes,
    )
    return str(Path(path)) if path else ""


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def _is_monospaced(family: str) -> bool:
    """
    Return True when every character in *family* has the same advance width.

    We measure a narrow character ('i') and a wide character ('W') at a
    neutral size.  If the font is truly monospaced both measurements are equal.
    A try/except guards against broken font entries that Tk cannot render.
    """
    try:
        f = tkfont.Font(family=family, size=12)
        return f.measure("i") == f.measure("W")
    except Exception:
        return False


def _get_system_fonts(mono_only: bool = False) -> list[str]:
    """
    Return a sorted, deduplicated list of font family names installed on this
    machine, ignoring blank or whitespace-only entries.

    When *mono_only* is True only monospaced families are returned.
    """
    all_families: list[str] = sorted(
        {f for f in tkfont.families() if f.strip()}
    )
    if mono_only:
        return [f for f in all_families if _is_monospaced(f)]
    return all_families


# ---------------------------------------------------------------------------
# Lightweight hover tooltip
# ---------------------------------------------------------------------------

class _Tooltip:
    """A small delayed hover tooltip for a single widget. Shows after a
    short delay on mouse-enter (avoids flicker while just passing over),
    hides immediately on leave or click."""

    _DELAY_MS = 500

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self._after_id = self._widget.after(self._DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None or not _TOOLTIPS["show"]:
            return
        x = self._widget.winfo_rootx()
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self._text, justify="left",
            background="#ffffe0", relief="solid", borderwidth=1,
            font=("Segoe UI", 9), wraplength=320, padx=6, pady=4,
        ).pack()

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class KiubApp(tk.Tk):
    _POLL_INTERVAL_MS = 50       # how often (ms) the log area polls the queue

    _LABEL_FONT = ("Segoe UI", 10)
    _ENTRY_FONT = ("Segoe UI", 10)
    _LOG_FONT   = ("Consolas", 9)

    _DEFAULT_FONT = "KiCad Font"

    # Fonts pinned to the top of the Font combobox (in this order), when
    # actually present on the system -- followed by a visual separator,
    # then every other installed font alphabetically. Purely a display
    # convenience; selection/validity logic still works against the
    # underlying font list, not this ordering (see _refresh_font_list).
    _FONT_PRIORITY = ("Ultiboard Stroke", "DejaVu Sans Mono")
    _FONT_SEPARATOR = "\u2500" * 24     # display-only, never a real font

    def __init__(self) -> None:
        super().__init__()
        self.title("KIUB  –  Ultiboard DDF → KiCad PCB Converter")
        self.resizable(True, True)
        self.minsize(760, 540)

        self._log_queue:    queue.Queue[str] = queue.Queue()
        self._running:      bool             = False
        # The output folder, shown but not to be chosen: <name>_KiCad or
        # <name>_lib_KiCad next to the design's files (kiub.default_output_dir).
        self._out_dir_var:  tk.StringVar     = tk.StringVar()
        self._infile_var:   tk.StringVar     = tk.StringVar()

        # Font + per-font Height/Width ratio memory (kiub_gui.ini
        # [font_ratios]/[last_used]). Loaded before creating the font Var
        # so the last session's font is the combobox's initial value.
        self._font_ratios: dict[str, tuple[float, float]] = _load_font_ratios()
        _last_font = _load_last_used_font() or self._DEFAULT_FONT

        self._font_var:         tk.StringVar = tk.StringVar(value=_last_font)
        self._font_height_var:  tk.StringVar = tk.StringVar()
        self._font_width_var:   tk.StringVar = tk.StringVar()
        self._last_valid_font:  str = _last_font    # see _on_font_changed

        self._verbose_var:  tk.BooleanVar    = tk.BooleanVar(value=True)   # default ON
        # What the input converts to, shown beside the output folder.
        self._mode_var:     tk.StringVar     = tk.StringVar()
        self._mono_var:     tk.BooleanVar    = tk.BooleanVar(value=True)   # default ON

        # Editable board defaults (kicad_pcb "setup" section + kicad_pro
        # "rules" section). Loaded from kiub_gui.ini, overlaid onto
        # KIUB.BOARD_DEFAULTS_SPEC's built-in defaults; edited via the
        # "Board defaults…" dialog, not inline fields.
        self._board_defaults: dict[str, float] = {
            name: default for name, default, *_ in KIUB.BOARD_DEFAULTS_SPEC
        }
        self._board_defaults.update(_load_board_defaults())

        # Editable fine-tuning constants (geometry/visual fit + cautious
        # DRC-adjacent fallback clearances). Loaded from kiub_gui.ini,
        # overlaid onto KIUB.FINE_TUNING_SPEC's built-in defaults; edited
        # via the separate "Fine-tuning…" dialog.
        self._fine_tuning: dict[str, float] = {
            name: default for name, default, *_ in KIUB.FINE_TUNING_SPEC
        }
        self._fine_tuning.update(_load_fine_tuning())

        # (default, lo, hi) lookup used by the main window's live ratio
        # validation and by _build_args' range check -- built once here
        # rather than re-scanning KIUB.FINE_TUNING_SPEC on every keystroke.
        self._fine_specs_lookup: dict[str, tuple[float, float, float]] = {
            name: (default, lo, hi) for name, default, lo, hi, *_ in KIUB.FINE_TUNING_SPEC
        }

        # font_height_ratio/font_width_ratio are governed by the per-font
        # [font_ratios] table (keyed on the currently selected font), not
        # by the flat [fine_tuning] section above -- apply the looked-up
        # pair for the starting font now that both exist. If the starting
        # font has no stored entry, self._fine_tuning's own default
        # (1.208/1.186, from KIUB.FINE_TUNING_SPEC) is left as-is.
        self._apply_font_ratio_preset(_last_font)

        # KiCad launcher state: the saved KiCad path ('' until one is
        # needed and found, see _open_in_kicad), and the last conversion's
        # project (cleared when a conversion starts, set when it succeeds),
        # which Open in KiCad opens first, as KIUC's.
        self._kicad_exe:     str = _load_kicad_exe()
        self._last_pro_path: str = ""

        # Font list is built once after the Tk root exists (tkfont.families()
        # requires a live Tk instance).
        self._all_fonts:  list[str] = []
        self._mono_fonts: list[str] = []

        self._build_ui()
        self._load_fonts()          # populate combobox after window is ready

        # Start the polling loop once; it keeps rescheduling itself forever.
        self.after(self._POLL_INTERVAL_MS, self._poll_log)

    # -----------------------------------------------------------------------
    # Font loading
    # -----------------------------------------------------------------------

    def _load_fonts(self) -> None:
        """Populate the font lists and initialise the combobox values."""
        raw_all_fonts = _get_system_fonts(mono_only=False)
        raw_mono_fonts = _get_system_fonts(mono_only=True)
        """Filter @ fonts (list comprehension)"""
        self._all_fonts  = [f for f in raw_all_fonts if not f.startswith('@')]
        self._mono_fonts = [f for f in raw_mono_fonts if not f.startswith('@')]
        # The list is the only way to choose a font (no typing: a name the
        # list doesn't hold can't be assessed here, and KiCad would just show
        # its own font). A remembered font since uninstalled: KiCad Font, said
        # in the log.
        last = self._font_var.get()
        if last != self._DEFAULT_FONT and last not in self._all_fonts:
            self._font_var.set(self._DEFAULT_FONT)
            self._direct_log(f"Font: {last}, used last time, is no longer installed; "
                             f"{self._DEFAULT_FONT} is selected.\n", "warn")
        self._refresh_font_list()

    def _ordered_font_list(self, fonts: list[str]) -> list[str]:
        """The font list as shown: KiCad Font first (it can't be typed), any
        of _FONT_PRIORITY that are present (in that order), a visual
        separator, then the rest exactly as given (already alphabetical --
        see _get_system_fonts). As KIUC's."""
        present = [f for f in self._FONT_PRIORITY if f in fonts]
        rest = [f for f in fonts if f not in present and f != self._DEFAULT_FONT]
        return [self._DEFAULT_FONT] + present + [self._FONT_SEPARATOR] + rest

    def _refresh_font_list(self) -> None:
        """Update the combobox to show either all fonts or only monospaced
        ones. A chosen font the list no longer holds becomes KiCad Font
        (which is always listed)."""
        fonts = self._mono_fonts if self._mono_var.get() else self._all_fonts
        self._font_combo["values"] = self._ordered_font_list(fonts)
        current = self._font_var.get()
        if current != self._DEFAULT_FONT and current not in fonts:
            self._font_var.set(self._DEFAULT_FONT)

    def _on_font_key(self, event: Any) -> str | None:
        """A letter selects the next font starting with it (after the
        current one, round to the start), skipping the divider: the list
        can't be typed in, and 200+ fonts are too many to scroll through.
        As KIUC's."""
        ch = (getattr(event, "char", "") or "").lower()
        if not ch or not ch.isprintable():
            return None
        values = [v for v in self._font_combo["values"] if v != self._FONT_SEPARATOR]
        if not values:
            return "break"
        current = self._font_var.get()
        start = values.index(current) + 1 if current in values else 0
        for v in values[start:] + values[:start]:
            if v.lower().startswith(ch):
                self._font_var.set(v)
                break
        return "break"

    def _use_default_font(self) -> None:
        """Reset the font field to the KiCad default and uncheck mono filter."""
        self._mono_var.set(False)
        self._refresh_font_list()
        self._font_var.set(self._DEFAULT_FONT)   # triggers _on_font_changed

    def _apply_font_ratio_preset(self, font_name: str) -> None:
        """Look up font_name in the per-font ratio table and, if found,
        set both the live fine-tuning values and the main-window
        Height/Width fields to match. If font_name has no stored entry,
        the current values are left untouched -- per-font memory is
        opt-in, not an auto-reset to the global default (see
        _SEED_FONT_RATIOS)."""
        pair = self._font_ratios.get(font_name)
        if pair is not None:
            self._fine_tuning['font_height_ratio'] = pair[0]
            self._fine_tuning['font_width_ratio']  = pair[1]
        self._font_height_var.set(str(self._fine_tuning['font_height_ratio']))
        self._font_width_var.set(str(self._fine_tuning['font_width_ratio']))

    def _on_font_changed(self, *_: Any) -> None:
        value = self._font_var.get().strip()
        if value == self._FONT_SEPARATOR:
            # Display-only divider row in the dropdown -- not a real font.
            # Revert rather than let it sit in the field or affect ratios.
            self._font_var.set(self._last_valid_font)
            return
        self._last_valid_font = value
        self._apply_font_ratio_preset(value)

    _RATIO_STYLE_INVALID = "Invalid.TEntry"

    def _validate_ratio_field(self, var: tk.StringVar, entry: ttk.Entry,
                              spec_name: str) -> None:
        """Live-validate a font ratio field against its FINE_TUNING_SPEC
        range, colouring the entry red when the value is missing, not a
        number, or outside the suggested range. Purely visual -- doesn't
        block typing or clamp the value; Start Conversion still confirms
        an out-of-range value via the same style of prompt the Conversion
        Settings dialog uses (see _read_font_ratio_fields)."""
        _default, lo, hi = self._fine_specs_lookup[spec_name]
        valid = False
        try:
            v = float(var.get().strip())
            valid = lo <= v <= hi
        except ValueError:
            valid = False
        entry.configure(style="TEntry" if valid else self._RATIO_STYLE_INVALID)

    def _ratio_tooltip_text(self, spec_name: str) -> str:
        """Hover text for a font ratio field: its FINE_TUNING_SPEC
        description (the calculation it feeds into) plus its suggested
        range and default, sourced from the spec directly so the tooltip
        can't drift out of sync with it."""
        default, lo, hi = self._fine_specs_lookup[spec_name]
        desc = next((d for n, _default, _lo, _hi, d, _cat in KIUB.FINE_TUNING_SPEC
                     if n == spec_name), "")
        return f"{desc}\n\nSuggested range: {lo}\u2013{hi} (default {default})"

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        ttk.Style().configure(self._RATIO_STYLE_INVALID, fieldbackground="#ffdddd")

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # The grid uses 3 columns:
        #   col 0 – labels (fixed width)
        #   col 1 – main input widgets (stretches)
        #   col 2 – left-aligned checkboxes / extra buttons
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(6, weight=1)

        # ── Input file ──────────────────────────────────────────────────────
        infile_label = ttk.Label(outer, text="Input file:", font=self._LABEL_FONT)
        infile_label.grid(row=0, column=0, sticky=tk.W, pady=(0, 4))
        _Tooltip(infile_label,
                 "An Ultiboard design or shape file (.DDF), or an Ultiboard library: "
                 ".LIB (V4) or .L55 (V5). A library holds pad, drill and trace codes "
                 "only, so it converts with its technology (.TEC data), asked for at "
                 "Start. A library, or a DDF holding shapes only, converts to a library "
                 "only.")

        infile_frame = ttk.Frame(outer)
        infile_frame.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 4))
        infile_frame.columnconfigure(0, weight=1)

        ttk.Entry(infile_frame, textvariable=self._infile_var,
                  font=self._ENTRY_FONT).grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self._infile_var.trace_add("write", self._on_infile_changed)

        ttk.Button(infile_frame, text="Browse…",
                   command=self._browse_infile).grid(row=0, column=1)

        # ── Output folder: shown, not chosen ────────────────────────────────
        out_label = ttk.Label(outer, text="Output folder:", font=self._LABEL_FONT)
        out_label.grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 4))
        _Tooltip(out_label,
                 "Where the conversion goes, next to the design's files; it can't be "
                 "chosen. A design: <name>_KiCad, the KiCad project folder KIUB and KIUC "
                 "share -- <name>.kicad_pcb, the library board <name>_lib.kicad_pcb, the "
                 "footprint library <name>.pretty with its fp-lib-table entry, and "
                 "<name>_log.txt. A library, or a DDF holding shapes only: <name>_lib_KiCad, "
                 "without the design board. A V2/V3 DDF is converted to V4.60 into "
                 "<name>_corrected; the original is never written. To convert somewhere "
                 "else, copy the design's files there first.")
        ttk.Entry(outer, textvariable=self._out_dir_var, font=self._ENTRY_FONT,
                  state="readonly").grid(row=1, column=1, sticky=tk.EW, pady=(0, 4))

        # What the input converts to -- column 2, beside the folder
        ttk.Label(outer, textvariable=self._mode_var, font=self._LABEL_FONT).grid(
            row=1, column=2, sticky=tk.W, padx=(6, 0), pady=(0, 4))

        # ── Font row ─────────────────────────────────────────────────────────
        # Layout:
        #   col 0 : "Font:" label
        #   col 1 : [Combobox (stretches)] [Use KiCad Font button]
        #   col 2 : "Mono only" checkbox  ← left-aligned
        ttk.Label(outer, text="Font:", font=self._LABEL_FONT).grid(
            row=2, column=0, sticky=tk.W, pady=(0, 4))

        font_inner = ttk.Frame(outer)
        font_inner.grid(row=2, column=1, sticky=tk.EW, pady=(0, 4))
        font_inner.columnconfigure(0, weight=1)

        self._font_combo = ttk.Combobox(
            font_inner,
            textvariable=self._font_var,
            font=self._ENTRY_FONT,
            state="readonly",      # choose only; a letter jumps (_on_font_key)
        )
        self._font_combo.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self._font_combo.bind("<KeyPress>", self._on_font_key)
        self._font_var.trace_add("write", self._on_font_changed)

        ttk.Button(
            font_inner, text="Use KiCad Font",
            command=self._use_default_font,
        ).grid(row=0, column=1)

        # "Mono only" checkbox – left-aligned in column 2, same row as Font
        ttk.Checkbutton(
            outer,
            text="Mono only",
            variable=self._mono_var,
            command=self._refresh_font_list,
        ).grid(row=2, column=2, sticky=tk.W, padx=(6, 0), pady=(0, 8))

        # ── Ratio row – row 4, columns 0-1, under the Font row ──────────────
        # Height/Width for the selected font's font_height_ratio/
        # font_width_ratio (formerly on the Geometry tab of the Conversion
        # Settings dialog -- see _ConversionSettingsDialog's intro text).
        # Auto-filled per font via _apply_font_ratio_preset/_on_font_changed;
        # saved to kiub_gui.ini only when Start Conversion is clicked (see
        # _persist_font_ratio), not on every keystroke or font switch.
        ttk.Label(outer, text="Ratio:", font=self._LABEL_FONT).grid(
            row=3, column=0, sticky=tk.W, pady=(0, 8))

        ratio_inner = ttk.Frame(outer)
        ratio_inner.grid(row=3, column=1, sticky=tk.W, pady=(0, 8))

        ttk.Label(ratio_inner, text="Height:", font=self._LABEL_FONT).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 4))
        self._font_height_entry = ttk.Entry(
            ratio_inner, textvariable=self._font_height_var,
            font=self._ENTRY_FONT, width=8)
        self._font_height_entry.grid(row=0, column=1, sticky=tk.W, padx=(0, 16))

        ttk.Label(ratio_inner, text="Width:", font=self._LABEL_FONT).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 4))
        self._font_width_entry = ttk.Entry(
            ratio_inner, textvariable=self._font_width_var,
            font=self._ENTRY_FONT, width=8)
        self._font_width_entry.grid(row=0, column=3, sticky=tk.W)

        self._font_height_var.trace_add("write", lambda *_: self._validate_ratio_field(
            self._font_height_var, self._font_height_entry, 'font_height_ratio'))
        self._font_width_var.trace_add("write", lambda *_: self._validate_ratio_field(
            self._font_width_var, self._font_width_entry, 'font_width_ratio'))
        # Set the initial visual state -- trace_add doesn't fire retroactively
        # for the value each Var already held when the trace was registered.
        self._validate_ratio_field(self._font_height_var, self._font_height_entry, 'font_height_ratio')
        self._validate_ratio_field(self._font_width_var, self._font_width_entry, 'font_width_ratio')

        _Tooltip(self._font_height_entry, self._ratio_tooltip_text('font_height_ratio'))
        _Tooltip(self._font_width_entry, self._ratio_tooltip_text('font_width_ratio'))

        # ── Verbose checkbox – left-aligned in column 2, row 4 ─────────────
        ttk.Checkbutton(
            outer,
            text="Verbose output",
            variable=self._verbose_var,
        ).grid(row=3, column=2, sticky=tk.W, padx=(6, 0), pady=(0, 8))

        # ── Action buttons ───────────────────────────────────────────────────
        # Layout (left → right): ▶ Start Conversion | Open in KiCad | Clear Log | ⚙ Conversion Settings…
        btn_frame = ttk.Frame(outer)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=(0, 8))

        self._start_btn = ttk.Button(
            btn_frame, text="▶  Start Conversion",
            command=self._start_conversion, width=22)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 10))

        # Always active: the output folder is fixed, so the design's project
        # is known as soon as an input is chosen, also from an earlier
        # conversion (see _open_in_kicad).
        self._open_btn = ttk.Button(
            btn_frame, text="⎋  Open in KiCad",
            command=self._open_in_kicad, width=18)
        self._open_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="Clear Log",
                   command=self._clear_log, width=12).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="⚙  Conversion Settings…",
                   command=self._open_conversion_settings_dialog, width=22).pack(side=tk.LEFT)

        # ── Log area ─────────────────────────────────────────────────────────
        ttk.Label(outer, text="Conversion log:", font=self._LABEL_FONT).grid(
            row=5, column=0, columnspan=3, sticky=tk.W)

        self._log = scrolledtext.ScrolledText(
            outer,
            font=self._LOG_FONT,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
            height=16,
        )
        self._log.grid(row=6, column=0, columnspan=3, sticky=tk.NSEW, pady=(4, 0))

        self._log.tag_config("info",    foreground="#9cdcfe")
        self._log.tag_config("success", foreground="#4ec9b0")
        self._log.tag_config("error",   foreground="#f44747")
        self._log.tag_config("warn",    foreground="#dcdcaa")
        self._log.tag_config("plain",   foreground="#d4d4d4")
        self._log.tag_config("skipped", foreground="#ff0000", background="#ffff00")

        # ── Status bar ───────────────────────────────────────────────────────
        # Status text, and at its right Show tooltips: on by default,
        # remembered (kiub_gui.ini [ui]), as KIUC's.
        status_frame = ttk.Frame(outer)
        status_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))
        status_frame.columnconfigure(0, weight=1)
        self._status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_frame, textvariable=self._status_var,
                  relief=tk.SUNKEN, anchor=tk.W).grid(row=0, column=0, sticky=tk.EW)
        _TOOLTIPS["show"] = _load_show_tooltips()
        self._tooltips_var = tk.BooleanVar(value=_TOOLTIPS["show"])
        ttk.Checkbutton(status_frame, text="Show tooltips", variable=self._tooltips_var,
                        command=self._on_tooltips_toggled).grid(row=0, column=1, sticky=tk.E,
                                                                padx=(6, 0))

    # -----------------------------------------------------------------------
    # File / folder dialogs
    # -----------------------------------------------------------------------

    def _browse_infile(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Ultiboard file",
            filetypes=[("Ultiboard files", "*.ddf *.DDF *.lib *.LIB *.l55 *.L55"),
                       ("Ultiboard DDF", "*.ddf *.DDF"),
                       ("Ultiboard library V4", "*.lib *.LIB"),
                       ("Ultiboard library V5", "*.l55 *.L55"),
                       ("All files", "*.*")],
        )
        if path:
            self._infile_var.set(str(Path(path)))   # normalise to OS separators

    # -----------------------------------------------------------------------
    # The output folder, shown as the input changes
    # -----------------------------------------------------------------------

    def _on_infile_changed(self, *_: Any) -> None:
        """When the input file changes, show the output folder -- <name>_KiCad
        (a design) or <name>_lib_KiCad (a library-only input) next to the
        design's files, see kiub.default_output_dir; never chosen -- and
        what the input converts to. The log is cleared: it belonged to the
        previous input (as KIUC's)."""
        self._clear_log()
        infile = self._infile_var.get().strip()
        if not infile:
            self._out_dir_var.set("")
            self._mode_var.set("")
            return
        kind = self._input_kind(infile)
        library_only = kind == KIUB.DDF_LIBRARY
        self._mode_var.set({KIUB.DDF_LIBRARY: "Library only", KIUB.DDF_DESIGN: "Design and library",
                            KIUB.DDF_UNSURE: "Design or library?"}[kind])
        try:
            self._out_dir_var.set(KIUB.default_output_dir(infile, library_only))
        except Exception:
            self._out_dir_var.set("")

    @staticmethod
    def _input_kind(path: str) -> str:
        """What the input converts to (kiub.input_kind)."""
        try:
            return KIUB.input_kind(path)
        except Exception:
            return KIUB.DDF_DESIGN

    # -----------------------------------------------------------------------
    # Validation and conversion
    # -----------------------------------------------------------------------

    def _build_args(self) -> argparse.Namespace | None:
        """Validate inputs and return an argparse.Namespace for Converter.
        args.outfile is the output kiub.default_output_path names (as the
        CLI): kiub.OutputPlan names every output file from it."""
        infile  = self._infile_var.get().strip()
        font    = self._font_var.get().strip() or self._DEFAULT_FONT

        if not infile:
            messagebox.showerror("Missing input", "Please select an input file.")
            return None
        if (Path(infile).suffix.lower() not in (".ddf", ".lib", ".l55")
                and not os.path.exists(infile)):
            infile += ".ddf"
        if not os.path.exists(infile):
            messagebox.showerror("File not found", f"Input file not found:\n{infile}")
            return None
        kind = self._input_kind(infile)
        library_only = kind == KIUB.DDF_LIBRARY

        height_ratio, width_ratio = self._read_font_ratio_fields()
        if height_ratio is None:
            return None

        return argparse.Namespace(
            infile=infile, outfile=KIUB.default_output_path(infile, library_only), font=font,
            verbose=self._verbose_var.get(),
            library_only=library_only, input_kind=kind,
            tec=None, tec_split=None,           # filled in by _ask_technology
            prep=None, working=infile,          # a design's: filled in by _check_corrected
            **self._board_defaults,
            **{**self._fine_tuning,
               'font_height_ratio': height_ratio,
               'font_width_ratio':  width_ratio},
        )

    def _read_font_ratio_fields(self) -> tuple[float, float] | tuple[None, None]:
        """Parse and range-check the main window's Height/Width fields --
        same validation style as the Conversion Settings dialog's own
        fine-tuning fields (not a number is a hard stop; out of the
        suggested range asks for confirmation). This is the single point
        where the live-edited fields actually take effect; the visual red/
        white cue from _validate_ratio_field is informational only."""
        default_h, lo_h, hi_h = self._fine_specs_lookup['font_height_ratio']
        default_w, lo_w, hi_w = self._fine_specs_lookup['font_width_ratio']
        try:
            height = float(self._font_height_var.get().strip())
            width  = float(self._font_width_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid value",
                "Font Height and Width ratio must both be numbers.")
            return None, None
        for label, value, lo, hi, default in (
            ("Height", height, lo_h, hi_h, default_h),
            ("Width",  width,  lo_w, hi_w, default_w),
        ):
            if not (lo <= value <= hi):
                ok = messagebox.askyesno("Value out of suggested range",
                    f"Font {label} ratio = {value} is outside the suggested "
                    f"range {lo}\u2013{hi} (default {default}).\n\nUse it anyway?")
                if not ok:
                    return None, None
        return height, width

    def _persist_font_ratio(self, args: argparse.Namespace) -> None:
        """Save the current font + its Height/Width ratio to kiub_gui.ini.
        The only point these are written to disk -- see _start_conversion.
        Called with an already-validated args (from _build_args), so no
        re-parsing/re-checking here."""
        self._font_ratios[args.font] = (args.font_height_ratio, args.font_width_ratio)
        _save_font_ratios(self._font_ratios)
        _save_last_used_font(args.font)
        self._fine_tuning['font_height_ratio'] = args.font_height_ratio
        self._fine_tuning['font_width_ratio']  = args.font_width_ratio

    def _check_refdes_prescan(self, infile: str, working: str) -> bool:
        """Pre-scan the DDF to convert (the design's working DDF) for
        non-digit-ending reference designators and, if any are found, pop
        up a list and ask whether to continue or abort. Returns True to
        proceed, False to abort. Read-only: does not rename or modify
        anything (see kiub.scan_non_digit_refdes).
        """
        try:
            ddf_handle = KIUB.open_ddf(working, verbose=False)
            try:
                ddf_text = ddf_handle.read().decode("CP437", errors="replace")
            finally:
                ddf_handle.close()
        except Exception:
            # If the pre-scan itself fails for any reason, don't block the
            # conversion on it -- the real converter will surface any
            # genuine problem with the file.
            return True

        offending = KIUB.scan_non_digit_refdes(ddf_text)
        if not offending:
            return True

        advice, has_schematic = KIUB.refdes_advice(infile)
        msg = ("The following component reference designators do not end "
               "in a digit:\n\n  " + "\n  ".join(offending) + "\n\n" + advice + "\n\n")
        if has_schematic:
            # Stop by default: converting this file would give a board whose
            # references no longer match the schematic once it is reannotated.
            msg += "Continue with this file anyway (not recommended)?"
            return messagebox.askyesno("Non-digit-ending reference designators found", msg,
                                       icon=messagebox.WARNING, default=messagebox.NO)
        msg += "Continue with the conversion anyway?"
        return messagebox.askyesno("Non-digit-ending reference designators found", msg)

    def _check_staircase_prompt(self, args: argparse.Namespace) -> bool:
        """If args.infile is a V2/V3 DDF, prompt about staircase recovery
        and/or corner-slanting (chamfering) before conversion, whichever
        applies:
          - Staircases found: _StaircaseDialog, covering both the
            staircase grid correction and the chamfer toggle together
            (they're shown in one dialog specifically to avoid two
            consecutive pop-ups for the same conversion).
          - No staircases found: _ChamferOnlyDialog, covering just the
            chamfer toggle -- corner-slanting still applies to ordinary
            90-degree corners regardless of staircase presence, so this
            case still needs its own (much smaller) prompt rather than
            silently chamfering with no dialog at all -- but only when the
            conversion would chamfer at least one corner (the result's
            "chamfer_corners"); otherwise no dialog, a log line instead.
        Sets args.v2v3_staircase_limit_mil and/or
        args.v2v3_corner_slant_limit_mil accordingly -- the two are set
        independently of each other exactly when they need to diverge
        (e.g. staircase recovery disabled but chamfering still wanted,
        which would otherwise silently disable chamfering too, since it
        tracks the staircase limit by default -- see kiub.open_ddf's own
        docstring on v2v3_corner_slant_limit_mil).
        Returns True to proceed, False to abort the conversion (user
        clicked "Cancel conversion"). A failed pre-check (any exception)
        doesn't block conversion -- the real converter will surface any
        genuine problem with the file.
        """
        try:
            result = KIUB.check_v2v3_staircases(args.working)
        except Exception:
            return True
        if not result:
            return True

        if result.get("found"):
            dlg = _StaircaseDialog(self, result)
            self.wait_window(dlg)
            if dlg.action is None:
                return False
            if dlg.action == "disable":
                args.v2v3_staircase_limit_mil = 0
                # Chamfering tracks the staircase limit by default, so
                # with staircase recovery off it needs its own explicit
                # value here to still apply at all.
                args.v2v3_corner_slant_limit_mil = dlg.grid_mil if dlg.chamfer_enabled else 0
            else:
                args.v2v3_staircase_limit_mil = dlg.grid_mil
                # Left unset when enabled: tracking the staircase limit
                # above already gives the same result.
                if not dlg.chamfer_enabled:
                    args.v2v3_corner_slant_limit_mil = 0
        elif result.get("chamfer_corners", 1):
            dlg = _ChamferOnlyDialog(self, result)
            self.wait_window(dlg)
            if dlg.action is None:
                return False
            if not dlg.chamfer_enabled:
                args.v2v3_corner_slant_limit_mil = 0
        else:
            # Nothing the chamfering would change (no 90-degree corner on
            # an open trace, e.g. only closed loops): no question to ask.
            self._direct_log("V2/V3: no staircases and no trace corners to chamfer.\n", "info")

        return True

    def _check_legacy_v3(self, args: argparse.Namespace) -> bool:
        """An earlier KIUB's '<name>_V3.DDF' (kiub.legacy_v3_question): ask
        before converting it as a design of its own; defaults to No.
        True to go on."""
        try:
            question = KIUB.legacy_v3_question(args.infile)
        except Exception:
            return True
        if not question:
            return True
        return messagebox.askyesno("V2/V3 original of an earlier KIUB",
                                   f"{question}\n\nConvert it as a design of its own anyway?",
                                   icon=messagebox.WARNING, default=messagebox.NO, parent=self)

    def _check_corrected(self, args: argparse.Namespace) -> bool:
        """The design's corrected folder (CORRECTED_FOLDER.md), first of the
        design's checks: refuse a design that can't be converted safely, ask
        when the corrected copies no longer match the originals (redo from
        the originals, or cancel -- the default when copies edited by hand
        would be lost), and set args.prep and args.working, the DDF to
        convert. A library-only input writes no copy: only the read-only
        refusal. False if refused or cancelled."""
        if args.library_only:
            refusal = KIUB.check_writable(args.infile)
            if refusal:
                messagebox.showerror("Cannot convert this file", refusal, parent=self)
                return False
            return True
        prep = KIUB.prepare_design(args.infile)
        if prep.error:
            messagebox.showerror("Cannot convert this design", prep.error, parent=self)
            return False
        question = prep.redo_question()
        if question:
            text, default = question
            if not messagebox.askyesno("Corrected files out of date", text,
                                       icon=messagebox.WARNING,
                                       default=messagebox.YES if default else messagebox.NO,
                                       parent=self):
                self._direct_log("Conversion cancelled -- nothing changed.\n", "warn")
                return False
            self._direct_log(KIUB.redo_from_originals(prep) + "\n", "warn")
        for line in KIUB.working_notes(prep):
            self._direct_log(line + "\n", "warn" if line.startswith("Note") else "info")
        args.prep = prep
        args.working = str(KIUB.working_ddf(prep))
        return True

    def _start_conversion(self) -> None:
        if self._running:
            return
        self._clear_log()

        args = self._build_args()
        if args is None:
            return

        self._persist_font_ratio(args)

        kind = self._library_kind(args.infile)
        if kind == "ulticap":
            messagebox.showerror(
                "Ulticap library",
                "This is an Ulticap symbol library, not an Ultiboard one: convert it "
                "with KIUC.", parent=self)
            return
        if kind == "ultiboard":
            if not self._ask_technology(args):
                self._status_var.set("Conversion cancelled.")
                return
        if args.input_kind == KIUB.DDF_UNSURE:
            answer = messagebox.askyesnocancel(
                "Design or library?",
                f"{KIUB.UNSURE_QUESTION}\n\n"
                "Convert it as a library only?\n\n"
                "Yes: the library board and the footprint library only.\n"
                "No: as a design -- the design board too.",
                icon=messagebox.QUESTION, default=messagebox.NO, parent=self)
            if answer is None:
                self._status_var.set("Conversion cancelled.")
                return
            if answer:
                args.library_only = True
                args.outfile = KIUB.default_output_path(args.infile, True)

        # A library-only input writes no corrected copy, converts no traces
        # (no staircase question) and names its components S1, S2, ... (no
        # designator check).
        if not args.library_only and not self._check_legacy_v3(args):
            self._status_var.set("Conversion cancelled.")
            return

        if not self._check_corrected(args):
            self._status_var.set("Conversion cancelled.")
            return

        if not args.library_only:
            if not self._check_refdes_prescan(args.infile, args.working):
                self._status_var.set("Conversion cancelled.")
                return

            if not self._check_staircase_prompt(args):
                self._status_var.set("Conversion cancelled.")
                return

        if not self._check_overwrite(args):
            self._status_var.set("Conversion cancelled.")
            return

        self._running = True
        self._last_pro_path = ""
        self._start_btn.config(state=tk.DISABLED)
        self._status_var.set("Converting…")
        for line in self._header_lines(args):
            self._direct_log(line, "info")
        self._direct_log("─" * 60 + "\n",             "plain")

        threading.Thread(
            target=self._run_conversion, args=(args,), daemon=True,
        ).start()

    @staticmethod
    def _library_kind(path: str) -> str | None:
        """'ultiboard', 'ulticap' or None (a DDF), by the file's signature."""
        try:
            return KIUB._load_kiub_library_module().library_kind(path)
        except Exception:
            return None

    def _ask_technology(self, args: argparse.Namespace) -> bool:
        """Ask for an Ultiboard library's technology, remember the choice and
        put it on args (tec / tec_split, as the CLI's --tec / --tec-split).
        Incomplete tables ask whether to continue. False when cancelled."""
        dialog = _TechnologyDialog(self, Path(args.infile).name, _load_technology_choice())
        self.wait_window(dialog)
        choice = dialog.result
        if choice is None:
            return False
        _save_technology_choice(choice)
        if choice["mode"] == "single":
            args.tec = choice["single"]
        else:
            args.tec_split = [choice[k] for k in ("top", "bottom", "inner", "drills")]
            if choice["traces"]:
                args.tec_split.append(choice["traces"])
        lib = KIUB._load_kiub_library_module()
        try:
            tech = KIUB.technology_from_args(args)
        except lib.LibraryError as e:
            messagebox.showerror("Technology", f"{e}.", parent=self)
            return False
        if tech.incomplete:
            return messagebox.askyesno(
                "Incomplete technology",
                "The technology has incomplete tables:\n\n"
                + "\n".join(tech.incomplete)
                + "\n\nThe missing codes count as undefined. Continue with the "
                  "conversion?",
                icon=messagebox.WARNING, default=messagebox.NO, parent=self)
        return True

    def _check_overwrite(self, args: argparse.Namespace) -> bool:
        """Ask before replacing boards that already exist (they may hold
        work done in KiCad); defaults to No. True to go on."""
        existing = KIUB.existing_outputs(KIUB.OutputPlan(args.outfile, args.library_only))
        if not existing:
            return True
        return messagebox.askyesno(
            "Replace existing files?",
            f"{KIUB.OVERWRITE_QUESTION}\n\n" + "\n".join(existing) + "\n\nReplace them?",
            icon=messagebox.WARNING, default=messagebox.NO, parent=self)

    @staticmethod
    def _header_lines(args: argparse.Namespace) -> list[str]:
        """The log's header: input (and the working DDF converted, when that
        is its corrected copy), output folder and name, font, mode."""
        out = Path(args.outfile)
        mode = "Library only" if args.library_only else "Design and library"
        lines = [f"Input:  {args.infile}\n"]
        if getattr(args, "working", args.infile) != args.infile:
            lines.append(f"Read:   {args.working}\n")
        return lines + [f"Output: {out.parent}\n", f"Name:   {out.stem}\n",
                        f"Font:   {args.font}\n", f"Mode:   {mode}\n"]

    def _run_conversion(self, args: argparse.Namespace) -> None:
        """
        Run the conversion (kiub.convert_input) in a worker thread,
        capturing all stdout into the log. Also writes <name>_log.txt into
        the output folder, including the header lines shown in the GUI log
        window. On completion a sentinel string is placed on the queue so
        the main thread can re-enable the UI.
        """
        plan = KIUB.OutputPlan(args.outfile, args.library_only)
        Path(plan.folder).mkdir(parents=True, exist_ok=True)

        with open(plan.log, "w", encoding="utf-8") as f:
            # Write the header lines that _start_conversion already sent to the
            # GUI log widget directly (they bypass _QueueWriter, so we echo them
            # to the file here before redirecting stdout).
            for line in self._header_lines(args):
                f.write(line)
            f.write("─" * 60 + "\n")
            f.flush()

            writer      = _QueueWriter(self._log_queue, f)
            orig_stdout = sys.stdout
            sys.stdout  = writer

            success = False
            try:
                if getattr(args, "tec", None) or getattr(args, "tec_split", None):
                    # An Ultiboard library, with its technology
                    ddf_handle = KIUB.open_library(args.infile,
                                                   KIUB.technology_from_args(args))
                elif args.library_only:
                    # A library-only DDF is read as it is: no V4.60 copy.
                    ddf_handle = KIUB.open_ddf(args.infile, verbose=args.verbose, args=args)
                else:
                    ddf_handle = None
                if ddf_handle is not None:
                    try:
                        ddf_bytes = ddf_handle.read()
                    finally:
                        ddf_handle.close()
                else:
                    # A design: its working DDF (args.prep from _check_corrected;
                    # prepared here when called without it); a V2/V3 one's
                    # V4.60 copy is written into <name>_corrected now the
                    # conversion goes on.
                    prep = args.prep or KIUB.prepare_design(args.infile)
                    if prep.error:
                        raise RuntimeError(prep.error)
                    design = KIUB.read_design(prep, args, verbose=args.verbose)
                    line = KIUB.write_v460_copy(prep, design)
                    if line:
                        print(line)
                    ddf_bytes = design.data
                KIUB.convert_input(args, ddf_bytes, plan)
                success = True
            except Exception as e:
                lib_mod = KIUB._kiub_library_module
                if lib_mod is not None and isinstance(e, lib_mod.LibraryError):
                    print(f"Error: {e}. Nothing written.")   # no shapes, or a technology fault
                else:
                    self._log_queue.put("\n" + traceback.format_exc() + "\n")
            finally:
                sys.stdout = orig_stdout

        self._log_queue.put("\x00DONE\x00" + ("OK:" + (plan.board or plan.library_board)
                                               + "\x01" + plan.project if success else "FAIL"))

    # -----------------------------------------------------------------------
    # Log polling (runs continuously on the main thread via after())
    # -----------------------------------------------------------------------

    def _poll_log(self) -> None:
        """
        Drain the log queue and update the ScrolledText widget.

        Reschedules itself unconditionally so it keeps running regardless of
        whether a conversion is in progress.
        """
        try:
            while True:
                text = self._log_queue.get_nowait()
                if text.startswith("\x00DONE\x00"):
                    payload = text[len("\x00DONE\x00"):]
                    if payload.startswith("OK:"):
                        parts = payload[3:].split("\x01", 1)
                        pcb_path = parts[0]
                        pro_path = parts[1] if len(parts) > 1 else ""
                        self._on_conversion_done(True, pcb_path, pro_path)
                    else:
                        self._on_conversion_done(False, "", "")
                else:
                    self._append_log(text)
        except queue.Empty:
            pass

        self.after(self._POLL_INTERVAL_MS, self._poll_log)

    def _append_log(self, text: str) -> None:
        """Append text from the queue to the log widget with colour tagging."""
        self._log.config(state=tk.NORMAL)

        # # Trim log size
        # if int(self._log.index(tk.END).split(".")[0]) > 5000:
        #     self._log.delete("1.0", "100.0")

        ansi_skipped = "\x1b[2;31;43m SKIPPED \x1b[0;0m"

        if ansi_skipped in text:
            # Reformat 'SKIPPED' code
            parts = text.split(ansi_skipped)
            for i, part in enumerate(parts):
                if part:
                    # Create normal text tag
                    lower_part = part.lower()
                    if "error" in lower_part or "traceback" in lower_part:
                        tag = "error"
                    elif "warn" in lower_part:
                        tag = "warn"
                    else:
                        tag = "plain"
                    self._log.insert(tk.END, part, tag)
                
                # Add the colored " SKIPPED " label
                if i < len(parts) - 1:
                    self._log.insert(tk.END, " SKIPPED ", "skipped")
        else:
            # Default text logic
            lower = text.lower()
            if "error" in lower or "traceback" in lower or "exception" in lower:
                tag = "error"
            elif "skipped" in lower or "warn" in lower:
                tag = "warn"
            elif any(kw in lower for kw in ("layer", "shape", "default padset")):
                tag = "info"
            else:
                tag = "plain"
            self._log.insert(tk.END, text, tag)

        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _direct_log(self, text: str, tag: str) -> None:
        """Write directly to the log widget from the main thread."""
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, text, tag)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)

    # -----------------------------------------------------------------------
    # Completion callback (called from _poll_log on the main thread)
    # -----------------------------------------------------------------------

    def _on_conversion_done(self, success: bool, pcb_path: str, pro_path: str) -> None:
        self._running = False
        self._start_btn.config(state=tk.NORMAL)

        if success:
            self._last_pro_path = pro_path
            self._direct_log("\n✓ Conversion complete.\n",   "success")
            self._direct_log(f"  PCB:     {pcb_path}\n",    "success")
            self._direct_log(f"  Project: {pro_path}\n",    "success")
            self._status_var.set(f"Done  –  {Path(pcb_path).name}")
        else:
            self._direct_log("\n✗ Conversion failed. See traceback above.\n", "error")
            self._status_var.set("Failed.")

    # -----------------------------------------------------------------------
    # KiCad launcher
    # -----------------------------------------------------------------------

    # No question at startup and no KiCad Path button (as KIUC): Open in
    # KiCad finds KiCad when clicked -- the saved path, else the program the
    # system opens .kicad_pro files with (KiCad's installer sets that up),
    # and only when neither works, a question, whose answer is saved.

    def _on_tooltips_toggled(self) -> None:
        _TOOLTIPS["show"] = bool(self._tooltips_var.get())
        try:
            _save_show_tooltips(_TOOLTIPS["show"])
        except OSError:
            pass                            # a convenience only

    def _ask_kicad_exe(self) -> bool:
        """Ask where KiCad is; save the answer. Returns whether one was given."""
        messagebox.showinfo(
            "Where is KiCad?",
            "KiCad could not be started. Please locate the KiCad executable (kicad or "
            "kicad.exe); it is saved in kiub_gui.ini.",
            parent=self,
        )
        path = _browse_kicad_exe(parent=self)
        if not path:
            return False
        self._kicad_exe = path
        _save_kicad_exe(path)
        self._status_var.set(f"KiCad path saved: {path}")
        return True

    def _open_conversion_settings_dialog(self) -> None:
        """Open the consolidated Conversion Settings pop-up (Board
        Defaults / Geometry / Fallback tabs). Each tab's results still
        persist through their own separate function/ini section, exactly
        as when these were two separate dialogs -- only the dialog itself
        is consolidated.
        """
        dlg = _ConversionSettingsDialog(self, self._board_defaults, self._fine_tuning)
        self.wait_window(dlg)
        if dlg.saved:
            self._board_defaults.update(dlg.board_defaults_result)
            _save_board_defaults(self._board_defaults)
            self._fine_tuning.update(dlg.fine_tuning_result)
            _save_fine_tuning(self._fine_tuning)
            self._status_var.set("Conversion settings saved.")

    def _kicad_target(self) -> Path | None:
        """What Open in KiCad opens, as KIUC's: the last conversion's
        project, also when another input has been chosen since; else the
        selected input's project -- the design's, or the library board's
        for a library-only input -- from an earlier conversion (the output
        folder is fixed, so it is known before converting). None when there
        is none yet."""
        if self._last_pro_path and Path(self._last_pro_path).exists():
            return Path(self._last_pro_path)
        infile = self._infile_var.get().strip()
        if not infile:
            return None
        try:
            kind = self._input_kind(infile)
            plans = [KIUB.OutputPlan(KIUB.default_output_path(infile, lib), lib)
                     for lib in ((True,) if kind == KIUB.DDF_LIBRARY else (False, True))]
        except Exception:
            return None
        for plan in plans:
            if Path(plan.project).exists():
                return Path(plan.project)
        return None

    def _open_in_kicad(self) -> None:
        """Open the input's KiCad project: with the saved KiCad path if it
        still exists, else with the program the system associates with
        .kicad_pro files; only when both fail, ask for KiCad (saved), and
        again when that fails to start."""
        target = self._kicad_target()
        if target is None:
            messagebox.showinfo("Nothing to open yet",
                                "Convert the design first: Open in KiCad opens its KiCad project.",
                                parent=self)
            return
        if self._kicad_exe and Path(self._kicad_exe).is_file() and _launch(self._kicad_exe, target):
            return
        if _open_with_system(target):
            return
        while self._ask_kicad_exe():
            if _launch(self._kicad_exe, target):
                return
            messagebox.showerror("Could not start KiCad",
                                 f"{self._kicad_exe} could not be started.", parent=self)


def _launch(exe: str, target: Path) -> bool:
    """Start KiCad with target; whether it started."""
    try:
        subprocess.Popen([exe, str(target)])
        return True
    except OSError:
        return False


def _open_with_system(target: Path) -> bool:
    """Open target with the program the system associates with its type
    (KiCad, for a .kicad_pro, once KiCad is installed). Whether that
    worked: on Windows and macOS a missing association is an error; on
    Linux xdg-open reports one by its exit code. As KIUC's."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))       # type: ignore[attr-defined]  # Windows only
            return True
        cmd = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return True                         # still busy starting it
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = KiubApp()
    app.mainloop()
