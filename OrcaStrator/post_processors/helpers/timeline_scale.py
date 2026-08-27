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
Shared time-calibration and canvas-geometry helpers for every processor
that draws a time-based timeline SVG (tool_temperature_graph.py,
toolchange_heatmap.py, and any future one).

This logic must live in one place: two processors independently
calibrating their own copy is an easy way to drift apart, since
naive_time_scale_factor() has a subtle input that's easy to get wrong
-- whether toolchange_line_idxs/tool_change_time are passed to subtract
known toolchange overhead from the residual before turning it into a
ratio, or not. Skip that argument in one caller and not the other and
the two processors calibrate their naive-move-time-to-real-seconds
ratio differently for the exact same g-code file, so a given line index
lands at a different real-seconds timestamp depending on which
processor you asked -- their x-axes would silently disagree even when
graphing the same print. Centralising the calibration here (single call
site, one set of arguments) makes that class of drift impossible: every
caller gets the exact same tool_change_time, print_duration, and
naive_scale for a given (lines, cfg, toolchange_line_idxs) triple, so
their timelines can be compared and overlaid directly.

canvas geometry (resolve_canvas_dims) is included for the same reason,
even though it doesn't affect calibration: it's the other half of "does
1 real second cover the same on-screen distance", and two callers
solving it slightly differently would be just as easy a way to drift
apart as the timing math itself.
"""
import bisect
import re

from .time_estimator import build_cumulative_time, naive_time_scale_factor

PAT_MACHINE_TOOL_CHANGE_TIME = re.compile(r'^\s*;\s*machine_tool_change_time\s*=\s*([\d.]+)', re.IGNORECASE)
PAT_ESTIMATED_PRINT_TIME = re.compile(
    r'^\s*;\s*estimated printing time.*?=\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?', re.IGNORECASE)

# Fixed internal coordinate space -- NOT physical mm, and NOT raw elapsed
# seconds either. A caller's x-axis runs 0..x_max (from
# resolve_canvas_dims) across the print's full calibrated duration,
# regardless of whether that print is 10 minutes or 10 hours long. Every
# timeline processor shares this same reference and pad so that, at
# matching canvas_width_px/canvas_height_px, their pixel-per-second rate
# is identical too -- not just their event timestamps.
CANVAS_UNIT_REF = 1000.0
CANVAS_PAD = 6.0


def find_machine_tool_change_time(lines):
    """OrcaSlicer writes its resolved print settings as "; key = value"
    comment lines in a config block, usually near the end of the file --
    machine_tool_change_time is the number of seconds it estimates one
    toolchange's swap/purge/park routine costs. Returns None if the line
    isn't present (older OrcaSlicer versions, or a non-OrcaSlicer
    file)."""
    for ln in lines:
        m = PAT_MACHINE_TOOL_CHANGE_TIME.match(ln)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def find_estimated_print_time_seconds(lines):
    """OrcaSlicer writes "; estimated printing time (normal mode) = 2h 34m 34s"
    near the top of the file -- its own estimate of the FULL print
    duration, covering everything, not just the span between
    toolchanges. Used as a timeline's real extent: the naive move-time
    model this module's calibration otherwise relies on only ever
    reaches as far as the last measured event, so a file that runs on
    for a long stretch after that (nothing left to measure) would have
    that entire trailing stretch silently missing from the canvas -- not
    compressed, just absent. Returns None if the line isn't present."""
    for ln in lines:
        m = PAT_ESTIMATED_PRINT_TIME.match(ln)
        if m and any(m.groups()):
            h = int(m.group(1)) if m.group(1) else 0
            mi = int(m.group(2)) if m.group(2) else 0
            s = int(m.group(3)) if m.group(3) else 0
            return float(h * 3600 + mi * 60 + s)
    return None


def resolve_canvas_dims(cfg: dict, default_width_px: float, default_height_px: float,
                         pad: float = CANVAS_PAD, unit_ref: float = CANVAS_UNIT_REF):
    """Turns canvas_width_px/canvas_height_px into the (x_max, y_max, pad,
    max_size) quartet a payload's "canvas" key expects. Both axes scale
    by the SAME factor (whichever of x_max/y_max is dominant sets it),
    so canvas units stay isotropic -- 1 unit is the same number of
    pixels in both directions, which line-width-in-pixels math relies
    on. Whichever pixel target (width_px/height_px) is larger becomes
    the dominant, unit_ref-scaled axis; the other axis's unit value is
    solved backwards from that same scale factor, so it doesn't silently
    dictate its own, different scale.

    default_width_px/default_height_px are per-caller -- each timeline
    has its own natural size/aspect ratio -- everything else here is
    identical math every caller shares."""
    width_px = max(40.0, float(cfg.get("canvas_width_px") or default_width_px))
    height_px = max(20.0, float(cfg.get("canvas_height_px") or default_height_px))
    max_size = max(width_px, height_px)
    inner = max(1.0, max_size - 2.0 * pad)
    if width_px >= height_px:
        x_max = unit_ref
        y_max = max(1.0, (height_px - 2.0 * pad) * x_max / inner)
    else:
        y_max = unit_ref
        x_max = max(1.0, (width_px - 2.0 * pad) * y_max / inner)
    return x_max, y_max, pad, max_size


class Timeline:
    """The calibrated time model returned by calibrate_timeline() --
    everything needed to turn a g-code line index into real, calibrated
    seconds the exact same way every timeline processor does."""

    def __init__(self, cum, toolchange_line_idxs, tool_change_time, print_duration, naive_scale):
        self.cum = cum
        self.toolchange_line_idxs = toolchange_line_idxs
        self.tool_change_time = tool_change_time
        self.print_duration = print_duration
        self.naive_scale = naive_scale

    def time_at(self, line_idx: int) -> float:
        """Real, calibrated seconds at line_idx: naive per-line time
        rescaled by naive_scale, plus the toolchange overhead (already
        real seconds, not naive-model time) for every toolchange
        strictly before this line -- see naive_time_scale_factor()'s
        docstring for why overhead is added after scaling, not before.
        bisect_left on the sorted toolchange line list is exact and
        doesn't require walking the file again per call."""
        n_before = bisect.bisect_left(self.toolchange_line_idxs, line_idx)
        return self.cum[line_idx] * self.naive_scale + n_before * self.tool_change_time


def calibrate_timeline(lines, cfg: dict, toolchange_line_idxs) -> Timeline:
    """Single source of truth for tool_change_time, print_duration, and
    the naive_time_scale_factor() calibration -- the three numbers that
    determine where any g-code line lands on a calibrated time axis.
    Every timeline processor MUST build its event timestamps through
    this (via the returned Timeline.time_at()), or two processors
    graphing the same print can silently disagree on where a given line
    sits, even though nothing about the underlying g-code differs.

    toolchange_line_idxs must be the FULL, sorted list of every
    toolchange's line index in the file -- including any "first/initial
    selection" toolchange a caller might otherwise ignore for its OWN
    display purposes (see toolchange_heatmap.py's
    ignore_first_toolchange) -- naive_time_scale_factor() needs the
    complete set to know how much of the naive-vs-real gap is
    already-known toolchange overhead rather than generic move-time
    error.

    Honours the same two manual overrides every timeline processor
    exposes: tool_change_time_seconds and print_duration_seconds.
    """
    cum = build_cumulative_time(lines)

    override = cfg.get("tool_change_time_seconds")
    if override is not None and float(override) >= 0:
        tool_change_time = float(override)
    else:
        tool_change_time = find_machine_tool_change_time(lines) or 0.0

    duration_override = cfg.get("print_duration_seconds")
    if duration_override is not None and float(duration_override) > 0:
        print_duration = float(duration_override)
    else:
        print_duration = find_estimated_print_time_seconds(lines)

    naive_scale = naive_time_scale_factor(
        cum, lines, print_duration,
        toolchange_line_idxs=toolchange_line_idxs, tool_change_time=tool_change_time,
    )

    return Timeline(cum, toolchange_line_idxs, tool_change_time, print_duration, naive_scale)
