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


def _load_kicad_exe() -> str:
    """Return the stored KiCad executable path, or '' if not set / invalid."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")
    path = cfg.get(_CONFIG_SECTION, _CONFIG_KEY, fallback="").strip()
    return path if path and Path(path).is_file() else ""


def _save_kicad_exe(path: str) -> None:
    """Persist the KiCad executable path to the config file."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")      # keep any existing keys
    if not cfg.has_section(_CONFIG_SECTION):
        cfg.add_section(_CONFIG_SECTION)
    cfg.set(_CONFIG_SECTION, _CONFIG_KEY, path)
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
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")
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
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")      # keep any existing keys
    if not cfg.has_section(_BOARD_DEFAULTS_SECTION):
        cfg.add_section(_BOARD_DEFAULTS_SECTION)
    for name, value in values.items():
        cfg.set(_BOARD_DEFAULTS_SECTION, name, repr(value))
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


class _BoardDefaultsDialog(tk.Toplevel):
    """Fine-tuning pop-up for the editable board defaults (kicad_pcb "setup"
    section + kicad_pro "rules" section). Fields are generated entirely from
    KIUB.BOARD_DEFAULTS_SPEC -- adding a new tunable there is all that's
    needed for it to appear here; no layout changes required. Mirrors
    KIUC's own _TuningDialog (kiuc_gui.py) so both tools share the same UX.

    Always opened on the main thread (button command), so no thread-safety
    concerns.
    """

    def __init__(self, parent: tk.Tk, current: dict) -> None:
        super().__init__(parent)
        self.title("Board defaults")
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False
        self.result: dict = {}

        self._specs = {name: (default, lo, hi, desc, target)
                       for name, default, lo, hi, desc, target in KIUB.BOARD_DEFAULTS_SPEC}
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(
            self,
            text="These values are written into the converted kicad_pcb's "
                 "(setup) section and/or the kicad_pro's design rules. "
                 "Changes apply to the next conversion and are saved to "
                 "kiub_gui.ini.",
            wraplength=480, justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))

        row = 1
        for name, default, lo, hi, desc, target in KIUB.BOARD_DEFAULTS_SPEC:
            ttk.Label(self, text=name, font=("Consolas", 9, "bold")).grid(
                row=row, column=0, sticky="nw", padx=(12, 6), pady=(6, 0))

            var = tk.StringVar(value=str(current.get(name, default)))
            self._vars[name] = var
            ttk.Entry(self, textvariable=var, width=10, font=("Consolas", 9)).grid(
                row=row, column=1, sticky="nw", pady=(6, 0))

            ttk.Label(self, text=f"(default {default}, {target})",
                     foreground="#888").grid(row=row, column=2, sticky="nw",
                                             padx=(6, 12), pady=(6, 0))
            row += 1
            ttk.Label(self, text=f"{desc} (suggested range {lo}\u2013{hi})",
                     wraplength=480, justify="left",
                     foreground="#555").grid(
                row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))
            row += 1

        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 0))
        row += 1

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(8, 12))
        ttk.Button(frm_btns, text="Reset to defaults",
                  command=self._on_reset).pack(side="left")
        ttk.Button(frm_btns, text="Cancel",
                  command=self._on_cancel).pack(side="right")
        ttk.Button(frm_btns, text="Save",
                  command=self._on_save).pack(side="right", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.grab_set()

    def _on_reset(self) -> None:
        for name, (default, _lo, _hi, _desc, _target) in self._specs.items():
            self._vars[name].set(str(default))

    def _on_save(self) -> None:
        values = {}
        for name, (default, lo, hi, _desc, _target) in self._specs.items():
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
            values[name] = v

        self.result = values
        self.saved = True
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()


# ---------------------------------------------------------------------------
# Fine-tuning config (persisted alongside the KiCad path / board defaults,
# same ini file). Kept in a SEPARATE dialog/section from board_defaults so
# sensitive DRC-adjacent settings aren't mixed in with generic ones.
# Mirrors KIUC's [tuning] section / kiuc.ini pattern (kiuc_gui.py).
# ---------------------------------------------------------------------------

_FINE_TUNING_SECTION = "fine_tuning"


def _load_fine_tuning() -> dict:
    """Load saved fine-tuning values from kiub_gui.ini. Any name not
    present in the file (fresh install, or a newly-added tunable) is simply
    left out, so the caller should overlay this onto KIUB.FINE_TUNING_SPEC's
    built-in defaults rather than assume every key is present."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")
    values = {}
    if cfg.has_section(_FINE_TUNING_SECTION):
        for name, _default, _lo, _hi, _desc, _category in KIUB.FINE_TUNING_SPEC:
            if cfg.has_option(_FINE_TUNING_SECTION, name):
                try:
                    values[name] = cfg.getfloat(_FINE_TUNING_SECTION, name)
                except ValueError:
                    pass   # corrupted entry; fall back to current default
    return values


def _save_fine_tuning(values: dict) -> None:
    """Persist fine-tuning values to the config file."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")      # keep any existing keys
    if not cfg.has_section(_FINE_TUNING_SECTION):
        cfg.add_section(_FINE_TUNING_SECTION)
    for name, value in values.items():
        cfg.set(_FINE_TUNING_SECTION, name, repr(value))
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


class _FineTuningDialog(tk.Toplevel):
    """Fine-tuning pop-up for geometry/style constants and cautious DRC-
    adjacent fallback defaults. Fields are generated entirely from
    KIUB.FINE_TUNING_SPEC -- adding a new tunable there is all that's needed
    for it to appear here; no layout changes required.

    Kept as a SEPARATE dialog from "Board defaults…" (which only holds true
    manufacturing clearances written straight into kicad_pcb/kicad_pro) so
    these more sensitive/empirical settings don't get mixed in with generic
    ones. Entries are grouped by their 'category' tag: 'geometry' (visual/
    rendering fit, safe to adjust freely) and 'clearance' (DRC-adjacent
    fallback values, alter cautiously). Mirrors KIUC's own _TuningDialog
    (kiuc_gui.py) almost exactly, including its category-grouping approach.

    Always opened on the main thread (button command), so no thread-safety
    concerns.
    """

    _SECTION_INTRO = {
        'geometry':  "These affect how converted geometry looks (text size, "
                     "line widths, outline snapping). Safe to adjust for "
                     "visual fit against KiCad's rendering -- they don't "
                     "affect manufacturability or DRC.",
        'clearance': "These are fallback copper clearances/widths used only "
                     "where the DDF doesn't specify a value of its own. "
                     "Alter cautiously -- values set too aggressively can "
                     "trigger DRC clearance violations elsewhere on the "
                     "board.",
    }
    _SECTION_TITLE = {
        'geometry':  'Geometry / visual fit',
        'clearance': 'Fallback clearances (DRC-adjacent -- alter cautiously)',
    }

    def __init__(self, parent: tk.Tk, current: dict) -> None:
        super().__init__(parent)
        self.title("Fine-tuning")
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False
        self.result: dict = {}

        self._specs = {name: (default, lo, hi, desc, category)
                       for name, default, lo, hi, desc, category in KIUB.FINE_TUNING_SPEC}
        self._vars: dict[str, tk.StringVar] = {}

        by_category: dict[str, list] = {}
        for entry in KIUB.FINE_TUNING_SPEC:
            by_category.setdefault(entry[5], []).append(entry)

        row = 0
        for category in ('geometry', 'clearance'):
            entries = by_category.get(category)
            if not entries:
                continue

            ttk.Label(self, text=self._SECTION_TITLE.get(category, category),
                     font=("Segoe UI", 10, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w",
                padx=12, pady=(12, 2))
            row += 1
            ttk.Label(self, text=self._SECTION_INTRO.get(category, ''),
                     wraplength=480, justify="left").grid(
                row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 8))
            row += 1

            for name, default, lo, hi, desc, _category in entries:
                ttk.Label(self, text=name, font=("Consolas", 9, "bold")).grid(
                    row=row, column=0, sticky="nw", padx=(12, 6), pady=(6, 0))

                var = tk.StringVar(value=str(current.get(name, default)))
                self._vars[name] = var
                ttk.Entry(self, textvariable=var, width=10, font=("Consolas", 9)).grid(
                    row=row, column=1, sticky="nw", pady=(6, 0))

                ttk.Label(self, text=f"(default {default}, range {lo}\u2013{hi})",
                         foreground="#888").grid(row=row, column=2, sticky="nw",
                                                 padx=(6, 12), pady=(6, 0))
                row += 1
                ttk.Label(self, text=desc, wraplength=480, justify="left",
                         foreground="#555").grid(
                    row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))
                row += 1

            ttk.Separator(self, orient="horizontal").grid(
                row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 0))
            row += 1

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(8, 12))
        ttk.Button(frm_btns, text="Reset to defaults",
                  command=self._on_reset).pack(side="left")
        ttk.Button(frm_btns, text="Cancel",
                  command=self._on_cancel).pack(side="right")
        ttk.Button(frm_btns, text="Save",
                  command=self._on_save).pack(side="right", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.grab_set()

    def _on_reset(self) -> None:
        for name, (default, _lo, _hi, _desc, _category) in self._specs.items():
            self._vars[name].set(str(default))

    def _on_save(self) -> None:
        values = {}
        for name, (default, lo, hi, _desc, _category) in self._specs.items():
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
            values[name] = v

        self.result = values
        self.saved = True
        self.destroy()

    def _on_cancel(self) -> None:
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
# Main application window
# ---------------------------------------------------------------------------

class KiubApp(tk.Tk):
    _POLL_INTERVAL_MS = 50       # how often (ms) the log area polls the queue

    _LABEL_FONT = ("Segoe UI", 10)
    _ENTRY_FONT = ("Segoe UI", 10)
    _LOG_FONT   = ("Consolas", 9)

    _DEFAULT_FONT = "KiCad Font"

    def __init__(self) -> None:
        super().__init__()
        self.title("KIUB  –  Ultiboard DDF → KiCad PCB Converter")
        self.resizable(True, True)
        self.minsize(760, 540)

        self._log_queue:    queue.Queue[str] = queue.Queue()
        self._running:      bool             = False
        self._out_dir_var:  tk.StringVar     = tk.StringVar()
        self._infile_var:   tk.StringVar     = tk.StringVar()
        self._outfile_var:  tk.StringVar     = tk.StringVar()
        self._font_var:     tk.StringVar     = tk.StringVar(value=self._DEFAULT_FONT)
        self._verbose_var:  tk.BooleanVar    = tk.BooleanVar(value=True)   # default ON
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

        # KiCad launcher state
        self._kicad_exe:    str = _load_kicad_exe()   # '' until confirmed valid
        self._last_pro_path: str = ""                 # set after successful conversion

        # Font list is built once after the Tk root exists (tkfont.families()
        # requires a live Tk instance).
        self._all_fonts:  list[str] = []
        self._mono_fonts: list[str] = []

        self._build_ui()
        self._load_fonts()          # populate combobox after window is ready

        # If no valid KiCad path is stored, ask the user now (non-blocking:
        # we do it after mainloop starts via after() so the window is visible).
        if not self._kicad_exe:
            self.after(200, self._ask_kicad_exe)

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
        self._refresh_font_list()

    def _refresh_font_list(self) -> None:
        """Update the combobox to show either all fonts or only monospaced ones."""
        fonts = self._mono_fonts if self._mono_var.get() else self._all_fonts
        self._font_combo["values"] = fonts

        # If the currently selected font is no longer in the filtered list,
        # clear to avoid showing a value that is not present in the dropdown.
        # But never clear "KiCad Font" — it is valid regardless of the list.
        current = self._font_var.get()
        if current != self._DEFAULT_FONT and current not in fonts:
            self._font_var.set("")

    def _use_default_font(self) -> None:
        """Reset the font field to the KiCad default and uncheck mono filter."""
        self._mono_var.set(False)
        self._refresh_font_list()
        self._font_var.set(self._DEFAULT_FONT)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # The grid uses 3 columns:
        #   col 0 – labels (fixed width)
        #   col 1 – main input widgets (stretches)
        #   col 2 – left-aligned checkboxes / extra buttons
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        # ── Input file ──────────────────────────────────────────────────────
        ttk.Label(outer, text="Input DDF file:", font=self._LABEL_FONT).grid(
            row=0, column=0, sticky=tk.W, pady=(0, 4))

        infile_frame = ttk.Frame(outer)
        infile_frame.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 4))
        infile_frame.columnconfigure(0, weight=1)

        ttk.Entry(infile_frame, textvariable=self._infile_var,
                  font=self._ENTRY_FONT).grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self._infile_var.trace_add("write", self._on_infile_changed)

        ttk.Button(infile_frame, text="Browse…",
                   command=self._browse_infile).grid(row=0, column=1)

        # ── Output folder ───────────────────────────────────────────────────
        ttk.Label(outer, text="Output folder:", font=self._LABEL_FONT).grid(
            row=1, column=0, sticky=tk.W, pady=(0, 4))

        outdir_frame = ttk.Frame(outer)
        outdir_frame.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=(0, 4))
        outdir_frame.columnconfigure(0, weight=1)

        ttk.Entry(outdir_frame, textvariable=self._out_dir_var,
                  font=self._ENTRY_FONT).grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self._out_dir_var.trace_add("write", lambda *_: self._refresh_outfile_path())

        ttk.Button(outdir_frame, text="Browse…",
                   command=self._browse_outdir).grid(row=0, column=1)

        # ── Output filename ─────────────────────────────────────────────────
        ttk.Label(outer, text="Output filename:", font=self._LABEL_FONT).grid(
            row=2, column=0, sticky=tk.W, pady=(0, 4))

        ttk.Entry(outer, textvariable=self._outfile_var,
                  font=self._ENTRY_FONT).grid(
            row=2, column=1, columnspan=2, sticky=tk.EW, pady=(0, 4))

        # ── Font row ─────────────────────────────────────────────────────────
        # Layout:
        #   col 0 : "Font:" label
        #   col 1 : [Combobox (stretches)] [Use KiCad Font button]
        #   col 2 : "Mono only" checkbox  ← left-aligned
        ttk.Label(outer, text="Font:", font=self._LABEL_FONT).grid(
            row=3, column=0, sticky=tk.W, pady=(0, 4))

        font_inner = ttk.Frame(outer)
        font_inner.grid(row=3, column=1, sticky=tk.EW, pady=(0, 4))
        font_inner.columnconfigure(0, weight=1)

        self._font_combo = ttk.Combobox(
            font_inner,
            textvariable=self._font_var,
            font=self._ENTRY_FONT,
            state="normal",        # allow free-typing as well as selection
        )
        self._font_combo.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))

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
        ).grid(row=3, column=2, sticky=tk.W, padx=(6, 0), pady=(0, 8))

        # ── Verbose checkbox – left-aligned in column 2, row 4 ─────────────
        ttk.Checkbutton(
            outer,
            text="Verbose output",
            variable=self._verbose_var,
        ).grid(row=4, column=2, sticky=tk.W, padx=(6, 0), pady=(0, 8))

        # ── Action buttons ───────────────────────────────────────────────────
        # Layout (left → right): ▶ Start Conversion | Open in KiCad | Clear Log | ⚙ Board defaults… | ⚙ Fine-tuning… | ⚙ KiCad Path…
        btn_frame = ttk.Frame(outer)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=(0, 8))

        self._start_btn = ttk.Button(
            btn_frame, text="▶  Start Conversion",
            command=self._start_conversion, width=22)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self._open_btn = ttk.Button(
            btn_frame, text="⎋  Open in KiCad",
            command=self._open_in_kicad, width=18,
            state=tk.DISABLED)          # enabled only after a successful conversion
        self._open_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="Clear Log",
                   command=self._clear_log, width=12).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="⚙  Board defaults…",
                   command=self._open_board_defaults_dialog, width=18).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="⚙  Fine-tuning…",
                   command=self._open_fine_tuning_dialog, width=16).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="⚙  KiCad Path…",
                   command=self._change_kicad_exe, width=16).pack(side=tk.LEFT)

        # ── Log area ─────────────────────────────────────────────────────────
        ttk.Label(outer, text="Conversion log:", font=self._LABEL_FONT).grid(
            row=6, column=0, columnspan=3, sticky=tk.W)

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
        self._log.grid(row=7, column=0, columnspan=3, sticky=tk.NSEW, pady=(4, 0))

        self._log.tag_config("info",    foreground="#9cdcfe")
        self._log.tag_config("success", foreground="#4ec9b0")
        self._log.tag_config("error",   foreground="#f44747")
        self._log.tag_config("warn",    foreground="#dcdcaa")
        self._log.tag_config("plain",   foreground="#d4d4d4")
        self._log.tag_config("skipped", foreground="#ff0000", background="#ffff00")

        # ── Status bar ───────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self._status_var,
                  relief=tk.SUNKEN, anchor=tk.W).grid(
            row=8, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))

    # -----------------------------------------------------------------------
    # File / folder dialogs
    # -----------------------------------------------------------------------

    def _browse_infile(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Ultiboard DDF file",
            filetypes=[("Ultiboard DDF", "*.ddf *.DDF"), ("All files", "*.*")],
        )
        if path:
            self._infile_var.set(str(Path(path)))   # normalise to OS separators

    def _browse_outdir(self) -> None:
        directory = filedialog.askdirectory(title="Select output folder")
        if directory:
            self._out_dir_var.set(str(Path(directory)))   # normalise to OS separators

    # -----------------------------------------------------------------------
    # Automatic output path derivation
    # -----------------------------------------------------------------------

    def _on_infile_changed(self, *_: Any) -> None:
        """When the input file changes, auto-fill the output folder and filename."""
        infile = self._infile_var.get().strip()
        if not infile:
            return
        if not self._out_dir_var.get():
            self._out_dir_var.set(str(Path(infile).parent))
        self._refresh_outfile_path()

    def _refresh_outfile_path(self) -> None:
        """Recompute the full output path from the current input path and output dir."""
        infile  = self._infile_var.get().strip()
        out_dir = self._out_dir_var.get().strip()
        if not infile:
            return
        stem    = Path(infile).stem
        out_dir = out_dir or str(Path(infile).parent)
        self._outfile_var.set(str(Path(out_dir) / f"{stem}.kicad_pcb"))

    # -----------------------------------------------------------------------
    # Validation and conversion
    # -----------------------------------------------------------------------

    def _build_args(self) -> argparse.Namespace | None:
        """Validate inputs and return an argparse.Namespace for Converter."""
        infile  = self._infile_var.get().strip()
        outfile = self._outfile_var.get().strip()
        # An empty font field means the user cleared it; fall back to default.
        font    = self._font_var.get().strip() or self._DEFAULT_FONT

        if not infile:
            messagebox.showerror("Missing input", "Please select a DDF input file.")
            return None
        if not infile.lower().endswith(".ddf"):
            infile += ".ddf"
        if not os.path.exists(infile):
            messagebox.showerror("File not found", f"Input file not found:\n{infile}")
            return None
        if not outfile:
            outfile = str(Path(infile).with_suffix(".kicad_pcb"))
        if not outfile.lower().endswith(".kicad_pcb"):
            outfile += ".kicad_pcb"

        return argparse.Namespace(
            infile=infile, outfile=outfile, font=font,
            verbose=self._verbose_var.get(),
            **self._board_defaults,
            **self._fine_tuning,
        )

    def _check_refdes_prescan(self, infile: str) -> bool:
        """Pre-scan the DDF for non-digit-ending reference designators and,
        if any are found, pop up a list and ask whether to continue or
        abort. Returns True to proceed, False to abort. Read-only: does not
        rename or modify anything (see kiub.scan_non_digit_refdes).
        """
        try:
            ddf_handle = KIUB.open_ddf(infile, verbose=False)
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

        sibling = KIUB.find_sibling_schematic(infile)
        msg = ("The following component reference designators do not end "
               "in a digit:\n\n  " + "\n  ".join(offending) + "\n\n")
        if sibling:
            msg += (f"A sibling schematic was found:\n  {sibling}\n\n"
                     "Run KIUC's Refdes Reannotate tool FIRST, before changing "
                     "any of these references -- it is what keeps the "
                     "schematic and PCB in sync. Renaming them directly (in "
                     "the PCB or the schematic alone) will break that "
                     "sync.\n\n")
        else:
            msg += ("No sibling schematic (.SCH/.kicad_sch) was found next "
                     "to this DDF.\n\n"
                     "KiCad's PCB editor accepts non-digit-ending references "
                     "without issue, so no action is needed if this board "
                     "has no schematic. If a schematic for this board exists "
                     "elsewhere, run KIUC's Refdes Reannotate tool first -- "
                     "renaming these independently of that schematic will "
                     "break their sync.\n\n")
        msg += "Continue with the conversion anyway?"

        return messagebox.askyesno("Non-digit-ending reference designators found", msg)

    def _start_conversion(self) -> None:
        if self._running:
            return
        self._clear_log()

        args = self._build_args()
        if args is None:
            return

        if not self._check_refdes_prescan(args.infile):
            self._status_var.set("Conversion cancelled.")
            return

        self._running = True
        self._start_btn.config(state=tk.DISABLED)
        self._status_var.set("Converting…")
        self._direct_log(f"Input:  {args.infile}\n", "info")
        self._direct_log(f"Output: {args.outfile}\n", "info")
        self._direct_log(f"Font:   {args.font}\n",   "info")
        self._direct_log("─" * 60 + "\n",             "plain")

        threading.Thread(
            target=self._run_conversion, args=(args,), daemon=True,
        ).start()

    def _run_conversion(self, args: argparse.Namespace) -> None:
        """
        Run Converter in a worker thread, capturing all stdout into the log.
        Also writes to <input_stem>_log.txt including the header lines shown
        in the GUI log window.
        On completion a sentinel string is placed on the queue so the main
        thread can re-enable the UI.
        """
        input_path    = Path(args.infile)
        log_file_path = input_path.with_name(f"{input_path.stem}_log.txt")

        with open(log_file_path, "w", encoding="utf-8") as f:
            # Write the header lines that _start_conversion already sent to the
            # GUI log widget directly (they bypass _QueueWriter, so we echo them
            # to the file here before redirecting stdout).
            f.write(f"Input:  {args.infile}\n")
            f.write(f"Output: {args.outfile}\n")
            f.write(f"Font:   {args.font}\n")
            f.write("─" * 60 + "\n")
            f.flush()

            writer      = _QueueWriter(self._log_queue, f)
            orig_stdout = sys.stdout
            sys.stdout  = writer

            success  = False
            pro_path = ""
            try:
                ddf_handle = KIUB.open_ddf(args.infile, verbose=args.verbose, args=args)
                try:
                    with open(args.outfile, "w", encoding="utf-8", errors="replace") as kicad:
                        converter = Converter(ddf_handle, kicad, args)
                        converter.convert()
                finally:
                    ddf_handle.close()
                pro_path = str(Path(args.outfile).with_suffix(".kicad_pro"))
                converter.write_kicad_pro(pro_path)
                success = True
            except Exception:
                self._log_queue.put("\n" + traceback.format_exc() + "\n")
            finally:
                sys.stdout = orig_stdout

        self._log_queue.put("\x00DONE\x00" + ("OK:" + args.outfile + "\x01" + pro_path
                                               if success else "FAIL"))

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
            self._open_btn.config(state=tk.NORMAL)
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

    def _ask_kicad_exe(self) -> None:
        """Prompt the user to locate KiCad on first run (or when path is missing)."""
        messagebox.showinfo(
            "KiCad location required",
            "Please locate the KiCad executable so the 'Open in KiCad' button works.\n\n"
            "This is saved in kiub_gui.ini and only asked once.",
            parent=self,
        )
        self._change_kicad_exe()

    def _change_kicad_exe(self) -> None:
        """Let the user browse for the KiCad executable and save it."""
        path = _browse_kicad_exe(parent=self)
        if path:
            self._kicad_exe = path
            _save_kicad_exe(path)
            self._status_var.set(f"KiCad path saved: {path}")

    def _open_board_defaults_dialog(self) -> None:
        """Open the Board defaults pop-up."""
        dlg = _BoardDefaultsDialog(self, self._board_defaults)
        self.wait_window(dlg)
        if dlg.saved:
            self._board_defaults.update(dlg.result)
            _save_board_defaults(self._board_defaults)
            self._status_var.set("Board defaults saved.")

    def _open_fine_tuning_dialog(self) -> None:
        """Open the separate Fine-tuning pop-up."""
        dlg = _FineTuningDialog(self, self._fine_tuning)
        self.wait_window(dlg)
        if dlg.saved:
            self._fine_tuning.update(dlg.result)
            _save_fine_tuning(self._fine_tuning)
            self._status_var.set("Fine-tuning values saved.")

    def _open_in_kicad(self) -> None:
        """Launch KiCad with the last converted .kicad_pro file."""
        if not self._last_pro_path:
            return

        # Re-validate the stored path in case the user changed it since startup.
        if not self._kicad_exe or not Path(self._kicad_exe).is_file():
            self._ask_kicad_exe()
            if not self._kicad_exe:
                return

        try:
            subprocess.Popen([self._kicad_exe, self._last_pro_path])
        except OSError as exc:
            messagebox.showerror(
                "Could not launch KiCad",
                f"Failed to start KiCad:\n{exc}\n\n"
                "Use ⚙ KiCad Path… to set the correct executable.",
                parent=self,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = KiubApp()
    app.mainloop()
