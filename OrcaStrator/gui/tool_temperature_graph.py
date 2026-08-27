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
Settings-form spec for tool_temperature_graph.json.

See gui/orcastrator.py for the full walkthrough of this convention, and
gui/dock_collision_guard.py for the full HAS_PREVIEW/PREVIEW_CONTROLS/
build_preview_payload() "rich preview" contract this mirrors -- same
shape as gui/toolchange_heatmap.py's own rich preview, which this is
closest to structurally (see that module's own docstring for the parts
of the convention not re-explained here).

Built from tool_temperature_graph.py's own last REAL run -- its debug
dump already has every tool's calibrated (t, target_temp) event list on
disk (see read_debug_dump() in helpers/debug_dump.py), so
build_preview_payload() re-derives each tool's curve from that real
data via build_tool_curve() + build_svg_payload(), using the CURRENT
(live-edited) cfg, on every field change. tool_colors/curve_style/
line_width_px/area_alpha/y_max_celsius/canvas size/layout/stack lane
sizing all update live as a result.

Three things can't move live, for the same reason toolchange_heatmap's
preview calls out: tool_change_time_seconds and print_duration_seconds
both affect event timing/axis extent that's already baked into the
dump's recorded (t, target_temp) pairs (print_duration_seconds is the
one exception -- see below, it's re-applied live same as the heatmap's
own copy of this same field), and the warmup/cooldown ramp durations
themselves aren't a setting on THIS page at all (they live in
tool_preheat.json, shared with insert_missing_tool_preheat.py/
disable_unused_tool_temps.py) -- this preview always reads that shared
file's CURRENT value live, same as a real run would, so editing
tool_preheat.json elsewhere and reopening this page does reflect it,
just not instantly while tool_preheat.json's own page is what's open.
m109_settle_seconds IS a setting on this page (Timing section) and DOES
move live -- it's read straight from the live-edited cfg on every
rebuild, same as curve_style/line_width_px/etc, since -- unlike lead_
seconds/cooldown_seconds -- it isn't baked into the dump's own event
list, only into build_tool_curve()'s output.

Each recorded event in the dump also carries its own "blocking" flag
(True for M109, False for M104) written by the real run -- this preview
reads that back per-event so an M109-bounded reheat previews the same
short settle ramp a real run would give it, rather than the ordinary
lead_seconds/cooldown_seconds ramp. A dump missing this flag entirely
just won't have it; that's read back as "not blocking", never an error.

The "Debug log" preview control works identically to toolchange_heatmap's
-- see that module's docstring for the full rationale on Auto vs. a
picked historical run.
"""
import sys as _sys
import pathlib as _pathlib
import json as _json
import datetime as _dt

_GUI_DIR = _pathlib.Path(__file__).resolve().parent
if str(_GUI_DIR) not in _sys.path:
    _sys.path.insert(0, str(_GUI_DIR))
from _plugin_support import load_processor_module

# This processor's own module -- for build_preview_payload() below to call
# its build_tool_curve()/build_svg_payload()/read_debug_dump()/
# load_shared_preheat_config()/SCRIPT_PATH. A plugin depending on its own
# processor is correct coupling; see gui/_plugin_support.py's docstring.
_ttg = load_processor_module("tool_temperature_graph")

# load_processor_module() (via _plugin_support) already put post_processors/
# on sys.path, so helpers/ resolves the same way it does inside a processor.
from helpers.debug_dump import list_debug_dumps as _list_debug_dumps


def _format_timestamp(ts):
    """"YYYYMMDD_HHMMSS_mmm" (see debug_dump.py's _TIMESTAMP_FMT) -> a
    readable "YYYY-MM-DD HH:MM:SS" for the picker label. Falls back to
    the raw string if it doesn't parse (still sortable, still readable
    enough)."""
    if not ts:
        return ""
    try:
        return _dt.datetime.strptime(ts, "%Y%m%d_%H%M%S_%f").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def _discover_debug_logs():
    """(value, label) pairs for the "Debug log" log_list control below --
    see gui/toolchange_heatmap.py's identical helper for the full
    rationale, duplicated here rather than shared since it's a few lines
    of glue specific to each processor's own name/DEFAULTS/SCRIPT_PATH."""
    options = [("", "Auto (last run)")]
    try:
        dumps = _list_debug_dumps("tool_temperature_graph", _ttg.DEFAULTS.get("debug", {}), _ttg.SCRIPT_PATH)
        for path, ts in dumps:
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            gcode_name = data.get("file") or path.name
            when = _format_timestamp(ts)
            label = f"{gcode_name} -- {when}" if when else gcode_name
            options.append((str(path), label))
    except Exception:
        pass  # discovery is best-effort -- worst case, just "Auto" is offered
    return options


TITLE = "Tool Temperature Graph"
SUBTITLE = "Per-tool temperature-vs-time curves, ramped by tool_preheat.json's lead/cooldown times, with a live preview from the last real run."
CONFIG = "tool_temperature_graph.json"
KIND = "rich"
HAS_PREVIEW = True

# Same "only worth offering when there's real history" reasoning as
# toolchange_heatmap.py -- see there.
_discovered_logs = _discover_debug_logs()
PREVIEW_CONTROLS = [
    dict(kind="log_list", var="debug_log", label="Choose debug log to run preview on",
         options=_discovered_logs, default=""),
] if len(_discovered_logs) > 2 else []

SECTIONS = [
    ("Display", [
        dict(kind="choice", label="Show on", path=("svg_target",),
             options=["pc", "printer", "both"],
             tooltip="Where the graph shows up. 'pc' = only in OrcaStrator's own progress window at export "
                     "time. 'printer' = only embedded in the g-code for the printer's own console/screen. "
                     "'both' = show it in both places. No 'off' -- this whole processor exists to show this, "
                     "so hiding it everywhere isn't a meaningful choice to offer."),
        dict(kind="number", label="Width (px)", path=("canvas_width_px",),
             min=40, max=2000, step=10,
             tooltip="Width of the rendered graph, in pixels. Independent of Height below -- each is solved "
                     "for directly, not derived from the other via a fixed aspect ratio."),
        dict(kind="number", label="Height (px)", path=("canvas_height_px",),
             min=20, max=2000, step=10,
             tooltip="Height of the rendered graph, in pixels. Independent of Width above. Overridden by "
                     "lane height x tool count when Layout is 'stacked' -- see Stacked lane height below.",
             show_if=[(("layout",), "overlay")]),
        dict(kind="choice", label="Layout", path=("layout",),
             options=["overlay", "stacked"],
             tooltip="'overlay' draws every tool's curve on the same band, so overlapping tools sit on top "
                     "of each other (that's what Area opacity below is for). 'stacked' instead gives each "
                     "tool its own horizontal lane, offset vertically so nothing ever overlaps -- same "
                     "temperature scale in every lane, just shifted, so peaks are still directly comparable "
                     "by height. Falls back to 'overlay' with only one tool in the file."),
        dict(kind="number", label="Stacked lane height (px)", path=("stack_lane_height_px",),
             min=10, max=500, step=5,
             tooltip="Height of each tool's own lane, in pixels, when Layout is 'stacked'. The graph's total "
                     "rendered height becomes (lane height x tool count) + (gap x (tool count - 1)), "
                     "overriding Height above.",
             show_if=[(("layout",), "stacked")]),
        dict(kind="number", label="Stacked lane gap (px)", path=("stack_gap_px",),
             min=0, max=100, step=1,
             tooltip="Vertical gap between lanes, in pixels, when Layout is 'stacked'.",
             show_if=[(("layout",), "stacked")]),
    ]),
    ("Timing", [
        dict(kind="nullable_number", label="Per-toolchange time cost (s)", path=("tool_change_time_seconds",),
             min=0, max=300, step=1, auto_placeholder=0,
             tooltip="How many seconds one toolchange's swap/purge/park routine costs -- added once per "
                     "toolchange that's already happened by a given point, since the underlying time estimate "
                     "treats a toolchange line itself as instantaneous. Auto reads OrcaSlicer's own "
                     "'machine_tool_change_time' setting from the file (0 if that's not present). Editing "
                     "this here won't move the live preview below -- every event's timestamp already has it "
                     "baked in from the real run the preview is drawn from; only a real re-run picks up a "
                     "change to this."),
        dict(kind="nullable_number", label="Print duration override (s)", path=("print_duration_seconds",),
             min=0, max=999999, step=60, auto_placeholder=0,
             tooltip="Sets the timeline's right edge, so a stretch of print after the last temperature "
                     "command (nothing left to heat/cool, still printing) shows as a flat hold instead of "
                     "being silently cut off. Auto reads OrcaSlicer's own 'estimated printing time' line from "
                     "the file."),
        dict(kind="number", label="M109 (wait-for-temp) settle time (s)", path=("m109_settle_seconds",),
             min=0, max=120, step=1,
             tooltip="How long M109's ramp takes to draw, regardless of warming or cooling. M109 BLOCKS the "
                     "print until its target is genuinely reached, unlike fire-and-forget M104 -- so instead "
                     "of the usual multi-ten-second lead/cooldown ramp (an estimate of whether a command got "
                     "there before something else interrupted it), an M109 always has, by definition, so it "
                     "only needs a short settle ramp to read as 'reached' rather than a real thermal climb. "
                     "Raise this if short settle ramps look too abrupt at your canvas size; lower it toward 0 "
                     "for a near-vertical jump."),
    ]),
    ("Curve", [
        dict(kind="choice", label="Curve style", path=("curve_style",),
             options=["line", "area", "area_line"],
             tooltip="'line' draws each tool's curve as a colored line only. 'area' fills the area under "
                     "each tool's curve (translucent, so an overlapped tool is still visible underneath). "
                     "'area_line' draws both -- the filled area plus a solid line on top for definition."),
        dict(kind="number", label="Line width (px)", path=("line_width_px",),
             min=1, max=15, step=1,
             tooltip="Width of the line itself. Only used by 'line' and 'area_line' curve styles.",
             show_if=[(("curve_style",), ["line", "area_line"])]),
        dict(kind="number", label="Area opacity", path=("area_alpha",),
             min=0, max=1, step=0.05,
             tooltip="Opacity (0-1) of the filled area under each tool's curve. Only used by 'area' and "
                     "'area_line' curve styles. Real translucency on the printer/browser-rendered side; "
                     "approximated with a dither pattern in OrcaStrator's own Tk progress window. Lower it "
                     "if several tools' filled areas overlapping gets visually noisy; 1.0 makes it fully "
                     "opaque (later-drawn tools will occlude earlier ones wherever they overlap).",
             show_if=[(("curve_style",), ["area", "area_line"])]),
        dict(kind="nullable_number", label="Temperature axis max (C)", path=("y_max_celsius",),
             min=0, max=500, step=5, auto_placeholder=0,
             tooltip="Auto/override for the top of the temperature axis. Auto uses the highest commanded "
                     "target temp across every tool in this file, plus a small margin. Set a fixed number "
                     "instead to keep the scale consistent when comparing graphs across different prints."),
        dict(kind="number", label="Minimum tools to render", path=("min_tools",),
             min=0, max=20, step=1, is_int=True,
             tooltip="Skip rendering entirely if fewer than this many tools have any temperature-setting "
                     "command in the file -- nothing meaningful to graph with 0."),
    ]),
    ("Colors", [
        dict(kind="hex_color_list", label="Tool colors", path=("tool_colors",),
             default=_ttg.DEFAULTS["tool_colors"], index_prefix="T", add_label="+ Add tool color",
             tooltip="One color per tool, in order -- the T0 swatch is T0's color, T1's is T1's, and so on. "
                     "Click a swatch to change it, or add/remove tools with the buttons. Leave a tool's entry "
                     "blank (or the whole list empty, the default) to fall back to that tool's ACTUAL filament "
                     "color as loaded in the slicer for this export, read straight from the g-code -- only "
                     "when that's also unavailable does it fall back further to an auto-generated color "
                     "spread across the hue wheel. There's no fixed tool count here, it's auto-detected from "
                     "the g-code either way."),
    ]),
    ("Tool labels", [
        dict(kind="bool", label="Show tool labels", path=("tool_labels_enabled",),
             tooltip="Identifies which curve belongs to which tool. In 'stacked' layout, each lane gets a "
                     "small, colored 'T<n>' label in its own top-right corner (the opposite corner from a "
                     "reference line's own temp label, so the two never compete for space) -- with a bit of "
                     "extra headroom reserved above each lane's plot area so the label stays clear of a curve "
                     "holding at peak temp right at that edge. In 'overlay' layout -- where every tool shares "
                     "the same band, so a corner label wouldn't say which curve is which -- this instead draws "
                     "a centered legend below the graph: each tool's 'T<n>', colored to match its curve (color "
                     "alone is enough to tell them apart, so no extra line-sample swatch next to it). Only "
                     "shown in overlay mode with more than one tool on screen; a single tool's curve is "
                     "already unambiguous without one."),
        dict(kind="number", label="Label size (px)", path=("tool_label_size_px",),
             min=4, max=100, step=1,
             tooltip="Same approximation as the reference lines' own label size above (scales WITH the "
                     "graph's canvas rather than staying a literal fixed pixel size -- see that field's own "
                     "tooltip for why). Also sets how much extra vertical room gets reserved for the label "
                     "itself: in 'stacked' layout, the headroom strip above each lane; in 'overlay' layout, "
                     "the legend band below the plot. A larger label size means more reserved room either way, "
                     "not just bigger text.",
             show_if=[(("tool_labels_enabled",), True)]),
    ]),
    ("Reference lines", [
        dict(kind="bool", label="Show reference lines", path=("reference_lines_enabled",),
             tooltip="Draws a full-width horizontal line at every distinct temperature actually commanded "
                     "somewhere in the g-code (0 excluded, since that's 'off', not a temperature) -- e.g. a "
                     "line at 250C and another at 170C if those are the only two S-values used anywhere. "
                     "Makes it easy to see at a glance whether a tool's curve actually reached a temp it was "
                     "supposed to, versus falling just short. In 'stacked' layout, the same set of lines "
                     "repeats in every lane at the same relative height, not just the lanes where that "
                     "particular tool used that particular temp -- so a lane's curve can still be checked "
                     "against a temp it never itself commanded."),
        dict(kind="hex_color", label="Color", path=("reference_line_color",), default="#ffffff",
             tooltip="Color of the reference lines. Independent of every tool's own curve color, since a "
                     "reference line isn't tied to any one tool -- it's a shared value across all of them.",
             show_if=[(("reference_lines_enabled",), True)]),
        dict(kind="number", label="Opacity", path=("reference_line_opacity",),
             min=0, max=1, step=0.05,
             tooltip="Opacity (0-1) of the reference lines. Kept well under 1.0 by default so they read as a "
                     "background guide rather than competing with the tool curves themselves for attention.",
             show_if=[(("reference_lines_enabled",), True)]),
        dict(kind="number", label="Width (px)", path=("reference_line_width_px",),
             min=0.5, max=10, step=0.5,
             tooltip="Thickness of the reference lines.",
             show_if=[(("reference_lines_enabled",), True)]),
        dict(kind="choice", label="Style", path=("reference_line_style",),
             options=["dashed", "dotted", "solid"],
             tooltip="'dashed'/'dotted' keep a reference line visually distinct from the tool curves crossing "
                     "it (which are always solid); 'solid' draws an unbroken line instead.",
             show_if=[(("reference_lines_enabled",), True)]),
        dict(kind="bool", label="Show labels", path=("reference_line_labels_enabled",),
             tooltip="Prints the temperature value (e.g. '250°C') just above each reference line, so it's "
                     "readable at a glance without having to match a line's height against the temperature "
                     "axis by eye.",
             show_if=[(("reference_lines_enabled",), True)]),
        dict(kind="number", label="Label size (px)", path=("reference_line_label_size_px",),
             min=4, max=100, step=1,
             tooltip="Roughly how large a label reads at this processor's own default ~500px-wide canvas -- "
                     "unlike the line width above, this ISN'T a literal final pixel size: a label scales up "
                     "or down together with the rest of the graph (bigger canvas, bigger label) rather than "
                     "staying fixed regardless of how large the graph itself ends up, so it stays legible "
                     "and correctly proportioned at any canvas size rather than looking oversized on a small "
                     "graph or tiny on a large one.",
             show_if=[(("reference_lines_enabled",), True), (("reference_line_labels_enabled",), True)]),
    ]),
]


def build_preview_payload(cfg, controls):
    """
    The generic half of the "rich preview" contract -- see module
    docstring. config_editor.pyw calls this on every field change (and
    every "Debug log" list selection) and feeds the SVG_PAYLOAD-shaped
    dict straight into orcastrator.py's own generic canvas renderer, with
    no idea what's inside it.

    controls["debug_log"] is "" (Auto) or one of the paths
    _discover_debug_logs() found -- same Auto-vs-picked convention as
    toolchange_heatmap.py's build_preview_payload(); see there.
    """
    chosen_path = (controls or {}).get("debug_log") or ""
    if chosen_path:
        try:
            dbg = _json.loads(_pathlib.Path(chosen_path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Could not read the selected debug log ({chosen_path}): {exc}")
    else:
        debug_cfg = cfg.get("debug", {}) or {}
        dbg = _ttg.read_debug_dump("tool_temperature_graph", debug_cfg, _ttg.SCRIPT_PATH)
    if not dbg:
        raise RuntimeError(
            "No debug log yet for this processor. Run OrcaStrator once (with Debug enabled below) to "
            "populate this preview -- it renders against that real recorded data, not a synthetic sample."
        )
    raw_events = dbg.get("events") or {}
    if not raw_events:
        raise RuntimeError("Last debug log has no tool temperature events recorded -- nothing to preview.")

    # The shared ramp durations, read LIVE from tool_preheat.json -- not
    # a setting on this page, so there's nothing here for cfg to
    # override; this always reflects that file's current value, same as
    # a real run would. See module docstring.
    shared = _ttg.load_shared_preheat_config()
    lead_seconds = shared["target_lead_seconds"]
    cooldown_seconds = shared["target_cooldown_seconds"]

    # These are the FINAL calibrated timestamps from that real run
    # (tool_change_time_seconds already applied) -- see module
    # docstring for why editing that specific setting here won't move
    # this preview.
    events_by_tool = {}
    last_event_t = 0.0
    for tool_str, raw in raw_events.items():
        tool = int(tool_str)
        # "blocking" is absent on a dump missing this flag entirely --
        # read back as False (M104-style). See module docstring.
        converted = [(e.get("t"), e.get("target_temp"), bool(e.get("blocking", False))) for e in raw]
        events_by_tool[tool] = converted
        if converted:
            last_event_t = max(last_event_t, converted[-1][0])

    duration_override = cfg.get("print_duration_seconds")
    if duration_override is not None and float(duration_override) > 0:
        total_time = max(last_event_t, float(duration_override), 1.0)
    else:
        total_time = max(last_event_t, float(dbg.get("total_time_seconds") or 0.0), 1.0)

    # Unlike lead_seconds/cooldown_seconds above, m109_settle_seconds IS
    # a setting on this page -- read live from cfg (with the same
    # fallback default the processor itself uses), not from the dump.
    m109_settle_seconds = float(cfg.get("m109_settle_seconds")
                                 if cfg.get("m109_settle_seconds") is not None
                                 else _ttg.DEFAULTS["m109_settle_seconds"])

    tool_curves = {
        tool: _ttg.build_tool_curve(events, total_time, lead_seconds, cooldown_seconds, m109_settle_seconds)
        for tool, events in events_by_tool.items()
    }

    # Same "the real S-values, not the curve's own post-ramp vertices"
    # reasoning as process() -- see build_svg_payload()'s reference_temps
    # param doc. Reference_lines_enabled/color/opacity/width/style are
    # all read straight from cfg by build_svg_payload() itself below, so
    # they update live the same way curve_style/line_width_px do.
    reference_temps = sorted({temp for events in events_by_tool.values() for _, temp, _ in events if temp > 0})

    payload, _summary = _ttg.build_svg_payload(tool_curves, cfg, total_time, dbg.get("filament_colors") or [],
                                                 reference_temps)
    payload["title"] = f"{payload.get('title', 'Tool Temperature Graph')} -- preview from {dbg.get('file', 'last run')}"
    return payload
