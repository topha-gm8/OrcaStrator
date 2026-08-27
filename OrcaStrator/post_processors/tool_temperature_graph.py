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
Tool temperature graph -- OrcaSlicer post-processing script
=========================================================================
Read-only visualization: a temperature-vs-time graph, one colored curve
per tool, built from every `M104`/`M109 S<temp> T<n>` command actually
present in the FINAL g-code. This is a simulated curve, not a real
thermistor reading -- there's no telemetry available at export time --
so it's only ever as accurate as the two ramp assumptions it's built on:

  - Warming up (a new commanded target higher than wherever the curve
    currently sits) ramps linearly over tool_preheat.json's
    target_lead_seconds.
  - Cooling down (a new commanded target lower than wherever the curve
    currently sits) ramps linearly over tool_preheat.json's
    target_cooldown_seconds.
  - EXCEPT `M109` (wait-for-temp), which is exempt from both of the
    above: real firmware BLOCKS the print until that target is
    genuinely reached, so unlike `M104` there's no "did it get there
    before the next command interrupted it" question to model -- it
    always has, by the time anything after it runs. Applying the same
    multi-ten-second lead_seconds/cooldown_seconds ramp to an `M109`
    anyway would make a tool that's cycled through short,
    frequent M109-bounded ramps (reheat, wait, quick feature, cooldown,
    repeat) look like it never finished climbing -- the naive-time gap
    to the NEXT command is often shorter than lead_seconds itself, so
    build_tool_curve()'s own interruption behavior (an event landing
    mid-ramp bends the curve from wherever it actually is, see below)
    would keep lopping the climb off early even though the real tool did
    reach target every single time. `M109` events instead ramp over
    m109_settle_seconds (config, default 5s) -- short enough to read as
    "already there" rather than a real thermal ramp, since that's what
    it represents: the guarantee, not a simulated approach to it.

target_lead_seconds/target_cooldown_seconds come from tool_preheat.json
-- shared with insert_missing_tool_preheat.py and disable_unused_tool_
temps.py -- rather than being duplicated here with their own defaults
that could drift out of sync. See tool_preheat.json for the full
rationale on those two numbers. m109_settle_seconds is NOT shared with
those two -- it's a pure rendering assumption specific to how THIS
graph visualizes an already-guaranteed temperature, with no bearing on
the actual g-code timing decisions those other two processors make --
so it lives in this processor's own config instead.

Every tool starts cold (0 deg) at t=0. Each new commanded target starts
a fresh ramp from the curve's CURRENT value at that moment -- including
mid-ramp, so a command that lands before a previous ramp finishes bends
the curve from wherever it actually is, not from the old target. See
build_tool_curve() for the exact state machine.

Because every ramp is already linear by construction, the whole curve is
just the straight line connecting the segment boundaries -- no KDE-style
resampling needed (unlike toolchange_heatmap.py's gradient blocks, which
approximate a genuinely nonlinear density curve with many thin strips).

Purely informational -- never blocks a print, never touches the g-code.
Always exits 0 and only ever emits "info" notices. Depends on seeing the
FINAL placement of every M104/M109 (including ones insert_missing_tool_
preheat.py/disable_unused_tool_temps.py insert or relocate), so add it
to explicit_order_last in configs/orcastrator.json (OrcaStrator Settings
-> Processor Selection -> "Runs last") once it's dropped in, same as
toolchange_heatmap.py.

Timing model: reuses helpers/time_estimator.py's naive (distance/
feedrate) build_cumulative_time() for a line's raw elapsed-time position,
then layers on the same three corrections toolchange_heatmap.py already
established are necessary and applies them the same way (see that
module's docstring for the full rationale on why each one exists):

  - every line's naive time gets rescaled by naive_time_scale_factor()
    so the point where real machine motion stops (the g-code's
    `; EXECUTABLE_BLOCK_END` marker, or EOF if that's missing) lines up
    with OrcaSlicer's own stated estimated printing time, rather than
    the naive model's own (systematically shorter, since it has no
    acceleration/jerk model) estimate for that same point. Without this,
    an event sitting right at the end of the executable g-code -- e.g.
    disable_unused_tool_temps.py's final `M104 S0` after a tool's last
    use -- lands noticeably earlier than it really would, leaving an
    inflated flat stretch between it and the axis's right edge that
    looks like idle time but is really just model drift.
  - machine_tool_change_time seconds get added once per toolchange that
    has ALREADY happened by a given line -- not just at toolchange
    events themselves, since a temperature command usually sits a few
    lines away from its toolchange, not directly on it. This is real
    seconds already, not naive-model time, so it's added AFTER the
    rescale above rather than before.
  - the timeline's right edge is OrcaSlicer's own stated estimated
    printing time, never just the last temperature command's own
    timestamp, so a trailing stretch of the print with nothing left to
    heat/cool doesn't silently vanish off the right edge of the graph.

Rendering: no gradient-fill primitive exists on this canvas (same
constraint toolchange_heatmap.py works around), but a temperature curve
is already piecewise-LINEAR by construction (see above), so no
approximation is needed there either -- each tool's line is a single
"path" shape (a real stroked polyline, one shape covering every curve
vertex) rather than a filled ribbon polygon standing in for one. A real
stroke-width has no assumption about canvas scale to get wrong, since
both renderers (Klipper's _SVG_TOOLS / OrcaStrator_render.cfg and
orcastrator.py's own Tk progress window, see svg_tools.cfg's polyline()
and orcastrator.py's own "path" case in _draw_payload) apply it as a
literal final-pixel width regardless of scale -- unlike a pre-computed
ribbon polygon, whose thickness would need to be baked in assuming a
particular px-per-canvas-unit ratio, and could silently drift out of
sync with however large the canvas actually ends up displayed. One
"path" shape per tool plus one per reference line (see reference_temps
param below) is all this processor emits for anything line-shaped.
Every point emitted (both curves and the area fill below) is also
rounded to COORD_DECIMALS -- full float64 precision is wasted on a
canvas rendered at a few hundred px. The filled area (if curve_style
includes one) is still a single closed "polygon" under the curve --
that one's genuinely a filled shape, not a line standing in for one, so
"polygon" is exactly right for it.
Renders where svg_target says to (default "both") via the exact same
SVG_PAYLOAD contract and generic canvas renderer every other processor
here uses.

Unlike toolchange_heatmap.py's flat, fully-opaque fills (a deliberate
choice there to avoid classic Tk's alpha-as-stipple dithering reading as
noise on a single-color band), this processor's filled areas use real
alpha (area_alpha) on purpose: several tools' curves routinely overlap
in time, and a translucent fill is the only way an occluded tool's area
stays visible underneath another one. The printer/browser-rendered SVG
side gets true alpha; OrcaStrator's own Tk progress window approximates
it with the same dither-stipple _color_to_tk always falls back to for
alpha < 1 -- tolerable for a handful of overlapping curves, unlike a
single dense band. Line strokes stay fully opaque either way (fill_style
"solid"), since a line loses its whole purpose (marking exactly where a
curve is) if it's translucent.

Colors are read from a single tool_colors config string (comma-separated
hex, tool number = list position) rather than a fixed per-tool field
list -- there's no cap on how many tools a toolchanger might have, and a
fixed set of N color fields would either waste GUI space for a small
setup or silently run out for a large one. A tool beyond the end of that
list (or an invalid/blank entry within it) gets an auto-generated color
instead, spread across the hue wheel with the golden-angle step
(0.6180339887...) so consecutive auto-assigned tools stay visually
distinct from each other even when there are many of them -- see
auto_color_for_tool().
"""
import colorsys
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from helpers.time_estimator import PAT_TOOLCHANGE, PAT_M104, PAT_M109, find_config_path
from helpers.timeline_scale import (
    CANVAS_PAD, CANVAS_UNIT_REF, calibrate_timeline, resolve_canvas_dims,
)
from helpers.debug_dump import write_debug_dump, read_debug_dump, list_debug_dumps
from helpers.notice import display_flag as _notice_display_flag

SCRIPT_PATH = pathlib.Path(__file__).resolve()

SHARED_CONFIG_FILENAME = "tool_preheat.json"
# Fallback ONLY if tool_preheat.json is missing/unreadable -- matches
# disable_unused_tool_temps.py's own fallback exactly, so a missing
# shared config produces the same assumed ramp everywhere it's used.
DEFAULT_TARGET_LEAD_SECONDS = 90.0
DEFAULT_TARGET_COOLDOWN_SECONDS = 0.0

# NOT "default_filament_colour" -- that's the color profile's own
# default before any per-plate override, "filament_colour" (no
# "default_") is what was actually loaded/picked for THIS export. Both
# live in OrcaSlicer's trailing "; key = value" config-dump block, same
# place helpers/timeline_scale.py's own patterns read from.
PAT_FILAMENT_COLOUR = re.compile(r'^\s*;\s*filament_colour\s*=\s*(.*)$', re.IGNORECASE)
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

# CANVAS_UNIT_REF/CANVAS_PAD now live in helpers/timeline_scale.py
# (imported above) -- shared with toolchange_heatmap.py so both
# processors' timelines use the exact same internal coordinate space
# and can be compared/overlaid directly. Only the per-processor default
# pixel size stays local, since each timeline has its own natural size.
DEFAULT_CANVAS_WIDTH_PX = 480.0
DEFAULT_CANVAS_HEIGHT_PX = 220.0
DEFAULT_STACK_LANE_HEIGHT_PX = 90.0
DEFAULT_STACK_GAP_PX = 6.0

DEFAULTS = {
    "tool_colors": "",
    "curve_style": "area_line",
    "line_width_px": 2.0,
    "area_alpha": 0.35,
    "m109_settle_seconds": 5.0,
    "reference_lines_enabled": True,
    "reference_line_color": "#ffffff",
    "reference_line_opacity": 0.5,
    "reference_line_width_px": 1.0,
    "reference_line_style": "dashed",
    "reference_line_labels_enabled": True,
    "reference_line_label_size_px": 24.0,
    "tool_labels_enabled": True,
    "tool_label_size_px": 24.0,
    "y_max_celsius": None,
    "tool_change_time_seconds": None,
    "print_duration_seconds": None,
    "min_tools": 1,
    "svg_target": "both",
    "canvas_width_px": DEFAULT_CANVAS_WIDTH_PX,
    "canvas_height_px": DEFAULT_CANVAS_HEIGHT_PX,
    "layout": "overlay",
    "stack_lane_height_px": DEFAULT_STACK_LANE_HEIGHT_PX,
    "stack_gap_px": DEFAULT_STACK_GAP_PX,
    "debug": {"enabled": True},
    "notice": {"display": True},
}

_SVG_TARGET_MAP = {"pc": ["pc"], "printer": ["printer"], "both": ["pc", "printer"]}


def resolve_svg_targets(cfg: dict):
    """Same convention as toolchange_heatmap.py's resolve_svg_targets() --
    no "off", since this whole processor IS the visualization."""
    return _SVG_TARGET_MAP.get(str(cfg.get("svg_target") or "both").strip().lower(), ["pc", "printer"])


# Set once process() has loaded this processor's own config -- see
# helpers/notice.py's docstring. Starts empty (reads as "display on",
# the default) so nothing before that point is ever embedded with
# display=false by a config it hasn't read yet.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


def print_svg_payload(payload: dict) -> None:
    print("SVG_PAYLOAD:" + json.dumps(payload, separators=(",", ":")))


def friendly_filename(p: pathlib.Path) -> str:
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["debug"] = dict(DEFAULTS["debug"])
    cfg["notice"] = dict(DEFAULTS["notice"])
    path = find_config_path("tool_temperature_graph.json", SCRIPT_PATH)
    if path is None:
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    for key in ("tool_colors", "curve_style", "line_width_px", "area_alpha", "m109_settle_seconds",
                "reference_lines_enabled", "reference_line_color", "reference_line_opacity",
                "reference_line_width_px", "reference_line_style",
                "reference_line_labels_enabled", "reference_line_label_size_px",
                "tool_labels_enabled", "tool_label_size_px",
                "y_max_celsius", "tool_change_time_seconds", "print_duration_seconds", "min_tools",
                "svg_target", "canvas_width_px", "canvas_height_px", "layout", "stack_lane_height_px",
                "stack_gap_px"):
        if key in raw:
            cfg[key] = raw[key]
    if isinstance(raw.get("debug"), dict):
        cfg["debug"] = {**cfg["debug"], **raw["debug"]}
    if isinstance(raw.get("notice"), dict):
        cfg["notice"] = {**cfg["notice"], **raw["notice"]}
    return cfg


def load_shared_preheat_config() -> dict:
    """target_lead_seconds/target_cooldown_seconds from tool_preheat.json --
    the SAME shared file insert_missing_tool_preheat.py/disable_unused_
    tool_temps.py read, via the standard find_config_path() lookup.
    Missing/unreadable file falls back to this processor's own copy of
    the same defaults those two scripts use."""
    result = {"target_lead_seconds": DEFAULT_TARGET_LEAD_SECONDS,
              "target_cooldown_seconds": DEFAULT_TARGET_COOLDOWN_SECONDS}
    path = find_config_path(SHARED_CONFIG_FILENAME, SCRIPT_PATH)
    if path is None:
        return result
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return result
    if "target_lead_seconds" in raw:
        try:
            result["target_lead_seconds"] = float(raw["target_lead_seconds"])
        except (TypeError, ValueError):
            pass
    if "target_cooldown_seconds" in raw:
        try:
            result["target_cooldown_seconds"] = float(raw["target_cooldown_seconds"])
        except (TypeError, ValueError):
            pass
    return result


def _resolve_canvas_dims(cfg: dict):
    return resolve_canvas_dims(cfg, DEFAULT_CANVAS_WIDTH_PX, DEFAULT_CANVAS_HEIGHT_PX)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def find_filament_colors(lines):
    """Parses OrcaSlicer's own "; filament_colour = #a;#b;#c;..." line --
    the actual colors chosen for this export, in filament/extruder slot
    order (position 0 == T0, matching tool_colors' own convention). This
    is the physical filament color loaded for each tool, not a graph
    styling choice, so it's read from the g-code same as machine_tool_
    change_time/estimated printing time above rather than being a
    setting on this processor's own config at all.

    Returns a list of "#rrggbb" strings, or [] if the line's missing.
    A malformed entry becomes None at that position, same non-shifting
    convention as parse_tool_colors() -- one bad entry doesn't discard
    every color after it. Some OrcaSlicer versions emit 8 hex digits
    (alpha channel included); the trailing 2 are dropped since nothing
    downstream of this uses alpha.
    """
    for ln in lines:
        m = PAT_FILAMENT_COLOUR.match(ln)
        if not m:
            continue
        out = []
        for part in m.group(1).strip().split(";"):
            part = part.strip()
            if not part:
                out.append(None)
                continue
            candidate = part if part.startswith("#") else "#" + part
            if len(candidate) == 9:  # "#rrggbbaa" -> "#rrggbb"
                candidate = candidate[:7]
            out.append(candidate.lower() if _HEX_RE.match(candidate) else None)
        return out
    return []


def hex_to_rgb(h: str):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        h = "888888"
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (136, 136, 136)


def auto_color_for_tool(tool: int) -> str:
    """Golden-angle hue step -- consecutive tool numbers land far apart
    on the color wheel regardless of how many there are, so auto-
    assigned tools stay visually distinct from each other (and, in
    practice, usually from the manual tool_colors palette too) even for
    a toolchanger with far more than 7 tools."""
    hue = (tool * 0.6180339887498949) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.92)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def parse_tool_colors(cfg: dict):
    """Comma-separated hex string -> list of "#rrggbb" strings, tool
    number == list position. A blank or invalid entry becomes None at
    that position (falls back to the g-code's own filament_colour, then
    auto_color_for_tool, for that specific tool) rather than shifting
    every entry after it -- so a typo in the middle of the list doesn't
    silently reassign every later tool's color too. An entirely empty/
    unset config (the shipped default) returns [] -- "no manual choice
    made anywhere", not "one blank entry at position 0" -- so it doesn't
    even shadow position 0's filament_colour/auto fallback below."""
    raw = str(cfg.get("tool_colors") or "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            out.append(None)
            continue
        candidate = part if part.startswith("#") else "#" + part
        out.append(candidate.lower() if _HEX_RE.match(candidate) else None)
    return out


def color_for_tool(tool: int, manual_colors: list, filament_colors: list = None) -> str:
    """Three tiers, in order: (1) an explicit tool_colors entry for this
    tool -- a deliberate choice always wins outright; (2) the g-code's
    own filament_colour for this tool -- what OrcaStrator falls back to
    by default now, since it's the ACTUAL filament color loaded for the
    print, not a guess; (3) auto_color_for_tool()'s hue-wheel generation,
    same last resort as always, for whichever tool neither of the above
    has anything for."""
    if 0 <= tool < len(manual_colors) and manual_colors[tool]:
        return manual_colors[tool]
    if filament_colors and 0 <= tool < len(filament_colors) and filament_colors[tool]:
        return filament_colors[tool]
    return auto_color_for_tool(tool)


def find_tool_events(lines):
    """
    Returns (toolchange_line_idxs, temp_events_by_tool):
      - toolchange_line_idxs: sorted list of every line index matching a
        bare/parameterized T<n> toolchange -- used only for the
        machine_tool_change_time offset below, not for deciding which
        tools get graphed.
      - temp_events_by_tool: {tool: [(line_idx, target_temp, is_blocking),
        ...]} from every M104/M109 S<temp> T<n> in the file, in file
        order. A tool only appears here -- and only gets graphed -- if
        it has at least one actual temperature-setting command; a tool
        that's only ever toolchanged to but never explicitly heated has
        nothing meaningful to plot. is_blocking is True for M109 (waits
        for the target to actually be reached before the print
        continues) and False for M104 (fire-and-forget) -- see
        build_tool_curve()'s m109_settle_seconds handling for why that
        distinction matters to the curve, not just the notice text.
    """
    toolchange_line_idxs = []
    temp_events_by_tool = {}
    for i, ln in enumerate(lines):
        if PAT_TOOLCHANGE.match(ln.strip()):
            toolchange_line_idxs.append(i)
            continue
        m104 = PAT_M104.match(ln)
        if m104:
            temp_events_by_tool.setdefault(int(m104.group(2)), []).append(
                (i, int(m104.group(1)), False))
            continue
        m109 = PAT_M109.match(ln)
        if m109:
            temp_events_by_tool.setdefault(int(m109.group(2)), []).append(
                (i, int(m109.group(1)), True))
    return toolchange_line_idxs, temp_events_by_tool


def build_tool_curve(events, total_time, lead_seconds, cooldown_seconds, m109_settle_seconds=5.0):
    """
    events: (t, target_temp, is_blocking) triples for ONE tool, t already
    non-decreasing. Returns a list of (t0, v0, t1, v1) segments,
    contiguous, covering [0, total_time].

    Every tool starts cold (0.0) at t=0. Each event starts a fresh ramp
    from the curve's value AT THAT MOMENT (see value_and_close below) --
    including mid-ramp, so an interrupting command bends the curve from
    wherever it actually is rather than snapping from the previous
    target. Climbing uses lead_seconds, descending uses cooldown_seconds,
    UNLESS is_blocking is True (an M109), in which case it's
    m109_settle_seconds regardless of direction -- M109 blocks the print
    until its target is genuinely reached, so there's nothing to
    linearly approximate the way there is for a fire-and-forget M104;
    see module docstring. A repeat of the already-reached temp is
    always a zero-duration ramp (no visible segment) no matter which
    command produced it. Whatever's left active at total_time gets
    closed out there too, so the returned segments always fully span
    the axis.
    """
    segments = []
    ramp = None          # (start_t, start_v, target_v, end_t) or None
    flat_t, flat_v = 0.0, 0.0

    def value_and_close(event_t):
        nonlocal ramp, flat_t, flat_v
        if ramp is not None:
            r_t0, r_v0, r_target, r_t1 = ramp
            if event_t < r_t1:
                frac = (event_t - r_t0) / (r_t1 - r_t0) if r_t1 > r_t0 else 1.0
                v_now = r_v0 + (r_target - r_v0) * frac
                if event_t > r_t0:
                    segments.append((r_t0, r_v0, event_t, v_now))
                ramp = None
                flat_t, flat_v = event_t, v_now
                return v_now
            segments.append((r_t0, r_v0, r_t1, r_target))
            ramp = None
            flat_t, flat_v = r_t1, r_target
            if event_t > flat_t:
                segments.append((flat_t, flat_v, event_t, flat_v))
                flat_t = event_t
            return flat_v
        if event_t > flat_t:
            segments.append((flat_t, flat_v, event_t, flat_v))
            flat_t = event_t
        return flat_v

    for event_t, target_v, is_blocking in events:
        event_t = max(event_t, flat_t)
        v_now = value_and_close(event_t)
        if target_v == v_now:
            duration = 0.0
        elif is_blocking:
            duration = max(0.0, m109_settle_seconds)
        elif target_v > v_now:
            duration = max(0.0, lead_seconds)
        else:
            duration = max(0.0, cooldown_seconds)
        ramp = (event_t, v_now, target_v, event_t + duration)

    value_and_close(max(total_time, flat_t))
    return segments


def flatten_vertices(segments):
    if not segments:
        return []
    vertices = [(segments[0][0], segments[0][1])]
    for seg in segments:
        vertices.append((seg[2], seg[3]))
    return vertices


# Coordinate rounding for every point emitted into a payload. The canvas
# is a fixed CANVAS_UNIT_REF=1000-unit space rendered at a few hundred
# px, so anything past 2 decimals is invisible -- default float repr
# carries ~17 significant digits, which is most of what made this
# processor's payload large before curves/reference lines moved to real
# "path" strokes (see module docstring) instead of hand-built ribbon
# polygons.
COORD_DECIMALS = 2


def _round_pt(pt):
    x, y = pt
    return [round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)]


def build_svg_payload(tool_curves: dict, cfg: dict, total_time: float, filament_colors: list = None,
                       reference_temps=None):
    """
    Returns (payload, summary) -- summary carries the numbers used for
    the NOTICE message and the debug dump.

    tool_curves: {tool: segments} from build_tool_curve(), already
    computed by the caller (also reused as-is by the GUI's live preview,
    see gui/tool_temperature_graph.py).

    filament_colors: g-code's own find_filament_colors() result (or the
    debug dump's saved copy of it, in the GUI preview's case) -- see
    color_for_tool() for exactly where this sits in the fallback order.
    Optional/None here so any other future caller that doesn't have a
    g-code file handy at all still works, just without that fallback
    tier.

    reference_temps: distinct ACTUAL commanded target temperatures
    (S<temp> from M104/M109, across every tool, already deduplicated
    and with 0 excluded) -- draws a full-width horizontal reference
    line at each one, styled via the reference_line_* cfg keys below.
    Deliberately NOT derived from tool_curves' own vertices here --
    those include every interrupted/interpolated ramp value the curve
    passes through (e.g. an M104 cut off mid-climb at 242.3 degrees,
    see build_tool_curve's docstring), which are not "specifically set
    in the g-code" the way an actual S-value is. The caller (process()
    for a real run, build_preview_payload() for the GUI) computes this
    from the same events_by_tool that fed build_tool_curve() in the
    first place, where the real S-values are still intact. None (the
    default) draws no reference lines at all -- not "derive them from
    the curve" -- so a caller that doesn't have real event data handy
    doesn't get misleading interpolated values passed off as commanded
    ones.

    layout ("overlay", the default, or "stacked"): overlay draws every
    tool's curve against the SAME 0..y_max band, so overlapping tools
    sit on top of each other (curve_style's area_alpha exists mainly to
    keep that readable). "stacked" instead gives each tool its own
    horizontal lane, offset vertically so no two tools ever occupy the
    same band -- same temperature scale (y_max) in every lane, just
    shifted, so relative peaks are still directly comparable by height,
    only now with zero overlap to disambiguate. Lane order top-to-bottom
    follows tool number ascending (T0 on top), matching tool_colors'
    own position-0-is-T0 convention. Falls back to "overlay" with 1 (or
    0) tools -- stacking a single lane buys nothing.

    tool_labels_enabled/tool_label_size_px identify which curve is which
    tool -- but AS which visual differs by layout, since the two modes
    have different disambiguation needs. Stacked already keeps every
    tool visually separate (that's the whole point), it just doesn't say
    WHICH tool is in which lane -- so each lane gets a small "T<n>"
    label in its own top-right corner (opposite the reference lines' own
    temp label, which sits top-LEFT -- see label_x_inset above -- so the
    two never compete for the same space). Overlay is the opposite
    problem: every tool already shares the same band and same color-
    identified curve, so a corner label wouldn't say which curve is
    which at all -- instead a centered legend renders in a reserved band
    below the plot (see the overlay branch of the layout/lane_offset
    setup below), one [short line sample]-["T<n>"] pair per tool, same
    "solve canvas dims against an inflated effective height" trick
    stacked's own lane reservation uses. Only shown in overlay with more
    than one tool -- a single tool's curve is already unambiguous.
    """
    all_vertices = {tool: flatten_vertices(segs) for tool, segs in tool_curves.items()}
    active_tools = sorted(tool for tool, verts in all_vertices.items() if verts)
    num_tools = len(active_tools)

    layout = str(cfg.get("layout") or "overlay").strip().lower()
    if layout not in ("overlay", "stacked"):
        layout = "overlay"
    stacked = layout == "stacked" and num_tools > 1

    if stacked:
        # Stacking needs more vertical room than the configured height
        # alone would give each tool -- solve canvas dims against an
        # EFFECTIVE height (num_tools lanes + gaps between them, PLUS a
        # label margin above each lane if tool_labels_enabled -- see
        # below) instead of the raw cfg value, same _resolve_canvas_dims()
        # used for overlay otherwise. canvas_width_px is left as
        # configured; only height grows with tool count.
        lane_height_px = max(10.0, float(cfg.get("stack_lane_height_px") or 90.0))
        gap_px = max(0.0, float(cfg.get("stack_gap_px") or 6.0))
        show_tool_labels_stacked = bool(cfg.get("tool_labels_enabled", True))
        # A curve sitting right at its own lane's top edge (e.g. holding
        # at peak temp) would otherwise collide with/obscure a corner
        # label drawn on top of it there -- so when labels are on, each
        # lane gets its OWN small reserved strip above its plot area
        # (not shared with the label size setting used elsewhere the way
        # width_px is a literal pixel; this one just reserves enough
        # headroom to comfortably fit the label's own text height, sized
        # off it) purely for the label to sit in, clear of anything
        # actually plotted.
        tool_label_size_for_layout = max(4.0, float(cfg.get("tool_label_size_px") or 24.0))
        label_margin_px = (tool_label_size_for_layout * 1.3) if show_tool_labels_stacked else 0.0
        per_lane_px = lane_height_px + label_margin_px
        effective_height_px = num_tools * per_lane_px + (num_tools - 1) * gap_px
        dims_cfg = dict(cfg)
        dims_cfg["canvas_height_px"] = effective_height_px
        CANVAS_X_UNITS, CANVAS_Y_UNITS, CANVAS_PAD, CANVAS_MAX_SIZE = _resolve_canvas_dims(dims_cfg)
        # Ratio-based, not a separate px->unit conversion, so these
        # always sum back to exactly CANVAS_Y_UNITS regardless of how
        # _resolve_canvas_dims() itself handles padding internally.
        lane_pitch_units = CANVAS_Y_UNITS * (per_lane_px / effective_height_px)
        label_margin_units = CANVAS_Y_UNITS * (label_margin_px / effective_height_px)
        gap_units = CANVAS_Y_UNITS * (gap_px / effective_height_px)
        # The PLOT portion of each lane -- what y_of()/lane_height_units
        # actually scale temperatures into below -- is the full per-lane
        # pitch MINUS that reserved label strip. Curves/reference lines
        # only ever see this smaller number; the label strip itself is
        # never plotted into, only labeled into (see the per-tool loop's
        # own corner-label placement, which uses lane_pitch_units instead
        # specifically to reach up into that reserved strip).
        lane_height_units = lane_pitch_units - label_margin_units
        # Tool at index 0 (lowest number) gets the TOPMOST lane. Canvas
        # y=0 renders at the BOTTOM (both renderers flip the axis -- see
        # orcastrator.py's to_px()), so the topmost lane is the one with
        # the largest y-offset.
        lane_offset = {
            tool: (num_tools - 1 - i) * (lane_pitch_units + gap_units)
            for i, tool in enumerate(active_tools)
        }
    else:
        # Overlay's own equivalent of stacked's lane reservation above: a
        # legend identifying which curve is which tool only makes sense
        # with more than one tool on screen at once (a single tool's
        # curve is already unambiguous), and only in overlay -- stacked
        # mode gets its own per-lane corner label instead (see the main
        # per-tool loop below), since each lane's already visually
        # separated and doesn't need a shared key. Same "solve canvas
        # dims against an inflated effective height, then work out what
        # fraction of the result that reservation became" trick as the
        # stacked branch above, just reserving a legend row along the
        # bottom instead of N lanes.
        show_legend = bool(cfg.get("tool_labels_enabled", True)) and num_tools > 1
        if show_legend:
            legend_label_size = max(4.0, float(cfg.get("tool_label_size_px") or 24.0))
            legend_height_px = legend_label_size * 1.8
            base_height_px = max(20.0, float(cfg.get("canvas_height_px") or DEFAULT_CANVAS_HEIGHT_PX))
            effective_height_px = base_height_px + legend_height_px
            dims_cfg = dict(cfg)
            dims_cfg["canvas_height_px"] = effective_height_px
            CANVAS_X_UNITS, CANVAS_Y_UNITS, CANVAS_PAD, CANVAS_MAX_SIZE = _resolve_canvas_dims(dims_cfg)
            legend_band_units = CANVAS_Y_UNITS * (legend_height_px / effective_height_px)
            lane_height_units = CANVAS_Y_UNITS - legend_band_units
            # Every tool shares the SAME lane in overlay mode (that's the
            # definition of "overlay") -- just shifted up out of the
            # reserved legend band at the bottom, same shift for all of
            # them.
            lane_offset = {tool: legend_band_units for tool in active_tools}
        else:
            CANVAS_X_UNITS, CANVAS_Y_UNITS, CANVAS_PAD, CANVAS_MAX_SIZE = _resolve_canvas_dims(cfg)
            lane_height_units = CANVAS_Y_UNITS
            lane_offset = {tool: 0.0 for tool in active_tools}
            legend_band_units = 0.0
        # No separate label-margin concept in overlay -- the legend lives
        # in its own reserved band below the plot instead of eating into
        # each tool's own lane the way stacked's corner label does, so
        # there's nothing for a per-tool "pitch vs plot height" split to
        # do here. Kept equal to lane_height_units purely so any shared
        # code that expects lane_pitch_units to exist (there isn't any
        # today, but see stacked's own use of it below) doesn't have to
        # special-case overlay to avoid a NameError.
        lane_pitch_units = lane_height_units

    override = cfg.get("y_max_celsius")
    if override is not None and float(override) > 0:
        y_max = float(override)
    else:
        highest = max((v for verts in all_vertices.values() for _, v in verts), default=0.0)
        y_max = highest * 1.1 if highest > 0 else 250.0

    def x_of(t):
        return (t / total_time * CANVAS_X_UNITS) if total_time > 0 else 0.0

    def y_of(v):
        # Maps into a single tool's lane (lane_height_units == the full
        # canvas in overlay mode); lane_offset shifts it into place.
        return max(0.0, min(lane_height_units, (v / y_max * lane_height_units) if y_max > 0 else 0.0))

    manual_colors = parse_tool_colors(cfg)
    filament_colors = filament_colors or []
    curve_style = str(cfg.get("curve_style") or "area_line").strip().lower()
    if curve_style not in ("line", "area", "area_line"):
        curve_style = "area_line"
    area_alpha = max(0.0, min(1.0, float(cfg.get("area_alpha") if cfg.get("area_alpha") is not None else 0.35)))

    # Reference lines -- one real "path" shape (see module docstring) per
    # distinct commanded temp per lane, full canvas width. Built and
    # prepended to `shapes` below, ahead of the tool curves themselves,
    # so a curve painted on top of a reference line stays legible rather
    # than the (much thinner) line getting buried under a filled curve
    # area.
    reference_shapes = []
    if bool(cfg.get("reference_lines_enabled", True)) and reference_temps:
        ref_temps = sorted({t for t in reference_temps if t and t > 0})
        if ref_temps:
            ref_hex = str(cfg.get("reference_line_color") or "#ffffff")
            rr, rg, rb = hex_to_rgb(ref_hex)
            ref_alpha = max(0.0, min(1.0, float(cfg.get("reference_line_opacity")
                                                 if cfg.get("reference_line_opacity") is not None else 0.5)))
            ref_rgba = f"rgba({rr},{rg},{rb},{ref_alpha})"
            ref_width_px = max(0.5, float(cfg.get("reference_line_width_px") or 1.0))
            ref_style = str(cfg.get("reference_line_style") or "dashed").strip().lower()
            if ref_style not in ("solid", "dashed", "dotted"):
                ref_style = "dashed"
            # In real pixels -- both renderers apply width_px/dash as
            # literal final-pixel values via vector-effect="non-scaling-
            # stroke" (SVG side) / create_line's own always-literal width
            # (Tk side), so no px-per-canvas-unit conversion is needed
            # here.
            ref_dash = {"solid": None, "dotted": [2, 4]}.get(ref_style, [8, 5])

            labels_enabled = bool(cfg.get("reference_line_labels_enabled", True))
            # Unlike width_px above, this ISN'T a literal pixel size --
            # see the "text" shape's own docs (svg_tools.cfg / Tk's
            # "text" case in _draw_payload) for why a label is sized to
            # scale WITH the canvas instead. The "_px" name is kept only
            # for consistency with this section's other fields (a label
            # this size would read as roughly that many px on THIS
            # processor's own default ~500px-wide canvas); it'll read
            # larger/smaller than that on a differently sized one.
            label_size = max(4.0, float(cfg.get("reference_line_label_size_px") or 24.0))
            # A small fixed inset from the left edge, in the SAME units
            # as label_size (rather than a separate constant), so it
            # scales down together with the label on a small canvas
            # instead of the label shrinking while the gap to the edge
            # stays fixed and starts to look disproportionate.
            label_x_inset = label_size * 0.4
            # Nudged up off the line itself by roughly half a label
            # height, so the label sits just above the line (readable on
            # its own) rather than centered on top of it (harder to read
            # either the number or the line through the other).
            label_y_nudge = label_size * 0.6

            # One line per distinct temp, repeated in every lane (or
            # just once, in overlay mode -- lane_offset always has
            # exactly one distinct value there, whatever it is: 0.0
            # with no legend, or legend_band_units with one, see the
            # layout/lane_offset setup above) -- same shared temperature
            # scale in every lane (see docstring above), so a global
            # temp's line sits at the same relative height everywhere,
            # letting a lane's curve be checked against it directly
            # even for a temp that particular tool never itself used.
            # Reading the offset(s) straight from lane_offset itself
            # (rather than re-deriving "0.0 unless stacked" here) is
            # what keeps this in sync with whatever that setup actually
            # decided -- duplicating the logic instead is exactly how a
            # legend's own reservation up there once drifted out of sync
            # with reference lines still assuming an unshifted 0.0 here.
            lane_offsets = sorted(set(lane_offset.values()))
            for offset in lane_offsets:
                for temp in ref_temps:
                    y = offset + y_of(temp)
                    reference_shapes.append({
                        "type": "path",
                        "points": [_round_pt((0.0, y)), _round_pt((CANVAS_X_UNITS, y))],
                        "color": ref_rgba, "width_px": ref_width_px, "dash": ref_dash,
                    })
                    if labels_enabled:
                        reference_shapes.append({
                            "type": "text",
                            "x": round(label_x_inset, 2), "y": round(min(y + label_y_nudge, offset + lane_height_units - label_size * 0.5), 2),
                            "text": f"{temp:g}\u00b0C",
                            "color": ref_rgba, "size": label_size, "anchor": "start",
                        })

    shapes = list(reference_shapes)
    tool_summaries = []
    tool_labels_enabled = bool(cfg.get("tool_labels_enabled", True))
    tool_label_size = max(4.0, float(cfg.get("tool_label_size_px") or 24.0))
    tool_rgb = {}
    for tool in active_tools:
        verts = all_vertices[tool]
        color_hex = color_for_tool(tool, manual_colors, filament_colors)
        r, g, b = hex_to_rgb(color_hex)
        tool_rgb[tool] = (r, g, b)
        offset = lane_offset[tool]
        canvas_pts = [(x_of(t), offset + y_of(v)) for t, v in verts]
        peak_temp = max(v for _, v in verts)
        tool_summaries.append({"tool": tool, "color": color_hex, "peak_temp_celsius": peak_temp})

        if curve_style in ("area", "area_line") and len(canvas_pts) >= 2:
            top = canvas_pts
            # Each tool's own lane floor, not global 0 -- offset alone
            # in overlay mode (offset == 0.0), same as before.
            bottom = [(x, offset) for x, _ in reversed(canvas_pts)]
            shapes.append({
                "type": "polygon", "points": [_round_pt(p) for p in top + bottom],
                "fill": f"rgba({r},{g},{b},{area_alpha})",
                "stroke": f"rgba({r},{g},{b},{area_alpha})",
                "fill_style": "dithered",
            })

        if curve_style in ("line", "area_line") and len(canvas_pts) >= 2:
            solid = f"rgba({r},{g},{b},1.0)"
            shapes.append({
                "type": "path",
                "points": [_round_pt(p) for p in canvas_pts],
                "color": solid, "width_px": float(cfg.get("line_width_px") or 2.0),
            })

        if stacked and tool_labels_enabled:
            # Opposite corner from the reference lines' own temp labels
            # (top-left, see label_x_inset above) -- top-right. Uses
            # lane_pitch_units, not lane_height_units, to reach up into
            # the reserved margin strip ABOVE the plot area (see the
            # stacked branch of the layout/lane_offset setup) rather than
            # sitting at the plot's own top edge, where it would collide
            # with a curve holding at peak temp right at that edge.
            inset = tool_label_size * 0.4
            shapes.append({
                "type": "text",
                "x": round(CANVAS_X_UNITS - inset, 2), "y": round(offset + lane_pitch_units - inset, 2),
                "text": f"T{tool}",
                "color": f"rgba({r},{g},{b},1.0)", "size": tool_label_size, "anchor": "end", "weight": "bold",
            })

    if not stacked and show_legend:
        # A centered row of "T<n>" labels, colored to match each tool's
        # own curve, in the legend band reserved above (see the overlay
        # branch of the layout/lane-offset setup) -- color alone is
        # enough to tie a label back to its curve here, so no separate
        # line-sample swatch next to it. No real font-metrics API is
        # available here (this has to work identically whether it ends
        # up in Tk or in a Klipper-embedded SVG, neither of which hands
        # this code back an actual measured text width), so each
        # label's width is ESTIMATED from its character count -- fine
        # for what's always just "T" plus 1-2 digits, not a general
        # solution for arbitrary text.
        est_char_width = tool_label_size * 0.62
        entry_gap = tool_label_size * 1.0
        entries = []
        for tool in active_tools:
            label = f"T{tool}"
            entries.append((tool, label, len(label) * est_char_width))
        total_width = sum(w for _, _, w in entries) + entry_gap * max(0, len(entries) - 1)
        cursor = max(tool_label_size * 0.5, (CANVAS_X_UNITS - total_width) / 2.0)
        legend_y = legend_band_units * 0.5
        for tool, label, text_w in entries:
            r, g, b = tool_rgb[tool]
            shapes.append({
                "type": "text",
                "x": round(cursor, 2), "y": round(legend_y, 2),
                "text": label, "color": f"rgba({r},{g},{b},1.0)", "size": tool_label_size, "anchor": "start",
                "weight": "bold",
            })
            cursor += text_w + entry_gap

    payload = {

        "title": "Tool Temperature Graph",
        "canvas": {"x_max": CANVAS_X_UNITS, "y_max": CANVAS_Y_UNITS, "pad": CANVAS_PAD, "max_size": CANVAS_MAX_SIZE},
        "shapes": shapes,
        "targets": resolve_svg_targets(cfg),
    }
    summary = {
        "total_time_seconds": total_time,
        "y_max_celsius": y_max,
        "curve_style": curve_style,
        "tools": tool_summaries,
    }
    return payload, summary


def process(gcode_path: str) -> None:
    p = pathlib.Path(gcode_path)
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    cfg = load_config()
    global _notice_cfg
    _notice_cfg = cfg
    shared_cfg = load_shared_preheat_config()
    lead_seconds = shared_cfg["target_lead_seconds"]
    cooldown_seconds = shared_cfg["target_cooldown_seconds"]

    toolchange_line_idxs, temp_events_by_tool = find_tool_events(lines)
    filament_colors = find_filament_colors(lines)

    debug_data = {
        "file": friendly_filename(p),
        "config": {
            "target_lead_seconds": lead_seconds,
            "target_cooldown_seconds": cooldown_seconds,
            "curve_style": cfg.get("curve_style"),
            "m109_settle_seconds": cfg.get("m109_settle_seconds", DEFAULTS["m109_settle_seconds"]),
            "reference_lines_enabled": cfg.get("reference_lines_enabled", DEFAULTS["reference_lines_enabled"]),
            "reference_line_color": cfg.get("reference_line_color", DEFAULTS["reference_line_color"]),
            "reference_line_opacity": cfg.get("reference_line_opacity", DEFAULTS["reference_line_opacity"]),
            "reference_line_width_px": cfg.get("reference_line_width_px", DEFAULTS["reference_line_width_px"]),
            "reference_line_style": cfg.get("reference_line_style", DEFAULTS["reference_line_style"]),
            "reference_line_labels_enabled": cfg.get("reference_line_labels_enabled", DEFAULTS["reference_line_labels_enabled"]),
            "reference_line_label_size_px": cfg.get("reference_line_label_size_px", DEFAULTS["reference_line_label_size_px"]),
        },
        "filament_colors": filament_colors,
        "result": None,
    }

    min_tools = int(cfg.get("min_tools") or 1)
    if len(temp_events_by_tool) < min_tools:
        debug_data["result"] = "skipped_too_few_tools"
        debug_data["tools_found"] = len(temp_events_by_tool)
        write_debug_dump("tool_temperature_graph", cfg.get("debug", {}), debug_data, SCRIPT_PATH)
        print_notice("info", "Tool Temperature Graph",
                     f"Only {len(temp_events_by_tool)} tool(s) with any temperature command found -- "
                     f"nothing meaningful to graph.")
        return

    # calibrate_timeline() is the same call toolchange_heatmap.py makes --
    # see helpers/timeline_scale.py's module docstring for why that
    # matters: both processors need the exact same tool_change_time,
    # print_duration, and naive_time_scale_factor() calibration so a
    # given g-code line lands at the same real-seconds timestamp in
    # either one's timeline.
    timeline = calibrate_timeline(lines, cfg, toolchange_line_idxs)
    tool_change_time = timeline.tool_change_time
    print_duration = timeline.print_duration
    naive_scale = timeline.naive_scale
    adjusted_time = timeline.time_at

    events_by_tool = {}
    last_event_t = 0.0
    for tool, raw_events in temp_events_by_tool.items():
        converted = sorted(
            (adjusted_time(idx), temp, is_blocking) for idx, temp, is_blocking in raw_events
        )
        events_by_tool[tool] = converted
        if converted:
            last_event_t = max(last_event_t, converted[-1][0])

    total_time = max(last_event_t, float(print_duration or 0.0), 1.0)

    # The actual commanded S-values, deduplicated across every tool, 0
    # excluded -- see build_svg_payload()'s reference_temps param doc
    # for why this has to come from the raw events rather than from
    # tool_curves' own (post-ramp, possibly-interrupted) vertex values.
    reference_temps = sorted({temp for events in events_by_tool.values() for _, temp, _ in events if temp > 0})

    # If the very last temperature event of ANY tool lands at (or within
    # a ramp's own width of) the timeline's right edge -- expected now
    # that events are calibrated against the very same real-duration
    # anchor total_time itself is pinned to, see naive_time_scale_factor()
    # -- give that ramp room to actually draw. Without this, build_tool_
    # curve()'s closing call sees zero time left between the event and
    # total_time and drops the ramp segment entirely, silently rendering
    # as "stayed at the previous value forever" instead of "started
    # cooling/heating right at the end" -- worse than the flat trailing
    # tail this whole calibration exists to shrink. This deliberately
    # pushes total_time PAST OrcaSlicer's stated duration in exactly this
    # case, because the underlying physical event (e.g. disable_unused_
    # tool_temps.py's permanent-shutoff M104, which by design sits right
    # at the very end of the executable g-code) really does keep running
    # after the print is nominally "done".
    ramp_margin = max(lead_seconds, cooldown_seconds)
    if last_event_t >= total_time - ramp_margin:
        total_time = last_event_t + ramp_margin

    m109_settle_seconds = float(cfg.get("m109_settle_seconds")
                                 if cfg.get("m109_settle_seconds") is not None
                                 else DEFAULTS["m109_settle_seconds"])

    tool_curves = {
        tool: build_tool_curve(events, total_time, lead_seconds, cooldown_seconds, m109_settle_seconds)
        for tool, events in events_by_tool.items()
    }

    payload, summary = build_svg_payload(tool_curves, cfg, total_time, filament_colors, reference_temps)
    print_svg_payload(payload)

    debug_data["result"] = "rendered"
    debug_data["total_time_seconds"] = total_time
    debug_data["tool_change_time_seconds"] = tool_change_time
    debug_data["naive_time_scale_factor"] = naive_scale
    debug_data["reference_temps"] = reference_temps
    debug_data["events"] = {
        str(tool): [{"t": t, "target_temp": temp, "blocking": is_blocking} for t, temp, is_blocking in events]
        for tool, events in events_by_tool.items()
    }
    debug_data["curves"] = {str(tool): segs for tool, segs in tool_curves.items()}
    debug_data.update(summary)
    write_debug_dump("tool_temperature_graph", cfg.get("debug", {}), debug_data, SCRIPT_PATH)

    tool_bits = ", ".join(
        f"T{t['tool']} ({t['color']}): peak {t['peak_temp_celsius']:.0f}\u00b0C" for t in summary["tools"]
    )
    print_notice("info", "Tool Temperature Graph",
                 f"{len(summary['tools'])} tool(s) over {format_duration(total_time)} -- {tool_bits}.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tool_temperature_graph.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1])
