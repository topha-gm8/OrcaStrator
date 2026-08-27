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
Settings-form spec for toolchange_heatmap.json.

See gui/orcastrator.py for the full walkthrough of this convention, and
gui/dock_collision_guard.py for the full HAS_PREVIEW/PREVIEW_CONTROLS/
build_preview_payload() "rich preview" contract this mirrors.

Unlike dock_collision_guard's preview (synthetic sample geometry, since
there's never a real object to check mid-edit), this one is built from
toolchange_heatmap.py's own last REAL run -- its debug dump already has
every toolchange's line/tool/calibrated timestamp on disk (see
read_debug_dump() in helpers/debug_dump.py), so there's real data to
render against instead of making something up. build_preview_payload()
re-runs build_svg_payload() with the CURRENT (live-edited) cfg against
those recorded events on every field change, so colors/gamma/kernel
width/cluster gap/x-axis mode/etc. all update live. Two settings can't:
tool_change_time_seconds and ignore_first_toolchange both affect which
events exist and their timestamps BEFORE the dump was written, and
redoing that from the dump alone isn't possible (only the final
calibrated times are stored, not the raw per-line ones) -- changing
either of those here won't visibly move the preview; only a real re-run
will.

The "Debug log" preview control lets a person pick which recorded run to
preview against, rather than always the most recent one, via
list_debug_dumps() in helpers/debug_dump.py -- the same multi-log-aware
history that processor's OWN "Debug Logs" viewer page uses, respecting
whatever the central debug.mode/cap in configs/orcastrator.json are set
to. In "single" mode (the default) there's normally just the one
overwritten file, so this ends up being just "Auto". In "multiple" mode
there's real history to pick from, newest first. Discovery runs once, at
gui/toolchange_heatmap.py's own import time (like PREVIEW_CONTROLS
itself) -- reopening OrcaStrator Settings re-scans, but a run that
happens mid-session won't appear in the list until then (though "Auto"
still always re-resolves live, so it'll reflect a mid-session run even
without a rescan).
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
# its build_svg_payload()/read_debug_dump()/SCRIPT_PATH. A plugin depending
# on its own processor is correct coupling; see gui/_plugin_support.py's
# docstring.
_heatmap = load_processor_module("toolchange_heatmap")

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
    """
    (value, label) pairs for the "Debug log" log_list control below --
    "" (Auto, always most-recent-and-live) first, then every dump
    list_debug_dumps() finds for this processor's own default debug
    location, newest first, labeled with the ORIGINAL gcode filename
    recorded inside each dump (falling back to the json filename) plus,
    for multi-log entries, when it was written.
    """
    options = [("", "Auto (last run)")]
    try:
        dumps = _list_debug_dumps("toolchange_heatmap", _heatmap.DEFAULTS.get("debug", {}), _heatmap.SCRIPT_PATH)
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


TITLE = "Toolchange Heatmap"
SUBTITLE = "Timeline of toolchange clustering, rendered as gradient blocks, with a live preview from the last real run."
CONFIG = "toolchange_heatmap.json"
KIND = "rich"
HAS_PREVIEW = True

# Only worth offering a choice when there's more than one real log to
# choose between -- with 0 or 1 found, "Auto" already points at the only
# thing there is (or the only thing worth distinguishing from itself),
# and a picker with nothing meaningful to pick is just clutter. (index 0
# of _discovered_logs is always the synthetic "Auto" entry, so "more than
# one real log" is length > 2, not > 1.)
_discovered_logs = _discover_debug_logs()
PREVIEW_CONTROLS = [
    dict(kind="log_list", var="debug_log", label="Choose debug log to run preview on",
         options=_discovered_logs, default=""),
] if len(_discovered_logs) > 2 else []

SECTIONS = [
    ("Display", [
        dict(kind="choice", label="Show on", path=("svg_target",),
             options=["pc", "printer", "both"],
             tooltip="Where the heatmap shows up. 'pc' = only in OrcaStrator's own progress window at "
                     "export time. 'printer' = only embedded in the g-code for the printer's own console/"
                     "screen. 'both' = show it in both places. No 'off' -- this whole processor exists to "
                     "show this, so hiding it everywhere isn't a meaningful choice to offer."),
        dict(kind="number", label="Width (px)", path=("canvas_width_px",),
             min=40, max=2000, step=10,
             tooltip="Width of the rendered strip, in pixels. Independent of Height below -- each is "
                     "solved for directly, not derived from the other via a fixed aspect ratio."),
        dict(kind="number", label="Height (px)", path=("canvas_height_px",),
             min=20, max=2000, step=10,
             tooltip="Height of the rendered strip, in pixels. Independent of Width above."),
    ]),
    ("Timing", [
        dict(kind="nullable_number", label="Per-toolchange time cost (s)", path=("tool_change_time_seconds",),
             min=0, max=300, step=1, auto_placeholder=0,
             tooltip="How many seconds one toolchange's swap/purge/park routine costs -- added once per "
                     "toolchange that's already happened, since the underlying time estimate treats a "
                     "toolchange line itself as instantaneous. Auto reads OrcaSlicer's own "
                     "'machine_tool_change_time' setting from the file (0 if that's not present)."),
        dict(kind="nullable_number", label="Print duration override (s)", path=("print_duration_seconds",),
             min=0, max=999999, step=60, auto_placeholder=0,
             tooltip="Sets the timeline's right edge, so a stretch of print after the last toolchange "
                     "(nothing left to swap) shows as base color instead of being silently cut off. Auto "
                     "reads OrcaSlicer's own 'estimated printing time' line from the file."),
        dict(kind="bool", label="Ignore first toolchange", path=("ignore_first_toolchange",),
             tooltip="The first toolchange in the file is the printer's initial tool selection at print "
                     "start, not a mid-print swap -- there's nothing before it to have been close together "
                     "with. On by default: dropped entirely, not just hidden."),
    ]),
    ("Clustering", [
        dict(kind="number", label="Kernel width (seconds)", path=("kernel_sigma_seconds",),
             min=1, max=300, step=1,
             tooltip="How wide, in estimated real seconds, the clustering kernel looks around each point in "
                     "time. Smaller = only toolchanges within a few seconds of each other register as a "
                     "cluster; larger = treats a broader neighborhood as one cluster."),
        dict(kind="nullable_number", label="Cluster gap (seconds)", path=("cluster_gap_seconds",),
             min=1, max=1200, step=1, auto_placeholder=0,
             tooltip="How many seconds can separate two consecutive toolchanges and still count them as the "
                     "same cluster. Toolchanges within a cluster render as ONE gradient block spanning first "
                     "to last -- cool at the edges, hot at the real center of density -- instead of one flat "
                     "color per toolchange (this canvas can't draw a true gradient fill directly, so a block "
                     "fakes one with many thin strips). Anything with nothing this close to it is its own "
                     "cluster of one and just draws as a single line. Auto uses 3x the kernel width above."),
        dict(kind="nullable_number", label="Density scale", path=("density_scale",),
             min=0, max=1000, step=1, auto_placeholder=0,
             tooltip="Auto uses this print's own busiest cluster as the reference point for 'fully hot', so "
                     "the color scale always adapts to the file. Set a fixed number instead to keep the scale "
                     "consistent when comparing the heatmap across different prints."),
    ]),
    ("Rendering", [
        dict(kind="number", label="Minimum toolchanges to render", path=("min_toolchanges",),
             min=0, max=20, step=1, is_int=True,
             tooltip="Skip rendering entirely if the file has fewer toolchanges than this -- nothing "
                     "meaningful to show with 0 or 1."),
        dict(kind="hex_color", label="Cool color (cluster edge)", path=("cool_color",), default="#2b6cb0",
             tooltip="Color a point takes at the edge of a cluster, or an isolated toolchange with nothing "
                     "nearby."),
        dict(kind="hex_color", label="Hot color (cluster center)", path=("hot_color",), default="#e53e3e",
             tooltip="Color a point takes at the real center of density of the print's busiest cluster."),
        dict(kind="hex_color", label="Base color (no toolchanges)", path=("base_color",), default="#3a4152",
             tooltip="Flat fill for the whole strip everywhere that isn't inside a cluster's block -- a "
                     "separate color from Cool, not just the coolest end of that same gradient, so 'nothing "
                     "happened here' can't be mistaken for 'a real toolchange with nothing nearby'."),
        dict(kind="number", label="Isolated line width (px)", path=("line_width_px",),
             min=1, max=15, step=1, is_int=True,
             tooltip="Width of a single isolated toolchange's line (a cluster of one). No effect on "
                     "multi-toolchange blocks, which are sized by their own real time span instead."),
        dict(kind="number", label="Gradient smoothness (px/strip)", path=("block_strip_target_px",),
             min=1, max=20, step=1, is_int=True,
             tooltip="Roughly how many screen pixels wide each strip inside a gradient block is. Lower = "
                     "smoother-looking gradient but more shapes to render; higher = coarser but faster."),
        dict(kind="number", label="Color curve (gamma)", path=("color_curve_gamma",),
             min=0.2, max=5, step=0.1,
             tooltip="Reshapes the cool->hot transition without changing which point is actually the "
                     "busiest. 1.0 = linear. If most toolchanges land at similarly high density, the "
                     "timeline can read as 'mostly hot, tiny sliver of cool' even though it's technically "
                     "correct -- raise this (try 2-3) to spread the transition out and make more of the "
                     "gradient visible. Below 1.0 warms things up faster instead."),
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
    _discover_debug_logs() found. Auto re-resolves against cfg's own
    live debug settings via read_debug_dump(); the extra discovered
    options are fixed to whatever they were at scan time (see module
    docstring). Both land at the same default debug folder now that
    per-processor debug.path overrides are gone.
    """
    chosen_path = (controls or {}).get("debug_log") or ""
    if chosen_path:
        try:
            dbg = _json.loads(_pathlib.Path(chosen_path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Could not read the selected debug log ({chosen_path}): {exc}")
    else:
        debug_cfg = cfg.get("debug", {}) or {}
        dbg = _heatmap.read_debug_dump("toolchange_heatmap", debug_cfg, _heatmap.SCRIPT_PATH)
    if not dbg:
        raise RuntimeError(
            "No debug log yet for this processor. Run OrcaStrator once (with Debug enabled below) to "
            "populate this preview -- it renders against that real recorded data, not a synthetic sample."
        )
    raw_events = dbg.get("events") or []
    if not raw_events:
        raise RuntimeError("Last debug log has no toolchanges recorded -- nothing to preview.")

    # These are the FINAL calibrated timestamps from that real run (after
    # tool_change_time_seconds and ignore_first_toolchange were already
    # applied) -- see the module docstring for why editing those two
    # settings here won't move this preview.
    events = [{"line": e.get("line"), "tool": e.get("tool"), "t": e.get("t")} for e in raw_events]

    duration_override = cfg.get("print_duration_seconds")
    if duration_override is not None and float(duration_override) > 0:
        print_duration = float(duration_override)
    else:
        print_duration = dbg.get("print_duration_seconds")

    payload, _summary = _heatmap.build_svg_payload(events, cfg, print_duration_seconds=print_duration)
    payload["title"] = f"{payload.get('title', 'Toolchange Heatmap')} -- preview from {dbg.get('file', 'last run')}"
    return payload
