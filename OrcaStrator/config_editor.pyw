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
Standalone GUI for editing the settings of post processors.

Every processor's own field list lives in gui/*.py, discovered
automatically (see discover_gui_specs()) -- adding, editing, or
removing a processor never requires a change in this file. A processor
that wants a live preview (dock collision does, for its dock boundary +
the "svg" block of dock_collision_guard.json) implements the
HAS_PREVIEW/PREVIEW_CONTROLS/build_preview_payload contract in its own
gui/*.py, also discovered automatically -- see gui/dock_collision_guard.py
for the full convention. The preview re-renders on every change using
the SAME rendering code the real processor uses, so what you see is
exactly what will show up during an actual export, not an approximation
of it.

Run directly:
    python3 config_editor.py
"""
import colorsys
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys
import types
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, colorchooser

HERE = pathlib.Path(__file__).resolve().parent
CONFIGS_DIR = HERE / "configs"
BACKUPS_DIR = CONFIGS_DIR / "backups"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The real OrcaStrator script and shared plugin helpers are loaded as
# modules (not just imported normally) since they live in a plain script
# layout, not an installed package. orcastrator.py guards its own entry
# point with `if __name__ == "__main__"`, so loading it here for its
# functions and color constants has no side effects.
#
# Nothing dock-collision-specific loads here: build_preview_data()/
# build_preview_payload() live in gui/dock_collision_guard.py, which
# loads its own processor module via gui/_plugin_support.py's
# load_processor_module(). That means this file can't crash on startup
# if dock_collision_guard.py (or any other processor) is ever deleted
# -- see gui/_plugin_support.py.
orcastrator = _load_module("orcastrator", str(HERE / "orcastrator.py"))
_plugin_support = _load_module("_plugin_support", str(HERE / "gui" / "_plugin_support.py"))
get_in = _plugin_support.get_in
set_in = _plugin_support.set_in

# Unlike the two loads above, _window_anchor.py is an enhancement to
# HOW resizing looks, not something the editor structurally depends on
# -- so this one's guarded (matches orcastrator.py's own defensive
# load of the same module in _finalize_svg_size). _window_anchor is
# None if it can't be loaded for any reason; _autosize_to_scroll_content
# falls back to the previous plain-clamp behavior in that case.
try:
    _window_anchor = _load_module("_window_anchor", str(HERE / "gui" / "_window_anchor.py"))
except Exception:
    _window_anchor = None

ORCA_BG = orcastrator.ORCA_BG
ORCA_PANEL_BG = orcastrator.ORCA_PANEL_BG
ORCA_BORDER = orcastrator.ORCA_BORDER
ORCA_FG = orcastrator.ORCA_FG
ORCA_FG_DIM = orcastrator.ORCA_FG_DIM
ORCA_ACCENT = orcastrator.ORCA_ACCENT
ORCA_ACCENT_HOVER = orcastrator.ORCA_ACCENT_HOVER
ORCA_ACCENT_FG = orcastrator.ORCA_ACCENT_FG
ORCA_TITLEBAR = orcastrator.ORCA_TITLEBAR
ORCA_TITLEBAR_FG = orcastrator.ORCA_TITLEBAR_FG
ERROR_COLOR = "#ff6b6b"


def discover_processor_scripts() -> list:
    """
    Every post-processor script currently sitting in post_processors/,
    sorted alphabetically -- the source of truth the denylist and
    explicit-order pickers in the OrcaStrator settings form (kind
    "processor_denylist" / "processor_order" in _add_field()) build
    their lists from, instead of free-text entry. Picking from what's
    actually on disk means neither list can end up with a typo'd or
    stale script name -- see orcastrator.py's own discover_processors(),
    which already tolerates exactly that kind of bad entry at runtime
    (an unmatched name is just logged and ignored), so this doesn't
    need to be strict either; it just makes ending up with one far less
    likely in the first place.
    """
    d = orcastrator.POST_PROCESSORS_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.py") if p.is_file())


def discover_gui_page_names() -> list:
    """
    Every gui/*.py filename currently on disk, except orcastrator.py
    itself -- its card is always pinned first on the landing page (see
    build_config_registry()) so it never makes sense to offer it a spot
    in the reorderable list -- and any "_"-prefixed private helper
    module (gui/_plugin_support.py). Same pick-from-disk source of
    truth as discover_processor_scripts() above, just for the
    "gui_order" picker (OrcaStrator Settings -> Settings Landing Page)
    instead of the processor run-order/denylist ones.
    """
    if not GUI_DIR.is_dir():
        return []
    return sorted(p.name for p in GUI_DIR.glob("*.py")
                  if p.is_file() and not p.name.startswith("_") and p.name != "orcastrator.py")


# ---------------------------------------------------------------------------
# Generic nested-dict path helpers -- every field spec below addresses its
# config value with a tuple path, e.g. ("svg", "toolchange_markers", "show").
# get_in()/set_in() themselves live in gui/_plugin_support.py now (loaded
# above), shared with any plugin that needs them for its own preview --
# see that module's docstring.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# rgba(...) string <-> (r, g, b, a) helpers for the color-picker widget.
# ---------------------------------------------------------------------------

def parse_rgba(s, default=(128, 128, 128, 1.0)):
    if not s:
        return default
    nums = re.findall(r"[-\d.]+", s)
    if len(nums) < 3:
        return default
    r, g, b = (max(0, min(255, int(float(n)))) for n in nums[:3])
    a = float(nums[3]) if len(nums) > 3 else 1.0
    return r, g, b, max(0.0, min(1.0, a))


def format_rgba(r, g, b, a):
    return f"rgba({int(r)},{int(g)},{int(b)},{round(a, 2)})"


def rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _joined_comment(comment):
    """
    Several configs' top-level "_comment" is a list of strings (blank
    entries used as paragraph breaks) rather than one string -- this
    normalizes either shape into a single block of text for a
    description panel.
    """
    if isinstance(comment, list):
        return "\n".join(str(x) for x in comment)
    if isinstance(comment, str):
        return comment
    return ""


def lookup_comment(cfg, path):
    """
    Looks for this config JSON's own documentation convention: a sibling
    "_<key>" string immediately next to the real key, at the same nesting
    level (e.g. cfg["svg"]["_near_miss_margin"] documents cfg["svg"]
    ["near_miss_margin"]). Returns None when there's no such sibling --
    several fields (mostly ones nested a level or two deeper, like
    toolchange_markers.toolchange.color) are only covered by one shared
    comment describing multiple sibling keys at once; those get an
    explicit "tooltip" in the field spec instead, see SECTIONS below.
    """
    if not path:
        return None
    parent = get_in(cfg, path[:-1], {})
    if not isinstance(parent, dict):
        return None
    comment = parent.get(f"_{path[-1]}")
    return comment if isinstance(comment, str) else None


def lookup_section_comment(cfg, path):
    """
    Best-effort comment for a whole SECTION heading, as opposed to one
    field. The config files actually use two different conventions for
    this:
      1. an internal "_comment" key on the object living AT this path
         (orcastrator.json documents e.g. its "window"/"on_error"/
         "theme" sub-objects this way, from inside each one), or
      2. the usual sibling "_<key>" comment in the PARENT (dock_
         collision_guard.json's convention, used at every level,
         already handled by lookup_comment above).
    Tries both, in that order. Returns "" if neither exists.
    """
    if not path:
        return ""
    node = get_in(cfg, path, None)
    if isinstance(node, dict):
        joined = _joined_comment(node.get("_comment"))
        if joined:
            return joined
    return lookup_comment(cfg, path) or ""


def _merge_backup_onto_current(current, backup):
    """
    Restoring a backup should bring back the *values a person chose*,
    never the schema or the documentation that happened to ship with
    whatever version wrote that backup -- those two things drift over
    time as the app is updated, the backup's copies of them don't.

    `current` is always whatever's freshly on disk at self.cfg_path
    right now (i.e. whatever the currently-running version put there),
    which is the only thing in this app that's guaranteed to reflect
    the CURRENT schema and CURRENT wording -- there's no separate
    bundled "shipped defaults" file to diff against, so this doubles as
    that reference. `backup` is the old snapshot being restored.

    Recursively walks `current`, keyed off `current`'s own keys (never
    `backup`'s):
      - a "_"-prefixed key (this config's inline documentation
        convention, see lookup_comment()/lookup_section_comment() above)
        always comes from `current`, full stop -- `backup` is never even
        consulted for these, so a restore can never reintroduce stale
        wording.
      - a real key present in both: recurse if both sides are dicts
        (so unrelated sibling fields in the same section merge
        independently); otherwise `backup`'s value wins outright --
        this is the actual restored setting. A list (e.g. a template
        list, a point table) is a leaf here, not something to merge
        item-by-item -- `backup`'s whole list replaces `current`'s.
      - a real key only `current` has: a field added since this backup
        was taken. Keeps `current`'s (shipped default) value rather
        than leaving it out -- so the form and the saved file always
        agree on what's present, instead of a field showing its
        default in the UI but silently vanishing from disk on Save
        because nothing ever called set_in() for it.
      - a real key only `backup` has: a field removed/renamed since --
        dropped silently rather than reintroducing dead config.
    """
    if not isinstance(current, dict) or not isinstance(backup, dict):
        return backup
    merged = {}
    for key, cur_val in current.items():
        if key.startswith("_"):
            merged[key] = cur_val
        elif key in backup:
            merged[key] = _merge_backup_onto_current(cur_val, backup[key])
        else:
            merged[key] = cur_val
    return merged


def _section_common_path(fields):
    """
    The longest path prefix shared by every field in a section -- i.e.
    the deepest object all of them live inside, which is what a section
    heading's own tooltip should actually be describing. Fields that
    sit at different nesting depths (e.g. a lone top-level toggle
    grouped alongside a nested sub-object's fields) naturally shrink
    this down, possibly to nothing.
    """
    paths = [tuple(f["path"]) for f in fields if f.get("path")]
    if not paths:
        return ()
    common = paths[0]
    for p in paths[1:]:
        i = 0
        while i < len(common) and i < len(p) and common[i] == p[i]:
            i += 1
        common = common[:i]
        if not common:
            break
    return common


def _field_visible(cfg, spec, defaults=None):
    """
    Evaluates a field spec's optional "show_if": a list of
    (path, expected) pairs, ALL of which must hold for the field to be
    shown (AND). `expected` is either a single value to match exactly,
    or a list/tuple/set of acceptable values. No "show_if" at all just
    means always visible.

    `defaults` maps a referenced path to whatever that OTHER field's
    own widget would fall back to if the key's missing from cfg (see
    SettingsApp._remember_default) -- without it, a condition like
    `(("window", "remember_position"), False)` fails the moment that
    key doesn't exist yet in an on-disk config predating this field
    (get_in's own fallback is None, and None != False), hiding the
    dependent field until the referenced checkbox gets toggled once
    and actually writes a real value into cfg. Falls back to plain
    get_in(cfg, path) -- i.e. None if missing -- when no default is on
    record for that path, same as before.
    """
    conds = spec.get("show_if")
    if not conds:
        return True
    defaults = defaults or {}
    for path, expected in conds:
        val = get_in(cfg, path, defaults.get(tuple(path)))
        if isinstance(expected, (list, tuple, set)):
            if val not in expected:
                return False
        elif val != expected:
            return False
    return True


class Tooltip:
    """
    Small delayed hover tooltip. Attach to any widget; shows `text` in a
    borderless popup near the cursor after a short delay, Orca-skinned to
    match the rest of the window.

    `text` supports a couple of lightweight markup bits on top of plain
    strings -- deliberately minimal (just what config field descriptions
    actually need), not a general markdown renderer:
      - Literal "\\n" line breaks -- Tk just renders embedded newlines as-is.
      - "**bold**" spans -- rendered in a bold weight of the same font.
    Everything else (the text outside **markers**) renders in the normal
    tooltip font.
    """

    # Matches non-greedy so "**a** and **b**" is two bold spans, not one
    # bold span swallowing the "and" in between. DOTALL so a bold span is
    # allowed to itself contain a literal \n without breaking the match.
    _BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)

    def __init__(self, widget, text, wraplength=320, delay=450):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.delay = delay
        self._after_id = None
        self._tip = None
        if not text:
            return
        # Split into (chunk, is_bold) runs up front (once, not on every
        # hover) -- re.split with a single capturing group hands back
        # [before, bold_1, between, bold_2, ..., after], i.e. every ODD
        # index is text that was between ** markers.
        parts = self._BOLD_RE.split(text)
        self._segments = [(part, bool(i % 2)) for i, part in enumerate(parts) if part]
        # The Text widget this renders into needs its width (chars) and
        # height (lines) fixed BEFORE it's ever shown -- everything below
        # computes those analytically via font.measure() on the string
        # content alone. Deliberately NOT done by creating the real Text
        # widget early and asking Tk to report back its own rendered
        # geometry (dlineinfo/"displaylines") -- that requires the widget
        # to have already been mapped and painted by the window manager,
        # which an overrideredirect+topmost popup like this one doesn't
        # reliably guarantee by the time such a query runs (pack() and
        # update_idletasks() alone weren't enough -- confirmed the exact
        # failure mode: dlineinfo() silently returns None for every line
        # before the window's actually painted, collapsing the fit-width
        # math to its 1-column floor and wrapping almost every character
        # onto its own line). Measuring the STRING instead of the WIDGET
        # has no such timing dependency -- font.measure() only needs a Tk
        # application to exist, not a mapped, visible window.
        try:
            regular_font = tkfont.Font(family="Segoe UI", size=9)
            avg_px = regular_font.measure("0123456789") / 10
            line_count, max_px = self._simulate_wrap(text, wraplength, regular_font)
            self._fit_width = max(5, int(max_px / avg_px) + 2)  # +2 cols: bold glyphs run slightly
            self._line_count = max(line_count, 1)               # wider than this regular-font estimate
        except Exception:
            # Same "must never be able to stop the tooltip from showing"
            # rule as everywhere else sizing-related in this app -- worst
            # case here is just a wider-than-ideal box, never a missing
            # or truncated one.
            self._fit_width = max(10, wraplength // 7)
            self._line_count = max(1, text.count("\n") + 1)
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    @staticmethod
    def _simulate_wrap(text, wraplength, font):
        """
        Reproduces Tk's greedy word-wrap in pure Python: walks `text`
        word by word (space-separated, "**" markers already irrelevant
        here since this measures the RAW text -- a couple extra
        `font.measure()` calls on "**" pairs make no difference to which
        line a word lands on), starting a new line whenever the next word
        would push the current line past `wraplength` px, and always
        starting a fresh line on an explicit "\\n". Returns
        (line_count, max_line_px) -- the two numbers _show() needs to
        size the real widget with no further measurement required.
        """
        max_px = 0
        line_count = 0
        space_px = font.measure(" ")
        for paragraph in text.split("\n"):
            words = paragraph.split(" ")
            current = ""
            current_px = 0
            for word in words:
                word_px = font.measure(word)
                add_px = word_px if not current else word_px + space_px
                if current and current_px + add_px > wraplength:
                    max_px = max(max_px, current_px)
                    line_count += 1
                    current, current_px = word, word_px
                else:
                    current = f"{current} {word}" if current else word
                    current_px += add_px
            max_px = max(max_px, current_px)
            line_count += 1  # every paragraph (even an empty one, e.g. "\n\n") is >=1 line
        return line_count, max_px

    def _schedule(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except Exception:
            pass
        tw.wm_geometry(f"+{x}+{y}")
        border = tk.Frame(tw, bg=ORCA_ACCENT, padx=1, pady=1)
        border.pack()
        # width/height are both already fully resolved (in __init__, via
        # _simulate_wrap on the plain string) -- nothing below needs to
        # inspect this widget's own rendered geometry at all, so there's
        # no packing-order or mapped-before-measuring trap left to fall
        # into.
        body = tk.Text(border, wrap="word", width=self._fit_width, height=self._line_count,
                        bg=ORCA_PANEL_BG, fg=ORCA_FG, font=("Segoe UI", 9), padx=8, pady=6,
                        borderwidth=0, highlightthickness=0, cursor="arrow", relief="flat", takefocus=0)
        body.tag_configure("bold", font=("Segoe UI", 9, "bold"))
        for chunk, is_bold in self._segments:
            body.insert("end", chunk, ("bold",) if is_bold else ())
        body.configure(state="disabled")
        body.pack()

    def _hide(self, event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ---------------------------------------------------------------------------
# Every config's field specs -- dock_collision_guard.json included as of
# this split -- live in their own file in gui/, discovered automatically
# below (discover_gui_specs()). Same for the live SVG preview (see
# HAS_PREVIEW/PREVIEW_CONTROLS/build_preview_payload in that module, and
# _build_preview_panel()/refresh_preview() below for the generic engine
# side). What still makes dock_collision_guard.json a "rich" entry
# rather than "simple" is purely HAS_PREVIEW -- see CLAUDE.md for the
# "rich" vs "simple" distinction. Its Dock Boundary table is a plain
# "point_table" field now, same as every other field kind.
# ---------------------------------------------------------------------------

def _debug_section_for(cfg):
    """
    Auto-builds a standard "Debug" GUI section for ANY config with a
    top-level "debug": {"enabled": ..., ...} block -- the shape every
    processor that opts into helpers/debug_dump.py's shared debug-dump
    feature uses (see that module's docstring). A processor gets an
    editable Debug section for free just by adding that block to its
    own config -- zero GUI code of its own required, matching the same
    "convention, not configuration" spirit as gui/*.py auto-discovery
    itself. Returns None for any config without that shape, so a
    processor that hasn't opted in still has no Debug section at all.

    Deliberately keyed on "enabled" specifically, not just the presence
    of a "debug" key -- so this doesn't collide with orcastrator.json's
    OWN "debug": {"dir": ...} block, which is the different, central
    setting (see gui/orcastrator.py's manually-authored "Debug" section)
    rather than a per-processor on/off switch.
    """
    debug_cfg = cfg.get("debug") if isinstance(cfg, dict) else None
    if not isinstance(debug_cfg, dict) or "enabled" not in debug_cfg:
        return None
    fields = [
        dict(kind="bool", label="Write debug dump", path=("debug", "enabled")),
    ]
    return ("Debug", fields, "Writes a JSON snapshot of this processor's last run for "
                              "troubleshooting -- browsable read-only from the \"Debug Logs\" card "
                              "on the settings landing page. Always lands in the central Debug Log "
                              "Directory from OrcaStrator Settings, or next to this processor's own "
                              "script if that's empty -- same location every opted-in processor uses.")


def _notice_section_for(cfg):
    """
    _debug_section_for()'s sibling: auto-builds a standard "Notices"
    GUI section for ANY config with a top-level "notice": {"display":
    ...} block -- the shape every processor that opts into
    helpers/notice.py's shared display-gate uses (see that module's
    docstring). Same "add the block, get the section for free" deal --
    zero gui/*.py code of its own required.

    Deliberately keyed on "display" specifically, not just the presence
    of a "notice" key, for the same collision-avoidance reason
    _debug_section_for() keys on "enabled" -- keeps this from ever
    accidentally matching some unrelated future "notice" block that
    isn't shaped like this one.

    gcode_template_notice.py deliberately has NO "notice" block of its
    own -- it already has a per-template "destinations" control (see
    that processor's own config), which is a finer-grained version of
    the same idea, so this generic processor-level toggle would be
    redundant on top of it.
    """
    notice_cfg = cfg.get("notice") if isinstance(cfg, dict) else None
    if not isinstance(notice_cfg, dict) or "display" not in notice_cfg:
        return None
    fields = [
        dict(kind="bool", label="Show in Klipper console", path=("notice", "display")),
    ]
    return ("Notices", fields, "Whether this processor's own info/warning NOTICE messages show "
                                "up on the printer console (via ORCASTRATOR_RENDER). The notice "
                                "is still always written into the g-code either way -- this only "
                                "tells the printer-side macro whether to print it, so nothing is "
                                "lost from the file or from a Debug dump if that's on, just muted "
                                "on the console. Handy for decluttering the console around a "
                                "gcode_template_notice.py template you actually want to stand out. "
                                "A genuine abort/refuse-to-print notice, if this processor ever "
                                "emits one, is never muted by this setting.")


GUI_DIR = HERE / "gui"

# Landing-page display order for discovered gui/*.py specs -- same
# purpose and pattern as orcastrator.py's own EXPLICIT_ORDER for run
# order: anything listed here sorts first (in this order), anything
# found but not listed here follows alphabetically after. Purely
# cosmetic; a new processor's gui/*.py works fine with no entry here,
# it'll just land at the end of the list.
#
# This is only the FALLBACK default now -- the actual order shown is
# whatever's saved as "gui_order" in configs/orcastrator.json (editable
# from OrcaStrator Settings -> Settings Landing Page, same "pick from
# what's on disk" picker as explicit_order/denylist), read fresh by
# load_gui_order() below every time the landing page is built. A config
# with no "gui_order" saved yet (or an invalid one) falls back to this
# constant, so nothing changes for anyone who's never touched the new
# setting.
GUI_EXPLICIT_ORDER = [
    "dock_collision_guard.py",
    "tool_preheat.py",
    "insert_missing_tool_preheat.py",
    "disable_unused_tool_temps.py",
    "restore_pos_fix.py",
]


def load_gui_order() -> list:
    """
    Persisted override for GUI_EXPLICIT_ORDER, read straight from
    configs/orcastrator.json's "gui_order" key. Deliberately its own
    small reader rather than going through orcastrator.py's
    load_orcastrator_config() -- this is purely a config_editor.pyw
    display concern (which card comes first on ITS OWN landing page),
    orcastrator.py's pipeline never looks at this key at all. Same
    tolerance as everywhere else in this codebase: file missing/
    unreadable/malformed, key missing, or not a list of strings all
    just fall back to the hardcoded GUI_EXPLICIT_ORDER default above
    rather than erroring.

    An empty list, though, is NOT treated as invalid -- it's exactly
    what's saved when the "Settings Landing Page" picker has had every
    item removed from it, and legitimately means "no explicit order for
    the other pages, alphabetical is fine" (discover_gui_specs() already
    falls back to alphabetical for anything not named in `order`). Only
    a genuinely missing/malformed key falls back to GUI_EXPLICIT_ORDER.
    GUI_EXPLICIT_ORDER itself deliberately excludes "orcastrator.py" --
    build_config_registry() below always prepends that name to whatever
    this function returns, so including it here too would double it up
    into a duplicate landing-page card whenever this fallback fires.
    """
    try:
        raw = json.loads((CONFIGS_DIR / "orcastrator.json").read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    order = raw.get("gui_order") if isinstance(raw, dict) else None
    if isinstance(order, list) and all(isinstance(n, str) for n in order):
        return order
    return list(GUI_EXPLICIT_ORDER)


def discover_gui_specs(order=None) -> list:
    """
    Mirrors orcastrator.py's own discover_processors(): every *.py in
    gui/ is one processor's own settings-form spec (title/subtitle/
    kind/sections -- see gui/orcastrator.py for the convention), loaded
    by file path and turned into a CONFIG_REGISTRY-shaped dict. A new
    processor's form needs zero edits to this file -- just a new file
    living next to the others, not a code change scattered into
    config_editor.py itself.

    A module's shape:
      TITLE, SUBTITLE   -- shown on the landing page
      CONFIG            -- filename in configs/ this form edits (omit
                            for a KIND="none" entry with nothing to load)
      SECTIONS          -- the field-spec list; presence of this implies
                            KIND="simple" unless KIND is set explicitly
      KIND              -- optional override; inferred as "simple" if
                            SECTIONS is present, else "none". A module can
                            set KIND="rich" AND still provide SECTIONS
                            (gui/dock_collision_guard.py does this) --
                            those fields render through the exact same
                            generic path a "simple" config's do, the
                            "rich" editor just also adds a live preview
                            around them (if HAS_PREVIEW says to).
      PREVIEW           -- optional, e.g. "theme" (see orcastrator's) --
                            unrelated to HAS_PREVIEW below despite the
                            similar name; this is the older, simpler
                            "preview a couple of theme colors" hook a
                            "simple" config can use, not a live SVG
                            preview.
      INFO              -- shown for a KIND="none" entry
      HAS_PREVIEW       -- optional, "rich" configs only. True if this
                            module implements the generic live-SVG-
                            preview contract (see gui/dock_collision_
                            guard.py for the full convention this mirrors
                            in every other processor's PREVIEW):
      PREVIEW_CONTROLS  -- declarative list of preview-only controls
                            (NOT persisted to the config) -- see
                            _build_preview_controls() below for the
                            supported control kinds
      build_preview_payload(cfg, controls) -- function, cfg + a plain
                            {control_var: value} dict in, an
                            SVG_PAYLOAD-shaped dict out, fed straight
                            into orcastrator.py's own already-generic
                            canvas renderer by refresh_preview() below
    """
    if order is None:
        order = GUI_EXPLICIT_ORDER
    entries = []
    if not GUI_DIR.is_dir():
        return entries

    found = sorted(p for p in GUI_DIR.glob("*.py") if p.is_file() and not p.name.startswith("_"))
    ordered = [p for name in order for p in found if p.name == name]
    remainder = [p for p in found if p.name not in order]

    for path in ordered + remainder:
        try:
            mod = _load_module(f"orcastrator_gui_{path.stem}", str(path))
        except Exception as e:
            print(f"[config_editor] failed to load gui/{path.name}: {e}", file=sys.stderr)
            continue

        sections = getattr(mod, "SECTIONS", None)
        kind = getattr(mod, "KIND", "simple" if sections is not None else "none")
        config_name = getattr(mod, "CONFIG", None)

        entry = dict(
            id=path.stem, title=getattr(mod, "TITLE", path.stem),
            subtitle=getattr(mod, "SUBTITLE", ""), kind=kind,
            path=(CONFIGS_DIR / config_name) if config_name else None,
        )
        if sections is not None:
            entry["sections"] = sections
        preview = getattr(mod, "PREVIEW", None)
        if preview:
            entry["preview"] = preview
        if getattr(mod, "HAS_PREVIEW", False):
            entry["has_preview"] = True
            entry["preview_controls"] = getattr(mod, "PREVIEW_CONTROLS", [])
            entry["build_preview_payload"] = getattr(mod, "build_preview_payload", None)
        # Optional per-config override of the settings column's minimum
        # width -- open_rich_editor()'s default (440px) is
        # sized for a "rich" config with short numeric fields (dock_
        # collision_guard's own); one with genuinely wide content (a
        # multiline template editor, say) can ask for more room up front.
        entry["settings_min_width"] = getattr(mod, "SETTINGS_MIN_WIDTH", 440)
        if kind == "none":
            entry["info"] = getattr(mod, "INFO", "")
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# The landing page's list of known configs. "kind" drives which editor
# opens: "rich" = the full settings+live-preview editor above, "simple"
# = a generic form using the sections list, "none" = an informational
# screen for a processor with nothing to configure. Any other *.json
# found on disk that ISN'T listed here (by path) still shows up on the
# landing page automatically, editable as raw JSON text -- see
# SettingsApp.show_landing().
#
# Every entry here -- dock_collision_guard.json included -- now comes
# from gui/*.py via discover_gui_specs() above; nothing is hand-appended
# to CONFIG_REGISTRY anymore. dock_collision_guard.json still opens the
# "rich" editor (its live SVG preview, see open_rich_editor()
# below) because gui/dock_collision_guard.py sets KIND="rich" -- but its
# title/subtitle/path/
# sections all come from that file now, same mechanism as every other
# config, just with a different KIND. Landing-page position is handled
# by GUI_EXPLICIT_ORDER, same as any other gui/*.py file, rather than
# a special hardcoded slot.
#
# Landing-page ORDER (see show_landing()): OrcaStrator itself first,
# then every processor, then any unconfigured *.json found on disk that
# isn't claimed by an entry here, then Debug Logs last -- it's not a
# processor's config at all, just a read-only log browser, so it
# belongs after everything that actually configures something.
# DEBUG_LOGS_ENTRY is kept OUT of CONFIG_REGISTRY entirely rather than
# just appended last here, because show_landing() appends the
# unconfigured-json entries after CONFIG_REGISTRY too, and those need
# to land before Debug Logs, not after it.
# ---------------------------------------------------------------------------

def build_config_registry() -> list:
    """
    Rebuilds the landing page's config list fresh from disk -- both
    which gui/*.py pages exist AND what order they're in (via
    load_gui_order()) -- rather than computing it once at import time.
    Cheap enough (a handful of small files) to call this every time
    show_landing() runs, which is what makes a saved "gui_order" change
    show up the moment you're back on the landing page, no app restart
    needed. OrcaStrator's own card is always pinned first regardless of
    gui_order -- see GUI_EXPLICIT_ORDER's/load_gui_order()'s comments --
    so it's pulled out and re-prepended here the same way the old
    module-level split used to.
    """
    gui_specs = discover_gui_specs(order=["orcastrator.py"] + load_gui_order())
    orcastrator_spec = [e for e in gui_specs if e["id"] == "orcastrator"]
    other_specs = [e for e in gui_specs if e["id"] != "orcastrator"]
    return orcastrator_spec + other_specs


CONFIG_REGISTRY = build_config_registry()

DEBUG_LOGS_ENTRY = dict(id="debug_logs", title="Debug Logs", kind="logviewer",
                         subtitle="Browse *_debug.json dumps from any processor with debug logging on -- read-only.",
                         path=None)



class SettingsApp:
    def __init__(self, start_path: pathlib.Path = None):
        self.root = tk.Tk()
        self.root.title("OrcaStrator Settings")

        # Always-remember, no config toggle (unlike the progress
        # window's opt-in remember_position) -- restores the FULL last
        # geometry, not just position, since this window (unlike the
        # progress window) is freely resizable throughout its life and
        # a remembered width is a real preference worth keeping, not
        # just something auto-computed per screen. Falls back to the
        # original fixed default whenever nothing's saved yet, the
        # anchor module didn't load, or the saved geometry is stale
        # (see gui/_window_anchor.py's restore_geometry()).
        restored = None
        if _window_anchor is not None:
            try:
                restored = _window_anchor.restore_geometry("config_editor")
            except Exception:
                restored = None
        if restored is not None:
            rx, ry, rw, rh = restored
            self.root.geometry(f"{rw}x{rh}+{rx}+{ry}")
        else:
            self.root.geometry("1180x780")

        self.root.configure(bg=ORCA_BG)
        self.root.minsize(700, 500)
        self._set_app_icon()
        orcastrator.apply_dark_titlebar(self.root, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)

        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self._style_ttk()

        # Per-config state -- populated whenever an editor screen is open,
        # cleared (cfg_path/cfg = None) while on the landing page.
        self.cfg_path = None
        self.cfg = None
        self.dirty = False
        self.file_label = None
        # {source_name: [message, ...]} -- non-empty list for any key blocks
        # save(). A field that validates its own data (a "point_table"
        # kind, e.g. dock collision's boundary) writes its own entry
        # here; cleared out on open/reload.
        self.validation_errors = {}
        self.reload_btn = None
        self.save_btn = None
        self._tp_frame = None  # theme-preview mockup, only built for the OrcaStrator config screen
        self._refresh_hook = lambda: None   # rich editor overrides this with refresh_preview
        # Fields/sections whose visibility depends on another field's
        # current value (spec's optional "show_if") -- rebuilt fresh by
        # whichever screen is currently showing, see _register_visibility.
        self._field_vis_rules = []
        self._field_defaults = {}
        self._section_vis_rules = []
        # Declared top-to-bottom order of every section frame on the
        # current screen, filled in the same loop that packs them --
        # lets a hidden-then-reshown section go back to its actual
        # spot (see _pack_section_in_order) instead of wherever plain
        # pack() would drop it.
        self._section_frame_order = []
        # {group_key: {"shared_left_list": Listbox, "shared_left_frame": Frame,
        #              "min_row"/"max_row": int, "members": [{"current", "refresh_right"}, ...],
        #              "refresh_group": callback}}
        # -- every processor_denylist/processor_order/gui_order picker on
        # the current screen registers itself here (see _add_field). All
        # pickers sharing the same item universe (e.g. "Runs first"/
        # "Runs last"/"Denylist", all drawing from post_processors/*.py)
        # point at ONE shared "Discovered..." listbox instead of each
        # showing an identical copy of it, and picking a name into any of
        # them removes it from that shared list for the others too.
        # Rebuilt fresh per screen, same as the visibility rules above.
        self._picker_groups = {}
        self._section_field_labels = []
        # Frames that opt OUT of _align_field_columns's page-wide column
        # alignment -- currently just whichever section(s) hold a
        # processor_order/processor_denylist/gui_order picker (see
        # _add_field), added there rather than here since membership
        # depends on what fields actually land in each frame.
        self._align_exempt_frames = set()
        self._reopen = lambda: self.show_landing(_skip_confirm=True)  # what "Reload" re-invokes

        # Which Spinbox/Combobox (if any) currently owns the mousewheel --
        # set by _guard_wheel_until_clicked on focus/click, released by
        # an outside click, Enter, or Escape. See _on_wheel_fallback and
        # _on_global_click for how this is used.
        self._active_scroll_field = None
        self._active_scroll_snapshot = None

        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True, padx=10, pady=10)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Bound once, globally, for the app's lifetime -- see
        # _on_wheel_fallback for why this replaced per-canvas
        # Enter/Leave-based bind_all toggling.
        self.root.bind_all("<MouseWheel>", self._on_wheel_fallback)
        # Releases a locked field on a click anywhere outside it -- see
        # _on_global_click for why this (rather than <FocusOut>) is what
        # actually releases the lock.
        self.root.bind_all("<Button-1>", self._on_global_click, add="+")

        if start_path is not None:
            self._open_for_path(start_path)
        else:
            self.show_landing()

    # -- styling ------------------------------------------------------

    def _set_app_icon(self):
        """
        Swaps out Tk's stock feather-logo icon for our own. Currently
        just a plain opaque white 32x32 placeholder in assets/icon.png
        -- drop a real icon in over it (same filename, any size Tk's
        PhotoImage can load: PNG/GIF/PPM) whenever there's actual
        artwork, no code changes needed.

        A transparent icon renders as a solid black square in the
        Windows titlebar -- Tk's PhotoImage-to-HICON conversion there
        doesn't honor alpha, so a transparent pixel's underlying RGB
        (black) is what actually shows. Opaque white sidesteps that
        regardless of how alpha gets handled, so keep any replacement
        artwork opaque too.

        iconphoto(True, ...) sets it as the *default* for every
        Toplevel this interpreter creates from here on (e.g. the Load
        Backup dialog), not just the root window, so this only needs
        doing once. Missing file or a Tk build without PNG support
        (pre-8.6) just means we keep Tk's own icon -- not worth
        failing the whole app over.
        """
        icon_path = HERE / "assets" / "icon.png"
        try:
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._app_icon)
        except Exception:
            pass

    def _style_ttk(self):
        s = self.style
        s.configure(".", background=ORCA_BG, foreground=ORCA_FG)
        s.configure("TFrame", background=ORCA_BG)
        s.configure("TLabel", background=ORCA_BG, foreground=ORCA_FG)
        s.configure("Hint.TLabel", background=ORCA_BG, foreground=ORCA_FG_DIM, font=("Segoe UI", 8))
        # Bigger/bolder than Hint.TLabel and full-brightness foreground
        # (not dimmed) -- used above each listbox in the processor_order/
        # processor_denylist/gui_order dual-list picker, where it's the
        # ONLY label identifying what that particular list is (see
        # _add_field: those field kinds skip the normal row label
        # entirely, since three side-by-side pickers each repeating
        # "Runs first"/"Runs last"/"Denylist" down the left edge was
        # redundant with "Order (runs first...)"/"Denylisted (...)"
        # already saying the same thing, just less legibly).
        s.configure("PickerHeader.TLabel", background=ORCA_BG, foreground=ORCA_FG, font=("Segoe UI", 10, "bold"))
        s.configure("TCheckbutton", background=ORCA_BG, foreground=ORCA_FG)
        s.map("TCheckbutton", background=[("active", ORCA_BG)])
        s.configure("TRadiobutton", background=ORCA_BG, foreground=ORCA_FG)
        s.map("TRadiobutton", background=[("active", ORCA_BG)])
        s.configure("TCombobox", fieldbackground=ORCA_PANEL_BG, background=ORCA_PANEL_BG,
                    foreground=ORCA_FG, arrowcolor=ORCA_FG)
        s.map("TCombobox", fieldbackground=[("readonly", ORCA_PANEL_BG)], foreground=[("readonly", ORCA_FG)])
        s.configure("TSpinbox", fieldbackground=ORCA_PANEL_BG, background=ORCA_PANEL_BG,
                    foreground=ORCA_FG, arrowcolor=ORCA_FG)
        s.configure("TEntry", fieldbackground=ORCA_PANEL_BG, foreground=ORCA_FG, insertcolor=ORCA_FG)
        s.configure("Invalid.TEntry", fieldbackground=ORCA_PANEL_BG, foreground=ERROR_COLOR, insertcolor=ORCA_FG)
        s.configure("TLabelframe", background=ORCA_BG, bordercolor=ORCA_BORDER)
        s.configure("TLabelframe.Label", background=ORCA_BG, foreground=ORCA_ACCENT, font=("Segoe UI", 9, "bold"))
        s.configure("TButton", background=ORCA_BORDER, foreground=ORCA_FG, borderwidth=0)
        s.map("TButton", background=[("active", ORCA_ACCENT_HOVER)])
        s.configure("Accent.TButton", background=ORCA_ACCENT, foreground=ORCA_ACCENT_FG, borderwidth=0)
        s.map("Accent.TButton", background=[("disabled", ORCA_BORDER), ("active", ORCA_ACCENT_HOVER)],
              foreground=[("disabled", ORCA_FG_DIM)])
        # Flat, slim scrollbars in the spirit of OrcaSlicer's own UI.
        # A custom ttk layout that drops the up/down (or left/right)
        # arrow buttons for a fully flat look is tempting, but a custom
        # ttk layout re-specifies which elements exist, and that breaks
        # click-to-page and thumb dragging (the arrow-less thumb stops
        # responding to mouse input in the clam theme). Restyling the
        # native layout instead
        # -- same trough/arrow/thumb elements Tk already knows how to
        # hit-test and drag -- keeps everything clickable while still
        # looking flat and slim via colors/width alone.
        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            s.configure(name, troughcolor=ORCA_BG, background=ORCA_BORDER, bordercolor=ORCA_BG,
                        arrowcolor=ORCA_FG, relief="flat", width=12, arrowsize=12)
            s.map(name, background=[("active", ORCA_ACCENT)],
                  arrowcolor=[("active", ORCA_ACCENT_FG)])


    def _make_scroll_area(self, parent, vertical=True, horizontal=False, bg=None, fit_width=False,
                            stretch_inner=False):
        """
        Build a themed, auto-hiding scroll area: a Canvas embedding an
        inner ttk.Frame, wired up with mousewheel scrolling and the thin
        Orca-style scrollbar(s) above. The scrollbar(s) only appear once
        content actually overflows the viewport -- a scrollbar with
        nothing to scroll is just visual noise, and OrcaSlicer's own
        panels don't show one until it's needed either.

        fit_width=True makes the canvas (and its scrollbar) hug the
        actual width of `inner`'s content instead of stretching to
        whatever width the caller's layout hands `outer` -- for a column
        that's deliberately given a generous FIXED width up front to
        avoid clipping its widest possible field (see open_simple_editor's
        theme-preview branch, minsize=860 for exactly this reason),
        stretching without fit_width would drag the scrollbar far out
        past the actual content, next to whatever sits in the NEXT
        column over, rather than sitting right beside what it's
        actually scrolling. Only
        meant for a caller that also switches its own outer.pack() from
        fill="both" to fill="y" (still expand=True) -- fit_width leaves
        horizontal space unclaimed on purpose, so a "both" pack would
        just have pack stretch it right back out.

        stretch_inner makes `inner` track the canvas's actual on-screen
        width instead of sitting at whatever width its own content
        naturally requests -- the fix for a recurring problem where a
        settings column's width was effectively fixed by its widest
        child's natural size (a Text box's default width, say), with any
        extra window width just becoming unused blank canvas rather than
        space the content could actually grow into. Only meaningful
        combined with fill="x"/expand packing inside `inner` -- this
        makes the ROOM available, the content still has to ask to fill
        it. Mutually exclusive with fit_width in practice (one shrinks
        the canvas to inner, the other grows inner to the canvas); don't
        pass both True.

        Returns (outer, canvas, inner). `outer` is what the caller
        grids/packs into their own layout; `inner` is where the caller
        should pack/grid its actual content.
        """
        bg = bg or ORCA_BG
        outer = ttk.Frame(parent)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=0 if fit_width else 1)

        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                             style="Vertical.TScrollbar") if vertical else None
        hsb = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview,
                             style="Horizontal.TScrollbar") if horizontal else None

        inner = ttk.Frame(canvas)
        inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        if vertical:
            canvas.configure(yscrollcommand=vsb.set)
        if horizontal:
            canvas.configure(xscrollcommand=hsb.set)

        def _sync_stretch_width(_evt=None):
            # Keep `inner`'s width pinned to the canvas's actual current
            # width (never its own natural request) -- resizing the
            # window from here on genuinely resizes the content area,
            # rather than just revealing more blank canvas beside it.
            canvas.itemconfig(inner_window, width=max(1, canvas.winfo_width()))

        if stretch_inner:
            canvas.bind("<Configure>", _sync_stretch_width, add="+")

        state = {"overflow_y": False, "overflow_x": False}

        def _sync_visibility(_evt=None):
            bbox = canvas.bbox("all")
            if not bbox:
                return
            if fit_width:
                # Match inner's own natural width exactly -- this is
                # what actually pulls the scrollbar in flush against
                # the content instead of the far edge of whatever width
                # `outer` was handed. winfo_reqwidth() (not the bbox,
                # which is canvas-coordinate and shifts with scrolling)
                # is `inner`'s true requested width regardless of
                # current scroll position.
                canvas.configure(width=max(1, inner.winfo_reqwidth()))
            if vertical:
                overflow_y = (bbox[3] - bbox[1]) > canvas.winfo_height()
                state["overflow_y"] = overflow_y
                mapped = bool(vsb.winfo_ismapped())
                if overflow_y and not mapped:
                    vsb.grid(row=0, column=1, sticky="ns")
                elif not overflow_y and mapped:
                    vsb.grid_remove()
                if not overflow_y:
                    # Content fits -- pin the view to the top rather than
                    # leaving it wherever a prior scroll (or a resize
                    # that shrank the content below the old scroll
                    # position) left it, so nothing can be shown
                    # half-scrolled with no scrollbar visible to explain why.
                    canvas.yview_moveto(0)
            if horizontal:
                overflow_x = (bbox[2] - bbox[0]) > canvas.winfo_width()
                state["overflow_x"] = overflow_x
                mapped = bool(hsb.winfo_ismapped())
                if overflow_x and not mapped:
                    hsb.grid(row=1, column=0, sticky="ew")
                elif not overflow_x and mapped:
                    hsb.grid_remove()
                if not overflow_x:
                    canvas.xview_moveto(0)

        def _sync_scrollregion(_evt=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            _sync_visibility()

        inner.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_visibility, add="+")

        def _wheel(event):
            # Nothing to scroll -> do nothing, rather than letting the
            # canvas drift into blank space beyond its own content just
            # because a scrollregion happened to be a few pixels larger
            # than the viewport. This is what "no scrollbar visible"
            # should actually mean.
            if not state["overflow_y"]:
                return
            # Defensive: the app-wide dispatcher (_on_wheel_fallback)
            # resolves this canvas fresh from whatever's under the
            # pointer on every event, so a destroyed canvas shouldn't
            # normally be reachable here at all -- but if it ever is,
            # just swallow the error. unbind_all is application-global,
            # not scoped to this canvas, so calling it here would take
            # out the one shared dispatcher for every scroll area in
            # the app rather than just this stale one.
            try:
                canvas.yview_scroll(-1 * (event.delta // 120 or (1 if event.delta > 0 else -1)), "units")
            except tk.TclError:
                pass
        # A tempting alternative is toggling bind_all on Enter/Leave to
        # scope scrolling to whichever canvas the pointer is actually
        # over (with more than one scroll area on screen, a plain
        # bind_all would let whichever was built LAST permanently steal
        # every scroll event). But Enter/Leave doesn't fire reliably
        # once the pointer is over one
        # of the canvas's own embedded child widgets (basically anything
        # in `inner`) -- from the canvas's point of view the child is a
        # sibling-like window, so entering it looks like leaving the
        # canvas entirely, and scrolling silently stops almost anywhere
        # over the actual content. Exposing the handler here instead lets
        # _on_wheel_fallback -- one dispatcher, bound once, globally --
        # resolve the correct canvas per-event by walking up from
        # whatever's actually under the pointer, which works the same
        # whether that's the canvas's bare background or something deep
        # inside `inner`.
        canvas._wheel_handler = _wheel

        return outer, canvas, inner

    def _labelframe(self, parent, title, tooltip_text=""):
        """
        A drop-in replacement for ttk.Labelframe(parent, text=title)
        that also lets the heading itself carry a hover tooltip. ttk's
        built-in -text option draws the label via an internal sub-
        element with no reliable, theme-independent way to hover-detect
        just that text; using -labelwidget with OUR OWN Label sidesteps
        that entirely (and renders identically -- labelwidget is what
        ttk uses to draw the border's notch either way, whether it's
        one it created implicitly or one handed to it).
        """
        frame = ttk.Labelframe(parent)
        lbl = ttk.Label(frame, text=title, style="TLabelframe.Label")
        frame.configure(labelwidget=lbl)
        if tooltip_text:
            Tooltip(lbl, tooltip_text)
        return frame

    def _section_tooltip(self, fields, override=None):
        """
        Resolves a section heading's tooltip: `override` is either
        literal text (used as-is), a path tuple (looked up directly),
        or None (auto -- uses the deepest object common to every field
        in the section). Empty/not-found all just mean no tooltip.
        """
        if isinstance(override, str):
            return override
        path = override if isinstance(override, tuple) else _section_common_path(fields)
        return lookup_section_comment(self.cfg, path)

    # -- navigation -------------------------------------------------------

    def _autosize_to_scroll_content(self, inner, outer=None, scale=1.0):
        """
        Grows/shrinks the window's height to fit `inner`'s actual
        natural content height (on top of the toolbar/title chrome
        already above it), capped to the screen -- so a normal-length
        screen doesn't need scrolling, and switching to a shorter
        screen doesn't leave a slab of empty space left over from
        whatever was open before. The scrollbar from _make_scroll_area
        is still there as the fallback for anything that doesn't fit
        even at full screen height.

        `inner` isn't itself managed by pack/grid -- it's placed on its
        Canvas via create_window -- so it's the one widget in this
        chain whose winfo_reqheight() actually reflects what its
        children need. Everything above it (the canvas, `outer`,
        `body`) just reports whatever height it's currently allocated,
        not what the scrollable content actually wants, which is why
        this reads `inner` directly rather than e.g. self.root's own
        reqheight for the scrollable portion.

        Pass `outer` (the Frame _make_scroll_area returned alongside
        `inner`) when available: self.root's own reqheight already
        includes that scroll area's own small baseline request (a bare
        Tk Canvas asks for a non-zero default size even with nothing
        drawn on it), and adding `inner`'s content height on top of an
        already-counted baseline over-grows the window by that same
        baseline amount every time. Subtracting it back out is what
        keeps this from leaving unused space on a short screen.

        Only ever changes height, not width -- the window keeps its
        current width (including any manual resize the person already
        did). Position now follows gui/_window_anchor.py: grows
        straight down from the window's current top edge by default,
        only flipping to a bottom anchor if there's not enough room
        below on the CURRENT monitor -- same behavior as, and sharing
        the same module as, orcastrator.py's progress window resize
        (_finalize_svg_size). Falls back to the previous "leave x/y
        untouched, just clamp height to the primary screen" behavior
        if _window_anchor couldn't be loaded at startup.

        `scale` inflates just the content portion (not the toolbar/
        title chrome above it) before the screen-height cap is
        applied -- e.g. 1.5 asks for 50% more room for `inner`'s own
        content, still shrinking back down to fit a small monitor the
        same as scale=1.0 always has. Per-screen opt-in only (default
        1.0, unchanged) -- see the "template_list" field kind's own
        call for the one screen that currently asks for more.
        """
        self.root.update_idletasks()
        content_h = int(inner.winfo_reqheight() * scale)
        chrome_h = self.root.winfo_reqheight()  # toolbar/title + the scroll area's own baseline request
        if outer is not None:
            chrome_h -= outer.winfo_reqheight()

        cur_w = self.root.winfo_width() or 1180
        cur_x, cur_y = self.root.winfo_x(), self.root.winfo_y()
        cur_h = self.root.winfo_height()

        # Prefer the actual per-monitor work area (taskbar excluded)
        # over the primary screen's raw winfo_screenheight() -- same
        # upgrade _usable_screen_height() already made for the progress
        # window. Only available when _window_anchor loaded and we're
        # on Windows; None falls through to the plain raw-screen figure.
        monitor = _window_anchor.get_monitor_work_area(cur_x + cur_w // 2, cur_y + cur_h // 2) \
            if _window_anchor is not None else None
        screen_h = (monitor[3] - monitor[1]) if monitor is not None else self.root.winfo_screenheight()

        target_h = min(chrome_h + content_h + 40, screen_h - 80)
        target_h = max(target_h, 500)  # matches self.root.minsize()
        target_h = int(target_h)

        if _window_anchor is not None:
            try:
                new_x, new_y = _window_anchor.resize_toward_center(cur_x, cur_y, cur_w, cur_h, cur_w, target_h)
            except Exception:
                new_x, new_y = cur_x, cur_y
        else:
            new_x, new_y = cur_x, cur_y

        self.root.geometry(f"{cur_w}x{target_h}+{new_x}+{new_y}")

    def _clear_container(self):
        for child in self.container.winfo_children():
            child.destroy()

    def _revert_live_titlebar(self):
        """Snaps THIS window's own titlebar color back to whatever's
        actually saved on disk. _refresh_theme_preview() pushes titlebar
        edits straight onto self.root live as you type (see its own
        docstring for why that one field is safe to do that with) -- so
        unlike every other field, an unsaved titlebar edit lives outside
        self.cfg entirely, as a change already applied to the real OS
        window, and discarding the rest of a dirty config back to
        self.cfg's last-loaded state doesn't touch it. Called wherever
        unsaved changes get discarded (see _confirm_leave's "No"
        branch), so the titlebar goes back to matching what's on disk
        the same as everything else does. Cheap enough to just always
        call on every discard, whether or not the config being left was
        even orcastrator.json -- reads the same on-disk source of truth
        apply_dark_titlebar's other callers already use, so a discard
        anywhere in the app leaves the titlebar exactly where a fresh
        launch would."""
        try:
            theme = orcastrator.load_orcastrator_config()["theme"]
            orcastrator.apply_dark_titlebar(self.root, caption_hex=theme["titlebar"],
                                             text_hex=theme["titlebar_fg"])
        except Exception:
            pass

    def _confirm_leave(self) -> bool:
        """Guards any navigation away from a dirty editor. True = go ahead."""
        if not self.dirty:
            return True
        resp = self._askyesnocancel("Unsaved changes", "Save changes before leaving this config?")
        if resp is None:
            return False
        if resp:
            self.save()
            if self.dirty:
                # save() refused (validation errors) and already told the
                # user why -- don't also discard their edits by leaving.
                return False
        else:
            self._revert_live_titlebar()
        return True

    def _open_for_path(self, path: pathlib.Path):
        """Used for the `python config_editor.py <path>` CLI form -- opens
        whichever editor kind matches a known registry entry, or falls
        back to the raw JSON editor for an unrecognized path."""
        path = path.resolve()
        for entry in CONFIG_REGISTRY:
            if entry.get("path") and entry["path"].resolve() == path:
                self._open_entry(entry)
                return
        self.open_raw_editor(path, path.name)

    def _open_entry(self, entry: dict):
        kind = entry["kind"]
        if entry.get("path") is not None and not entry["path"].exists():
            self._error(
                "Config not found",
                f"{entry['title']}: expected to find\n{entry['path']}\nbut it's not there.")
            return
        if kind == "rich":
            self.open_rich_editor(entry)
        elif kind == "simple":
            self.open_simple_editor(entry)
        elif kind == "raw":
            self.open_raw_editor(entry["path"], entry["title"])
        elif kind == "none":
            self.open_info_view(entry)
        elif kind == "logviewer":
            self.open_debug_log_viewer(entry)

    def show_landing(self, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        # Rebuilt fresh every time rather than reusing the module-level
        # default -- picks up a "gui_order" change saved from the
        # OrcaStrator settings screen (Settings Landing Page section)
        # immediately, since this always runs on the way back here
        # (e.g. via _reopen after Save) without needing an app restart.
        global CONFIG_REGISTRY
        CONFIG_REGISTRY = build_config_registry()
        self.cfg_path = None
        self.cfg = None
        self.dirty = False
        self._reopen = lambda: self.show_landing(_skip_confirm=True)
        self._clear_container()
        self.root.title("OrcaStrator Settings")

        ttk.Label(self.container, text="OrcaStrator Settings", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(self.container, text="Pick a config to view or edit.", style="Hint.TLabel").pack(
            anchor="w", pady=(0, 12))

        outer, canvas, inner = self._make_scroll_area(self.container)
        outer.pack(fill="both", expand=True)

        # Tracking the canvas's actual width (instead of a hardcoded
        # wraplength) lets the window go as narrow as the title/button
        # need, with the subtitle simply wrapping more.
        self._landing_subtitles = []

        def _resize_subtitles(event):
            wrap = max(160, event.width - 130)  # ~130px for row padding + button
            for lbl in self._landing_subtitles:
                lbl.configure(wraplength=wrap)
        canvas.bind("<Configure>", _resize_subtitles, add="+")

        entries = list(CONFIG_REGISTRY)
        claimed = {e["path"].resolve() for e in entries if e.get("path")}
        # gui_state.json is deliberately excluded here -- it's this
        # app's own auto-written window-geometry cache (see
        # gui/_window_anchor.py), not a processor config, and was never
        # meant to be hand-edited or even seen on this screen.
        extra_paths = sorted(p for p in CONFIGS_DIR.glob("*.json") if p.name != "gui_state.json")
        for p in extra_paths:
            if p.resolve() not in claimed:
                entries.append(dict(
                    id=str(p), title=p.name, kind="raw", path=p,
                    subtitle=f"Not registered with a settings form yet -- editable as raw JSON "
                              f"({p.relative_to(HERE)})."))

        # Debug Logs isn't a processor's config -- it's a read-only log
        # browser -- so it goes last, after even the unconfigured raw
        # jsons above, rather than being part of CONFIG_REGISTRY (which
        # would put it before them).
        entries.append(DEBUG_LOGS_ENTRY)

        for entry in entries:
            self._add_landing_row(inner, entry)

        # Autosizing here (like every other screen) avoids the window
        # showing up with a scrollbar and rows cut off, or a slab of
        # empty space below the list, depending on whatever geometry
        # the previous screen left the window at.
        self._autosize_to_scroll_content(inner, outer)

    def _add_landing_row(self, parent, entry):
        row = tk.Frame(parent, bg=ORCA_PANEL_BG, highlightthickness=1, highlightbackground=ORCA_BORDER)
        row.pack(fill="x", pady=4)
        text_frame = tk.Frame(row, bg=ORCA_PANEL_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        tk.Label(text_frame, text=entry["title"], bg=ORCA_PANEL_BG, fg=ORCA_FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        subtitle = tk.Label(text_frame, text=entry.get("subtitle", ""), bg=ORCA_PANEL_BG, fg=ORCA_FG_DIM,
                             font=("Segoe UI", 9), wraplength=780, justify="left")
        subtitle.pack(anchor="w")
        self._landing_subtitles.append(subtitle)
        btn_text = "View" if entry["kind"] == "none" else "Open"
        ttk.Button(row, text=btn_text, style="Accent.TButton",
                   command=lambda e=entry: self._open_entry(e)).pack(side="right", padx=14)

    def _build_toolbar(self, title=None, on_reload=None, on_save=None, on_save_backup=None, on_load_backup=None):
        bar = tk.Frame(self.container, bg=ORCA_BG)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="\u25c0 All configs", command=self.show_landing).pack(side="left")
        # Processor name as a heading, in place of the full config-file
        # path this used to show -- the path was rarely useful at a
        # glance and the window title (see _update_title()) already
        # carries the file name for whoever needs that instead.
        self.file_label = ttk.Label(bar, text=title or "", font=("Segoe UI", 13, "bold"))
        self.file_label.pack(side="left", padx=(14, 0))
        btns = ttk.Frame(bar)
        btns.pack(side="right")
        self.reload_btn = None
        self.save_btn = None
        if on_reload:
            # Grayed out whenever the in-memory config already matches what's
            # on disk -- nothing to reload. This doubles as a save confirmation:
            # hitting Save flips dirty False, which visibly disables both this
            # button and Save, so you can see the save actually happened.
            self.reload_btn = ttk.Button(btns, text="Reload", command=on_reload,
                                          state=("normal" if self.dirty else "disabled"))
            self.reload_btn.pack(side="left", padx=4)
        if on_load_backup:
            ttk.Button(btns, text="Load Backup...", command=on_load_backup).pack(side="left", padx=4)
        if on_save_backup:
            ttk.Button(btns, text="Save Backup", command=on_save_backup).pack(side="left", padx=4)
        if on_save:
            # Same idea as Reload above, but Accent.TButton's disabled style
            # dims its background too (not just the text) -- it's the loud
            # "primary action" button, so nothing-to-save needs to read as
            # more visually "off" than Reload's subtler disabled look.
            self.save_btn = ttk.Button(btns, text="Save", style="Accent.TButton", command=on_save,
                                        state=("normal" if self.dirty else "disabled"))
            self.save_btn.pack(side="left", padx=4)
        return bar

    # -- editor: rich (with live SVG preview) -----------------------------

    def open_rich_editor(self, entry: dict, override_cfg: dict = None, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        path = entry["path"]
        self.cfg_path = path
        # entry["sections"] comes from gui/dock_collision_guard.py, same
        # as any "simple" config's fields -- _build_settings_form() below
        # renders these generically (Dock Boundary included, as a plain
        # "point_table" field now). entry itself is stashed on self too,
        # since refresh_preview() below needs entry["build_preview_payload"]
        # and entry["preview_controls"] -- this screen holds no
        # config_editor.pyw-local copy of dock collision's own field
        # list, preview controls, or preview-building logic at all.
        self._rich_sections = entry.get("sections", [])
        self._preview_entry = entry
        if override_cfg is not None:
            self.cfg = override_cfg
            self.dirty = True  # came from a backup, not what's on disk -- Save is still required
        else:
            with path.open(encoding="utf-8") as fh:
                self.cfg = json.load(fh)
            self.dirty = False
        self.validation_errors = {}
        self._refresh_hook = self.refresh_preview if entry.get("has_preview") else (lambda: None)
        self._reopen = lambda e=entry, override_cfg=None: self.open_rich_editor(
            e, override_cfg=override_cfg, _skip_confirm=True)
        self._clear_container()

        self._build_toolbar(title=entry["title"], on_reload=self.reload, on_save=self.save,
                             on_save_backup=self.save_backup, on_load_backup=self.load_backup)

        body = ttk.Frame(self.container)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=entry.get("settings_min_width", 440))
        if entry.get("has_preview"):
            body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._preview_vars = {}  # populated by _build_settings_panel() (log_list) and
                                  # _build_preview_panel() (choice_buttons/bool) below --
                                  # initialized here, once, so it exists no matter which
                                  # of those two runs first.
        self._build_settings_panel(body)
        if entry.get("has_preview"):
            self._build_preview_panel(body, entry)

        self._update_title()
        if entry.get("has_preview"):
            self.refresh_preview()

    def _build_settings_panel(self, parent):
        self.settings_outer, canvas, self.settings_inner = self._make_scroll_area(parent, stretch_inner=True)
        outer = self.settings_outer
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self._build_settings_form()

        # "log_list" preview controls (see _build_preview_panel()'s
        # docstring for the full contract) render down here, at the
        # bottom of the SETTINGS column, not the preview column --
        # they're a form control (choosing what to preview) more than
        # part of the visualization itself, and this keeps them scrolling
        # naturally along with the rest of the settings instead of being
        # pinned underneath the canvas regardless of how tall either side
        # ends up being. self._preview_vars is initialized once, by
        # open_rich_editor(), before this runs -- shared with
        # whatever _build_preview_panel() adds to it below.
        for spec in self._preview_entry.get("preview_controls", []):
            if spec.get("kind") == "log_list":
                self._build_log_list_control(self.settings_inner, spec)

    def _build_log_list_control(self, parent, spec):
        """
        A scrollable, vertically-stacked list of two-line cards (bold
        label on top, dim detail line below) -- deliberately the same
        visual/interaction style as open_debug_log_viewer()'s own
        _make_row() -- a picker over debug-dump history (see
        gui/toolchange_heatmap.py) should look and behave like the page
        that's already showing that same kind of data, not invent a
        second convention. dict keys: var, label, options [(value,
        label), ...], default. Empty options -> skipped rather than
        shown empty.
        """
        options = spec.get("options", [])
        if not options:
            return
        var_name = spec["var"]
        var = tk.StringVar(value=spec.get("default", ""))
        self._preview_vars[var_name] = var

        panel = ttk.Frame(parent)
        panel.pack(fill="x", pady=(12, 0))
        if spec.get("label"):
            ttk.Label(panel, text=spec["label"], style="Hint.TLabel").pack(anchor="w", pady=(0, 2))

        # Fixed-height viewport -- pack_propagate(False) is what keeps it
        # from growing to fit its content the way a plain Frame would;
        # the scroll area inside it (same helper the Debug Logs page and
        # the preview canvas both use) handles anything that overflows
        # that cap.
        height_box = tk.Frame(panel, height=220, bg=ORCA_BG)
        height_box.pack(fill="x")
        height_box.pack_propagate(False)
        list_outer, _list_canvas, list_inner = self._make_scroll_area(
            height_box, vertical=True, horizontal=False, bg=ORCA_PANEL_BG)
        list_outer.pack(fill="both", expand=True)

        row_widgets = []  # (value, row_frame) -- for restyling on selection

        def _set_selected(value, row_widgets=row_widgets):
            for rv, row in row_widgets:
                selected = (rv == value)
                row.configure(highlightbackground=ORCA_ACCENT if selected else ORCA_BORDER,
                              highlightthickness=2 if selected else 1)

        def _choose(value, var=var, _set_selected=_set_selected):
            var.set(value)
            _set_selected(value)
            self.refresh_preview()

        def _make_card(value, top_text, bottom_text, bold):
            row = tk.Frame(list_inner, bg=ORCA_PANEL_BG, highlightthickness=1,
                            highlightbackground=ORCA_BORDER, cursor="hand2")
            row.pack(fill="x", pady=2, padx=2)
            top_lbl = tk.Label(row, text=top_text, bg=ORCA_PANEL_BG, fg=ORCA_FG,
                                font=("Segoe UI", 9, "bold" if bold else "normal"),
                                anchor="w", cursor="hand2")
            top_lbl.pack(fill="x", padx=8, pady=(6, 0 if bottom_text else 6))
            widgets = [row, top_lbl]
            if bottom_text:
                bottom_lbl = tk.Label(row, text=bottom_text, bg=ORCA_PANEL_BG, fg=ORCA_FG_DIM,
                                       font=("Segoe UI", 8), anchor="w", cursor="hand2")
                bottom_lbl.pack(fill="x", padx=8, pady=(0, 6))
                widgets.append(bottom_lbl)
            for w in widgets:
                w.bind("<Button-1>", lambda _e, value=value: _choose(value))
            row_widgets.append((value, row))

        for value, label in options:
            # Labels here are either a single "Auto (...)" phrase
            # (value == "") or "<gcode file> -- <timestamp>" (see
            # _discover_debug_logs() in gui/toolchange_heatmap.py) --
            # split the latter back into the same two-line shape
            # _make_row() uses in the Debug Logs page, rather than
            # cramming both onto one line.
            if value == "" or " -- " not in label:
                _make_card(value, label, None, bold=(value == ""))
            else:
                top, bottom = label.split(" -- ", 1)
                _make_card(value, top, bottom, bold=False)

        _set_selected(spec.get("default", ""))

    def _build_settings_form(self):
        for child in self.settings_inner.winfo_children():
            child.destroy()
        self._field_vis_rules = []
        self._field_defaults = {}
        self._section_vis_rules = []
        self._section_frame_order = []
        self._section_field_labels = []
        self._align_exempt_frames = set()
        self._picker_groups = {}

        # No hand-built "Dock Boundary" block here anymore -- it's just
        # another section in self._rich_sections now (a "number" field
        # for safe_y + a "point_table" field for boundary), rendered
        # through the exact same generic path every other section is.
        # See gui/dock_collision_guard.py.
        for section in self._rich_sections:
            title, fields = section[0], section[1]
            override = section[2] if len(section) > 2 else None
            frame = self._labelframe(self.settings_inner, title, self._section_tooltip(fields, override))
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)

        debug_section = _debug_section_for(self.cfg)
        if debug_section and not any(s[0] == "Debug" for s in self._rich_sections):
            title, fields, override = debug_section
            frame = self._labelframe(self.settings_inner, title, override)
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)

        notice_section = _notice_section_for(self.cfg)
        if notice_section and not any(s[0] == "Notices" for s in self._rich_sections):
            title, fields, override = notice_section
            frame = self._labelframe(self.settings_inner, title, override)
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)

        self._apply_visibility()
        self._refresh_picker_groups()
        self._align_field_columns(self._section_frame_order)

    def _build_preview_panel(self, parent, entry):
        """
        Generic side of the "rich preview" contract -- see
        discover_gui_specs()'s docstring and gui/dock_collision_guard.py's
        HAS_PREVIEW/PREVIEW_CONTROLS/build_preview_payload. Builds
        whatever controls entry["preview_controls"] declares (this has
        no idea what "scenario" or "2 objects" mean, or even that this
        is dock collision at all) into self._preview_vars, and wires
        each one to call refresh_preview() on change.

        Supported control kinds:
          "choice_buttons" -- a row of mutually-exclusive Radiobuttons,
                               above the preview canvas (dict keys: var,
                               label, options [(value, label), ...],
                               default). Meant for a short, fixed set of
                               scenarios (see dock_collision_guard.py).
          "bool"            -- a single Checkbutton, same row (dict
                               keys: var, label, default).
          "log_list"        -- NOT built here -- see
                               _build_log_list_control(), called from
                               _build_settings_panel() instead. It's a
                               form control (choosing what to preview)
                               more than part of the visualization
                               itself, so it lives in the settings
                               column, not this one -- self._preview_vars
                               is shared between both builders regardless
                               (initialized once, by the caller, before
                               either runs).
        A plugin needing a control kind not listed here can add one --
        this loop is the only place that would need to grow.
        """
        outer = ttk.Frame(parent)
        outer.grid(row=0, column=1, sticky="nsew")
        outer.rowconfigure(3, weight=1)
        outer.columnconfigure(0, weight=1)

        controls = ttk.Frame(outer)
        controls.grid(row=0, column=0, sticky="w", pady=(0, 4))
        for spec in entry.get("preview_controls", []):
            kind = spec["kind"]
            var_name = spec["var"]
            if kind == "choice_buttons":
                var = tk.StringVar(value=spec.get("default", ""))
                self._preview_vars[var_name] = var
                if spec.get("label"):
                    ttk.Label(controls, text=spec["label"]).pack(side="left", padx=(0, 6))
                for value, label in spec.get("options", []):
                    ttk.Radiobutton(controls, text=label, value=value, variable=var,
                                     command=self.refresh_preview).pack(side="left", padx=3)
            elif kind == "bool":
                var = tk.BooleanVar(value=bool(spec.get("default", False)))
                self._preview_vars[var_name] = var
                ttk.Checkbutton(controls, text=spec.get("label", var_name), variable=var,
                                 command=self.refresh_preview).pack(side="left", padx=(16, 4))
            elif kind == "log_list":
                continue  # built in _build_settings_panel() -- see docstring
            else:
                print(f"[config_editor] preview control '{var_name}': unknown kind {kind!r}, skipped",
                      file=sys.stderr)

        self.status_label = ttk.Label(outer, text="", style="Hint.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(0, 6))

        # Fixed area for any preview content a plugin pins in place
        # (payload section "scroll": False) -- e.g. gcode_template_
        # notice.py's resolved-preview box, meant to stay in view while
        # only the (much longer) placeholder reference list below it
        # scrolls. Gridded above the scroll area with no row weight, so
        # it only ever takes its own natural (sticky="new") height and
        # never competes with row 3 for the leftover space; a plugin
        # that never pins anything leaves this empty at 0 height, so it
        # takes no space at all.
        self.preview_fixed_holder = ttk.Frame(outer)
        self.preview_fixed_holder.grid(row=2, column=0, sticky="new")

        preview_outer, pcanvas, self.preview_canvas_holder = self._make_scroll_area(
            outer, vertical=True, horizontal=True)
        preview_outer.grid(row=3, column=0, sticky="nsew")

    # -- editor: generic simple form (no preview) --------------------------

    def open_simple_editor(self, entry: dict, override_cfg: dict = None, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        self.cfg_path = entry["path"]
        if override_cfg is not None:
            self.cfg = override_cfg
            self.dirty = True  # came from a backup, not what's on disk -- Save is still required
        else:
            with self.cfg_path.open(encoding="utf-8") as fh:
                self.cfg = json.load(fh)
            self.dirty = False
        has_preview = entry.get("preview") == "theme"
        self._tp_frame = None
        self._refresh_hook = self._refresh_theme_preview if has_preview else (lambda: None)
        self._reopen = lambda e=entry, override_cfg=None: self.open_simple_editor(
            e, override_cfg=override_cfg, _skip_confirm=True)
        self._clear_container()

        self._build_toolbar(title=entry["title"], on_reload=self.reload, on_save=self.save,
                             on_save_backup=self.save_backup, on_load_backup=self.load_backup)

        body = ttk.Frame(self.container)
        body.pack(fill="both", expand=True, pady=(8, 0))
        if has_preview:
            # The settings column gets a fixed/minimum width and does NOT
            # expand. Giving it weight=1 instead (and weight=0 on the
            # preview column) lets the settings column's canvas balloon
            # out to fill the whole window, dragging its scrollbar away
            # from the actual fields and out to the far edge next to the
            # preview instead. Any extra window width goes to the preview
            # column, which simply centers its small mockup rather than
            # stretching it.
            #
            # No fixed/minsize width on column 0 -- that's what
            # _make_scroll_area's fit_width=True (passed below) is FOR:
            # the canvas sizes itself to inner's real content width every
            # time, so grid can just auto-size this column to whatever
            # form_parent actually needs, the same way dock_collision_
            # guard's own settings+preview screen already does (see
            # _build_settings_panel/_build_preview_panel). Without
            # fit_width, nothing shrinks the canvas back down once it's
            # expanded to fill the column, so the widest field (Processor
            # Selection's two listboxes + button columns) needs a
            # hardcoded minsize to avoid clipping -- at the cost of a big
            # dead gap between the scrollbar and the preview column on
            # every OTHER, narrower field. fit_width fixes the clipping
            # at the source, so neither tradeoff is needed.
            body.columnconfigure(0, weight=0)
            body.columnconfigure(1, weight=1, minsize=220)
            body.rowconfigure(0, weight=1)
            form_parent = ttk.Frame(body)
            form_parent.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        else:
            # A FIXED minsize column, same proven pattern as
            # open_rich_editor's own settings column (see its
            # matching body.columnconfigure call and settings_min_width)
            # -- deliberately NOT fit_width. fit_width sizes the canvas to
            # `inner`'s natural reqwidth, which fights with desc_label's
            # own wraplength-follows-canvas-width callback below: on the
            # very first layout pass the label has no wraplength yet, so
            # it requests its full, unwrapped (very wide) single-line
            # width; fit_width grows the canvas to match that; THEN the
            # wraplength callback fires off the resulting <Configure> and
            # shrinks the label back down to whatever's left -- visible
            # as a flash of full-width text that immediately re-wraps,
            # settling narrower than actually intended. A fixed minsize
            # column sidesteps the feedback loop entirely: the canvas is
            # exactly settings_min_width wide from the first layout pass
            # on, so wraplength only ever gets computed once, already
            # against the right width -- same as every "rich" screen's
            # settings column already does.
            body.columnconfigure(0, weight=0, minsize=entry.get("settings_min_width", 440))
            body.rowconfigure(0, weight=1)
            form_parent = ttk.Frame(body)
            form_parent.grid(row=0, column=0, sticky="nsew")

        if has_preview:
            outer, canvas, inner = self._make_scroll_area(form_parent, fit_width=True)
            # fit_width needs fill="y" here instead of the usual
            # fill="both" -- "both" would just have pack stretch outer
            # right back out to form_parent's full fixed width, undoing
            # the whole point of hugging the scrollbar to the content.
            outer.pack(fill="y", expand=True, anchor="nw")
        else:
            # stretch_inner (not fit_width) mirrors _build_settings_panel:
            # `inner` tracks the canvas's actual (fixed, minsize-driven)
            # width instead of the other way around, so fields/labels get
            # the FULL settings_min_width to lay out and wrap against, no
            # narrower than that no matter how little content a given
            # screen has (insert_missing_tool_preheat.json/
            # restore_pos_fix.json's sparse Debug/Notice-only forms
            # included) -- fixing both the too-narrow end width and the
            # full-width-then-rewrap flash fit_width caused here.
            outer, canvas, inner = self._make_scroll_area(form_parent, stretch_inner=True)
            outer.pack(fill="both", expand=True, anchor="nw")

        # The description is packed as the first thing inside `inner`,
        # not as a sibling Label above `body` in self.container --
        # self.container itself has no scrollbar, and the only scrollbar
        # in this screen lives on `inner`. Packing the description
        # outside `inner` would let a long enough description (e.g.
        # tool_preheat.json's) push the actual fields below the bottom
        # of the window with no way to reach them. Inside `inner`, it
        # scrolls along with the sections below it instead of squeezing
        # them off-window.
        desc = _joined_comment(self.cfg.get("_comment"))
        if desc:
            desc_label = ttk.Label(inner, text=desc, style="Hint.TLabel", justify="left")
            desc_label.pack(anchor="w", pady=(0, 12), fill="x")
            # Track the canvas's actual width rather than a hardcoded
            # wraplength -- same reasoning as _resize_subtitles on the
            # landing page (see show_landing): lets the window (and the
            # narrower settings column when a preview panel is present)
            # go narrower without the label getting clipped.
            def _resize_desc(event, lbl=desc_label):
                lbl.configure(wraplength=max(160, event.width - 12))
            canvas.bind("<Configure>", _resize_desc, add="+")

        self._field_vis_rules = []
        self._field_defaults = {}
        self._section_vis_rules = []
        self._section_frame_order = []
        self._section_field_labels = []
        self._align_exempt_frames = set()
        self._picker_groups = {}
        for section in entry["sections"]:
            title, fields = section[0], section[1]
            override = section[2] if len(section) > 2 else None
            frame = self._labelframe(inner, title, self._section_tooltip(fields, override))
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)


        debug_section = _debug_section_for(self.cfg)
        if debug_section and not any(s[0] == "Debug" for s in entry["sections"]):
            title, fields, override = debug_section
            frame = self._labelframe(inner, title, override)
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)

        notice_section = _notice_section_for(self.cfg)
        if notice_section and not any(s[0] == "Notices" for s in entry["sections"]):
            title, fields, override = notice_section
            frame = self._labelframe(inner, title, override)
            frame.pack(fill="x", padx=4, pady=6)
            self._section_frame_order.append(frame)
            self._add_section_fields(frame, fields)

        self._apply_visibility()
        self._refresh_picker_groups()
        self._align_field_columns(self._section_frame_order)

        if has_preview:
            preview_col = ttk.Frame(body)
            preview_col.grid(row=0, column=1, sticky="nw")
            ttk.Label(preview_col, text="Live preview", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
            ttk.Label(preview_col, text="(a mockup, not the real window -- see note below)",
                      style="Hint.TLabel", wraplength=200, justify="left").pack(anchor="w", pady=(0, 6))
            self._build_theme_preview_panel(preview_col)
            self._refresh_theme_preview()
            ttk.Label(
                preview_col,
                text="This app's own chrome (and any other already-open OrcaStrator window) "
                     "won't re-skin itself live -- mixing plain-tk and ttk widgets makes that "
                     "unreliable to do safely. The real progress window always matches this file "
                     "on its next run, though. Titlebar colors are the one exception -- this "
                     "window's own titlebar (top of screen) updates as you type, since that's a "
                     "plain OS-level color, not a tk/ttk widget.",
                style="Hint.TLabel", wraplength=200, justify="left").pack(anchor="w", pady=(10, 0))

        self._update_title()
        self._autosize_to_scroll_content(inner, outer)

    def _build_theme_preview_panel(self, parent):
        """
        A small mockup of the real progress window (see _TkProgressUI in
        orcastrator.py) -- header, accent rule, progress bar, log lines,
        Close button -- built from plain tk widgets (same as the real
        one) so their colors can be pushed live from self.cfg on every
        field change, without touching this app's own actual chrome.
        """
        frame = tk.Frame(parent, highlightthickness=1)
        frame.pack()
        self._tp_frame = frame

        titlebar = tk.Frame(frame, height=26)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        self._tp_titlebar = titlebar
        tb_title = tk.Label(titlebar, text="OrcaStrator", font=("Segoe UI", 8), anchor="w")
        tb_title.pack(side="left", fill="y", padx=(8, 0))
        self._tp_titlebar_title = tb_title
        tb_close = tk.Label(titlebar, text="\u2715", font=("Segoe UI", 8), anchor="e")
        tb_close.pack(side="right", fill="y", padx=(0, 8))
        self._tp_titlebar_close = tb_close

        header = tk.Label(frame, text="Running post-processors...", font=("Segoe UI", 10, "bold"), anchor="w")
        header.pack(fill="x", padx=10, pady=(10, 4))
        self._tp_header = header

        rule = tk.Frame(frame, height=2, width=200)
        rule.pack(fill="x", padx=10, pady=(0, 8))
        self._tp_rule = rule

        trough = tk.Frame(frame, height=14, width=200)
        trough.pack(fill="x", padx=10, pady=(0, 8))
        trough.pack_propagate(False)
        fill = tk.Frame(trough, height=14, width=130)
        fill.place(x=0, y=0)
        self._tp_trough = trough
        self._tp_fill = fill

        log = tk.Frame(frame, highlightthickness=1, width=200)
        log.pack(fill="x", padx=10, pady=(0, 8))
        self._tp_log = log
        self._tp_log_lines = []
        for text in ("[orcastrator] target: your_file.gcode",
                     "'dock_collision_guard.py' OK (68ms)",
                     "all processors completed successfully"):
            lbl = tk.Label(log, text=text, font=("Consolas", 8), anchor="w", justify="left")
            lbl.pack(fill="x", padx=6, pady=2)
            self._tp_log_lines.append(lbl)

        close_btn = tk.Label(frame, text="Close", font=("Segoe UI", 9, "bold"), padx=16, pady=4)
        close_btn.pack(pady=(0, 10))
        self._tp_close = close_btn

    def _theme_color(self, key, default):
        val = get_in(self.cfg, ("theme", key), default)
        return val if isinstance(val, str) and _HEX_RE.match(val) else default

    def _refresh_theme_preview(self):
        if self._tp_frame is None:
            return
        bg = self._theme_color("bg", "#2b2b2b")
        panel_bg = self._theme_color("panel_bg", "#1e1e1e")
        border = self._theme_color("border", "#3f3f3f")
        fg = self._theme_color("fg", "#e6e6e6")
        fg_dim = self._theme_color("fg_dim", "#9a9a9a")
        accent = self._theme_color("accent", "#00A886")
        accent_fg = self._theme_color("accent_fg", "#ffffff")
        titlebar = self._theme_color("titlebar", "#2b2b2b")
        titlebar_fg = self._theme_color("titlebar_fg", "#e6e6e6")

        self._tp_frame.configure(bg=bg, highlightbackground=border)
        self._tp_titlebar.configure(bg=titlebar)
        self._tp_titlebar_title.configure(bg=titlebar, fg=titlebar_fg)
        self._tp_titlebar_close.configure(bg=titlebar, fg=titlebar_fg)
        self._tp_header.configure(bg=bg, fg=fg)
        self._tp_rule.configure(bg=accent)
        self._tp_trough.configure(bg=panel_bg)
        self._tp_fill.configure(bg=accent)
        self._tp_log.configure(bg=panel_bg, highlightbackground=border)
        for i, lbl in enumerate(self._tp_log_lines):
            lbl.configure(bg=panel_bg, fg=(fg_dim if i == len(self._tp_log_lines) - 1 else fg))
        self._tp_close.configure(bg=accent, fg=accent_fg)
        # Unlike the rest of this mockup, the titlebar isn't a tk/ttk
        # widget at all -- it's a real OS-level DWM attribute on THIS
        # window's own actual titlebar (see apply_dark_titlebar() in
        # orcastrator.py), so unlike everything else in this preview
        # it's perfectly safe to push live onto self.root directly
        # instead of only onto the mockup, no "won't re-skin itself
        # live" caveat needed for this one field specifically.
        orcastrator.apply_dark_titlebar(self.root, caption_hex=titlebar, text_hex=titlebar_fg)

    # -- editor: raw JSON fallback for unrecognized configs -----------------

    def open_raw_editor(self, path: pathlib.Path, title: str, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        self.cfg_path = path
        self.cfg = None
        self.dirty = False
        self._refresh_hook = lambda: None
        self._reopen = lambda p=path, t=title: self.open_raw_editor(p, t, _skip_confirm=True)
        self._clear_container()

        self._build_toolbar(title=title, on_reload=self.reload, on_save=self._save_raw)

        ttk.Label(self.container, text="No settings form is registered for this config yet -- editing as "
                                        "raw JSON. Saved text is validated as JSON before it's written.",
                  style="Hint.TLabel", wraplength=820, justify="left").pack(anchor="w", pady=(4, 10))

        text_frame = tk.Frame(self.container, bg=ORCA_PANEL_BG, highlightthickness=1, highlightbackground=ORCA_BORDER)
        text_frame.pack(fill="both", expand=True)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self._raw_text = tk.Text(text_frame, bg=ORCA_PANEL_BG, fg=ORCA_FG, insertbackground=ORCA_FG,
                                  font=("Consolas", 10), borderwidth=0, wrap="none", undo=True)
        self._raw_text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        # Same reasoning as the debug-log viewer's log_text: a plain
        # packed Text widget has no scrolling of its own, and a config
        # with no gui/*.py spec falls back to this raw-JSON view, so a
        # file too long for one screenful was simply unreachable below
        # the fold. Both directions matter: wrap="none" means a long
        # line (e.g. a dense single-line JSON export) can run off the
        # right edge too, not just off the bottom.
        raw_vsb = ttk.Scrollbar(text_frame, orient="vertical", command=self._raw_text.yview,
                                 style="Vertical.TScrollbar")
        raw_hsb = ttk.Scrollbar(text_frame, orient="horizontal", command=self._raw_text.xview,
                                 style="Horizontal.TScrollbar")
        self._raw_text.configure(yscrollcommand=raw_vsb.set, xscrollcommand=raw_hsb.set)
        raw_vsb.grid(row=0, column=1, sticky="ns", pady=(8, 0))
        raw_hsb.grid(row=1, column=0, sticky="ew", padx=(8, 0))
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            raw_text = f"# could not read this file: {exc}"
        self._raw_text.insert("1.0", raw_text)
        self._raw_text.edit_modified(False)
        self._raw_text.bind("<<Modified>>", self._on_raw_modified)

        self._update_title()

    def _on_raw_modified(self, event=None):
        if self._raw_text.edit_modified():
            self.mark_dirty()
            self._raw_text.edit_modified(False)

    def _save_raw(self):
        text = self._raw_text.get("1.0", "end-1c")
        try:
            json.loads(text)  # validate before writing anything to disk
        except json.JSONDecodeError as exc:
            self._error("Invalid JSON", f"Not saved -- this isn't valid JSON:\n\n{exc}")
            return
        try:
            self.cfg_path.write_text(text, encoding="utf-8")
            self.dirty = False
            self._update_title()
        except Exception as exc:
            self._error("Save failed", str(exc))

    # -- screen: informational (processor has no settings) ------------------

    def open_info_view(self, entry: dict, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        self.cfg_path = None
        self.cfg = None
        self.dirty = False
        self._reopen = lambda e=entry: self.open_info_view(e, _skip_confirm=True)
        self._clear_container()

        self._build_toolbar(title=entry["title"])
        # Same reasoning as open_simple_editor's description fix: this
        # has no scrollbar of its own, so a long enough "info" string
        # would otherwise have no way to be fully read on a short window.
        outer, canvas, inner = self._make_scroll_area(self.container)
        outer.pack(fill="both", expand=True)
        info_label = ttk.Label(inner, text=entry.get("info", "This processor has no configurable settings."),
                                style="Hint.TLabel", justify="left")
        info_label.pack(anchor="w", fill="x")
        canvas.bind("<Configure>", lambda e: info_label.configure(wraplength=max(160, e.width - 12)), add="+")
        self.root.title(f"OrcaStrator Settings -- {entry['title']}")
        self._autosize_to_scroll_content(inner, outer)

    def _debug_log_dirs(self):
        """
        Directories to scan for "*_debug.json" dumps: the central
        debug.dir from configs/orcastrator.json (if set) plus
        post_processors/ itself -- the shared fallback every opted-in
        processor uses when no central dir is set (see
        helpers/debug_dump.py). Every opted-in processor's dumps land in
        exactly one of these two places now, no exceptions -- read
        errors on orcastrator.json just mean "no central dir known"
        here, same as everywhere else that reads it -- this is a
        read-only convenience view, never worth surfacing a dialog over.
        """
        dirs = []
        for candidate in (CONFIGS_DIR / "orcastrator.json", HERE / "orcastrator.json"):
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                break
            debug_cfg = raw.get("debug") if isinstance(raw.get("debug"), dict) else {}
            central = debug_cfg.get("dir")
            if isinstance(central, str) and central.strip():
                p = pathlib.Path(central.strip()).expanduser()
                if not p.is_absolute():
                    p = HERE / p
                dirs.append(p)
            break
        dirs.append(HERE / "post_processors")
        seen, result = set(), []
        for d in dirs:
            resolved = d.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(d)
        return result

    # "<processor>_debug.json" (single-log mode) or
    # "<processor>_debug_<timestamp>.json" (multi-log mode) -- MUST stay
    # in sync with debug_log_filename()/parse_debug_log_filename() in
    # post_processors/helpers/debug_dump.py, the module that actually
    # writes these files. Re-implemented here rather than imported so
    # config_editor.py never has to import anything out of
    # post_processors/ (see CLAUDE.md: "config_editor.py never
    # hard-imports any specific processor's own module") -- debug_dump.py
    # is shared infrastructure, not a processor, but the same
    # standalone-GUI/standalone-processor separation still applies.
    _DEBUG_LOG_RE = re.compile(r"^(?P<processor>.+)_debug(?:_(?P<ts>\d{8}_\d{6}_\d{3}))?\.json$")

    def _parse_debug_log_name(self, filename: str):
        """Returns (processor_name, timestamp_or_None), or None if
        `filename` isn't shaped like a debug dump at all."""
        m = self._DEBUG_LOG_RE.match(filename)
        return (m.group("processor"), m.group("ts")) if m else None

    def open_debug_log_viewer(self, entry: dict, _skip_confirm: bool = False):
        if not _skip_confirm and not self._confirm_leave():
            return
        self.cfg_path = None
        self.cfg = None
        self.dirty = False
        self._reopen = lambda e=entry: self.open_debug_log_viewer(e, _skip_confirm=True)
        self._clear_container()

        self._build_toolbar(title="Debug Logs")
        ttk.Label(self.container,
                  text="Read-only. Lists every *_debug.json dump found in the central debug directory "
                       "(OrcaStrator Settings -> Debug) and in post_processors/, the shared default "
                       "location when no central directory is set. A processor with more than one "
                       "saved log (Debug log mode set to \"multiple\") is grouped under its own name -- "
                       "click it to expand and pick which run to view.",
                  style="Hint.TLabel", wraplength=820, justify="left").pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(self.container)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=260)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        list_outer, list_canvas, list_inner = self._make_scroll_area(body)
        list_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        text_frame = tk.Frame(body, bg=ORCA_PANEL_BG, highlightthickness=1, highlightbackground=ORCA_BORDER)
        text_frame.grid(row=0, column=1, sticky="nsew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        log_text = tk.Text(text_frame, bg=ORCA_PANEL_BG, fg=ORCA_FG, font=("Consolas", 10),
                            borderwidth=0, wrap="none", state="disabled")
        log_text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        # Debug dumps can run to hundreds of KB (a full toolchange-by-
        # toolchange event list) -- a plain packed Text widget has no
        # scrolling of its own, so anything past one screenful was
        # simply unreachable. Both directions matter: wrap="none" means
        # a single long JSON line can run off the right edge too, not
        # just off the bottom.
        log_vsb = ttk.Scrollbar(text_frame, orient="vertical", command=log_text.yview,
                                 style="Vertical.TScrollbar")
        log_hsb = ttk.Scrollbar(text_frame, orient="horizontal", command=log_text.xview,
                                 style="Horizontal.TScrollbar")
        log_text.configure(yscrollcommand=log_vsb.set, xscrollcommand=log_hsb.set)
        log_vsb.grid(row=0, column=1, sticky="ns", pady=(8, 0))
        log_hsb.grid(row=1, column=0, sticky="ew", padx=(8, 0))

        def _show(f):
            log_text.configure(state="normal")
            log_text.delete("1.0", "end")
            try:
                log_text.insert("1.0", f.read_text(encoding="utf-8"))
            except Exception as exc:
                log_text.insert("1.0", f"# could not read this file: {exc}")
            log_text.configure(state="disabled")

        # Gather every *_debug*.json found (both the single-mode fixed
        # name and multi-mode timestamped names), then group by
        # processor -- entries is [(processor_name, [(path, mtime, ts), ...]), ...],
        # each inner list newest-first, sorted so the processor with the
        # most recent activity (of any kind) sorts first overall.
        seen, raw = set(), []
        for d in self._debug_log_dirs():
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*_debug*.json")):
                resolved = f.resolve()
                if resolved in seen:
                    continue
                parsed = self._parse_debug_log_name(f.name)
                if not parsed:
                    continue  # doesn't match either debug-dump shape at all
                seen.add(resolved)
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                raw.append((parsed[0], parsed[1], mtime, f))

        groups = {}
        for processor, ts, mtime, f in raw:
            groups.setdefault(processor, []).append((f, mtime, ts))
        for items in groups.values():
            items.sort(key=lambda item: item[1], reverse=True)  # newest mtime first
        ordered_processors = sorted(groups, key=lambda name: groups[name][0][1], reverse=True)

        if not ordered_processors:
            ttk.Label(list_inner, text="No debug dumps found yet -- enable \"Write debug dump\" in a "
                                        "processor's own Debug section, then run an export.",
                      style="Hint.TLabel", wraplength=240, justify="left").pack(anchor="w", padx=4, pady=4)

        def _make_row(parent, label_text, mtime, f, *, bold=True, indent=0):
            row = tk.Frame(parent, bg=ORCA_PANEL_BG, highlightthickness=1, highlightbackground=ORCA_BORDER,
                            cursor="hand2")
            row.pack(fill="x", pady=2, padx=(2 + indent, 2))
            time_text = dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            name_lbl = tk.Label(row, text=label_text, bg=ORCA_PANEL_BG, fg=ORCA_FG,
                                 font=("Segoe UI", 9, "bold" if bold else "normal"), anchor="w", cursor="hand2")
            name_lbl.pack(fill="x", padx=8, pady=(6, 0))
            time_lbl = tk.Label(row, text=time_text, bg=ORCA_PANEL_BG, fg=ORCA_FG_DIM, font=("Segoe UI", 8),
                                 anchor="w", cursor="hand2")
            time_lbl.pack(fill="x", padx=8, pady=(0, 6))
            for widget in (row, name_lbl, time_lbl):
                widget.bind("<Button-1>", lambda _e, f=f: _show(f))
            return row

        # The single overall-newest file across every processor gets
        # auto-selected on open, same as before -- if it belongs to a
        # grouped processor, that group starts expanded so the selected
        # row is actually visible rather than hidden behind a collapsed
        # header.
        newest_processor = ordered_processors[0] if ordered_processors else None

        for processor in ordered_processors:
            items = groups[processor]
            if len(items) == 1:
                f, mtime, _ts = items[0]
                _make_row(list_inner, f.name, mtime, f)
                continue

            # Grouped: a clickable header ("processor (N logs)") that
            # expands/collapses a child frame listing each individual
            # run, newest first. Automated purely by file count on
            # disk -- a processor naturally lands in group mode the
            # moment it has a second saved log, and drops back to a
            # plain single row if pruning (the debug.cap setting) or
            # manual deletion leaves it with just one again. Nothing
            # about this depends on the current Debug log mode setting,
            # only on what's actually on disk right now.
            header = tk.Frame(list_inner, bg=ORCA_PANEL_BG, highlightthickness=1,
                               highlightbackground=ORCA_BORDER, cursor="hand2")
            header.pack(fill="x", pady=2, padx=2)
            header_lbl = tk.Label(header, text=f"{processor}  ({len(items)} logs)", bg=ORCA_PANEL_BG,
                                   fg=ORCA_FG, font=("Segoe UI", 9, "bold"), anchor="w", cursor="hand2")
            header_lbl.pack(fill="x", padx=8, pady=6)

            children = tk.Frame(list_inner, bg=ORCA_PANEL_BG)
            for f, mtime, ts in items:
                if ts:
                    try:
                        label = dt.datetime.strptime(ts, "%Y%m%d_%H%M%S_%f").strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    except ValueError:
                        label = f.name
                else:
                    label = f.name  # a lingering single-mode file swept into this group
                _make_row(children, label, mtime, f, bold=False, indent=14)

            expanded = tk.BooleanVar(value=False)

            def _set_expanded(value, children=children, expanded=expanded, lbl=header_lbl, header=header,
                               processor=processor, count=len(items)):
                expanded.set(value)
                if value:
                    children.pack(fill="x", after=header)
                    lbl.configure(text=f"\u25be {processor}  ({count} logs)")
                else:
                    children.pack_forget()
                    lbl.configure(text=f"\u25b8 {processor}  ({count} logs)")

            def _toggle(_e=None, expanded=expanded, _set_expanded=_set_expanded):
                _set_expanded(not expanded.get())

            header_lbl.configure(text=f"\u25b8 {processor}  ({len(items)} logs)")
            header.bind("<Button-1>", _toggle)
            header_lbl.bind("<Button-1>", _toggle)
            if processor == newest_processor:
                _set_expanded(True)

        if newest_processor:
            _show(groups[newest_processor][0][0])

        self.root.title("OrcaStrator Settings -- Debug Logs")

    # -- field builder ----------------------------------------------------

    def _ancestor_canvas(self, widget):
        """Walks up from `widget` to the nearest enclosing scrollable
        Canvas built by _make_scroll_area (the widget itself counts if
        it's already one). Returns None if there isn't one -- e.g. the
        toolbar, a dialog, or anything else outside a scroll area."""
        w = widget
        while w is not None:
            if isinstance(w, tk.Canvas):
                return w
            w = w.master
        return None

    def _on_wheel_fallback(self, event):
        """
        App-wide mousewheel dispatcher, bound once for the app's
        lifetime (see __init__). A Spinbox/Combobox handles its own
        wheel behaviour and stops propagation here via "break" (see
        _guard_wheel_until_clicked), so by the time this fires the
        pointer is over something else entirely -- a label, a section
        frame, blank space, or a canvas's own background -- and it
        should just scroll whichever scroll area encloses it. Walking
        up from event.widget (the exact widget under the pointer)
        resolves that correctly no matter how deeply nested it is,
        which plain canvas Enter/Leave can't do reliably once the
        pointer is over one of the canvas's own embedded children.

        While a field has been clicked into, it owns the wheel
        exclusively -- nothing else scrolls (or spins) until it's
        released, so this is a no-op for as long as that's set.
        """
        if self._active_scroll_field is not None:
            return
        canvas = self._ancestor_canvas(getattr(event, "widget", None))
        if canvas is not None and hasattr(canvas, "_wheel_handler"):
            canvas._wheel_handler(event)

    def _on_global_click(self, event):
        """
        Releases a locked field on a click anywhere outside it -- bound
        once, globally, for the app's lifetime (see __init__).

        This is deliberately click-driven rather than piggybacking on
        <FocusOut>, which seems like the obvious choice but isn't
        reliable here: opening a Combobox's dropdown hands Tk's actual
        keyboard focus to an internal popdown listbox while it's open
        (not a widget we have a handle on), which fires <FocusOut> on
        the combobox itself even though the user hasn't left the field
        at all. Releasing the lock right then let the canvas scroll
        again while the dropdown was still open, and since the dropdown
        is a separate window positioned at the field's old on-screen
        spot, it stayed put while the field scrolled out from under it.
        Gating release on an actual click sidesteps that entirely.
        """
        widget = self._active_scroll_field
        if widget is None or event.widget is widget:
            return
        self._active_scroll_field = None
        self._active_scroll_snapshot = None

    def _guard_wheel_until_clicked(self, widget, var=None, commit=None):
        """
        ttk's Spinbox and Combobox both have a built-in MouseWheel
        binding that bumps their value the instant the pointer passes
        over them -- handy in isolation, but inside a scrollable
        settings list it means merely scrolling *past* a field on the
        way down the page silently edits it instead of scrolling the
        list underneath it.

        Binding at the widget's own instance level fires before that
        class-level default (Tk processes bindtags in the order
        widget -> class -> toplevel -> all), so we can gate it behind
        an actual click: only once the field has focus (i.e. it's
        been clicked into) does the wheel touch its value. Until
        then, the event is redirected to whichever scrollable canvas
        the field lives in and "break" stops the class binding from
        also firing, so the list keeps scrolling right over the top
        of the field -- the field's own area is no different from any
        other blank space in the list until it's clicked.

        Clicking a field also locks the wheel to it exclusively via
        self._active_scroll_field: while it's focused, hovering any
        OTHER field or blank space does nothing at all, rather than
        quietly scrolling the list (or spinning a different field)
        out from under an edit in progress. The lock releases on
        Enter, Escape, or a click outside the field -- see
        _on_global_click for why a plain blur isn't what drives this.

        `var`/`commit` (the field's tk variable and its own commit
        callback, both already defined by the caller in _add_field)
        are optional -- when given, Escape reverts the field to
        whatever it held when it gained focus and re-commits that,
        like a mini per-field reload rather than just abandoning an
        in-progress edit half-applied.
        """
        def _acquire(_evt=None, widget=widget, var=var):
            self._active_scroll_field = widget
            self._active_scroll_snapshot = var.get() if var is not None else None

        def _release(widget=widget):
            if self._active_scroll_field is widget:
                self._active_scroll_field = None
                self._active_scroll_snapshot = None

        def _confirm(_evt=None, widget=widget):
            _release(widget)
            widget.master.focus_set()

        def _revert(_evt=None, widget=widget, var=var, commit=commit):
            if self._active_scroll_field is widget and var is not None:
                var.set(self._active_scroll_snapshot)
                if commit is not None:
                    commit()
            _release(widget)
            widget.master.focus_set()

        widget.bind("<FocusIn>", _acquire, add="+")
        widget.bind("<Return>", _confirm, add="+")
        widget.bind("<Escape>", _revert, add="+")

        def _on_wheel(event, widget=widget):
            if self._active_scroll_field is widget:
                return  # focused -- let its own default MouseWheel binding run
            if self._active_scroll_field is not None:
                return "break"  # a DIFFERENT field owns the wheel right now
            canvas = self._ancestor_canvas(widget)
            if canvas is not None and hasattr(canvas, "_wheel_handler"):
                canvas._wheel_handler(event)
            return "break"
        widget.bind("<MouseWheel>", _on_wheel)

    def _add_field(self, parent, row, spec):
        kind = spec["kind"]
        path = spec["path"]
        tooltip_text = spec.get("tooltip") or lookup_comment(self.cfg, path) or ""

        if kind not in ("point_table", "hex_color_list", "processor_denylist", "processor_order", "gui_order",
                        "template_list"):
            # point_table's own label goes ABOVE its table instead (see
            # below) -- the standard label-in-column-0-next-to-column-1
            # layout every other kind uses works fine for a short value,
            # but squeezes a multi-row table sideways instead of letting
            # it sit flush left, widening the whole section for no
            # reason (this is exactly what regressed when BoundaryTable,
            # which laid itself out this way already, became this
            # generic field kind -- see CLAUDE.md's point_table section).
            # hex_color_list draws its own label the same way, above its
            # row of swatches (see its own holder/label_widget below) --
            # skipping it here too avoids a second, generic copy of the
            # label landing directly on top of (or behind) that row's
            # first swatch, since both would otherwise grid into the
            # same row=row, column=0 cell.
            #
            # The three dual-list picker kinds skip it for a different
            # reason: each one already labels itself, at the top of its
            # own left/right listboxes ("Discovered processors" / "Order
            # (runs first...)" / "Denylisted (...)" -- see below), via
            # PickerHeader.TLabel. Also drawing spec["label"] ("Runs
            # first"/"Runs last"/"Denylist"/"Card order") down the left
            # edge next to it just repeats the same information in a
            # smaller, dimmer, and honestly redundant way.
            lbl = ttk.Label(parent, text=spec["label"])
            lbl.grid(row=row, column=0, sticky="w", padx=(8, 6), pady=3)
            if tooltip_text:
                Tooltip(lbl, tooltip_text)
            # Tracked (rather than measured later via grid_slaves) so
            # _align_field_columns still counts this label's width even
            # if it's currently hidden by a show_if -- grid_remove()
            # drops a widget out of grid_slaves() entirely, so a
            # currently-hidden field with the page's longest label
            # would otherwise be invisible to that measurement, and
            # the column would visibly jump/misalign the moment that
            # field's condition later makes it visible.
            self._section_field_labels.append(lbl)

        if kind == "bool":
            default = spec.get("default", False)
            self._remember_default(path, default)
            var = tk.BooleanVar(value=bool(get_in(self.cfg, path, default)))

            def _on(path=path, var=var):
                self._on_change(path, var.get())

            cb = ttk.Checkbutton(parent, variable=var, command=_on)
            cb.grid(row=row, column=1, sticky="w", pady=3)
            if tooltip_text:
                Tooltip(cb, tooltip_text)

        elif kind == "choice":
            options = spec["options"]
            default = spec.get("default", options[0])
            self._remember_default(path, default)
            var = tk.StringVar(value=str(get_in(self.cfg, path, default)))

            def _on(event=None, path=path, var=var):
                self._on_change(path, var.get())

            cmb = ttk.Combobox(parent, textvariable=var, values=options, state="readonly",
                                width=spec.get("width", 12))
            cmb.bind("<<ComboboxSelected>>", _on)
            self._guard_wheel_until_clicked(cmb, var, _on)
            cmb.grid(row=row, column=1, sticky="w", pady=3)
            if tooltip_text:
                Tooltip(cmb, tooltip_text)

        elif kind == "number":
            is_int = spec.get("is_int", False)
            default = spec.get("default", 0)
            self._remember_default(path, default)
            val = get_in(self.cfg, path, default)
            var = tk.StringVar(value=str(val))

            def _on(event=None, path=path, var=var, is_int=is_int, lo=spec.get("min"), hi=spec.get("max")):
                raw = var.get().strip()
                try:
                    num = int(float(raw)) if is_int else float(raw)
                except ValueError:
                    return
                if lo is not None:
                    num = max(lo, num)
                if hi is not None:
                    num = min(hi, num)
                var.set(str(num))
                self._on_change(path, num)

            spin = ttk.Spinbox(parent, textvariable=var, from_=spec.get("min", -100000),
                                to=spec.get("max", 100000), increment=spec.get("step", 1),
                                width=spec.get("width", 9), command=_on)
            spin.bind("<Return>", _on)
            spin.bind("<FocusOut>", _on)
            self._guard_wheel_until_clicked(spin, var, _on)
            spin.grid(row=row, column=1, sticky="w", pady=3)
            if tooltip_text:
                Tooltip(spin, tooltip_text)

        elif kind == "text":
            default = spec.get("default", "")
            self._remember_default(path, default)
            var = tk.StringVar(value=str(get_in(self.cfg, path, default)))

            def _on(event=None, path=path, var=var):
                self._on_change(path, var.get())

            ent = ttk.Entry(parent, textvariable=var, width=spec.get("width", 24))
            ent.bind("<Return>", _on)
            ent.bind("<FocusOut>", _on)
            ent.grid(row=row, column=1, sticky="w", pady=3)
            if tooltip_text:
                Tooltip(ent, tooltip_text)

        elif kind == "multiline_text":
            # Same idea as "text" above but backed by a tk.Text box
            # instead of a single-line Entry -- for a value that's
            # naturally more than one line (a template string, a free-
            # form note). tk.Text has no Tk Variable of its own, so this
            # reads/writes the widget directly rather than through a
            # StringVar the way every other kind here does.
            default = spec.get("default", "")
            self._remember_default(path, default)
            initial = str(get_in(self.cfg, path, default))
            box = tk.Text(parent, wrap="word", height=spec.get("height", 3), width=spec.get("width", 48),
                           bg=ORCA_PANEL_BG, fg=ORCA_FG, insertbackground=ORCA_FG,
                           relief="flat", highlightthickness=1, highlightbackground=ORCA_BORDER)
            box.insert("1.0", initial)
            box.grid(row=row, column=1, sticky="w", pady=3)
            if tooltip_text:
                Tooltip(box, tooltip_text)

            def _on(event=None, path=path, box=box):
                self._on_change(path, box.get("1.0", "end-1c"))

            box.bind("<FocusOut>", _on)

        elif kind == "color":
            default = spec.get("default", "rgba(128,128,128,0.9)")
            self._remember_default(path, default)
            r, g, b, a = parse_rgba(get_in(self.cfg, path, default))
            holder = ttk.Frame(parent)
            holder.grid(row=row, column=1, sticky="w", pady=3)
            swatch = tk.Label(holder, width=3, bg=rgb_to_hex(r, g, b), cursor="hand2",
                               highlightthickness=1, highlightbackground=ORCA_BORDER)
            swatch.pack(side="left")
            alpha_var = tk.StringVar(value=str(a))
            if tooltip_text:
                Tooltip(swatch, tooltip_text)

            def _pick(event=None, path=path, default=default, swatch=swatch, alpha_var=alpha_var):
                cr, cg, cb, ca = parse_rgba(get_in(self.cfg, path, default))
                picked = colorchooser.askcolor(color=rgb_to_hex(cr, cg, cb), parent=self.root, title=spec["label"])
                if picked and picked[0]:
                    nr, ng, nb = (int(v) for v in picked[0])
                    swatch.configure(bg=rgb_to_hex(nr, ng, nb))
                    self._on_change(path, format_rgba(nr, ng, nb, float(alpha_var.get() or ca)))

            swatch.bind("<Button-1>", _pick)

            def _on_alpha(event=None, path=path, default=default, swatch=swatch, alpha_var=alpha_var):
                cr, cg, cb, ca = parse_rgba(get_in(self.cfg, path, default))
                try:
                    na = max(0.0, min(1.0, float(alpha_var.get())))
                except ValueError:
                    return
                self._on_change(path, format_rgba(cr, cg, cb, na))

            alpha_spin = ttk.Spinbox(holder, textvariable=alpha_var, from_=0.0, to=1.0,
                                      increment=0.05, width=5, command=_on_alpha)
            alpha_spin.bind("<Return>", _on_alpha)
            alpha_spin.bind("<FocusOut>", _on_alpha)
            self._guard_wheel_until_clicked(alpha_spin, alpha_var, _on_alpha)
            alpha_spin.pack(side="left", padx=(6, 3))
            ttk.Label(holder, text="alpha").pack(side="left")

        elif kind == "hex_color":
            default = spec.get("default", "#888888")
            self._remember_default(path, default)
            current = get_in(self.cfg, path, default)
            if not (isinstance(current, str) and _HEX_RE.match(current)):
                current = default
            holder = ttk.Frame(parent)
            holder.grid(row=row, column=1, sticky="w", pady=3)
            swatch = tk.Label(holder, width=3, bg=current, cursor="hand2",
                               highlightthickness=1, highlightbackground=ORCA_BORDER)
            swatch.pack(side="left")
            hex_label = ttk.Label(holder, text=current, style="Hint.TLabel")
            hex_label.pack(side="left", padx=(6, 0))
            if tooltip_text:
                Tooltip(swatch, tooltip_text)

            def _pick(event=None, path=path, default=default, swatch=swatch, hex_label=hex_label):
                cur = get_in(self.cfg, path, default)
                if not (isinstance(cur, str) and _HEX_RE.match(cur)):
                    cur = default
                picked = colorchooser.askcolor(color=cur, parent=self.root, title=spec["label"])
                if picked and picked[1]:
                    new_hex = picked[1]
                    swatch.configure(bg=new_hex)
                    hex_label.configure(text=new_hex)
                    self._on_change(path, new_hex)

            swatch.bind("<Button-1>", _pick)

        elif kind == "hex_color_list":
            # Generic editable ROW of colors, stored as a single
            # comma-separated hex string at path -- NOT a JSON list --
            # so a config that already parses this itself as "one hex
            # per position" (e.g. tool_temperature_graph.json's
            # tool_colors, position == tool number) keeps that exact
            # on-disk shape; this only changes how it's edited, not how
            # it's stored. Each swatch uses the same colorchooser.
            # askcolor() picker as the single-value "hex_color" kind
            # above -- this is that kind, repeated, plus add/remove,
            # same generic-list spirit as "point_table" above (a
            # config's SECTIONS entry has zero widget code either way).
            default = spec.get("default", "")
            self._remember_default(path, default)
            index_prefix = spec.get("index_prefix", "")
            add_label = spec.get("add_label", "+ Add color")

            def _parse_colors(raw):
                out = []
                for part in str(raw or "").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    candidate = part if part.startswith("#") else "#" + part
                    # An invalid entry becomes a visible neutral gray
                    # swatch rather than being silently dropped -- same
                    # "fall back to something editable, not something
                    # missing" reasoning as hex_color's own current/
                    # default fallback above.
                    out.append(candidate.lower() if _HEX_RE.match(candidate) else "#888888")
                return out

            current_colors = _parse_colors(get_in(self.cfg, path, default)) or _parse_colors(default) or ["#888888"]

            holder = ttk.Frame(parent)
            holder.grid(row=row, column=0, columnspan=2, sticky="w", pady=3)
            label_widget = ttk.Label(holder, text=spec["label"])
            label_widget.pack(anchor="w", padx=8, pady=(6, 0))
            if tooltip_text:
                Tooltip(label_widget, tooltip_text)

            body = ttk.Frame(holder)
            body.pack(fill="x", padx=8)

            def _sync(current_colors=current_colors, path=path):
                self._on_change(path, ",".join(current_colors))

            def _remove(i, current_colors=current_colors):
                # Removing down to zero is allowed -- an empty list is
                # itself a meaningful, valid state ("no manual choice
                # made anywhere"), exactly what parse_tool_colors() in
                # tool_temperature_graph.py treats an empty tool_colors
                # string as; color_for_tool() then falls back to the
                # g-code's filament_colour / auto_color_for_tool() for
                # every tool, tool 0 included. Refusing to go below one
                # swatch would leave the last remaining color
                # permanently stuck on manual with no way back to auto.
                current_colors.pop(i)
                _rebuild()
                _sync()

            def _add(current_colors=current_colors):
                # Seeds the new swatch as a distinct color via the same
                # golden-angle hue spread the processor itself falls
                # back to for a tool beyond the list (see
                # tool_temperature_graph.py's auto_color_for_tool()) --
                # inlined rather than imported, since this is generic
                # GUI plumbing with no business depending on one
                # specific processor's module.
                hue = (len(current_colors) * 0.6180339887498949) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.92)
                current_colors.append("#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255)))
                _rebuild()
                _sync()

            def _rebuild(current_colors=current_colors, body=body, index_prefix=index_prefix, add_label=add_label):
                # Wrapped defensively -- this runs from button callbacks
                # (_add/_remove/_pick), and this app ships as .pyw (no
                # console on Windows), so an uncaught exception here
                # wouldn't crash anything visibly -- Tk just silently
                # swallows it and the section is left however far the
                # last successful pass got, which is exactly the
                # "renders as a totally empty box, no error, no crash"
                # failure this guards against. A real failure now shows
                # as visible text plus a way back, not silence.
                try:
                    _do_rebuild(current_colors, body, index_prefix, add_label)
                except Exception as exc:
                    for child in body.winfo_children():
                        child.destroy()
                    ttk.Label(body, text=f"\u26a0 Couldn't render color list: {exc}",
                              style="Hint.TLabel", foreground=ERROR_COLOR, wraplength=380,
                              justify="left").pack(anchor="w", pady=2)

                    def _reset(current_colors=current_colors):
                        current_colors[:] = _parse_colors(default) or ["#888888"]
                        _rebuild()
                        _sync()

                    ttk.Button(body, text="Reset to defaults", command=_reset).pack(anchor="w", pady=2)

            def _do_rebuild(current_colors, body, index_prefix, add_label):
                for child in body.winfo_children():
                    child.destroy()

                # Sized from ONE throwaway probe cell (built, measured,
                # destroyed -- never displayed), the same "probe, measure,
                # destroy" pattern already used for the processor-picker
                # Up/Down column above, rather than repacking real cells
                # into a different container after the fact (an "in_="
                # ownership swap with no precedent anywhere else in this
                # file, and no way to confirm it renders correctly without
                # a live Tk session). Every real cell is created directly
                # inside the row frame it belongs to and never moved.
                # Sized against the WIDEST index label that will actually
                # appear (e.g. "T10", not "T0"), so the row-break count
                # stays a safe (if slightly conservative) fit even though
                # it assumes uniform cell width rather than measuring each
                # one individually.
                probe = ttk.Frame(body)
                if index_prefix:
                    ttk.Label(probe, text=f"{index_prefix}{max(len(current_colors) - 1, 0)}",
                              style="Hint.TLabel").pack()
                tk.Label(probe, width=3, highlightthickness=1).pack()
                ttk.Button(probe, text="\u2715", width=2).pack()
                probe.update_idletasks()
                cell_width = probe.winfo_reqwidth() + 6
                probe.destroy()

                # Fixed, generous-but-realistic guess at the panel's
                # usable width -- nothing in this settings panel is
                # actually width-constrained to the visible pane for a
                # live number to measure against (see _make_scroll_area's
                # "inner" frame -- sized to its own content, not clamped
                # to the canvas), same non-dynamic assumption the
                # point_table error_label above already makes with its
                # own hardcoded wraplength.
                FLOW_MAX_WIDTH = 380
                per_row = max(1, FLOW_MAX_WIDTH // cell_width)

                row_frame = None
                for i, color in enumerate(current_colors):
                    if row_frame is None or i % per_row == 0:
                        row_frame = ttk.Frame(body)
                        row_frame.pack(anchor="w", fill="x")
                    cell = ttk.Frame(row_frame)
                    cell.pack(side="left", padx=(0, 6), pady=2)
                    if index_prefix:
                        ttk.Label(cell, text=f"{index_prefix}{i}", style="Hint.TLabel").pack()
                    sw = tk.Label(cell, width=3, bg=color, cursor="hand2",
                                   highlightthickness=1, highlightbackground=ORCA_BORDER)
                    sw.pack()

                    def _pick(event=None, i=i, current_colors=current_colors):
                        title = f"{spec['label']} -- {index_prefix}{i}" if index_prefix else spec["label"]
                        picked = colorchooser.askcolor(color=current_colors[i], parent=self.root, title=title)
                        if picked and picked[1]:
                            current_colors[i] = picked[1]
                            _rebuild()
                            _sync()

                    sw.bind("<Button-1>", _pick)
                    ttk.Button(cell, text="\u2715", width=2,
                               command=lambda i=i: _remove(i)).pack(pady=(2, 0))

                # "+ Add color" always gets its own row below every color
                # row, rather than riding along at the end of the last one
                # when there happens to be room -- a fixed landing spot is
                # easier to find by eye than one that moves depending on
                # how many tools are currently listed.
                row_frame = ttk.Frame(body)
                row_frame.pack(anchor="w", fill="x")
                ttk.Button(row_frame, text=add_label, command=_add).pack(side="left", padx=(6, 0), pady=2, anchor="s")

            _rebuild()

        elif kind == "nullable_number":
            # A checkbox for "use the automatic/shared default" (null in
            # the JSON) plus a spinbox for an explicit override -- keeps
            # a field that's genuinely allowed to be null from becoming
            # a free-text box someone could accidentally leave in a
            # half-typed, invalid state.
            is_int = spec.get("is_int", False)
            default = spec.get("default")
            self._remember_default(path, default)
            current = get_in(self.cfg, path, default)
            holder = ttk.Frame(parent)
            holder.grid(row=row, column=1, sticky="w", pady=3)
            auto_var = tk.BooleanVar(value=current is None)
            placeholder = spec.get("auto_placeholder", spec.get("min", 0))
            val_var = tk.StringVar(value=str(current if current is not None else placeholder))

            def _apply(event=None, path=path, is_int=is_int, lo=spec.get("min"), hi=spec.get("max")):
                if auto_var.get():
                    spin.configure(state="disabled")
                    self._on_change(path, None)
                    return
                spin.configure(state="normal")
                raw = val_var.get().strip()
                try:
                    num = int(float(raw)) if is_int else float(raw)
                except ValueError:
                    return
                if lo is not None:
                    num = max(lo, num)
                if hi is not None:
                    num = min(hi, num)
                val_var.set(str(num))
                self._on_change(path, num)

            auto_cb = ttk.Checkbutton(holder, text=spec.get("auto_label", "Auto"), variable=auto_var, command=_apply)
            auto_cb.pack(side="left")
            spin = ttk.Spinbox(holder, textvariable=val_var, from_=spec.get("min", 0),
                                to=spec.get("max", 100000), increment=spec.get("step", 1),
                                width=spec.get("width", 8), command=_apply,
                                state=("disabled" if auto_var.get() else "normal"))
            spin.bind("<Return>", _apply)
            spin.bind("<FocusOut>", _apply)
            self._guard_wheel_until_clicked(spin, val_var, _apply)
            spin.pack(side="left", padx=(6, 3))
            if spec.get("unit"):
                ttk.Label(holder, text=spec["unit"]).pack(side="left")
            if tooltip_text:
                Tooltip(auto_cb, tooltip_text)
                Tooltip(spin, tooltip_text)

        elif kind == "point_table":
            # Generic editable list of numeric-column rows -- cfg's own
            # value at `path` is a list of {col_key: value, ...} dicts.
            # Row-level validation is delegated entirely to the field
            # spec's own validate_rows callable (same {row_id: message}
            # shape dock collision's validate_boundary_points already
            # returns) -- this kind has zero domain knowledge of what a
            # "valid" row means for any particular config; a config with
            # no validate_rows just gets the generic min_rows/parse
            # checks below.
            columns = spec["columns"]  # [(key, header_label), ...]
            min_rows = spec.get("min_rows", 1)
            min_rows_message = spec.get("min_rows_message", f"Needs at least {min_rows} row(s).")
            parse_error_message = spec.get("parse_error_message", "row(s) have non-numeric values and are being ignored.")
            validate_rows = spec.get("validate_rows")
            validation_key = spec.get("validation_key", "/".join(str(p) for p in path))

            holder = ttk.Frame(parent)
            # Full section width (columnspan=2), not squeezed into column
            # 1 next to a column-0 label -- see the comment where lbl is
            # normally created above. The field's own label goes inside
            # holder instead, packed above the table.
            holder.grid(row=row, column=0, columnspan=2, sticky="w", pady=3)

            label_widget = ttk.Label(holder, text=spec["label"])
            label_widget.pack(anchor="w", padx=8, pady=(6, 0))
            if tooltip_text:
                Tooltip(label_widget, tooltip_text)

            header = ttk.Frame(holder)
            header.pack(fill="x")
            for i, (key, col_label) in enumerate(columns):
                # Only the first column gets left padding (indenting the
                # whole table in from the section edge); columns after
                # it sit snug against their neighbor -- matches
                # BoundaryTable's original Y/Z spacing exactly.
                header_padx = (8, 4) if i == 0 else (0, 0)
                ttk.Label(header, text=col_label, width=10).pack(side="left", padx=header_padx)

            body = ttk.Frame(holder)
            body.pack(fill="x")

            error_label = ttk.Label(holder, text="", style="Hint.TLabel", foreground=ERROR_COLOR,
                                     justify="left", wraplength=460)

            table_rows = []  # [(vars_by_col, entries_by_col, row_frame), ...]

            def _validate_and_style(path=path, columns=columns, min_rows=min_rows, min_rows_message=min_rows_message,
                                     parse_error_message=parse_error_message, validate_rows=validate_rows,
                                     validation_key=validation_key, table_rows=table_rows, error_label=error_label):
                points = []
                parse_error_rows = set()
                for i, (vars_, _, _) in enumerate(table_rows):
                    try:
                        point = {key: float(vars_[key].get()) for key, _ in columns}
                        point["_row"] = i
                        points.append(point)
                    except ValueError:
                        parse_error_rows.add(i)

                bad_rows = validate_rows(points) if validate_rows else {}
                messages = list(bad_rows.values())
                if parse_error_rows:
                    messages.append(f"{len(parse_error_rows)} {parse_error_message}")
                if len(points) < min_rows:
                    messages.append(min_rows_message)

                for i, (_, entries, _) in enumerate(table_rows):
                    invalid = i in bad_rows or i in parse_error_rows
                    style = "Invalid.TEntry" if invalid else "TEntry"
                    for entry in entries.values():
                        entry.configure(style=style)
                error_label.configure(text=("\n".join(f"\u26a0 {m}" for m in messages) if messages else ""))
                self.validation_errors[validation_key] = messages
                return points

            def _sync(path=path, columns=columns):
                # Only rows that parsed as numbers get written to cfg; a
                # half-typed row is dropped from the saved list until
                # fixed -- the red field + warning label make that
                # visible instead of it happening silently. Doesn't touch
                # cfg or the dirty flag until an actual edit happens --
                # see the initial _validate_and_style() call below,
                # used on load instead of this.
                points = _validate_and_style()
                self._on_change(path, [{key: p[key] for key, _ in columns} for p in points])

            def _remove(row_frame, table_rows=table_rows):
                table_rows[:] = [r for r in table_rows if r[2] is not row_frame]
                row_frame.destroy()
                _sync()

            def _make_row(values, body=body, columns=columns, table_rows=table_rows):
                row_frame = ttk.Frame(body)
                row_frame.pack(fill="x")
                vars_, entries = {}, {}
                for i, (key, _) in enumerate(columns):
                    var = tk.StringVar(value=str(values.get(key, 0.0)))
                    entry = ttk.Entry(row_frame, textvariable=var, width=10)
                    entry_padx = (8, 4) if i == 0 else (0, 0)
                    entry.pack(side="left", padx=entry_padx, pady=2)
                    entry.bind("<Return>", lambda e: _sync())
                    entry.bind("<FocusOut>", lambda e: _sync())
                    vars_[key] = var
                    entries[key] = entry
                ttk.Button(row_frame, text="Remove", command=lambda rf=row_frame: _remove(rf)).pack(side="left", padx=8)
                table_rows.append((vars_, entries, row_frame))

            def _add_blank_row(columns=columns, table_rows=table_rows):
                # Seed the new row from the last row's current values
                # (not necessarily what was last saved to cfg -- reads
                # straight out of the entry StringVars) so "+ Add row"
                # continues a boundary table instead of dropping a 0,0
                # point into it. Falls back to 0.0 per-column when the
                # table is empty or a value doesn't parse.
                if table_rows:
                    last_vars = table_rows[-1][0]
                    seed = {}
                    for key, _ in columns:
                        try:
                            seed[key] = float(last_vars[key].get())
                        except ValueError:
                            seed[key] = 0.0
                else:
                    seed = {key: 0.0 for key, _ in columns}
                _make_row(seed)
                _sync()

            for p in get_in(self.cfg, path, []) or []:
                _make_row(p)
            ttk.Button(holder, text=spec.get("add_label", "+ Add row"), command=_add_blank_row).pack(
                anchor="w", padx=8, pady=(4, 8))
            error_label.pack(anchor="w", padx=8, pady=(0, 8))
            _validate_and_style()  # styles + records validation_errors without writing cfg or marking dirty

        elif kind == "template_list":
            # Generic editable list of {name, text, destinations} dicts --
            # cfg's own value at `path` is a plain JSON list, same "list
            # of dicts" shape point_table above uses for numeric rows,
            # just with a free-text name, a multiline template string,
            # and a set of destination checkboxes per row instead of
            # numeric columns. destination_options ([(value, label), ...])
            # comes from the field spec, so this kind isn't hardcoded to
            # gcode_template_notice.json's own two destinations -- any
            # future config with the same "list of named templates, each
            # sent to one or more destinations" shape can reuse it.
            destination_options = spec.get("destination_options", [])
            add_label = spec.get("add_label", "+ Add template")
            name_default = spec.get("name_default", "template")

            holder = ttk.Frame(parent)
            holder.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
            parent.grid_columnconfigure(1, weight=1)  # let holder's "ew" actually have room to expand into

            label_widget = ttk.Label(holder, text=spec["label"])
            label_widget.pack(anchor="w", padx=8, pady=(6, 0))
            if tooltip_text:
                Tooltip(label_widget, tooltip_text)

            body = ttk.Frame(holder)
            body.pack(fill="x")

            table_rows = []  # [{"frame", "name_var", "text_box", "dest_vars"}, ...]

            def _sync(path=path, table_rows=table_rows):
                items = []
                for r in table_rows:
                    items.append({
                        "name": r["name_var"].get().strip() or name_default,
                        "text": r["text_box"].get("1.0", "end-1c"),
                        "condition": r["cond_var"].get().strip(),
                        "destinations": [val for val, v in r["dest_vars"].items() if v.get()],
                    })
                self._on_change(path, items)
                if self._refresh_hook:
                    self._refresh_hook()

            def _remove(row_frame, table_rows=table_rows):
                table_rows[:] = [r for r in table_rows if r["frame"] is not row_frame]
                row_frame.destroy()
                _sync()

            def _make_row(values, body=body, table_rows=table_rows,
                           destination_options=destination_options, name_default=name_default):
                row_frame = tk.Frame(body, bg=ORCA_PANEL_BG, highlightthickness=1,
                                      highlightbackground=ORCA_BORDER)
                row_frame.pack(fill="x", pady=4, padx=2)

                top = ttk.Frame(row_frame)
                top.pack(fill="x", padx=8, pady=(6, 2))
                ttk.Label(top, text="Name").pack(side="left", padx=(0, 6))
                name_var = tk.StringVar(value=str(values.get("name") or name_default))
                name_entry = ttk.Entry(top, textvariable=name_var, width=28)
                name_entry.pack(side="left")
                name_entry.bind("<Return>", lambda e: _sync())
                name_entry.bind("<FocusOut>", lambda e: _sync())
                ttk.Button(top, text="Remove", command=lambda rf=row_frame: _remove(rf)).pack(
                    side="right")

                text_frame = ttk.Frame(row_frame)
                text_frame.pack(fill="x", padx=8, pady=(0, 4))
                text_box = tk.Text(text_frame, wrap="word", height=4,
                                    bg=ORCA_BG, fg=ORCA_FG, insertbackground=ORCA_FG,
                                    relief="flat", highlightthickness=1, highlightbackground=ORCA_BORDER)
                text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text_box.yview)
                text_box.configure(yscrollcommand=text_scroll.set)
                text_box.pack(side="left", fill="both", expand=True)
                text_scroll.pack(side="right", fill="y")
                text_box.bind("<FocusOut>", lambda e: _sync())

                def _autosize_text_box(_event=None, tb=text_box):
                    # tb.count(..., "displaylines") is the actual rendered
                    # line count -- unlike counting "\n" in the string, it
                    # already accounts for wrap="word" folding one long
                    # logical line into several visual ones, so the box
                    # grows correctly even for a template with no newlines
                    # at all, just a lot of text. Bound to <<Modified>>
                    # (fires on every insert/delete regardless of source --
                    # typing, paste, cut, undo -- unlike <KeyRelease>,
                    # which would miss a middle-click paste) rather than a
                    # size cap, since this row already lives inside the
                    # settings column's own scroll area (see
                    # _build_settings_panel's _make_scroll_area call), so
                    # there's nowhere better for the extra height to come
                    # from than that outer scroll growing to match.
                    if not tb.edit_modified():
                        return
                    # Force geometry before counting: on a row that was
                    # just created (e.g. loading an existing multi-line
                    # template from the saved config), the widget hasn't
                    # been laid out even once yet, and count() against
                    # un-realized geometry answers as if wrapping at the
                    # Text widget's tiny built-in default width instead
                    # of its actual configured one -- wildly over-
                    # counting displaylines for anything long enough to
                    # wrap at all.
                    tb.update_idletasks()
                    lines = tb.count("1.0", "end", "displaylines")
                    n = lines[0] if lines else 1
                    tb.configure(height=max(4, n))
                    tb.edit_modified(False)
                    # scale=1.5: this screen's templates tend to run long
                    # (multi-line g-code/notice text, several rows), so it
                    # gets 50% more auto-height room than other screens
                    # before hitting the usual screen-height cap.
                    self._autosize_to_scroll_content(self.settings_inner, self.settings_outer, scale=1.5)

                text_box.bind("<<Modified>>", _autosize_text_box)
                text_box.insert("1.0", str(values.get("text") or ""))
                _autosize_text_box()

                cond_row = ttk.Frame(row_frame)
                cond_row.pack(fill="x", padx=8, pady=(0, 4))
                cond_label = ttk.Label(cond_row, text="Only if:", style="Hint.TLabel")
                cond_label.pack(side="left", padx=(0, 8))
                cond_var = tk.StringVar(value=str(values.get("condition") or ""))
                cond_entry = ttk.Entry(cond_row, textvariable=cond_var)
                cond_entry.pack(side="left", fill="x", expand=True)
                cond_entry.bind("<Return>", lambda e: _sync())
                cond_entry.bind("<FocusOut>", lambda e: _sync())
                # Optional -- blank (the default) means "always fires". Kept
                # as its own row rather than crowded onto the Name row since
                # it can run just as long as a real expression sometimes
                # needs to (e.g. a multi-clause bed-type/nozzle check).
                Tooltip(cond_label, "Optional. A boolean expression (same grammar as a {expr} "
                                     "placeholder, just without the braces) that gates this WHOLE "
                                     "template -- every destination below only fires when this "
                                     "evaluates true. Leave blank to always fire. "
                                     "Example: curr_bed_type != \"High Temp Plate\". "
                                     "A condition that fails to evaluate (unknown placeholder, a typo) is "
                                     "treated as false -- it never fires -- and shows up as its own "
                                     "warning notice instead, so a typo can't silently block a "
                                     "print.")

                dest_row = ttk.Frame(row_frame)
                dest_row.pack(fill="x", padx=8, pady=(0, 8))
                ttk.Label(dest_row, text="Send to:", style="Hint.TLabel").pack(side="left", padx=(0, 8))
                current_dest = set(values.get("destinations") or [])
                dest_vars = {}
                for val, dlabel in destination_options:
                    v = tk.BooleanVar(value=(val in current_dest))
                    dest_vars[val] = v
                    check = ttk.Checkbutton(dest_row, text=dlabel, variable=v, command=_sync)
                    check.pack(side="left", padx=(0, 12))
                    if val == "abort":
                        # The one destination that can refuse a print outright
                        # (see helpers/notice.py's "abort" level) -- called out
                        # specifically since it reads very differently from
                        # "Printer notice"/"G-code comment" at a glance and is
                        # easy to tick by accident right next to them.
                        Tooltip(check, "Refuses the print outright (Klipper's action_raise_error) "
                                       "instead of just showing a console message -- the SAME "
                                       "mechanism the Dock Collision Check uses, just "
                                       "triggered by this template instead. Always shows on the "
                                       "console no matter what this processor's own Notices setting "
                                       "says. Almost always paired with an \"Only if:\" condition "
                                       "above -- with no condition this fires (and blocks) EVERY "
                                       "print.")

                table_rows.append({"frame": row_frame, "name_var": name_var,
                                    "text_box": text_box, "cond_var": cond_var, "dest_vars": dest_vars})

            def _add_blank_row():
                _make_row({"name": name_default, "text": "", "destinations": []})
                _sync()

            for item in get_in(self.cfg, path, []) or []:
                if isinstance(item, dict):
                    _make_row(item)
            ttk.Button(holder, text=add_label, command=_add_blank_row).pack(
                anchor="w", padx=8, pady=(4, 8))

        elif kind in ("processor_denylist", "processor_order", "gui_order"):
            # This whole field kind opts OUT of _align_field_columns's
            # page-wide column alignment (see _align_exempt_frames) --
            # unlike a normal label+value row, this one has no column-0
            # label at all (it labels itself, per-listbox, further
            # down), so forcing its section's column 0 out to match
            # every other section's label width would just be dead
            # space this section never needed, pushing its own shared
            # list and picker holder pointlessly far right instead of
            # sizing to its own content the way it always used to.
            self._align_exempt_frames.add(parent)
            # All three kinds are the same dual-list "pick from what's
            # actually on disk" widget -- move a name between "not
            # selected" and "selected" with Add/Remove, and for
            # processor_order/gui_order, reorder the selected side with
            # Up/Down. The JSON value is always just the right-hand
            # list's current names, top to bottom -- for
            # processor_denylist that list has no order (order
            # never mattered for what to skip); for processor_order
            # it's read top-to-bottom as EXPLICIT_ORDER (processor run
            # order); for gui_order it's read top-to-bottom as
            # GUI_EXPLICIT_ORDER (this app's own landing-page card
            # order, see load_gui_order()/build_config_registry()).
            reorder = kind in ("processor_order", "gui_order")
            all_scripts = discover_gui_page_names() if kind == "gui_order" else discover_processor_scripts()
            # Which shared "claim pool" this picker draws from -- gui_order
            # picks from gui/*.py page names, the other two from
            # post_processors/*.py script names, so they're kept in
            # separate groups (see self._picker_groups). Every picker in
            # a group draws from the SAME "Discovered..." listbox (built
            # once, by whichever picker in the group is built first --
            # see below) rather than each getting its own identical copy,
            # and a name picked into any ONE of them disappears from that
            # shared list, so "Runs first"/"Runs last"/"Denylist" can't
            # all claim the same script at once.
            group_key = "gui_pages" if kind == "gui_order" else "processors"
            raw_list = get_in(self.cfg, path, [])
            # Silently drops anything that isn't (or is no longer) an
            # actual file in post_processors/ (or gui/, for gui_order)
            # -- same tolerance orcastrator.py's own discover_processors()
            # already has for a stale/bad entry at runtime, just applied
            # here too so the picker never shows a name that isn't real.
            current = [n for n in raw_list if isinstance(n, str) and n in all_scripts] \
                if isinstance(raw_list, list) else []

            group = self._picker_groups.setdefault(
                group_key, {"shared_left_list": None, "shared_left_frame": None,
                            "min_row": row, "max_row": row, "members": []})
            group["min_row"] = min(group["min_row"], row)
            group["max_row"] = max(group["max_row"], row)

            if group["shared_left_list"] is None:
                # First picker in this group on the current screen --
                # builds the ONE "Discovered..." list every sibling
                # picker in the group reuses ("Runs first"/"Runs last"/
                # "Denylist" all draw from the same shared list) instead
                # of each showing its own identical copy. It's
                # a direct child of `parent` (the section's own grid),
                # not of any one picker's holder, specifically so it can
                # rowspan down across every row the group ends up
                # covering -- fixed up to its final span in
                # _refresh_picker_groups() once the whole screen is built
                # and the group's full row range is actually known.
                left_frame = ttk.Frame(parent)
                # padx left-hand 8px matches the 8px inset every other
                # field's own label gets (see the padx=(8, 6) a couple
                # dozen lines up) -- this group's row skips that label
                # entirely, so without an explicit inset here the shared
                # list was the one field in a section sitting flush
                # against the section's left border instead of matching
                # everything else's gap. This section is exempted from
                # _align_field_columns (see just above), so column 0
                # here stays its own natural near-zero width rather than
                # being forced to match every other section's -- this
                # manual pad is what stands in for that.
                left_frame.grid(row=row, column=1, sticky="ns", padx=(8, 6), pady=3)
                left_label = "Discovered pages" if kind == "gui_order" else "Discovered processors"
                ttk.Label(left_frame, text=left_label, style="PickerHeader.TLabel").pack(anchor="w")
                shared_left_list = tk.Listbox(left_frame, height=5, width=30, exportselection=False,
                                               bg=ORCA_PANEL_BG, fg=ORCA_FG, highlightthickness=1,
                                               highlightbackground=ORCA_BORDER, selectbackground=ORCA_ACCENT,
                                               selectforeground=ORCA_ACCENT_FG)
                shared_left_list.pack(fill="y", expand=True, anchor="w")
                group["shared_left_list"] = shared_left_list
                group["shared_left_frame"] = left_frame
                # Tooltip goes on the shared widget exactly ONCE, right
                # here at creation time -- not down in the per-picker
                # block below. Adding it there instead would run it for
                # every sibling picker in the group (up to 3x for
                # processor_order "first"/"last" + processor_denylist)
                # and, since each call just adds another <Enter> binding
                # on top of the SAME widget (see Tooltip.__init__'s
                # add="+"), leave hovering "Discovered..." popping up to
                # three tooltips stacked on top of each other -- one per
                # picker's own (differing) comment text. This one fixed,
                # group-generic description avoids that.
                shared_tt = ("Everything found in gui/ (besides orcastrator.py itself) that isn't already "
                             "claimed by the order list to the right." if kind == "gui_order" else
                             "Everything found in post_processors/ that isn't already claimed by \"Runs "
                             "first\", \"Runs last\", or \"Denylist\" to the right.")
                Tooltip(shared_left_list, shared_tt)
            else:
                shared_left_list = group["shared_left_list"]

            # Everything about THIS specific picker -- its Add/Remove(/Up/
            # Down) buttons and its own right-hand "selected" list -- sits
            # in its own holder, one per row, at a column past the shared
            # list (which is why the shared list above used column=1 and
            # this uses column=3: 2 is left free in case this field spec
            # ever grows a "hint", same convention every other kind uses).
            holder = ttk.Frame(parent)
            # padx right-hand 8px mirrors the left_frame inset above (and
            # the 8px right margin the "hint" column already uses
            # elsewhere, see padx=(10, 8) below) -- without it this was
            # the section's rightmost content, sitting flush against the
            # right border with no matching gap.
            holder.grid(row=row, column=3, sticky="w", pady=3, padx=(0, 8))

            btn_frame = ttk.Frame(holder)
            btn_frame.grid(row=0, column=0, sticky="ns", padx=(0, 6))

            right_frame = ttk.Frame(holder)
            right_frame.grid(row=0, column=1, sticky="n")
            if kind == "gui_order":
                right_title = "Landing-page order (top to bottom, after OrcaStrator itself)"
            elif kind == "processor_order":
                right_title = ("Order (runs last, top to bottom)" if spec.get("order_position") == "last"
                                else "Order (runs first, top to bottom)")
            else:
                right_title = "Denylisted (never runs)"
            # Listbox built (but not yet packed) before the label so its
            # real rendered width is known -- gui_order's title is a full
            # sentence, wider than the 274px listbox, and without a cap
            # the label becomes right_frame's widest child via
            # pack_propagate, dragging the WHOLE column (Up/Down
            # included, see below) wider than the listbox actually is,
            # even after anchoring the listbox itself to the left fixed
            # the centering half of this. Wrapping the label to the
            # listbox's own width keeps the frame's width pinned to the
            # listbox no matter how long any picker's title ends up.
            right_list = tk.Listbox(right_frame, height=5, width=30, exportselection=False,
                                     bg=ORCA_PANEL_BG, fg=ORCA_FG, highlightthickness=1,
                                     highlightbackground=ORCA_BORDER, selectbackground=ORCA_ACCENT,
                                     selectforeground=ORCA_ACCENT_FG)
            right_list.update_idletasks()
            ttk.Label(right_frame, text=right_title, style="PickerHeader.TLabel",
                      wraplength=right_list.winfo_reqwidth()).pack(anchor="w")
            # anchor="w" matters here specifically -- pack()'s default
            # anchor is "center", which was invisible for the three
            # processor pickers (their labels are narrower than the
            # 274px listbox, so the listbox was already the widest
            # child and centering was a no-op) but very visible for
            # gui_order's much longer label ("Landing-page order (top
            # to bottom, after OrcaStrator itself)"), which becomes the
            # frame's widest child and was centering the listbox inside
            # that extra width instead of sitting flush against it.
            right_list.pack(anchor="w")

            # Up/Down get their own column, to the right of the selected-
            # side listbox rather than sandwiched into btn_frame with
            # Add/Remove -- they only ever act on the right-hand list, so
            # sitting right next to it reads more clearly than sitting
            # between the two lists next to Add/Remove, which act on
            # either side. Buttons are only built for processor_order/
            # gui_order (reorder); processor_denylist has no order to
            # reorder. The column itself, though, is ALWAYS gridded --
            # a picker with no Up/Down buttons still needs to reserve
            # that column's width, or its holder ends up narrower than
            # a sibling picker that has them, throwing off which pixel
            # their right-hand listboxes actually start at even though
            # everything before this column lines up. Measured from a
            # throwaway probe button rather than a hardcoded pixel
            # guess, so it can't quietly drift out of sync if the
            # theme/font/button padding ever changes.
            updown_frame = ttk.Frame(holder)
            updown_frame.grid(row=0, column=2, sticky="ns", padx=(6, 0))
            if reorder:
                ttk.Label(updown_frame, text=" ", style="PickerHeader.TLabel").pack(anchor="w")
                ttk.Frame(updown_frame).pack(fill="y", expand=True)
                ttk.Button(updown_frame, text="\u2191 Up", width=10, command=lambda: _move(-1)).pack(pady=2)
                ttk.Button(updown_frame, text="\u2193 Down", width=10, command=lambda: _move(1)).pack(pady=2)
                ttk.Frame(updown_frame).pack(fill="y", expand=True)
            else:
                probe = ttk.Button(updown_frame, text="\u2193 Down", width=10)
                probe.update_idletasks()
                updown_frame.configure(width=probe.winfo_reqwidth())
                probe.destroy()
                updown_frame.grid_propagate(False)

            def _refresh_shared_left(group_key=group_key, all_scripts=all_scripts):
                """Redraws the ONE shared "Discovered..." list for this
                group. Since every picker in the group points at the
                same widget, there's no "claimed by everyone ELSE"
                distinction to make -- a name just needs excluding the
                moment ANY picker in the group has claimed it."""
                g = self._picker_groups.get(group_key)
                if g is None or g["shared_left_list"] is None:
                    return
                claimed = set()
                for member in g["members"]:
                    claimed.update(member["current"])
                widget = g["shared_left_list"]
                widget.delete(0, "end")
                items = sorted(s for s in all_scripts if s not in claimed)
                for n in items:
                    widget.insert("end", n)
                # Grow past the 5-row default rather than let the list
                # fill the box edge-to-edge -- with no empty row visible
                # there's no visual cue that this IS the full list (as
                # opposed to just scrolled to hide more below), so the
                # box always keeps at least one blank row past however
                # many items it's currently holding.
                widget.configure(height=max(5, len(items) + 1))

            def _refresh_right(current=current, right_list=right_list):
                right_list.delete(0, "end")
                for n in current:
                    right_list.insert("end", n)
                # Same "always one empty row" rule as the shared list
                # above, kept in sync on every add/remove/reorder.
                right_list.configure(height=max(5, len(current) + 1))

            def _refresh_group(group_key=group_key):
                """Re-draws the shared left list plus every picker's own
                right-hand list in this group -- picking a name in any ONE
                of them can only free it up or take it away from the
                others, so all of them need to re-check against the new
                claims, not just the picker that changed."""
                _refresh_shared_left(group_key)
                for member in self._picker_groups.get(group_key, {}).get("members", ()):
                    member["refresh_right"]()

            def _commit(path=path, current=current):
                self._on_change(path, list(current))

            def _add(event=None, current=current, shared_left_list=shared_left_list, group_key=group_key):
                sel = shared_left_list.curselection()
                if not sel:
                    return
                name = shared_left_list.get(sel[0])
                if name not in current:
                    current.append(name)
                _refresh_group(group_key)
                _commit()

            def _remove(event=None, current=current, right_list=right_list, group_key=group_key):
                sel = right_list.curselection()
                if not sel:
                    return
                del current[sel[0]]
                _refresh_group(group_key)
                _commit()

            def _move(delta, current=current, right_list=right_list):
                sel = right_list.curselection()
                if not sel:
                    return
                idx = sel[0]
                new_idx = idx + delta
                if not (0 <= new_idx < len(current)):
                    return
                current[idx], current[new_idx] = current[new_idx], current[idx]
                _refresh_right()
                right_list.selection_set(new_idx)
                _commit()

            group["members"].append(dict(current=current, refresh_right=_refresh_right))
            group["refresh_group"] = _refresh_group  # whichever picker built last wins; all equivalent

            # Vertically center Add/Remove (and Up/Down) against the
            # listboxes beside them -- a blank label matches the "Order
            # (...)"/"Discovered..." label height above the listboxes so
            # this column starts level with them rather than the very top
            # of the row, and the two expanding spacer frames around the
            # buttons split whatever room is left (the shared list can be
            # much taller than 2 buttons once it's rowspanning 3 rows)
            # evenly above and below, instead of leaving it all beneath.
            ttk.Label(btn_frame, text=" ", style="PickerHeader.TLabel").pack(anchor="w")
            ttk.Frame(btn_frame).pack(fill="y", expand=True)
            ttk.Button(btn_frame, text="Add \u2192", width=10, command=_add).pack(pady=2)
            ttk.Button(btn_frame, text="\u2190 Remove", width=10, command=_remove).pack(pady=2)
            ttk.Frame(btn_frame).pack(fill="y", expand=True)
            # No double-click-to-add on the shared list anymore -- with
            # up to 3 pickers now pointing at the same widget, a
            # double-click can't tell which one you meant. Selecting an
            # item and clicking THAT row's own Add button is unambiguous;
            # double-click-to-remove on a picker's own (never shared)
            # right-hand list stays, since that's never ambiguous.
            right_list.bind("<Double-Button-1>", _remove)

            if not all_scripts:
                empty_msg = ("No other gui/*.py pages found (besides orcastrator.py itself)."
                              if kind == "gui_order" else "No processor scripts found in post_processors/.")
                ttk.Label(holder, text=empty_msg,
                          style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

            _refresh_right()

            if tooltip_text:
                Tooltip(right_list, tooltip_text)

        if spec.get("hint"):
            ttk.Label(parent, text=spec["hint"], style="Hint.TLabel").grid(
                row=row, column=2, sticky="w", padx=(10, 8))

        return parent.grid_slaves(row=row)

    def _add_section_fields(self, frame, fields):
        """
        Adds every field in a section to `frame` and wires up show/hide
        for any that carry a "show_if". If EVERY field in the section
        turns out conditional (none are the unconditional toggle that
        governs the rest), the whole section frame hides too when
        they're all currently false -- an empty heading with nothing
        under it isn't worth keeping on screen.
        """
        entries = []
        for row, spec in enumerate(fields):
            widgets = self._add_field(frame, row, spec)
            entries.append({"widgets": widgets, "spec": spec})
            if spec.get("show_if"):
                self._field_vis_rules.append({"widgets": widgets, "spec": spec})
        if entries and all(e["spec"].get("show_if") for e in entries):
            self._section_vis_rules.append({
                "frame": frame,
                "pack_info": dict(frame.pack_info()),
                "entries": entries,
            })

    def _remember_default(self, path, value):
        """
        Records what a field's own widget would fall back to if `path`
        is missing from self.cfg -- see _field_visible's docstring for
        why this matters: a show_if condition referencing this path
        needs the SAME fallback the field itself uses, not plain
        get_in's None, or a config predating this field would hide
        whatever depends on it until the field got toggled once.
        """
        self._field_defaults[tuple(path)] = value

    def _apply_visibility(self):
        for rule in self._field_vis_rules:
            visible = _field_visible(self.cfg, rule["spec"], self._field_defaults)
            for w in rule["widgets"]:
                if visible:
                    w.grid()
                else:
                    w.grid_remove()
        for rule in self._section_vis_rules:
            frame = rule["frame"]
            any_visible = any(_field_visible(self.cfg, e["spec"], self._field_defaults) for e in rule["entries"])
            if any_visible and not frame.winfo_ismapped():
                self._pack_section_in_order(frame, rule["pack_info"])
            elif not any_visible and frame.winfo_ismapped():
                frame.pack_forget()

    def _align_field_columns(self, frames):
        """
        Lines up every section's label column at the same x position,
        so the entry/spinbox/combobox column reads as one straight
        line down the page -- matching OrcaSlicer's own settings
        panels -- instead of each Labelframe in `frames` sizing its
        own column 0 independently to just that section's own label
        widths (grid's normal per-container behavior, and what was
        happening before: a section with a long label like "Fail
        color (hop check failure)" pushed its own entries out further
        right than a section whose longest label was short, like
        "Enabled").

        Must run after every field in `frames` has been gridded (i.e.
        after _add_section_fields for each frame), since it measures
        the labels' actual requested widths via update_idletasks().

        Measures self._section_field_labels rather than walking each
        frame's grid_slaves(column=0) directly -- grid_remove() (used
        for show_if fields currently hidden) drops a widget out of
        grid_slaves() entirely, so a hidden field with the longest
        label on the page would otherwise be missed here, and the
        column would jump out of alignment the moment that field
        later became visible. self._section_field_labels is populated
        in _add_field regardless of visibility, right when each label
        is created.
        """
        self.root.update_idletasks()
        raw_max = max((lbl.winfo_reqwidth() for lbl in self._section_field_labels), default=0)
        if raw_max <= 0:
            return
        # Every label grids with padx=(8, 6) (see _add_field) -- that
        # padding is added to the CELL Tk actually needs for that row,
        # but winfo_reqwidth() only reports the label's own text size,
        # not the padding around it. Leaving it out here would let a
        # section whose own longest label sits close to (or at) this
        # page's overall widest label keep needing more room than a
        # bare-reqwidth minsize actually grants -- its cell would win
        # out over the minsize instead of matching it, nudging that
        # one section's entries a few pixels further right than
        # everywhere else on the page. Baking the same padding into
        # the minsize itself keeps it the true floor everywhere.
        max_width = raw_max + 14
        for frame in frames:
            if frame in self._align_exempt_frames:
                # Processor Selection / Settings Landing Page (see
                # _add_field's processor_denylist/processor_order/
                # gui_order branch) -- these sections have no label
                # column to speak of, so forcing column 0 out to this
                # page's max_width would just be blank space shoving
                # their picker widgets needlessly far right. Left at
                # grid's own default per-frame sizing instead.
                continue
            frame.grid_columnconfigure(0, minsize=max_width, weight=0)
            # weight=1 on the value column, NOT the label column, so that
            # any extra width a spanning row needs (point_table's
            # holder -- see _add_field -- grids at columnspan=2) gets
            # granted to column 1 instead of quietly inflating column
            # 0's actual width past the minsize just set above, which
            # would nudge that one section's entries out of line with
            # every other section again. Harmless everywhere else too:
            # every value widget grids with sticky="w", so any leftover
            # width column 1 picks up just becomes blank space on its
            # right, not a shift in where the widgets themselves sit.
            frame.grid_columnconfigure(1, weight=1)

    def _refresh_picker_groups(self):
        """
        One-time pass, right after every field on the current screen has
        been built (see the callers in open_simple_editor()/
        _build_settings_form()). Two things this fixes, both stemming
        from fields being built one row at a time, top to bottom, with no
        look-ahead at what's still to come:

        1. The shared "Discovered..." list for a group (see _add_field's
           processor_denylist/processor_order/gui_order branch) is built
           by whichever picker in the group comes first, at that
           picker's own row, spanning only itself -- it can't know yet
           how many more rows the rest of the group will occupy. Now
           that the whole screen is built and every member has
           registered, stretch it to its real span (min_row..max_row).

        2. That same first-built picker's initial draw of the shared list
           also couldn't yet exclude names claimed by pickers that didn't
           exist yet. A full redraw now, with the group's true final
           membership, fixes that stale initial state.
        """
        for group in self._picker_groups.values():
            frame = group.get("shared_left_frame")
            if frame is not None:
                span = group["max_row"] - group["min_row"] + 1
                frame.grid_configure(row=group["min_row"], rowspan=span)
            refresh = group.get("refresh_group")
            if refresh is not None:
                refresh()

    def _pack_section_in_order(self, frame, pack_info):
        """
        Re-shows a previously-hidden section frame back in its declared
        spot among sibling sections (self._section_frame_order), rather
        than at the end of the packing order. Plain frame.pack() can't do
        this on its own -- pack() only remembers a slave's own options
        (fill/padx/pady, saved as pack_info when it was hidden), not its
        position relative to siblings, so a forget-then-pack round trip
        normally drops the frame back in at the bottom of the stack.

        Walks forward through the declared order to the next sibling
        that's actually on screen right now and packs before it. If
        every later sibling is also currently hidden, there's nothing to
        go before, so a plain pack() (append at the end) is already the
        correct spot.
        """
        try:
            idx = self._section_frame_order.index(frame)
        except ValueError:
            frame.pack(**pack_info)
            return
        for sibling in self._section_frame_order[idx + 1:]:
            if sibling.winfo_ismapped():
                frame.pack(before=sibling, **pack_info)
                return
        frame.pack(**pack_info)

    # -- state changes ----------------------------------------------------


    def _on_change(self, path, value):
        set_in(self.cfg, path, value)
        self.mark_dirty()
        self._apply_visibility()
        self._refresh_hook()

    def mark_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        star = " *" if self.dirty else ""
        name = self.cfg_path.name if self.cfg_path else ""
        self.root.title(f"OrcaStrator Settings -- {name}{star}")
        if self.reload_btn is not None:
            self.reload_btn.configure(state=("normal" if self.dirty else "disabled"))
        if self.save_btn is not None:
            self.save_btn.configure(state=("normal" if self.dirty else "disabled"))

    def refresh_preview(self):
        for child in self.preview_fixed_holder.winfo_children():
            child.destroy()
        for child in self.preview_canvas_holder.winfo_children():
            child.destroy()
        build_fn = self._preview_entry.get("build_preview_payload")
        if build_fn is None:
            return
        controls = {name: var.get() for name, var in self._preview_vars.items()}
        try:
            payload = build_fn(self.cfg, controls)
        except Exception as exc:
            tk.Label(self.preview_canvas_holder, text=f"Preview error: {exc}",
                     bg=ORCA_BG, fg=ERROR_COLOR, justify="left", wraplength=520).pack(anchor="w", padx=8, pady=8)
            self.status_label.configure(text="")
            return

        if isinstance(payload, dict) and payload.get("kind") == "text":
            # Generic text preview -- a rich-preview plugin whose live
            # preview is more naturally "resolved text" than an
            # SVG_PAYLOAD (gcode_template_notice.py today, any future
            # one) returns {"kind": "text", "title": ..., "text": ...}
            # instead of a shapes/canvas payload, and gets one or more
            # plain read-only Text boxes here instead of the SVG canvas
            # path below. config_editor.pyw still has zero idea what's
            # INSIDE that text -- same "generic side of the contract"
            # spirit as the SVG path.
            #
            # "sections" (a list of {"title", "text", "scroll"}) is the
            # multi-box shape: each becomes its own labeled Text box.
            # "scroll" (default True) picks which holder a section packs
            # into -- self.preview_fixed_holder (stays put, e.g.
            # gcode_template_notice.py's resolved-preview box) or
            # self.preview_canvas_holder (scrolls, e.g. its much-longer
            # placeholder reference box below it) -- both stacked
            # top-to-bottom within their own holder, in the order given,
            # each genuinely a separate field rather than one blob a
            # plugin has to visually fake a split inside of. A plugin
            # with nothing to split just returns a single top-level
            # "text" (no "sections"), which renders exactly as before --
            # one scrolling box, no header, title on self.status_label.
            sections = payload.get("sections")
            if not sections:
                sections = [{"title": payload.get("title", ""), "text": payload.get("text", "")}]
            box_width = 90
            first_in_holder = {True: True, False: True}  # keyed by "is scrolling"
            for section in sections:
                scrolling = section.get("scroll", True)
                holder = self.preview_canvas_holder if scrolling else self.preview_fixed_holder
                text = section.get("text", "")
                title = section.get("title", "")
                if title and len(sections) > 1:
                    # Only headed per-box when there's more than one --
                    # a lone section still uses status_label for its
                    # title, matching the pre-existing single-box look.
                    ttk.Label(holder, text=title, style="PickerHeader.TLabel").pack(
                        anchor="w", padx=8, pady=(0 if first_in_holder[scrolling] else 10, 2))
                first_in_holder[scrolling] = False
                # height in tk.Text's own "lines" unit has to account for
                # word-wrap -- a naive text.count("\n") undercounts badly
                # the moment any single logical line (a long rendered
                # template, say) wraps across several visual ones,
                # silently clipping everything after it with no
                # scrollbar of its own to reveal it. This estimates each
                # logical line's wrapped visual-line count against
                # box_width instead.
                visual_lines = sum(max(1, -(-len(ln) // box_width)) for ln in text.split("\n"))
                box = tk.Text(holder, wrap="word", bg=ORCA_PANEL_BG, fg=ORCA_FG,
                               relief="flat", height=max(6, visual_lines + 2), width=box_width)
                box.insert("1.0", text)
                box.configure(state="disabled")
                # A pinned box only ever gets its own natural height
                # (fill="x", no expand) -- it isn't inside scrollable
                # space to stretch into, and letting it claim extra
                # vertical room would eat into what's left for the
                # scrolling holder below it. A scrolling box keeps the
                # original fill="both"/expand=True.
                if scrolling:
                    box.pack(fill="both", expand=True, padx=8, pady=8)
                else:
                    box.pack(fill="x", padx=8, pady=8)
            # A pinned section's own Text widget has no scrollbar of its
            # own and no extra room to grow into (fill="x" above, not
            # "both"), so if its content is long enough to need
            # scrolling, this is where that would silently show as
            # clipped text -- there's no good generic fallback here.
            # "scroll": False is a plugin's assertion that the content
            # is short enough to always fit at its natural height (e.g.
            # a handful of resolved template lines, not the much-longer
            # placeholder catalog next to it).
            self.status_label.configure(text=payload.get("title", "") if len(sections) == 1 else "")
            return

        # orcastrator.py's _TkProgressUI._draw_payload caps its on-screen
        # size against self._base_width (the real progress window's own
        # auto-sized width, set once during its __init__). This shim has
        # no such window, so it needs a stand-in -- generous enough that
        # it never actually clips a payload's own max_size (canvas_cfg
        # above).
        shim = types.SimpleNamespace(_tk=tk, _base_width=2000)
        try:
            orcastrator._TkProgressUI._draw_payload(shim, self.preview_canvas_holder, payload)
        except Exception as exc:
            tk.Label(self.preview_canvas_holder, text=f"Preview error: {exc}",
                     bg=ORCA_BG, fg=ERROR_COLOR, justify="left", wraplength=520).pack(anchor="w", padx=8, pady=8)
            self.status_label.configure(text="")
            return

        # Everything in this status line comes from the payload dict
        # itself (the generic SVG_PAYLOAD contract) -- nothing here is
        # dock-collision-specific knowledge, so it's accurate for any
        # future processor's HAS_PREVIEW payload too.
        canvas_cfg = payload.get("canvas", {})
        self.status_label.configure(
            text=(f"{payload.get('title', '')}  --  "
                  f"{canvas_cfg.get('x_max', 0):.0f}x{canvas_cfg.get('y_max', 0):.0f} mm, "
                  f"{len(payload.get('shapes', []))} shapes")
        )

    # -- file operations ----------------------------------------------------

    def save(self):
        all_errors = [msg for msgs in self.validation_errors.values() for msg in msgs]
        if all_errors:
            self._error(
                "Fix validation errors first",
                "Not saved -- this config has invalid values (see the red fields):\n\n"
                + "\n".join(f"\u2022 {m}" for m in all_errors))
            return
        try:
            with self.cfg_path.open("w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, indent=4)
            self.dirty = False
            self._update_title()
        except Exception as exc:
            self._error("Save failed", str(exc))

    def save_backup(self):
        """Snapshots the config *currently in the editor* (including any
        unsaved edits) to configs/backups/, timestamped by default but
        renameable via _ask_backup_name(). Never touches self.cfg_path or
        the real file -- purely a side copy. Not gated on validation: a
        backup is a safety net, not something a processor will ever load,
        so it's fine to snapshot a config mid-edit."""
        if self.cfg_path is None or self.cfg is None:
            return
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        label = self._ask_backup_name(stamp)
        if label is None:
            return  # cancelled
        try:
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            backup_path = BACKUPS_DIR / f"{self.cfg_path.stem}_{label}.json"
            if backup_path.exists() and not self._askyesno(
                    "Overwrite backup?",
                    f"A backup named \u201c{label}\u201d already exists for "
                    f"{self.cfg_path.name}. Overwrite it?"):
                return
            with backup_path.open("w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, indent=4)
        except Exception as exc:
            self._error("Backup failed", str(exc))
            return
        self._info("Backup saved", f"Saved a backup to:\n\n{backup_path}")

    def _ask_backup_name(self, default_label):
        """Modal prompt for the identifying part of a backup's filename,
        shown right before save_backup() writes it. The full filename is
        always ``<config_stem>_<label>.json`` -- the ``<config_stem>_``
        prefix stays fixed (not editable here) because _pick_backup_dialog()
        and load_backup() both rely on that exact prefix to find and to
        display this config's backups; only the label after it is
        user-editable. The entry is pre-filled with `default_label` (the
        auto-generated timestamp save_backup() already computed) with the
        full text selected, so accepting the default is just Enter and
        typing anything replaces it outright rather than requiring a
        manual select-all first.

        Returns the chosen label (str, never empty -- falls back to
        `default_label` if submitted blank) or None if cancelled.
        """
        win = tk.Toplevel(self.root)
        win.title("Save Backup")
        win.configure(bg=ORCA_BG)
        win.transient(self.root)
        win.resizable(False, False)
        orcastrator.apply_dark_titlebar(win, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)

        body = ttk.Frame(win, padding=(16, 16, 16, 12))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Backup name:").pack(anchor="w", pady=(0, 6))

        # Prefix/suffix shown as plain labels flanking the one editable
        # field, so it's visually obvious the "<stem>_" and ".json" parts
        # aren't part of what you're typing -- same fixed-frame-around-an-
        # entry look as a filename field with a locked extension.
        name_row = ttk.Frame(body)
        name_row.pack(fill="x")
        ttk.Label(name_row, text=f"{self.cfg_path.stem}_", foreground=ORCA_FG_DIM).pack(side="left")
        entry_var = tk.StringVar(value=default_label)
        entry = tk.Entry(name_row, textvariable=entry_var, bg=ORCA_PANEL_BG, fg=ORCA_FG,
                          insertbackground=ORCA_FG, relief="flat", highlightthickness=1,
                          highlightbackground=ORCA_BORDER, highlightcolor=ORCA_ACCENT, width=28)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Label(name_row, text=".json", foreground=ORCA_FG_DIM).pack(side="left")

        result = {"label": None}

        def _sanitize(raw: str) -> str:
            raw = raw.strip()
            # Same characters Windows itself disallows in filenames --
            # stripped rather than rejected outright so a pasted-in name
            # with e.g. a colon in it still saves instead of erroring.
            for ch in '<>:"/\\|?*':
                raw = raw.replace(ch, "")
            return raw

        def _confirm(event=None):
            label = _sanitize(entry_var.get()) or default_label
            result["label"] = label
            win.destroy()

        def _cancel(event=None):
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _cancel)
        win.bind("<Escape>", _cancel)
        entry.bind("<Return>", _confirm)

        btns = ttk.Frame(win, padding=(16, 0, 16, 16))
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=_confirm).pack(side="right")

        self._center_over_root(win)
        win.grab_set()
        entry.focus_set()
        entry.selection_range(0, "end")  # whole default pre-selected -- typing replaces it outright
        entry.icursor("end")
        win.wait_window()
        return result["label"]

    def load_backup(self):
        """Lets you pick a previous backup of *this* config and loads it
        into the editor in memory. Doesn't write anything -- self.cfg_path
        is untouched, so hitting Save afterward writes the restored
        content to the real config a processor actually reads."""
        if self.cfg_path is None:
            return
        matches = sorted(BACKUPS_DIR.glob(f"{self.cfg_path.stem}_*.json"), reverse=True) \
            if BACKUPS_DIR.exists() else []
        if not matches:
            self._info("No backups found",
                       f"No backups found for {self.cfg_path.name} in:\n\n{BACKUPS_DIR}")
            return
        chosen = self._pick_backup_dialog(matches)
        if chosen is None:
            return
        try:
            with chosen.open(encoding="utf-8") as fh:
                backup_cfg = json.load(fh)
        except Exception as exc:
            self._error("Load failed", f"Couldn't read that backup:\n\n{exc}")
            return
        # Reuses the same "unsaved changes?" guard as navigating away --
        # if the current edits aren't saved, you're asked before they're
        # replaced by the backup's contents.
        if not self._confirm_leave():
            return
        # Re-read the file fresh off disk (not self.cfg -- that may
        # already hold unsaved edits, or itself be the result of an
        # earlier restore) so the merge below always has today's schema
        # and today's comments to fall back onto. See
        # _merge_backup_onto_current()'s docstring for why this, and not
        # backup_cfg, is the side that supplies anything the backup is
        # missing or documents.
        try:
            with self.cfg_path.open(encoding="utf-8") as fh:
                current_cfg = json.load(fh)
        except Exception as exc:
            self._error("Load failed", f"Couldn't read the current config:\n\n{exc}")
            return
        merged_cfg = _merge_backup_onto_current(current_cfg, backup_cfg)
        self._reopen(override_cfg=merged_cfg)

    def _center_over_root(self, win):
        """Positions `win` (any Toplevel) centered over self.root rather
        than wherever the window manager happens to place a fresh
        Toplevel (commonly cascaded/offset, or screen center, neither of
        which tracks where our actual window is). Requires an up-to-date
        reqwidth/reqheight, so this needs calling AFTER every widget
        inside `win` is built, and before it's shown (win stays
        invisible-till-positioned since Toplevel geometry changes can
        otherwise flash at the wrong spot for a frame)."""
        win.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        x = rx + (rw - ww) // 2
        y = ry + (rh - wh) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _dialog(self, title, message, buttons, icon=""):
        """
        Orca-skinned stand-in for tkinter.messagebox, used for every
        info/error/yesno/yesnocancel popup in this app instead of the
        stdlib dialogs. The stdlib ones ignore our `parent=` for
        POSITION (Tk only honors it for modality/stacking -- a widely
        hit Tk limitation, not a bug on our end) and always land
        centered on the screen instead of over our window, however
        we call them. Building our own means _center_over_root() (same
        helper _pick_backup_dialog uses) actually works on it, and it
        matches the app's dark theme instead of the stock light one.

        `buttons` is an ordered list of (label, value) tuples, e.g.
        [("Yes", True), ("No", False), ("Cancel", None)] -- shown
        left to right in that order, first entry styled as the
        default/primary action and bound to Enter, last entry treated
        as the "dismiss" action for Escape and the titlebar close
        button. Returns whichever button's value was chosen.
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=ORCA_BG)
        win.transient(self.root)
        win.resizable(False, False)
        orcastrator.apply_dark_titlebar(win, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)

        result = {"value": buttons[-1][1]}

        def _choose(value):
            result["value"] = value
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", lambda: _choose(buttons[-1][1]))
        win.bind("<Escape>", lambda e: _choose(buttons[-1][1]))
        win.bind("<Return>", lambda e: _choose(buttons[0][1]))

        body = ttk.Frame(win, padding=(16, 16, 16, 12))
        body.pack(fill="both", expand=True)
        if icon:
            ttk.Label(body, text=icon, foreground=ORCA_ACCENT, font=("Segoe UI", 18)).pack(
                side="left", anchor="n", padx=(0, 12))
        ttk.Label(body, text=message, justify="left", wraplength=360).pack(
            side="left", anchor="w", fill="both", expand=True)

        btn_frame = ttk.Frame(win, padding=(16, 0, 16, 16))
        btn_frame.pack(fill="x")
        for i, (label, value) in enumerate(reversed(buttons)):
            style = "Accent.TButton" if value == buttons[0][1] else "TButton"
            ttk.Button(btn_frame, text=label, style=style, width=10,
                       command=lambda v=value: _choose(v)).pack(side="right", padx=(6, 0))

        win.grab_set()
        self._center_over_root(win)
        win.focus_set()
        win.wait_window()
        return result["value"]

    def _info(self, title, message):
        self._dialog(title, message, [("OK", True)], icon="\u2139")

    def _error(self, title, message):
        self._dialog(title, message, [("OK", True)], icon="\u26a0")

    def _askyesno(self, title, message):
        return bool(self._dialog(title, message, [("Yes", True), ("No", False)], icon="\u2753"))

    def _askyesnocancel(self, title, message):
        return self._dialog(title, message, [("Yes", True), ("No", False), ("Cancel", None)], icon="\u2753")

    def _pick_backup_dialog(self, backup_paths):
        """Modal listbox of backups (newest first), returns the chosen
        pathlib.Path or None if cancelled."""
        win = tk.Toplevel(self.root)
        win.title("Load Backup")
        win.configure(bg=ORCA_BG)
        win.transient(self.root)
        win.grab_set()
        orcastrator.apply_dark_titlebar(win, caption_hex=ORCA_TITLEBAR, text_hex=ORCA_TITLEBAR_FG)

        ttk.Label(win, text=f"Backups for {self.cfg_path.name}:").pack(anchor="w", padx=10, pady=(10, 4))
        list_frame = tk.Frame(win, bg=ORCA_PANEL_BG, highlightthickness=1, highlightbackground=ORCA_BORDER)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        listbox = tk.Listbox(list_frame, bg=ORCA_PANEL_BG, fg=ORCA_FG, selectbackground=ORCA_ACCENT,
                              selectforeground=ORCA_ACCENT_FG, borderwidth=0, highlightthickness=0,
                              activestyle="none", width=50, height=10)
        listbox.pack(fill="both", expand=True, padx=1, pady=1)
        for p in backup_paths:
            stamp = p.stem[len(self.cfg_path.stem) + 1:]  # strip "<config_stem>_" prefix
            listbox.insert("end", stamp)
        listbox.selection_set(0)

        result = {"path": None}

        def _confirm(event=None):
            sel = listbox.curselection()
            if sel:
                result["path"] = backup_paths[sel[0]]
            win.destroy()

        def _cancel(event=None):
            win.destroy()

        listbox.bind("<Double-Button-1>", _confirm)
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="Load", style="Accent.TButton", command=_confirm).pack(side="right")

        self._center_over_root(win)
        win.wait_window()
        return result["path"]

    def reload(self):
        """Shared by every editor kind -- each open_*() call points
        self._reopen at itself with its own path/entry captured, so
        reloading is just "run that same open call again", which
        rebuilds the whole screen fresh from disk with zero per-kind
        special-casing needed here."""
        if self.dirty and not self._askyesno("Discard changes?", "Reload from disk and discard unsaved changes?"):
            return
        self._reopen()

    def _on_close(self):
        if self.dirty:
            resp = self._askyesnocancel("Unsaved changes", "Save changes before closing?")
            if resp is None:
                return
            if resp:
                self.save()
        # Always-on (no toggle, see __init__) -- best-effort, must never
        # be able to block the window from actually closing.
        if _window_anchor is not None:
            try:
                self.root.update_idletasks()
                _window_anchor.save_window_geometry(
                    "config_editor",
                    self.root.winfo_x(), self.root.winfo_y(),
                    self.root.winfo_width(), self.root.winfo_height(),
                )
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    start_path = None
    if len(sys.argv) > 1:
        start_path = pathlib.Path(sys.argv[1]).resolve()
        if not start_path.exists():
            print(f"Config file not found: {start_path}", file=sys.stderr)
            sys.exit(1)
    app = SettingsApp(start_path)
    app.run()


if __name__ == "__main__":
    main()
