#!/usr/bin/env python3
# OrcaStrator, a graphical post-processor runner for multi-toolhead 3D printers
# Copyright (C) 2026  Topha_GM8
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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
OrcaStrator -- the post-processing pipeline runner for OrcaSlicer
=========================================================================
This is the ONE script you register in OrcaSlicer's Post-processing
Scripts field. It runs every processor script found in
POST_PROCESSORS_DIR (default: a "post_processors" folder next to this
file) against the exported g-code, in order, and reports a summary.

Its own settings (progress window position, error-handling behavior,
color theme) live in orcastrator.json, editable via
config_editor.py same as every processor's config.

To add a new processor: drop a .py file into post_processors/. It will be
picked up automatically on the next export -- no config changes needed,
as long as it follows the convention below.

Processor script convention
----------------------------
Each processor is a normal standalone script, run as:
    <python> post_processors/some_script.py <gcode_path>
It should read/modify the file at <gcode_path> in place and exit 0 on
success, non-zero on failure (an uncaught exception is fine too -- Python
already exits non-zero for you). That's the whole contract.

OPTIONAL: any processor can also print stdout lines of two kinds:

    SVG_PAYLOAD:{...compact json...}
(one payload per line -- print it more than once for multiple SVGs). Shape:
    {"title": "...", "canvas": {"x_max":.., "y_max":.., "pad":.., "max_size":..},
     "shapes": [{"type": "polygon"/"crosshair"/"marker"/"path"/"text", ...}, ...]}

    NOTICE:{"level": "info"|"warning"|"abort", "title": "...", "message": "..."}
(again, one per line, print as many as you need). "abort" means "refuse
to print this file" -- the Klipper-side macro raises an error and stops
the print from starting when it sees one, with no dock-check-specific (or
any other processor-specific) knowledge required on the printer side.
"warning"/"info" are just shown, non-fatal -- unless the notice also
carries a "display": false key, in which case the printer-side macro
still embeds it (nothing about the notice is lost) but doesn't print it
to the console. That key is entirely optional and processor-owned --
see post_processors/helpers/notice.py for the shared helper most
built-in processors use to set it from their own "notice": {"display":
...} config, so a busy console can quiet a processor's routine status
notices without losing them from the file. "abort" always ignores this
key regardless of its value -- see OrcaStrator_render.cfg's own comment
on why.

This OrcaStrator captures every SVG_PAYLOAD/NOTICE line from every
processor's stdout and embeds each (tagged with which processor produced
it) into the ORCASTRATOR_LOG block. The printer-side macro renders
whatever it finds there -- adding a new processor that wants to show a
visualization, or that needs to be able to block a print for its own
reasons, needs ZERO changes on the printer side. dock_collision_guard.py
is just the first processor that uses this; it is not special-cased
anywhere in this file or in the printer-side macro.

SAFETY DEFAULT: if a processor exits non-zero and didn't emit its own
"abort" notice explaining why, this OrcaStrator synthesizes one
automatically (see AUTO_ABORT_ON_UNEXPLAINED_FAILURE below). An
unexplained failure is not something that should be silently logged and
otherwise ignored.

Execution order
---------------
1. Anything listed in EXPLICIT_ORDER below, in that exact order.
2. Everything else found in post_processors/*.py, alphabetically.
3. Anything listed in EXPLICIT_ORDER_LAST below, in that exact order.

EXPLICIT_ORDER exists because some processors are order-dependent even
though nothing about their filenames signals that. Concretely:
restore_pos_fix.py must run BEFORE disable_unused_tool_temps.py --
disable_unused_tool_temps.py's idle-cooldown/reactivation-preheat feature
shares a naive-time-estimation model (post_processors/helpers/time_estimator.py)
with insert_missing_tool_preheat.py, and that model resyncs its X/Y/Z
position tracking from the "T3 X=.. Y=.. Z=.." annotations restore_pos_fix.py
adds to each toolchange line. Run disable_unused_tool_temps.py before
restore_pos_fix.py and it still works (its own tool-change regex matches
both bare and parameterized T-lines), just with less accurate lead-time
placement -- dock-move distance gets billed into the naive estimate instead
of resynced away.

insert_missing_tool_preheat.py has the same restore_pos_fix.py dependency
for the same reason, but isn't listed in EXPLICIT_ORDER below -- it doesn't
need to be. Anything not in EXPLICIT_ORDER always runs after everything
that is (see discover_processors()'s `ordered + remainder`), so as long as
restore_pos_fix.py stays pinned in EXPLICIT_ORDER, insert_missing_tool_preheat.py
is guaranteed to run after it regardless of alphabetical position. Don't
add insert_missing_tool_preheat.py to EXPLICIT_ORDER ahead of
restore_pos_fix.py without keeping that in mind.

EXPLICIT_ORDER_LAST is the mirror image, for a processor that needs to run
AFTER everything else instead -- a read-only reporting/summary step that
wants the final state of the g-code, say, once every other processor has
had its turn. Without it, pinning something last meant listing every OTHER
processor ahead of it in EXPLICIT_ORDER, which silently goes stale the
moment a new processor is added and forgotten there. Being pinned in
EXPLICIT_ORDER_LAST instead means "runs after everything -- including any
processor that shows up later and was never explicitly listed anywhere",
with no maintenance burden as the set of processors grows. If a name
somehow ends up in both lists, EXPLICIT_ORDER wins (see discover_processors())
and a note is printed -- that combination almost certainly isn't what was
intended.

Interpreter
-----------
Every processor is invoked with sys.executable -- i.e. whatever Python is
running THIS script. Point OrcaSlicer at one Python and every processor
automatically uses that same interpreter. No more python3-vs-py-launcher
mismatches between scripts.

Library files (shared helper modules, *.json configs) do NOT belong
directly in post_processors/ -- that folder's top level is auto-scanned
for *.py and treated as "runnable processors" (the scan is NOT
recursive, so subfolders are invisible to it). Keep shared libraries in
post_processors/helpers/, or one level up next to this OrcaStrator --
either works. See post_processors/helpers/poly_tools.py for an example.
"""

import json
import re
import subprocess
import sys
import time
import pathlib

# ---------------------------------------------------------------------------
# Optional progress UI
# ---------------------------------------------------------------------------
# Entirely best-effort: tkinter ships with the standard Python installer on
# Windows/Mac, no extra pip install needed, but this must never be able to
# break the actual postprocessing pipeline. If tkinter isn't importable (or
# anything about creating the window fails) everything below silently
# becomes a no-op and OrcaStrator behaves exactly as before, just
# without the window.
#
# This only replaces the ugly flashing terminal if you ALSO point OrcaSlicer's
# Post-processing Scripts field at pythonw.exe instead of python.exe (or a
# .bat wrapping python.exe). python.exe/cmd.exe are "console subsystem"
# executables -- Windows always gives them a console window, independent of
# anything this script does. pythonw.exe is the GUI-subsystem twin that
# never gets one, and it works completely transparently with how OrcaSlicer
# captures this script's stdout/stderr (that happens via OS pipes, not
# the console). With pythonw.exe, this tkinter window becomes the only
# visible UI.
#
# Any SVG_PAYLOAD collected from a processor's stdout also gets rendered
# directly onto this window (see _TkProgressUI.show_svgs/_draw_payload),
# generically -- OrcaStrator has no special knowledge of which
# processor produced a given payload, same as everything else here. If
# at least one payload was drawn, the window stays open with a button
# instead of auto-dismissing, so there's actually time to look at it.
# That button reads "Continue" if everything succeeded, or "Close" if
# there were failures -- same button/command either way, just different
# label depending on outcome.
#
# On a fully successful run (never on a failed one), auto_close in
# orcastrator.json can optionally close that window on its own after a
# configurable number of seconds instead of waiting on the Continue
# click -- off by default. See AUTO_CLOSE_ENABLED/AUTO_CLOSE_SECONDS and
# _TkProgressUI._start_auto_close_countdown() below.

SELF_DIR = pathlib.Path(__file__).resolve().parent


def _load_module(name, path):
    """
    Loads a plain-script module by file path (not a package import) --
    same convention config_editor.pyw uses for gui/_plugin_support.py.
    Kept local here (not shared) since orcastrator.py only ever needs
    to load one such module (gui/_window_anchor.py), so pulling in a
    whole separate shared-helpers file for one function isn't worth it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEFAULT_ORCASTRATOR_CFG = {
    "show_progress_ui": True,
    "window": {"position": "bottom-right", "margin": 40, "remember_position": False},
    "on_error": {"stop_on_error": False, "auto_abort_on_unexplained_failure": True},
    "denylist": [],
    "explicit_order": ["restore_pos_fix.py", "disable_unused_tool_temps.py"],
    "explicit_order_last": [],
    "auto_close": {"enabled": False, "seconds": 5},
    "debug": {"dir": ""},
    "sounds": {
        "on_error": {"enabled": False, "file": "error"},
        "on_success": {"enabled": False, "file": "success"},
    },
    "theme": {
        "bg": "#2b2b2b", "panel_bg": "#1e1e1e", "border": "#3f3f3f", "fg": "#e6e6e6",
        "fg_dim": "#9a9a9a", "accent": "#00A886", "accent_hover": "#1DC2A4", "accent_fg": "#ffffff",
        "titlebar": "#2b2b2b", "titlebar_fg": "#e6e6e6",
    },
}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_WINDOW_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}


def load_orcastrator_config() -> dict:
    """
    Loads configs/orcastrator.json, the folder next to this script.
    Every field is validated independently against
    DEFAULT_ORCASTRATOR_CFG -- an invalid or missing individual value
    falls back to its own default rather than discarding the whole
    file, and the file being missing/unreadable/malformed falls back to
    the full defaults. This can never be allowed to take down an actual
    export, so nothing here raises.
    """
    cfg = json.loads(json.dumps(DEFAULT_ORCASTRATOR_CFG))  # cheap deep copy
    raw = {}
    try:
        raw = json.loads((SELF_DIR / "configs" / "orcastrator.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    if not isinstance(raw, dict):
        raw = {}

    if isinstance(raw.get("show_progress_ui"), bool):
        cfg["show_progress_ui"] = raw["show_progress_ui"]

    w = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    pos = w.get("position")
    if pos in _WINDOW_POSITIONS:
        cfg["window"]["position"] = pos
    elif isinstance(pos, (list, tuple)) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos):
        cfg["window"]["position"] = tuple(pos)  # exact (x, y) -- advanced, JSON-only, not GUI-editable
    val = w.get("margin")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and 0 <= val <= 1000:
        cfg["window"]["margin"] = val
    if isinstance(w.get("remember_position"), bool):
        cfg["window"]["remember_position"] = w["remember_position"]

    oe = raw.get("on_error") if isinstance(raw.get("on_error"), dict) else {}
    for key in ("stop_on_error", "auto_abort_on_unexplained_failure"):
        if isinstance(oe.get(key), bool):
            cfg["on_error"][key] = oe[key]

    # Same tolerance as everything else here: a non-list, or a list with
    # some non-string entries mixed in, doesn't discard the whole
    # setting -- just keeps whatever of it is usable (or falls back to
    # the default if none of it is). Entries that ARE strings but don't
    # match an actual post_processors/*.py filename are deliberately
    # NOT filtered out here -- that check happens later, every run, in
    # discover_processors(), which already logs and ignores anything
    # unmatched. Filtering here too would just be the same check done
    # twice for no benefit.
    dl = raw.get("denylist")
    if isinstance(dl, list):
        cfg["denylist"] = [n for n in dl if isinstance(n, str)]

    eo = raw.get("explicit_order")
    if isinstance(eo, list):
        cfg["explicit_order"] = [n for n in eo if isinstance(n, str)]

    eol = raw.get("explicit_order_last")
    if isinstance(eol, list):
        cfg["explicit_order_last"] = [n for n in eol if isinstance(n, str)]

    ac = raw.get("auto_close") if isinstance(raw.get("auto_close"), dict) else {}
    if isinstance(ac.get("enabled"), bool):
        cfg["auto_close"]["enabled"] = ac["enabled"]
    secs = ac.get("seconds")
    if isinstance(secs, (int, float)) and not isinstance(secs, bool) and 1 <= secs <= 3600:
        cfg["auto_close"]["seconds"] = secs

    dbg = raw.get("debug") if isinstance(raw.get("debug"), dict) else {}
    if isinstance(dbg.get("dir"), str):
        cfg["debug"]["dir"] = dbg["dir"]

    snd = raw.get("sounds") if isinstance(raw.get("sounds"), dict) else {}
    for key in ("on_error", "on_success"):
        entry = snd.get(key) if isinstance(snd.get(key), dict) else {}
        if isinstance(entry.get("enabled"), bool):
            cfg["sounds"][key]["enabled"] = entry["enabled"]
        if isinstance(entry.get("file"), str):
            cfg["sounds"][key]["file"] = entry["file"]

    th = raw.get("theme") if isinstance(raw.get("theme"), dict) else {}
    for key in cfg["theme"]:
        val = th.get(key)
        if isinstance(val, str) and _HEX_COLOR_RE.match(val):
            cfg["theme"][key] = val

    return cfg


_CFG = load_orcastrator_config()

SHOW_PROGRESS_UI = _CFG["show_progress_ui"]
# One of "top-left", "top-right", "bottom-left", "bottom-right", "center",
# or an exact (x, y) pixel tuple for the window's top-left corner (JSON-only).
WINDOW_POSITION = _CFG["window"]["position"]
WINDOW_MARGIN = _CFG["window"]["margin"]  # pixel gap from screen edges, only used for corner presets
# When true, WINDOW_POSITION/WINDOW_MARGIN above are only ever used as
# the fallback -- see gui/_window_anchor.py's restore_geometry() and
# _TkProgressUI.__init__ -- for a first run, or whenever the last
# saved position turns out to be stale (monitor disconnected,
# resolution changed).
REMEMBER_POSITION = _CFG["window"]["remember_position"]

# Auto-close the progress window this many seconds after a fully
# successful run (no failures), even if an SVG preview stayed the
# window open for a look -- see _TkProgressUI.finish()/
# _start_auto_close_countdown(). NEVER applies to a failed run -- that
# always needs a manual Close, regardless of this setting. Off by
# default: someone reading an SVG preview shouldn't have the window
# vanish under them without opting in first.
AUTO_CLOSE_ENABLED = _CFG["auto_close"]["enabled"]
AUTO_CLOSE_SECONDS = _CFG["auto_close"]["seconds"]

# Completion sounds. "file" is a stem, not a filename -- see
# _resolve_sound_path() below for how that stem gets matched against
# whatever's actually sitting in assets/, extension and all. Off by
# default (both), so upgrading never suddenly starts making noise.
SOUND_ON_ERROR_ENABLED = _CFG["sounds"]["on_error"]["enabled"]
SOUND_ON_ERROR_FILE = _CFG["sounds"]["on_error"]["file"]
SOUND_ON_SUCCESS_ENABLED = _CFG["sounds"]["on_success"]["enabled"]
SOUND_ON_SUCCESS_FILE = _CFG["sounds"]["on_success"]["file"]

# Central debug-dump directory. NOT read by orcastrator.py itself -- every
# opted-in processor reads configs/orcastrator.json for this directly (see
# helpers/debug_dump.py._central_debug_dir(), which deliberately avoids
# importing this module -- see that function's docstring for why). Exposed
# here too only so it's validated/defaulted the same way as everything
# else in this file and easy to find from this side as well.
DEBUG_DIR = _CFG["debug"]["dir"]

# ---------------------------------------------------------------------------
# OrcaSlicer-ish skin: dark neutral grays + the teal accent OrcaSlicer itself
# uses for active tabs/buttons/highlights. Purely cosmetic -- none of this
# affects behavior, only the look of the progress window and the SVG panel
# drawn onto it.
# ---------------------------------------------------------------------------
ORCA_BG = _CFG["theme"]["bg"]                  # window/frame background
ORCA_PANEL_BG = _CFG["theme"]["panel_bg"]      # recessed panels: log text box, SVG canvases
ORCA_BORDER = _CFG["theme"]["border"]          # subtle borders/separators
ORCA_FG = _CFG["theme"]["fg"]                  # primary text
ORCA_FG_DIM = _CFG["theme"]["fg_dim"]          # secondary/disabled text
ORCA_ACCENT = _CFG["theme"]["accent"]
ORCA_ACCENT_HOVER = _CFG["theme"]["accent_hover"]
ORCA_ACCENT_FG = _CFG["theme"]["accent_fg"]    # text/icons drawn on top of the accent color
ORCA_TITLEBAR = _CFG["theme"]["titlebar"]      # OS titlebar background (Windows only, see apply_dark_titlebar)
ORCA_TITLEBAR_FG = _CFG["theme"]["titlebar_fg"]  # OS titlebar text/icons


class _NullProgressUI:
    """No-op fallback -- used whenever the real UI can't be created."""
    def update(self, name, status, ms, notices=None):
        pass

    def show_svgs(self, svg_entries):
        return False

    def finish(self, any_failed, has_svgs=False):
        pass


def _color_to_tk(color_str: str):
    """
    Tk canvas fill/outline options don't understand 'rgba(r,g,b,a)'
    strings (classic Tk has no real alpha compositing), so this pulls
    the RGB out as an opaque hex color and maps the alpha onto one of
    Tk's built-in stipple bitmaps (a dither pattern) as a rough visual
    stand-in for transparency. Plain hex/named colors pass through
    unchanged with no stipple. Best-effort: anything unparseable falls
    back to a neutral gray so a bad payload can't crash the window.

    Alpha ~0 (e.g. the printable-area boundary's "fill: none, outline
    only" rgba(0,0,0,0)) returns "" for the color -- stippling can only
    approximate partial transparency, not true invisibility, so without
    this a "transparent" fill would render as a solid stippled black
    rectangle instead of nothing. Callers should treat "" as "don't fill
    this at all" (Tk's own convention for an empty fill/outline).
    """
    if not color_str.startswith("rgba"):
        return color_str, ""
    try:
        nums = color_str[color_str.index("(") + 1: color_str.index(")")].split(",")
        r, g, b = (int(float(n)) for n in nums[:3])
        a = float(nums[3]) if len(nums) > 3 else 1.0
    except Exception:
        return "#888888", ""
    if a <= 0.02:
        return "", ""
    hex_color = f"#{r:02x}{g:02x}{b:02x}"
    if a >= 0.75:
        stipple = ""
    elif a >= 0.5:
        stipple = "gray75"
    elif a >= 0.3:
        stipple = "gray50"
    else:
        stipple = "gray25"
    return hex_color, stipple


def _usable_screen_height(probe) -> int:
    """
    Screen height actually available for placing/sizing a window,
    excluding the Windows taskbar (or equivalent reserved chrome).
    `probe`'s own winfo_screenheight() reports the monitor's full
    pixel height, taskbar included -- a window sized/positioned off
    that raw figure can end up with its bottom edge (e.g. the tail
    end of the SVG panel's scrollbar, or the Continue button) hidden
    behind the taskbar until the window is nudged or resized by hand,
    which is exactly the "have to resize to see the rest of it"
    symptom this exists to avoid.

    SPI_GETWORKAREA is the actual Win32 answer for this and is used
    whenever available; anywhere it isn't (non-Windows, or the call
    fails for any reason) falls back to knocking a conservative slice
    off the raw screen height instead.
    """
    raw = probe.winfo_screenheight()
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                work_h = rect.bottom - rect.top
                if work_h > 0:
                    return work_h
        except Exception:
            pass
    return raw - 60  # conservative stand-in for taskbar/menubar/dock chrome elsewhere


def _colorref(hex_color: str) -> int:
    """'#rrggbb' -> Win32 COLORREF (0x00BBGGRR -- byte order reversed
    from how the hex string reads)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_dark_titlebar(win, caption_hex=None, text_hex=None):
    """
    Best-effort Windows-only theming of `win`'s OS titlebar/chrome to
    match the app's own dark theme instead of the stock white one --
    every visible window in the app (this progress window, and every
    window/dialog in config_editor.pyw) calls this right after
    creation. A complete no-op (silently) on anything but Windows, on
    a `win` that isn't realized yet (winfo_id() needs an actual HWND),
    or on a Windows version too old for the attribute in question --
    same "missing capability just means it looks like it always did"
    tolerance _usable_screen_height and _set_app_icon already have,
    just for titlebar color instead of screen geometry/icon.

    DWMWA_USE_IMMERSIVE_DARK_MODE (dark titlebar, no custom color --
    the correct dark GRAY Windows itself uses for other dark-mode
    apps) works from Windows 10 1809 on, so it's applied unconditionally
    as the baseline. DWMWA_CAPTION_COLOR/DWMWA_TEXT_COLOR (Windows 11
    22000+) go a step further and recolor the bar to the app's own
    palette when both are given -- callers on Windows 10 simply get
    the baseline dark bar since those two calls fail harmlessly there.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwmapi = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                                      ctypes.byref(value), ctypes.sizeof(value))
        if caption_hex:
            DWMWA_CAPTION_COLOR = 35
            color = ctypes.c_int(_colorref(caption_hex))
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR,
                                          ctypes.byref(color), ctypes.sizeof(color))
        if text_hex:
            DWMWA_TEXT_COLOR = 36
            color = ctypes.c_int(_colorref(text_hex))
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR,
                                          ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass


def _resolve_sound_path(stem: str):
    """
    Turns a config "file" value (e.g. "error") into an actual file in
    assets/, whatever format it happens to be saved as -- assets/
    error.wav, assets/error.mp3, assets/error.ogg, etc. all match the
    same "error" setting. Also accepts the stem WITH an extension
    already on it (e.g. "error.wav") for an exact match, so either
    style works.

    Returns None (never raises) if `stem` is blank or nothing in
    assets/ matches -- same "a missing asset can't take down an
    export" tolerance as everything else best-effort in this file.
    """
    stem = (stem or "").strip()
    if not stem:
        return None
    assets_dir = SELF_DIR / "assets"
    exact = assets_dir / stem
    if exact.is_file():
        return exact
    matches = sorted(assets_dir.glob(f"{stem}.*"))
    return matches[0] if matches else None


def _play_sound(path):
    """
    Best-effort, asynchronous (never blocks the pipeline or the
    progress window), never raises. On Windows this deliberately
    doesn't specify an MCI "type" -- letting Windows infer the player
    from the file's own extension is what makes this generic across
    formats (wav, mp3, anything else with a registered MCI/codec
    association) instead of hardcoding one. winsound (stdlib) would be
    simpler but only ever plays WAV. Elsewhere, falls back to whatever
    common command-line player the OS is likely to have; best-effort
    only -- this app is primarily developed/tested on Windows (see
    apply_dark_titlebar/_usable_screen_height above for the same
    Windows-first, graceful-elsewhere pattern).
    """
    if path is None:
        return
    try:
        if sys.platform.startswith("win"):
            import ctypes
            winmm = ctypes.windll.winmm
            winmm.mciSendStringW(f'open "{path}" alias orcastrator_sound', None, 0, None)
            winmm.mciSendStringW("play orcastrator_sound", None, 0, None)
        else:
            import subprocess
            player = "afplay" if sys.platform == "darwin" else "paplay"
            subprocess.Popen([player, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class _TkProgressUI:
    @staticmethod
    def _position_for(w, h):
        """
        Computes the (x, y) top-left corner for a WxH window per
        WINDOW_POSITION. Used for the window's initial placement in
        __init__, and as the fallback _finalize_svg_size() reaches for
        if gui/_window_anchor.py can't be loaded -- the SVG-panel
        resize itself now grows from the window's current position via
        that module instead of recomputing from WINDOW_POSITION every
        time (see _finalize_svg_size for why).
        """
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        sw = probe.winfo_screenwidth()
        sh = _usable_screen_height(probe)
        probe.destroy()

        pos = WINDOW_POSITION
        if isinstance(pos, (tuple, list)) and len(pos) == 2:
            x, y = pos
        elif pos == "top-left":
            x, y = WINDOW_MARGIN, WINDOW_MARGIN
        elif pos == "top-right":
            x, y = sw - w - WINDOW_MARGIN, WINDOW_MARGIN
        elif pos == "bottom-left":
            x, y = WINDOW_MARGIN, sh - h - WINDOW_MARGIN
        elif pos == "bottom-right":
            x, y = sw - w - WINDOW_MARGIN, sh - h - WINDOW_MARGIN
        elif pos == "center":
            x, y = (sw - w) // 2, (sh - h) // 2
        else:
            x, y = WINDOW_MARGIN, WINDOW_MARGIN  # unrecognized value, fall back to a safe corner
        return int(x), int(y)

    def __init__(self, processor_names):
        import tkinter as tk
        from tkinter import ttk
        self._tk = tk
        self._ttk = ttk

        self.root = tk.Tk()
        self.root.title("OrcaStrator")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.configure(bg=ORCA_BG)
        self._set_app_icon()
        apply_dark_titlebar(self.root, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)

        # ttk's native themes ("vista"/"aqua"/etc) mostly ignore custom
        # colors -- "clam" is a built-in ttk theme available on every
        # platform that actually honors style overrides, which is what
        # lets the progress bar and scrollbar pick up the accent color
        # below instead of staying whatever gray the OS theme prefers.
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Orca.Horizontal.TProgressbar",
            troughcolor=ORCA_PANEL_BG, background=ORCA_ACCENT,
            bordercolor=ORCA_BG, lightcolor=ORCA_ACCENT, darkcolor=ORCA_ACCENT,
        )
        style.configure(
            "Orca.Vertical.TScrollbar",
            troughcolor=ORCA_BG, background=ORCA_BORDER,
            bordercolor=ORCA_BG, arrowcolor=ORCA_FG, relief="flat",
        )
        style.map("Orca.Vertical.TScrollbar", background=[("active", ORCA_ACCENT)])
        self._style = style

        header = tk.Label(self.root, text="Running post-processors...", font=("Segoe UI", 11, "bold"),
                           bg=ORCA_BG, fg=ORCA_FG)
        header.pack(anchor="w", padx=12, pady=(10, 4))

        # Thin accent rule under the header -- a lightweight nod to the
        # teal underline OrcaSlicer uses on its active tab, without
        # trying to fake actual tabs for a window that doesn't have any.
        tk.Frame(self.root, bg=ORCA_ACCENT, height=2).pack(fill="x", padx=12, pady=(0, 8))

        # No explicit length -- fill="x" stretches it to whatever width
        # the window ends up with (see the auto-sizing pass below),
        # instead of pinning it to a config-driven pixel width.
        self.progress = ttk.Progressbar(self.root, maximum=max(len(processor_names), 1),
                                          style="Orca.Horizontal.TProgressbar")
        self.progress.pack(padx=12, pady=(0, 8), fill="x")

        text_wrap = tk.Frame(self.root, bg=ORCA_BG)
        text_wrap.pack(padx=12, pady=(0, 8), fill="both", expand=True)
        self._text_wrap = text_wrap

        self.text = tk.Text(text_wrap, height=12, width=54, state="disabled",
                             bg=ORCA_PANEL_BG, fg=ORCA_FG, font=("Consolas", 9), borderwidth=0,
                             highlightthickness=1, highlightbackground=ORCA_BORDER, highlightcolor=ORCA_BORDER,
                             insertbackground=ORCA_FG)
        self._text_scrollbar = ttk.Scrollbar(text_wrap, orient="vertical", command=self.text.yview,
                                               style="Orca.Vertical.TScrollbar")

        def _on_text_yscroll(first, last):
            # Text's yscrollcommand fires on every content change AND
            # every view change, not just user scrolling -- so this
            # doubles as the "is there anything to scroll" check for
            # free, same intent as the SVG panel's
            # _update_svg_scrollbar_visibility but Text already reports
            # its own fill fraction, no bbox math needed. first==0.0
            # and last==1.0 together mean the whole log already fits,
            # same as the mousewheel handler further down effectively
            # no-ops once that's true.
            self._text_scrollbar.set(first, last)
            is_mapped = self._text_scrollbar.winfo_ismapped()
            if float(first) <= 0.0 and float(last) >= 1.0:
                if is_mapped:
                    self._text_scrollbar.pack_forget()
            elif not is_mapped:
                self._text_scrollbar.pack(side="right", fill="y")

        self.text.configure(yscrollcommand=_on_text_yscroll)
        # Scrollbar isn't packed here -- _on_text_yscroll above decides
        # that the first time it fires (append() below always inserts
        # at least once before anything's visible), same "start hidden,
        # only appear once actually needed" approach as the SVG panel.
        self.text.pack(side="left", fill="both", expand=True)

        self.close_btn = tk.Button(
            self.root, text="Close", command=self._on_close_click, state="disabled",
            bg=ORCA_BORDER, fg=ORCA_FG_DIM, activebackground=ORCA_ACCENT_HOVER,
            activeforeground=ORCA_ACCENT_FG, disabledforeground=ORCA_FG_DIM,
            relief="flat", borderwidth=0, padx=18, pady=5, font=("Segoe UI", 9, "bold"),
            cursor="arrow", highlightthickness=0,
        )
        self.close_btn.pack(pady=(0, 10))

        self._svg_inner = None
        self._svg_wrap = None
        self._svg_scroll_canvas = None
        self._svg_scrollbar = None

        # Auto-size to this window's own natural content (mainly driven
        # by the Text widget's character width/height above) instead of
        # a config-driven pixel size, then position it per
        # WINDOW_POSITION -- same idea as config_editor.py's
        # _autosize_to_scroll_content, just for width too since there's
        # no scrollable area here yet to size around. Stashed on self so
        # _finalize_svg_size() and _draw_payload() have a sane base
        # width/height to fall back to once the SVG panel is involved.
        self.root.update_idletasks()
        # x2 here on purpose -- the natural request width (driven by the
        # 54-char-wide Text log box) was cramped enough to wrap words
        # mid-line in the log. self._base_width is the single source
        # every other width decision derives from (initial geometry,
        # _finalize_svg_size's final geometry, and _draw_payload's SVG
        # canvas cap via self._base_width - 40), so doubling it here
        # once is all that's needed to widen the whole window -- log
        # box included, since it's packed with fill="x" and stretches
        # to match.
        self._base_width = self.root.winfo_reqwidth() * 2
        self._base_height = self.root.winfo_reqheight()

        # REMEMBER_POSITION only ever restores x/y, never width/height
        # -- this window's size is always automatic (driven by this
        # run's own content, orcastrator.json's own comment already
        # says as much), only WHERE it opens is a standing preference.
        # Falls back to the ordinary WINDOW_POSITION corner/center
        # placement whenever nothing's saved yet, or gui/_window_anchor.py
        # itself can't be loaded, or the saved position turns out to be
        # stale (see restore_geometry()'s own docstring).
        x = y = None
        if REMEMBER_POSITION:
            try:
                window_anchor = _load_module("_window_anchor", str(SELF_DIR / "gui" / "_window_anchor.py"))
                restored = window_anchor.restore_geometry("progress_window")
                if restored is not None:
                    x, y = restored[0], restored[1]
            except Exception:
                pass
        if x is None:
            x, y = self._position_for(self._base_width, self._base_height)

        self.root.geometry(f"{self._base_width}x{self._base_height}+{x}+{y}")
        self.root.update()

    def _on_close_click(self):
        """
        The Close/Continue button's actual command -- saves the
        window's final geometry (when REMEMBER_POSITION is on) before
        destroying, so this run's ending position becomes next run's
        starting one. Deliberately NOT wired into the auto-close
        countdown (_tick_auto_close) or the silent no-SVG/no-failure
        auto-dismiss in finish() -- only an explicit click here counts
        as the person actually choosing to leave the window where it
        is, same distinction as the config editor's close save.
        """
        if REMEMBER_POSITION:
            try:
                window_anchor = _load_module("_window_anchor", str(SELF_DIR / "gui" / "_window_anchor.py"))
                self.root.update_idletasks()
                window_anchor.save_window_geometry(
                    "progress_window",
                    self.root.winfo_x(), self.root.winfo_y(),
                    self.root.winfo_width(), self.root.winfo_height(),
                )
            except Exception:
                pass
        self.root.destroy()

    def _set_app_icon(self):
        """
        Same shared icon.png config_editor.py uses (opaque white
        placeholder for now -- see there for why it's opaque rather
        than transparent). Kept as a no-op-on-failure best-effort like
        the other one: a missing asset or a Tk build without PNG
        support just means this window keeps Tk's stock icon instead.
        """
        icon_path = SELF_DIR / "assets" / "icon.png"
        try:
            self._icon_img = self._tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _append(self, line, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", tag or ())
        self.text.tag_configure("ok", foreground="#7ec97e")
        self.text.tag_configure("fail", foreground="#e57373")
        self.text.tag_configure("notice_abort", foreground="#e57373")
        self.text.tag_configure("notice_warning", foreground="#e0b95c")
        self.text.tag_configure("notice_info", foreground="#9a9a9a")
        self.text.see("end")
        self.text.configure(state="disabled")
        self.root.update()

    def update(self, name, status, ms, notices=None):
        ok = status == "OK"
        mark = "OK" if ok else "FAIL"
        self._append(f"[{mark}] {name} ({ms}ms)", "ok" if ok else "fail")
        # Surface each processor's own NOTICE message (e.g. "COLLISION
        # DETECTED near 'Cube': ...") right under its result line, not
        # just the bare OK/FAIL -- otherwise the *cause* only ever made
        # it into stderr/the gcode comment, never this window, even
        # though the processor computed and emitted it every time.
        for raw_json in notices or []:
            try:
                notice = json.loads(raw_json)
            except Exception:
                continue
            level = notice.get("level", "info")
            message = notice.get("message", "")
            if not message:
                continue
            tag = {"abort": "notice_abort", "warning": "notice_warning"}.get(level, "notice_info")
            self._append(f"    -> {message}", tag)
        self.progress.step(1)
        self.root.update()

    def show_svgs(self, svg_entries) -> bool:
        """
        Renders every collected SVG_PAYLOAD directly onto this same
        window -- generic over whichever processor(s) emitted them, the
        same way the run log itself has zero special-cased knowledge of
        any one processor. No browser tab, no extra file: this is the
        window you already have open, can move, keep on top, or close
        yourself. Returns True if anything was actually drawn, so
        finish() knows whether to keep the window up for you to look at.
        """
        payloads = []
        for name, raw in svg_entries:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            # Same "targets" convention as the gcode-embed side (see
            # prepend_run_log) -- skip anything that isn't meant for the
            # PC. Absent field defaults to showing it, for processors
            # that don't know about targeting.
            targets = payload.get("targets", ["printer", "pc"])
            if "pc" not in targets:
                continue
            payloads.append((name, payload))
        if not payloads:
            return False

        tk = self._tk
        self.root.resizable(True, True)

        wrap = tk.Frame(self.root, bg=ORCA_BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        # self._text_wrap (the log box's frame, above) was packed with
        # expand=True so it would fill the window on its own before
        # there was anything else to expand into. Now that `wrap` exists
        # as a sibling with its own expand=True, Tk's packer splits any
        # leftover vertical space EQUALLY between every expand=True
        # child of self.root -- not proportionally to what each one
        # actually needs. Left as-is, roughly half of every pixel
        # _finalize_svg_size() adds to grow the window balloons the log
        # box instead of reaching the SVG panel it was actually sized
        # for, so the window measures out to the right total height
        # while the panel itself still comes up short and needs to
        # scroll. Pinning the log box's frame back to its own natural
        # (12-row) height here hands 100% of the room the sizing pass
        # computed to the panel that's actually meant to use it. fill="x"
        # is kept so it still stretches to match the window's width,
        # same as before -- only the vertical expand is dropped.
        self._text_wrap.pack_configure(fill="x", expand=False)

        scroll_canvas = tk.Canvas(wrap, bg=ORCA_BG, highlightthickness=0)
        scrollbar = self._ttk.Scrollbar(wrap, orient="vertical", command=scroll_canvas.yview,
                                          style="Orca.Vertical.TScrollbar")
        inner = tk.Frame(scroll_canvas, bg=ORCA_BG)

        def _on_inner_configure(event=None):
            scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))
            self._update_svg_scrollbar_visibility()

        inner.bind("<Configure>", _on_inner_configure)
        # Also re-check on the CANVAS's own <Configure> (fires when the
        # window itself is resized, not just when the content inside
        # changes) -- covers the person dragging the window bigger or
        # smaller by hand after _finalize_svg_size() has already run
        # once, which _on_inner_configure alone wouldn't catch since
        # `inner`'s own size doesn't change when the viewport around it
        # does.
        scroll_canvas.bind("<Configure>", lambda e: self._update_svg_scrollbar_visibility())
        scroll_canvas.create_window((0, 0), window=inner, anchor="nw")
        scroll_canvas.configure(yscrollcommand=scrollbar.set)
        scroll_canvas.pack(side="left", fill="both", expand=True)
        # Not packing the scrollbar here on purpose -- _update_svg_scrollbar_visibility()
        # (triggered by the <Configure> bindings above, and again once
        # more from _finalize_svg_size() after the window reaches its
        # true final size) decides whether it's actually needed and
        # packs/unpacks it accordingly, instead of it always being
        # there as a sliver even when every payload already fits.
        self._svg_scroll_canvas = scroll_canvas
        self._svg_scrollbar = scrollbar
        self._bind_svg_mousewheel(wrap, scroll_canvas)

        for name, payload in payloads:
            title = payload.get("title", name)
            tk.Label(inner, text=f"{title}  ({name})", fg=ORCA_FG, bg=ORCA_BG,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 2))
            self._draw_payload(inner, payload)

        # Sizing happens in _finalize_svg_size(), called from finish() --
        # not here. finish() still runs after this and can add its own
        # widgets below this panel (the close/continue button's text
        # change doesn't affect height, but the auto-close countdown
        # label does). Sizing the window right now, before those exist,
        # would claim a height that doesn't account for them: the
        # label appearing afterward would shrink this already-
        # fill/expand'd panel to make room for itself, clipping the
        # bottom of the SVG and forcing a scroll that shouldn't be
        # needed. Stashing these two lets finish() do the actual
        # measuring once everything that's going to exist, exists.
        self._svg_inner = inner
        self._svg_wrap = wrap
        self.root.update()
        return True

    def _bind_svg_mousewheel(self, wrap, scroll_canvas):
        """
        Lets the mouse wheel scroll the SVG panel from anywhere over it
        -- including over the individual per-payload Canvases and
        Labels packed inside `inner`, not just the scroll Canvas's own
        bare background. A plain per-widget <MouseWheel> binding on
        scroll_canvas alone wouldn't fire for those: from the pointer's
        perspective they're separate windows layered on top of it, not
        the canvas itself.

        Bound with bind_all (scoped to this window's own Tk
        interpreter -- each _TkProgressUI gets its own tk.Tk(), so this
        can't leak into any other window) and gated on the pointer
        actually being somewhere inside `wrap` via a walk up the widget
        tree, the same approach config_editor.py's scroll areas use.
        """
        tk = self._tk

        def _ancestor_is_wrap(widget):
            w = widget
            while w is not None:
                if w is wrap:
                    return True
                w = getattr(w, "master", None)
            return False

        def _on_wheel(event):
            if not _ancestor_is_wrap(getattr(event, "widget", None)):
                return
            bbox = scroll_canvas.bbox("all")
            if not bbox:
                return
            content_h = bbox[3] - bbox[1]
            if content_h <= scroll_canvas.winfo_height():
                return  # everything already fits -- nothing to scroll
            delta = getattr(event, "delta", 0)
            if delta:
                # Windows fires in multiples of 120; macOS fires small
                # per-pixel deltas -- the "or" falls back to a flat
                # +/-1 unit step for the latter so trackpad scrolling
                # doesn't end up doing nothing between 120-steps.
                units = -1 * (delta // 120 or (1 if delta > 0 else -1))
            else:
                # X11 reports wheel scroll as Button-4 (up) / Button-5
                # (down) instead of a <MouseWheel> delta.
                units = -1 if getattr(event, "num", None) == 4 else 1
            try:
                scroll_canvas.yview_scroll(units, "units")
            except tk.TclError:
                pass  # window closed mid-scroll -- nothing left to update

        self.root.bind_all("<MouseWheel>", _on_wheel)
        self.root.bind_all("<Button-4>", _on_wheel)
        self.root.bind_all("<Button-5>", _on_wheel)

    def _update_svg_scrollbar_visibility(self):
        """
        Packs the SVG panel's scrollbar in only when there's actually
        something to scroll -- i.e. the payloads' combined natural
        height doesn't fit in whatever vertical space the panel
        currently has. Otherwise it's forgotten (unpacked) so it
        doesn't sit there as a visible sliver on a run whose SVGs
        already fit, and _bind_svg_mousewheel's own content_h <=
        winfo_height() check already made the wheel a no-op in that
        case anyway -- this just makes the on-screen scrollbar agree
        with that.

        Safe to call before the window has ever been sized (during
        show_svgs(), from the <Configure> bindings there) as well as
        after _finalize_svg_size() reaches its final geometry, and
        again any time the person drags the window's edge by hand --
        every one of those is a legitimate moment for "is there
        something to scroll" to have changed.
        """
        canvas = self._svg_scroll_canvas
        scrollbar = self._svg_scrollbar
        if canvas is None or scrollbar is None:
            return
        try:
            bbox = canvas.bbox("all")
            content_h = (bbox[3] - bbox[1]) if bbox else 0
            needs_scroll = content_h > canvas.winfo_height()
            is_mapped = scrollbar.winfo_ismapped()
        except self._tk.TclError:
            return  # window/canvas torn down mid-check -- nothing left to update
        if needs_scroll and not is_mapped:
            scrollbar.pack(side="right", fill="y")
        elif not needs_scroll and is_mapped:
            scrollbar.pack_forget()

    def _finalize_svg_size(self):
        """
        Final sizing pass for the SVG panel -- called from finish(),
        AFTER every widget that might still land below it (the
        close/continue button's text change, and the auto-close
        countdown label if that's enabled) has already been packed. See
        the comment at the end of show_svgs() for why this can't just
        happen there instead.
        """
        if self._svg_inner is None:
            return
        self.root.update_idletasks()
        svg_area_h = self._svg_inner.winfo_reqheight()

        # The scroll Canvas never auto-sizes to its scrolled content --
        # left alone it just reports Tk's fixed bare-Canvas default
        # reqheight (measured ~276px in testing), completely unrelated
        # to what's actually drawn inside it. A naive fix would try to
        # cancel that placeholder back out (root.reqheight() minus the
        # wrap's bare reqheight, then plus svg_area_h and a flat +40
        # fudge for "the panel's own overhead"), but the placeholder is
        # usually much bigger than 40px larger than the real content --
        # e.g. toolchange_heatmap's default 260x56.64 strip has ~94px
        # of real content against a 276px placeholder, a ~140px gap the
        # flat fudge would never cover. The window would come out that
        # much too short, and the auto-close countdown label -- packed
        # dead last, after this panel -- would be the one left with no
        # room and silently never get mapped at all.
        #
        # Telling the canvas its real content height up front removes
        # the placeholder entirely: root.winfo_reqheight() below then
        # already reflects the panel's true size, same as every other
        # widget in the stack, so nothing packed after it (like that
        # countdown label) can come up short. Still capped defensively
        # -- an oversized request here doesn't matter beyond wasted
        # measurement work, since the screen_h clamp further down is
        # what actually bounds the window and hands off to the
        # scrollbar for anything taller than the screen.
        self._svg_scroll_canvas.configure(height=min(int(svg_area_h), 4000))
        self.root.update_idletasks()
        chrome_h = self.root.winfo_reqheight() - self._svg_scroll_canvas.winfo_reqheight()

        # screen_h needs the work area of whichever monitor this window
        # is actually ON, not the primary monitor -- _usable_screen_height()
        # falls back to Win32's SPI_GETWORKAREA, which is documented to
        # always report the PRIMARY monitor's work area no matter which
        # screen the call is made from. On a multi-monitor setup where
        # this window lives on a secondary display, that silently capped
        # the window's growth to the primary monitor's height even
        # though the monitor it's actually on had plenty more room --
        # the exact "window won't grow, but there's clearly screen space"
        # symptom this avoids. gui/_window_anchor.py's
        # get_monitor_work_area() is already monitor-aware (used below
        # for x/y anchoring) and is loaded once here and reused for both.
        window_anchor = None
        work_area = None
        try:
            window_anchor = _load_module("_window_anchor", str(SELF_DIR / "gui" / "_window_anchor.py"))
            self.root.update_idletasks()
            cx = self.root.winfo_x() + self.root.winfo_width() // 2
            cy = self.root.winfo_y() + self.root.winfo_height() // 2
            work_area = window_anchor.get_monitor_work_area(cx, cy)
        except Exception:
            pass
        if work_area is not None:
            screen_h = work_area[3] - work_area[1]
        else:
            screen_h = _usable_screen_height(self.root)

        new_h = min(chrome_h + svg_area_h + 40, screen_h - 40)
        new_h = max(new_h, self._base_height + 120)  # always show a meaningful slice, not a sliver


        # Re-anchor using gui/_window_anchor.py: default is to grow
        # straight down from wherever the window already is (ordinary
        # app behavior), only flipping to a bottom anchor if there's
        # not enough room below on the CURRENT monitor -- see that
        # module's docstring. This is deliberately NOT recomputing both
        # x and y from WINDOW_POSITION's fixed corner/center setting on
        # every resize -- that would work, but would mean the window
        # jumps to a config-defined spot instead of growing naturally
        # from wherever it (or the user) has put it.
        #
        # "center" is the one exception, handled by the
        # _position_for() recentering path instead: it isn't a corner
        # anchor at all, it's a "stay centered" invariant, and growing
        # it from its current top edge (which starts above true center)
        # would visibly creep the window toward the top of the screen
        # as it grows -- the opposite of what WINDOW_POSITION="center"
        # promises. Falls back the same way for every other position
        # if the anchor module can't be loaded for any reason -- this
        # must never be able to stop the window from appearing, same
        # rule as tkinter itself at the top of this file.
        #
        # REMEMBER_POSITION overrides that, though: if the person has
        # asked this window to remember where they left it, a resize
        # must never silently snap it back to true screen center --
        # that would throw away the remembered (possibly off-center,
        # possibly other-monitor) position on every single run, which
        # defeats the whole feature. In that case this falls through
        # to the same anchor-aware growth every other position uses,
        # so "center" only means "recenter on resize" when nothing is
        # being remembered.
        if WINDOW_POSITION == "center" and not REMEMBER_POSITION:
            new_x, new_y = self._position_for(self._base_width, new_h)
        else:
            try:
                if window_anchor is None:
                    window_anchor = _load_module("_window_anchor", str(SELF_DIR / "gui" / "_window_anchor.py"))
                self.root.update_idletasks()
                new_x, new_y = window_anchor.resize_toward_center(
                    self.root.winfo_x(), self.root.winfo_y(),
                    self.root.winfo_width(), self.root.winfo_height(),
                    self._base_width, new_h,
                )
            except Exception:
                new_x, new_y = self._position_for(self._base_width, new_h)

        self.root.geometry(f"{self._base_width}x{new_h}+{new_x}+{new_y}")
        self.root.update()
        # Windows redraws the actual OS titlebar as part of resizing
        # the frame here, which drops the DWMWA_CAPTION_COLOR/
        # DWMWA_TEXT_COLOR attributes apply_dark_titlebar() set back in
        # __init__ -- they're a one-shot paint, not a persistent style,
        # so the bar reverts to the stock (light) titlebar the instant
        # geometry() changes the window's size. Same DWM attribute,
        # same call, just re-issued after the resize actually lands --
        # matches config_editor.pyw's _revert_live_titlebar()/live
        # preview pattern of re-applying this on anything that touches
        # the window frame instead of assuming it sticks.
        apply_dark_titlebar(self.root, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)
        # new_h was already capped at screen_h - 40 above, so if the
        # payloads' full natural height didn't fit on this screen this
        # is the one legitimate case where a scrollbar is still needed
        # even at the window's max size -- the <Configure> bindings in
        # show_svgs() would eventually reach the same conclusion on
        # their own, but doing it once here right after geometry is
        # actually finalized avoids a visible flash of the scrollbar
        # briefly appearing/disappearing while those events settle.
        self._update_svg_scrollbar_visibility()

    def _draw_payload(self, parent, payload: dict) -> None:
        tk = self._tk
        canvas_cfg = payload.get("canvas", {}) or {}
        x_max = float(canvas_cfg.get("x_max", 100.0)) or 1.0
        y_max = float(canvas_cfg.get("y_max", 100.0)) or 1.0
        pad = float(canvas_cfg.get("pad", 5.0))
        max_size = float(canvas_cfg.get("max_size", 260))

        # Cap the on-screen pixel size independent of the payload's own
        # mm-space max_size, so a print that fills the whole build volume
        # doesn't blow the window out to something unusable.
        display_size = min(max_size, self._base_width - 40)
        scale = (display_size - 2 * pad) / max(x_max, y_max, 1e-6)
        w = x_max * scale + 2 * pad
        h = y_max * scale + 2 * pad

        cv = tk.Canvas(parent, width=w, height=h, bg=ORCA_PANEL_BG,
                        highlightthickness=1, highlightbackground=ORCA_BORDER)
        cv.pack(anchor="w", pady=(0, 6))

        def to_px(x, y):
            return (pad + x * scale, h - pad - y * scale)  # flip Z so 0 sits at the bottom

        for shape in payload.get("shapes", []):
            kind = shape.get("type")
            if kind == "polygon":
                flat = []
                for x, y in shape.get("points", []):
                    px, py = to_px(x, y)
                    flat.extend([px, py])
                if len(flat) < 6:
                    continue
                fill_hex, stipple = _color_to_tk(shape.get("fill", "rgba(136,136,136,0.3)"))
                stroke_raw = shape.get("stroke", "#cccccc")
                stroke_hex, _ = _color_to_tk(stroke_raw)
                if shape.get("fill_style", "solid") == "solid":
                    # A real flat, un-dithered fill. _color_to_tk's
                    # alpha-derived stipple (gray25 etc) is a fine
                    # checkerboard dither -- applying it here (as "dithered"
                    # still does, below) made "solid" look textured/grainy.
                    stipple = ""
                # else fill_style == "dithered" (or any unrecognized value):
                # leave `stipple` as _color_to_tk's alpha-derived dither.
                if shape.get("fill", None) == stroke_raw:
                    # Producers that set stroke == fill (e.g.
                    # toolchange_heatmap's base band, isolated-toolchange
                    # lines, and gradient strip segments) aren't asking for
                    # a visible border -- they're just filling in both
                    # fields because the payload shape requires one. Tk's
                    # create_polygon outline is centered on the edge
                    # though, so drawing it here would add ~0.75px of
                    # same-color bleed on every side regardless, silently
                    # inflating precisely-sized shapes (like the heatmap's
                    # configured line_width_px) past their intended pixel
                    # width. Skip the outline in that case. Shapes that
                    # DO want a distinct border (dock_collision_guard's
                    # silhouettes, zone highlights, etc.) set a stroke that
                    # differs from fill and keep their outline as before.
                    cv.create_polygon(*flat, fill=fill_hex, outline="",
                                       stipple=stipple)
                else:
                    cv.create_polygon(*flat, fill=fill_hex, outline=stroke_hex,
                                       stipple=stipple, width=1.5)
            elif kind == "path":
                # A real stroked line. Two different use patterns share
                # this same shape: a single 2-point path per independent
                # segment (e.g. dock_collision_guard's travel moves --
                # deliberately kept as separate shapes rather than one
                # strung-together polyline, since unrelated travel moves
                # aren't actually connected to each other and joining them
                # would draw a spurious connecting line), or one shape
                # covering every vertex of a single continuous curve (e.g.
                # tool_temperature_graph's tool curves), where a real
                # multi-point polyline is exactly what's wanted. width_px
                # is a literal on-screen pixel width (NOT scaled by
                # `scale` above, same as create_line always works) --
                # unlike a "polygon" standing in for a line via a
                # pre-computed filled ribbon, this can't drift out of sync
                # with the actual displayed size the way a ribbon sized
                # for some OTHER assumed scale would.
                flat = []
                for x, y in shape.get("points", []):
                    px, py = to_px(x, y)
                    flat.extend([px, py])
                if len(flat) < 4:
                    continue
                color, stipple = _color_to_tk(shape.get("color", "rgba(200,200,200,0.4)"))
                if not color:
                    # Fully transparent (_color_to_tk's own "" convention,
                    # see its docstring) -- nothing to draw.
                    continue
                width = float(shape.get("width_px", 1))
                dash = shape.get("dash")
                kwargs = {"fill": color, "width": width}
                if stipple:
                    # Same alpha-as-dither approximation _color_to_tk
                    # already provides for polygon fills above -- create_line
                    # accepts "stipple" too, so a semi-transparent line
                    # color (e.g. tool_temperature_graph's reference lines)
                    # gets an actual visible approximation of its configured
                    # opacity instead of silently always rendering fully
                    # solid regardless of the alpha requested.
                    kwargs["stipple"] = stipple
                if dash:
                    # Tkinter wants a tuple of ints (on/off pixel run
                    # lengths) -- same [dash_len, gap_len, ...] shape the
                    # SVG side's stroke-dasharray takes, just rounded.
                    kwargs["dash"] = tuple(max(1, round(d)) for d in dash)
                cv.create_line(*flat, **kwargs)
            elif kind == "crosshair":
                cx, cy = to_px(float(shape.get("x", 0)), float(shape.get("y", 0)))
                size = float(shape.get("size", 4.0)) * scale
                color, _ = _color_to_tk(shape.get("color", "yellow"))
                cv.create_line(cx - size, cy, cx + size, cy, fill=color, width=2)
                cv.create_line(cx, cy - size, cx, cy + size, fill=color, width=2)
            elif kind == "text":
                # Sized in canvas units like crosshair above (scaled by
                # `scale`, so it grows/shrinks WITH the graph), not a
                # fixed pixel size like marker's size_px below -- a label
                # should stay legibly proportioned whether this is a
                # tiny single-tool graph or a large multi-tool one, not
                # stay some fixed screen size regardless of either.
                cx, cy = to_px(float(shape.get("x", 0)), float(shape.get("y", 0)))
                size_px = max(6, round(float(shape.get("size", 12)) * scale))
                color, _ = _color_to_tk(shape.get("color", "#ffffff"))
                anchor = {"start": "w", "middle": "center", "end": "e"}.get(shape.get("anchor", "start"), "w")
                weight = "bold" if shape.get("weight") == "bold" else "normal"
                cv.create_text(cx, cy, text=str(shape.get("text", "")), fill=color, anchor=anchor,
                                font=("Segoe UI", size_px, weight))
            elif kind == "marker":
                # Toolchange/restore-position markers -- fixed PIXEL size
                # (unlike crosshair's mm-based size above), same reasoning
                # as the SVG/browser renderer: shouldn't rescale with
                # canvas_clip zoom, and there could be thousands of these.
                cx, cy = to_px(float(shape.get("x", 0)), float(shape.get("y", 0)))
                r = float(shape.get("size_px", 6))
                color, _ = _color_to_tk(shape.get("color", "yellow"))
                mshape = shape.get("shape", "cross")
                if mshape == "circle":
                    cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
                elif mshape == "square":
                    cv.create_rectangle(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
                elif mshape == "diamond":
                    cv.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy,
                                       fill=color, outline="")
                elif mshape == "triangle":
                    cv.create_polygon(cx, cy - r, cx + r, cy + r, cx - r, cy + r,
                                       fill=color, outline="")
                else:  # "cross"
                    cv.create_line(cx - r, cy, cx + r, cy, fill=color, width=2)
                    cv.create_line(cx, cy - r, cx, cy + r, fill=color, width=2)

    def finish(self, any_failed, has_svgs=False):
        if any_failed or has_svgs:
            self._append("")
            if any_failed:
                self._append("Completed with failures -- see above.", "fail")
                btn_text = "Close"
            else:
                self._append("All processors completed successfully.", "ok")
                btn_text = "Continue"
            self.close_btn.configure(text=btn_text, state="normal", bg=ORCA_ACCENT, fg=ORCA_ACCENT_FG, cursor="hand2")
            self.root.attributes("-topmost", False)
            if not any_failed and AUTO_CLOSE_ENABLED:
                self._start_auto_close_countdown(AUTO_CLOSE_SECONDS)
            if has_svgs:
                self._finalize_svg_size()
            self.root.mainloop()  # stay open, wait for the Continue/Close button (or the countdown below)
        else:
            self._append("All processors completed successfully.", "ok")
            self.root.update()
            time.sleep(0.6)  # brief, so it doesn't look like it flickered
            self.root.destroy()

    def _start_auto_close_countdown(self, seconds):
        """
        Auto-closes the window `seconds` after a fully successful run,
        even with an SVG preview still on screen -- opt-in via
        orcastrator.json's auto_close.enabled/seconds (see
        gui/orcastrator.py's Progress Window section). Only ever called
        from finish() when `not any_failed` -- a failed run always needs
        a manual Close, this never overrides that.

        A small label below the Continue button counts down and doubles
        as the cancel control: clicking ANYWHERE on the window cancels
        it and leaves the window open for a manual Continue, same as
        today, since a window that vanishes out from under someone
        actually looking at the SVG is worse than one that just doesn't
        auto-close. This relies on plain Tk bindtag propagation --
        binding on self.root (the toplevel) also fires for clicks on its
        child widgets (canvas, text, buttons), since "toplevel" is one of
        the bindtag levels every child widget carries by default, not
        just its own -- no need for a separate binding per child widget.
        """
        self._auto_close_remaining = max(1, int(seconds))
        self._auto_close_after_id = None
        self._auto_close_label = self._tk.Label(
            self.root, text="", bg=ORCA_BG, fg=ORCA_FG_DIM, font=("Segoe UI", 8))
        # Two things have to be true together for this label to survive
        # a tight window, and getting only one of them isn't enough:
        #
        # 1. side="bottom" -- so its parcel is carved from the window's
        #    bottom edge rather than stacked in "top" order.
        # 2. It has to be PACKED (i.e. this whole method called) before
        #    the SVG panel (`wrap`, in show_svgs()) gets its own turn --
        #    Tk's packer carves cavity space in PACKING-CALL order, not
        #    by side, so an expand=True widget processed earlier can
        #    still claim the entire remaining cavity for itself before
        #    a later side="bottom" widget ever gets a turn, leaving it
        #    zero-height regardless of which side it asked for.
        #
        # `wrap` is already packed by the time this runs (show_svgs()
        # ran back in run_all(), well before finish() gets here), so
        # re-packing it AFTER this label -- pack_forget() then pack()
        # again with the same options -- moves it back to the end of
        # the packing order without touching its own children (the
        # scroll canvas/scrollbar packed inside it are untouched by
        # their parent's own re-pack). That makes this label the one
        # guaranteed its footer space first; `wrap` then only gets
        # whatever's left, exactly like every other expand=True panel
        # squeezed by a fixed-size sibling -- it shrinks and the
        # existing scrollbar logic (_update_svg_scrollbar_visibility)
        # picks up the slack, instead of this label losing the fight.
        self._auto_close_label.pack(side="bottom", pady=(0, 8))
        if self._svg_wrap is not None:
            self._svg_wrap.pack_forget()
            self._svg_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.root.bind("<Button-1>", self._cancel_auto_close)
        self._tick_auto_close()

    def _tick_auto_close(self):
        try:
            if not self.root.winfo_exists():
                return  # cancelled/closed already -- nothing left to update or schedule
        except self._tk.TclError:
            return  # root itself is gone (Continue/Close was clicked) -- same as above
        if self._auto_close_remaining <= 0:
            self.root.destroy()
            return
        self._auto_close_label.configure(
            text=f"Closing automatically in {self._auto_close_remaining}s -- click anywhere to cancel")
        self._auto_close_remaining -= 1
        self._auto_close_after_id = self.root.after(1000, self._tick_auto_close)

    def _cancel_auto_close(self, event=None):
        if self._auto_close_after_id is not None:
            self.root.after_cancel(self._auto_close_after_id)
            self._auto_close_after_id = None
        self._auto_close_label.configure(text="Auto-close cancelled.")
        self.root.unbind("<Button-1>")


def _make_progress_ui(processor_names):
    if not SHOW_PROGRESS_UI:
        return _NullProgressUI()
    try:
        return _TkProgressUI(processor_names)
    except Exception:
        return _NullProgressUI()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POST_PROCESSORS_DIR = SELF_DIR / "post_processors"

# Scripts that must run before anything else discovered, in this exact
# order. See "Execution order" above for why. Set in orcastrator.json
# (explicit_order) -- editable from the settings GUI (OrcaStrator ->
# Processor Selection) as a pick-and-reorder list built from whatever's
# actually in post_processors/, rather than free-text entry.
EXPLICIT_ORDER = list(_CFG["explicit_order"])

# The mirror image: scripts that must run after everything else
# discovered, in this exact order -- for a processor that needs to see
# the final state of the g-code once every other processor (including
# ones added later and never listed anywhere) has already run. Set in
# orcastrator.json (explicit_order_last) -- same GUI picker as
# EXPLICIT_ORDER, just the "runs last" side of it. If a name ends up in
# both lists, EXPLICIT_ORDER wins (see discover_processors()) -- being
# pinned first always beats being pinned last.
EXPLICIT_ORDER_LAST = list(_CFG["explicit_order_last"])

# Extra safety: never treat these as runnable processors even if someone
# drops them in post_processors/ by mistake (e.g. a shared library that
# has no __main__ guard). Set in orcastrator.json (denylist) -- also
# editable from the settings GUI. See run_all()/discover_processors()
# for the separate, run-scoped --denylist CLI flag, which is for
# skipping a processor for one specific OrcaSlicer profile/gcode
# without touching this shared, always-applies list.
DENYLIST = set(_CFG["denylist"])

# If True: stop at the first failing processor instead of running the
# rest. Either way, any failure makes OrcaStrator exit non-zero,
# which OrcaSlicer surfaces as a failed export. Set in orcastrator.json
# (on_error.stop_on_error).
STOP_ON_ERROR = _CFG["on_error"]["stop_on_error"]

# If a processor exits non-zero and emitted no NOTICE of its own with
# level "abort", synthesize one so the printer-side gate still refuses
# to print the file. Only disable this if you're confident every
# processor you use always explains its own failures. Set in
# orcastrator.json (on_error.auto_abort_on_unexplained_failure).
AUTO_ABORT_ON_UNEXPLAINED_FAILURE = _CFG["on_error"]["auto_abort_on_unexplained_failure"]

PYTHON = sys.executable or "python3"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_processors(extra_denylist: frozenset = frozenset()) -> list:
    if not POST_PROCESSORS_DIR.is_dir():
        print(f"[orcastrator] post_processors dir not found: {POST_PROCESSORS_DIR}", file=sys.stderr)
        return []

    all_names = sorted(p.name for p in POST_PROCESSORS_DIR.glob("*.py") if p.is_file())
    effective_denylist = DENYLIST | extra_denylist
    found = [n for n in all_names if n not in effective_denylist]

    # A name in both lists is almost certainly a mistake -- EXPLICIT_ORDER
    # (runs first) wins, and it's dropped from the "runs last" side
    # entirely so it doesn't appear twice.
    conflicts = sorted(n for n in EXPLICIT_ORDER_LAST if n in EXPLICIT_ORDER)
    order_last = [n for n in EXPLICIT_ORDER_LAST if n not in EXPLICIT_ORDER]

    ordered_first = [name for name in EXPLICIT_ORDER if name in found]
    ordered_last = [name for name in order_last if name in found]
    remainder = [name for name in found if name not in EXPLICIT_ORDER and name not in order_last]
    missing = [name for name in EXPLICIT_ORDER if name not in found] + \
              [name for name in order_last if name not in found]
    # Only the CLI-supplied extras get this check -- denylist.py entries
    # in orcastrator.json come from the GUI's picker now, which can't
    # produce an unmatched name in the first place. --denylist is still
    # hand-typed on an OrcaSlicer profile's post-processing command
    # line, so a typo there is worth surfacing the same way a bad
    # EXPLICIT_ORDER entry already is above.
    unknown_extra = sorted(n for n in extra_denylist if n not in all_names)

    if conflicts:
        print(f"[orcastrator] note: listed in both explicit_order and explicit_order_last, "
              f"explicit_order wins: {conflicts}", file=sys.stderr)
    if missing:
        print(f"[orcastrator] note: EXPLICIT_ORDER/EXPLICIT_ORDER_LAST list scripts not present: {missing}",
              file=sys.stderr)
    if unknown_extra:
        print(f"[orcastrator] note: --denylist named scripts not present, ignored: {unknown_extra}",
              file=sys.stderr)

    return ordered_first + remainder + ordered_last


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

SVG_STDOUT_PREFIX = "SVG_PAYLOAD:"
NOTICE_STDOUT_PREFIX = "NOTICE:"


def _extract_prefixed_lines(stdout: str, prefix: str) -> list:
    """Returns a list of raw (unparsed) JSON strings, one per matching line."""
    found = []
    for line in stdout.splitlines():
        idx = line.find(prefix)
        if idx != -1:
            found.append(line[idx + len(prefix):].strip())
    return found


def run_processor(name: str, gcode_path: str):
    """Returns (ok, status_str, elapsed_ms, svg_payloads: list[str], notices: list[str])."""
    script = POST_PROCESSORS_DIR / name
    start = time.time()
    result = subprocess.run(
        [PYTHON, str(script), gcode_path],
        capture_output=True,
        text=True,
    )
    elapsed_ms = int((time.time() - start) * 1000)
    svg_payloads = _extract_prefixed_lines(result.stdout or "", SVG_STDOUT_PREFIX)
    notices = _extract_prefixed_lines(result.stdout or "", NOTICE_STDOUT_PREFIX)

    if result.returncode == 0:
        print(f"[orcastrator] '{name}' OK ({elapsed_ms}ms)")
        if result.stdout.strip():
            print(f"  stdout: {result.stdout.strip()}")
        return True, "OK", elapsed_ms, svg_payloads, notices
    else:
        print(f"[orcastrator] '{name}' FAILED (code {result.returncode}, {elapsed_ms}ms)", file=sys.stderr)
        if result.stdout.strip():
            print(f"  stdout: {result.stdout.strip()}", file=sys.stderr)
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)

        has_own_abort_notice = False
        for raw in notices:
            try:
                if json.loads(raw).get("level") == "abort":
                    has_own_abort_notice = True
                    break
            except Exception:
                pass
        if not has_own_abort_notice and AUTO_ABORT_ON_UNEXPLAINED_FAILURE:
            synthetic = json.dumps({
                "level": "abort",
                "title": "Postprocessing failure",
                "message": f"'{name}' exited with code {result.returncode} and didn't explain why. "
                           f"Treating this file as unsafe to print.",
            }, separators=(",", ":"))
            notices.append(synthetic)
            print(f"[orcastrator] '{name}' failed with no explanation -- synthesized an abort notice", file=sys.stderr)

        return False, f"FAILED (code {result.returncode})", elapsed_ms, svg_payloads, notices


# ---------------------------------------------------------------------------
# Run log -- a single consolidated marker so it's obvious what ran even
# for processors that don't leave their own trace in the file.
# ---------------------------------------------------------------------------

LOG_START = "; ORCASTRATOR_LOG_START"
LOG_END = "; ORCASTRATOR_LOG_END"


def prepend_run_log(gcode_path: str, run_results: list, svg_entries: list, notice_entries: list, total_ms: int) -> None:
    """
    Unconditionally prepends to the very top of the file -- not "before
    the first non-comment line" like a processor's own domain-specific
    inserts use (e.g. dock_collision_guard.py's CANCEL_PRINT_BASE
    block on a violation, which needs to stay as early in the file as
    possible). This must never land in between the file's actual header
    and a safety-critical insert like that, so it always goes above
    everything, full stop.

    svg_entries and notice_entries are lists of (processor_name,
    raw_json_str) pairs collected from every processor's stdout. Each
    gets wrapped in a small envelope ({"source": ..., "payload"/"notice":
    ...}) so the printer-side macro knows which processor produced it,
    and validated with json.loads here so a malformed line from a
    buggy/experimental processor gets skipped with a warning instead of
    corrupting the header.
    """
    lines = [LOG_START]
    for name, status, ms in run_results:
        lines.append(f"; {name}: {status} ({ms}ms)")

    for name, raw_json in svg_entries:
        try:
            payload = json.loads(raw_json)
        except Exception as exc:
            print(f"[orcastrator] '{name}' emitted a malformed SVG_PAYLOAD, skipping: {exc}", file=sys.stderr)
            continue
        # Optional "targets" field (see dock_collision_guard.py's
        # svg.display config) lets a processor say a given payload is
        # meant for the PC window only, not the printer -- skip those
        # here so a "pc"-only visualization never gets embedded in the
        # actual g-code. Absent field = old/other processors that don't
        # know about targeting -- default to "embed it", the pre-existing
        # behavior.
        targets = payload.get("targets", ["printer", "pc"])
        if "printer" not in targets:
            continue
        envelope = json.dumps({"source": name, "payload": payload}, separators=(",", ":"))
        lines.append(f"; SVG_PAYLOAD:{envelope}")

    for name, raw_json in notice_entries:
        try:
            notice = json.loads(raw_json)
        except Exception as exc:
            print(f"[orcastrator] '{name}' emitted a malformed NOTICE, skipping: {exc}", file=sys.stderr)
            continue
        envelope = json.dumps({"source": name, "notice": notice}, separators=(",", ":"))
        lines.append(f"; NOTICE:{envelope}")

    lines.append(f"; total: {total_ms}ms")
    lines.append(LOG_END)

    p = pathlib.Path(gcode_path)
    content = p.read_text(encoding="utf-8", errors="ignore")
    p.write_text("\n".join(lines) + "\n" + content, encoding="utf-8")


def run_all(gcode_path: str, extra_denylist: frozenset = frozenset()) -> int:
    processors = discover_processors(extra_denylist)
    if not processors:
        print("[orcastrator] no processors found, nothing to do")
        return 0

    print(f"[orcastrator] target: {gcode_path}")
    print(f"[orcastrator] order: {processors}")

    ui = _make_progress_ui(processors)

    overall_start = time.time()
    any_failed = False
    run_results = []
    svg_entries = []
    notice_entries = []

    for name in processors:
        ok, status, ms, svg_payloads, notices = run_processor(name, gcode_path)
        run_results.append((name, status, ms))
        ui.update(name, status, ms, notices)
        for raw_json in svg_payloads:
            svg_entries.append((name, raw_json))
        for raw_json in notices:
            notice_entries.append((name, raw_json))
        if not ok:
            any_failed = True
            if STOP_ON_ERROR:
                print(f"[orcastrator] stopping after '{name}' failure (STOP_ON_ERROR=True)", file=sys.stderr)
                break

    total_ms = int((time.time() - overall_start) * 1000)
    prepend_run_log(gcode_path, run_results, svg_entries, notice_entries, total_ms)
    has_svgs = ui.show_svgs(svg_entries)

    if any_failed and SOUND_ON_ERROR_ENABLED:
        _play_sound(_resolve_sound_path(SOUND_ON_ERROR_FILE))
    elif not any_failed and SOUND_ON_SUCCESS_ENABLED:
        _play_sound(_resolve_sound_path(SOUND_ON_SUCCESS_FILE))

    ui.finish(any_failed, has_svgs)

    if any_failed:
        print(f"[orcastrator] completed with failures ({total_ms}ms total)", file=sys.stderr)
        return 1

    print(f"[orcastrator] all processors completed successfully ({total_ms}ms total)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {pathlib.Path(__file__).name} [--denylist=script.py,...] <gcode_file>", file=sys.stderr)
        sys.exit(1)
    # OrcaSlicer appends the output file path as the final argument
    # regardless of whatever else is on the "post-processing scripts"
    # line, so anything in between is ours to parse. Currently just
    # --denylist: lets one specific OrcaSlicer profile (printer/filament/
    # process) skip a processor for just that profile's prints, without
    # touching orcastrator.json's own denylist, which applies to every
    # run everywhere regardless of which profile triggered it. Useful
    # since each profile calls this same orcastrator.py individually
    extra_denylist = set()
    for arg in sys.argv[1:-1]:
        if arg.startswith("--denylist="):
            extra_denylist |= {n.strip() for n in arg[len("--denylist="):].split(",") if n.strip()}
    sys.exit(run_all(sys.argv[-1], frozenset(extra_denylist)))
