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
Toolchange heatmap -- OrcaSlicer post-processing script
=========================================================================
Read-only visualization: a single-lane timeline of every toolchange in
the file, positioned by estimated ELAPSED PRINT TIME (not line number or
file order -- two toolchanges ten lines apart after a long travel move
aren't "close" in any way that matters). Each toolchange draws as a
single vertical line at its own position, over a flat base_color fill
covering everywhere else. A line's hue is driven by that toolchange's own
local *density* -- how many other toolchanges are clustered near it in
time, not just the distance to its single nearest neighbor: sitting at
the center of a tight run of changes reads hot; sitting further out from
that cluster's center, or alone, shifts back toward cool. base_color is a
third, distinct color, not just the cool end of that same gradient --
"no toolchanges happened here" and "a real toolchange happened here with
nothing else nearby" are different things and shouldn't look identical.

Purely informational -- never blocks a print. Always exits 0 and only
ever emits "info" notices. The very first toolchange in the file (the
initial tool selection at print start, not a mid-print swap) is excluded
by default -- see ignore_first_toolchange.

Note on the block sampling: a continuous sample point isn't leave-one-
out the way an actual event's own reading is (there's no single "self"
term to subtract from an arbitrary point in time), but it's still made
to subtract 1.0 for consistency with event_density -- without that, a
raw KDE sum runs about 1.0 higher than the leave-one-out reference it's
normalized against, and nearly an entire block clips to max-hot instead
of showing its real shape.

Renders where svg_target says to (default "pc" only, see resolve_svg_targets()
-- "pc", "printer", or "both"; no "off", since this whole processor IS the
visualization). The PC side shows directly in OrcaStrator's own progress
window at export time, via the exact same generic canvas renderer every
other processor's SVG_PAYLOAD already uses (_TkProgressUI._draw_payload
in orcastrator.py). Classic Tk has no real alpha compositing (see
orcastrator.py's _color_to_tk) -- alpha only maps onto a handful of
dither stipple tiers, which read as visual noise here. So every shape
renders fully opaque (fill_style "solid" on the base fill; line paths
are never stippled by the renderer regardless) and intensity is carried
entirely by hue -- a full HSV sweep between the two configured colors,
not a flat RGB blend, since blending two saturated, differently-hued
colors in RGB passes through a desaturated grey at the midpoint, exactly
wrong for a heatmap.

This is a read-only reporting step -- it never touches the g-code -- so
add it to explicit_order_last in configs/orcastrator.json (OrcaStrator
Settings -> Processor Selection -> "Runs last") once it's dropped in,
same as any other processor that wants the file's final state once
everything else has had its turn. It doesn't strictly depend on that --
the time model here only cares about move distances/feeds, not the
restore-position X/Y/Z annotations -- but there's no reason for it to run
before anything else either.

Timing note: helpers/time_estimator.py's build_cumulative_time() gives a
naive (distance/feedrate) estimate of MOVE time, and deliberately treats
an actual toolchange line itself as instantaneous/free (see its consume()
-- a toolchange only resyncs X/Y/Z there, no time added) -- the real
swap/purge/park dwell that a toolchange costs isn't move time, so it was
never going to show up in that model. This processor accounts for it
separately: it reads OrcaSlicer's own "; machine_tool_change_time = N"
line from the embedded config block at the end of the file and adds N
seconds per toolchange that's already happened by that point. That's
deliberately NOT the same thing as that module's calibrate_ratios() --
this processor does not use calibrate_ratios()/global_ratio at all. That
function's ratio is a narrow, local correction for OrcaSlicer's own
stated per-tool PREHEAT lead time against the naive model's estimate of
that one short span -- other processors here (disable_unused_tool_temps.py,
insert_missing_tool_preheat.py) use it correctly for exactly that. It is
not a general naive-to-real scaling factor for a whole file's elapsed
time, and using it as one here badly over-scaled the timeline whenever
one tool's single calibration sample happened to be a noisy outlier.

Similarly, the "time" x-axis's own right edge is NOT just the last
toolchange's own timestamp -- a file that keeps printing for a long
stretch after its last toolchange (nothing left to swap, still printing)
would otherwise have that entire trailing stretch silently missing from
the canvas, not compressed, just absent. Instead it reads OrcaSlicer's
own "; estimated printing time (normal mode) = XhYmZs" line -- see
find_estimated_print_time_seconds() -- for the real full duration, and
only ever extends the axis with it, never shrinks it below the last
toolchange's own time.

One more correction sits on top of both of the above: naive MOVE time
alone (excluding the toolchange overhead already handled above) is still
somewhat shorter than OrcaSlicer's own stated total, since the naive
model has no acceleration/jerk simulation for ordinary moves either.
naive_time_scale_factor() (see helpers/time_estimator.py) rescales that
residual -- real duration minus the toolchange overhead already known
from the previous correction -- against the naive move time at the point
where real machine motion stops (the `; EXECUTABLE_BLOCK_END` marker, or
EOF if missing). Subtracting known toolchange overhead first matters:
this processor's whole reason for existing is toolchange-heavy files,
where that overhead can be the vast majority of the naive-vs-real gap
(5500+ of 7100+ seconds missing on a 172-toolchange file measured while
building this). Computing the ratio from the UN-adjusted gap conflates
that overhead with genuine move-time error and produces a scale factor
several times too large, which then drags every toolchange's position --
including ones nowhere near the end of the file -- artificially later,
since the ratio is applied to raw move time from t=0. Isolating the
residual first keeps the scale factor small and keeps early/mid-file
toolchange positions close to where the (already-correct) toolchange-
overhead accounting alone would have put them.
"""
import colorsys
import json
import math
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from helpers.time_estimator import PAT_TOOLCHANGE, find_config_path
from helpers.timeline_scale import (
    CANVAS_PAD, CANVAS_UNIT_REF, calibrate_timeline, resolve_canvas_dims,
)
from helpers.debug_dump import write_debug_dump, read_debug_dump, list_debug_dumps
from helpers.notice import display_flag as _notice_display_flag

SCRIPT_PATH = pathlib.Path(__file__).resolve()

# CANVAS_UNIT_REF/CANVAS_PAD and the canvas-dims/timing-calibration math
# now live in helpers/timeline_scale.py (imported above) -- shared with
# tool_temperature_graph.py so both processors' timelines use the exact
# same internal coordinate space and calibration, and can be compared/
# overlaid directly. Only the per-processor default pixel size stays
# local, since each timeline has its own natural size/aspect ratio.
DEFAULT_CANVAS_WIDTH_PX = 260.0
DEFAULT_CANVAS_HEIGHT_PX = 56.64  # reproduces this processor's original fixed 1000x180 aspect ratio


def _resolve_canvas_dims(cfg: dict):
    return resolve_canvas_dims(cfg, DEFAULT_CANVAS_WIDTH_PX, DEFAULT_CANVAS_HEIGHT_PX)

DEFAULTS = {
    "kernel_sigma_seconds": 15.0,
    "density_scale": None,
    "min_toolchanges": 2,
    "cool_color": "#2b6cb0",
    "hot_color": "#e53e3e",
    "base_color": "#3a4152",
    "tool_change_time_seconds": None,
    "print_duration_seconds": None,
    "ignore_first_toolchange": True,
    "line_width_px": 2.0,
    "color_curve_gamma": 1.0,
    "cluster_gap_seconds": None,
    "block_strip_target_px": 2.0,
    "svg_target": "pc",
    "canvas_width_px": DEFAULT_CANVAS_WIDTH_PX,
    "canvas_height_px": DEFAULT_CANVAS_HEIGHT_PX,
    "debug": {"enabled": True},
    "notice": {"display": True},
}

_SVG_TARGET_MAP = {"pc": ["pc"], "printer": ["printer"], "both": ["pc", "printer"]}


def resolve_svg_targets(cfg: dict):
    """
    "pc" | "printer" | "both" -> the SVG_PAYLOAD "targets" list -- no
    "off" here (unlike dock_collision_guard.py's per-outcome display
    settings), since this whole processor IS the visualization; there's
    no non-visual purpose for it to run at all, so hiding its only output
    everywhere isn't a meaningful choice to offer. Unrecognized/missing
    value falls back to "pc" (this processor's default when unset).
    """
    return _SVG_TARGET_MAP.get(str(cfg.get("svg_target") or "pc").strip().lower(), ["pc"])


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
    path = find_config_path("toolchange_heatmap.json", SCRIPT_PATH)
    if path is None:
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    for key in ("kernel_sigma_seconds", "density_scale", "min_toolchanges",
                "cool_color", "hot_color", "base_color",
                "tool_change_time_seconds", "print_duration_seconds",
                "ignore_first_toolchange", "line_width_px", "color_curve_gamma",
                "cluster_gap_seconds", "block_strip_target_px", "svg_target",
                "canvas_width_px", "canvas_height_px"):
        if key in raw:
            cfg[key] = raw[key]
    if isinstance(raw.get("debug"), dict):
        cfg["debug"] = {**cfg["debug"], **raw["debug"]}
    if isinstance(raw.get("notice"), dict):
        cfg["notice"] = {**cfg["notice"], **raw["notice"]}
    return cfg


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def find_toolchanges(lines) -> list:
    events = []
    for i, ln in enumerate(lines):
        m = PAT_TOOLCHANGE.match(ln.strip())
        if m:
            events.append({"line": i, "tool": int(m.group(1))})
    return events





def hex_to_rgb(h: str):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        h = "888888"
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (136, 136, 136)


def hex_to_hsv(h: str):
    r, g, b = hex_to_rgb(h)
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)


def lerp_hsv(hsv0, hsv1, t: float):
    """
    Interpolates hue around the shorter arc of the color wheel, instead
    of averaging RGB channels directly. A straight RGB blend between two
    saturated, differently-hued colors (the default blue and red) passes
    through a desaturated grey/mauve at the midpoint -- exactly backwards
    for a heatmap, where the busiest region is the one point that must
    NOT read as the least colorful. Sweeping hue keeps every step of the
    scale visually distinct and saturated the whole way from cool to hot.
    """
    t = max(0.0, min(1.0, t))
    h0, s0, v0 = hsv0
    h1, s1, v1 = hsv1
    dh = h1 - h0
    if dh > 0.5:
        dh -= 1.0
    elif dh < -0.5:
        dh += 1.0
    h = (h0 + dh * t) % 1.0
    s = s0 + (s1 - s0) * t
    v = v0 + (v1 - v0) * t
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def density_at(t: float, times: list, sigma: float) -> float:
    """
    Sum of a Gaussian kernel centered at every timestamp in `times`,
    evaluated at `t`. High wherever several timestamps sit close
    together -- this is a standard kernel density estimate, so a run of
    three or four moderately-spaced toolchanges still reads as a hot
    cluster even if no single pair of them is razor-close, which is the
    whole point of using density here rather than nearest-neighbor gap.
    """
    return sum(math.exp(-0.5 * ((t - tj) / sigma) ** 2) for tj in times)


def build_svg_payload(events: list, cfg: dict, print_duration_seconds=None):
    """
    Returns (payload, summary) -- summary carries the numbers used for
    the NOTICE message and the debug dump, so they can't drift out of
    sync with what the picture actually shows.

    Design: a flat base_color fill for the whole strip, with toolchanges
    grouped into CLUSTERS -- runs where consecutive toolchanges are no
    more than cluster_gap_seconds apart -- and each cluster rendered as
    its own block spanning exactly its first to its last toolchange.
    There's no real gradient-fill primitive available on this canvas (see
    orcastrator.py's _draw_payload -- create_polygon only takes a flat
    fill or a coarse alpha-derived stipple, no linear/radial gradient), so
    a block fakes one with many thin adjacent solid strips, sampled
    finely enough to read as continuous, confined to an actual cluster's
    own span rather than smeared across the whole file -- so an empty
    stretch is still flatly base_color, not a fade. Within a block, each strip's color comes from
    the real kernel density AT that point in time -- naturally highest
    near a cluster's real center of mass and tapering toward its edges,
    which is what actually produces "hot at the center, cooler further
    out" rather than assuming every cluster is evenly/symmetrically
    packed. A cluster of exactly one toolchange (nothing within
    cluster_gap_seconds of it) has zero span and just draws as a single
    thin line instead, since there's no room for a gradient anyway.

    print_duration_seconds, if given, is the REAL full print duration
    (see find_estimated_print_time_seconds) and sets the "time" x-axis's
    right edge -- otherwise that edge would only ever reach the last
    toolchange's own timestamp, silently cutting off any trailing stretch
    of the print that happens after the last toolchange with nothing left
    to measure it. Never used to shrink the axis below the last
    toolchange's own time, only to extend it.
    """
    # Per-call, cfg-dependent canvas size -- deliberately shadows the
    # module-level CANVAS_UNIT_REF-derived names for the rest of this
    # function (see _resolve_canvas_dims()), so every use of
    # CANVAS_X_UNITS/CANVAS_Y_UNITS/CANVAS_PAD/CANVAS_MAX_SIZE/BAND_Y
    # below -- including inside the nested closures, which see this via
    # normal closure scoping -- picks up THIS payload's configured
    # width/height without having to rename any of them individually.
    CANVAS_X_UNITS, CANVAS_Y_UNITS, CANVAS_PAD, CANVAS_MAX_SIZE = _resolve_canvas_dims(cfg)
    BAND_Y = CANVAS_Y_UNITS / 2.0

    times = [e["t"] for e in events]
    n_events = len(events)
    last_event_time = max(times) if times else 0.0
    total_time = max(last_event_time, float(print_duration_seconds or 0.0))
    sigma = float(cfg.get("kernel_sigma_seconds") or 15.0)
    if sigma <= 0:
        sigma = 1e-6
    def x_of(t, idx):
        return (t / total_time * CANVAS_X_UNITS) if total_time > 0 else 0.0

    # Leave-one-out density at each event's own timestamp (subtract 1.0,
    # the event's own self-contribution of exp(0)) -- this IS "distance
    # from the center of its cluster": an event sitting at the dense
    # center of a run of close-together toolchanges has many near
    # neighbors and a high reading; one out at the edge of that same run,
    # or sitting alone, has few and reads low. Used for the color-scale
    # reference and the debug/notice numbers -- NOT for the block
    # gradient itself, which samples density_at() continuously instead
    # (see sample_block()).
    event_density = [density_at(ti, times, sigma) - 1.0 for ti in times]

    override = cfg.get("density_scale")
    if override is not None and float(override) > 0:
        reference = float(override)
    else:
        reference = max(event_density, default=0.0) or 1.0

    cool_hsv = hex_to_hsv(cfg.get("cool_color", "#2b6cb0"))
    hot_hsv = hex_to_hsv(cfg.get("hot_color", "#e53e3e"))
    base_color = cfg.get("base_color", "#3a4152")
    gamma = float(cfg.get("color_curve_gamma") or 1.0)
    if gamma <= 0:
        gamma = 1.0

    def color_for(density):
        norm = min(1.0, max(0.0, density / reference)) if reference > 0 else 0.0
        # gamma == 1.0: unchanged (linear). gamma > 1.0 pushes low/mid
        # readings further down before they start warming up -- useful
        # when most toolchanges in a file land at high density and the
        # whole timeline reads as "mostly hot, tiny sliver of cool"; a
        # gamma < 1.0 does the reverse, warming things up faster. This
        # only reshapes the cool->hot transition -- it doesn't change
        # which toolchange is the actual busiest (still norm == 1.0).
        eased = norm ** gamma
        r, g, b = lerp_hsv(cool_hsv, hot_hsv, eased)
        return f"rgba({r},{g},{b},1.0)", norm

    xs = [x_of(e["t"], i) for i, e in enumerate(events)]

    # A cluster is a run of consecutive (already time-sorted) toolchanges
    # where no gap between neighbors exceeds cluster_gap_seconds. Beyond
    # ~3 sigma the Gaussian kernel's contribution is negligible, so that's
    # the natural default cutoff -- past that distance two toolchanges
    # aren't meaningfully influencing each other's density reading anyway.
    gap_override = cfg.get("cluster_gap_seconds")
    if gap_override is not None and float(gap_override) > 0:
        cluster_gap = float(gap_override)
    else:
        cluster_gap = sigma * 3.0

    clusters = []
    if n_events:
        start = 0
        for i in range(1, n_events):
            if times[i] - times[i - 1] > cluster_gap:
                clusters.append((start, i - 1))
                start = i
        clusters.append((start, n_events - 1))

    # Target roughly this many screen pixels per gradient strip -- ties
    # the sample count to how it'll actually look rendered, rather than a
    # fixed count per block regardless of size (wasteful for a small
    # cluster, too coarse for a wide one).
    canvas_px = CANVAS_MAX_SIZE - 2.0 * CANVAS_PAD
    px_per_canvas_unit = (canvas_px / CANVAS_X_UNITS) if CANVAS_X_UNITS > 0 else 1.0
    strip_target_px = float(cfg.get("block_strip_target_px") or 2.0)
    strip_target_canvas = (strip_target_px / px_per_canvas_unit) if px_per_canvas_unit > 0 else 1.0

    def sample_block(s_idx, e_idx, n_samples):
        """
        (x, density) pairs evenly spaced across a cluster's span. Density
        here uses the SAME "-1.0" self-contribution subtraction as
        event_density above, applied consistently even at points that
        aren't an actual event -- without it, these continuous readings
        run about 1.0 higher than the leave-one-out reference they're
        normalized against (a raw KDE sum vs. one point's own contribution
        already subtracted out), clipping nearly the whole block to
        max-hot instead of showing its real shape.
        """
        t0, t1 = times[s_idx], times[e_idx]
        out = []
        for k in range(n_samples):
            t = t0 + (t1 - t0) * k / (n_samples - 1)
            x = (t / total_time * CANVAS_X_UNITS) if total_time > 0 else 0.0
            out.append((x, density_at(t, times, sigma) - 1.0))
        return out

    line_half = CANVAS_Y_UNITS * 0.42
    # line_width_px is a screen-pixel width, but the printer/Klipper side
    # (see resolve_svg_targets()) only understands "polygon"/"crosshair"
    # shapes -- no "path" line primitive there, unlike the PC-side Tk
    # renderer, which does support it. Rather than have isolated
    # toolchanges silently vanish whenever svg_target includes "printer",
    # they're drawn as a thin polygon rectangle instead, which both
    # renderers already handle -- converted from the configured pixel
    # width into canvas units using the same fixed px-per-canvas-unit
    # ratio as block_strip_target_px above, so it still reads as the same
    # width on screen either way.
    line_width_canvas = (float(cfg.get("line_width_px") or 2.0) / px_per_canvas_unit
                          if px_per_canvas_unit > 0 else 1.0)
    line_half_width = line_width_canvas / 2.0

    shapes = [
        # Flat base fill covering the whole strip -- everywhere there
        # isn't a toolchange (or a cluster's block) is this color, full
        # stop. Not a gradient endpoint, not the same thing as cool_color.
        {"type": "polygon",
         "points": [[0, 0], [CANVAS_X_UNITS, 0], [CANVAS_X_UNITS, CANVAS_Y_UNITS], [0, CANVAS_Y_UNITS]],
         "fill": base_color, "stroke": base_color, "fill_style": "solid"},
    ]

    summary_rows = []
    for s_idx, e_idx in clusters:
        if s_idx == e_idx:
            # Nothing within cluster_gap_seconds of it -- no span to
            # gradient across, just mark it.
            color, _ = color_for(event_density[s_idx])
            xc = xs[s_idx]
            shapes.append({
                "type": "polygon",
                "points": [[xc - line_half_width, BAND_Y - line_half], [xc + line_half_width, BAND_Y - line_half],
                           [xc + line_half_width, BAND_Y + line_half], [xc - line_half_width, BAND_Y + line_half]],
                "fill": color, "stroke": color, "fill_style": "solid",
            })
        else:
            block_width_canvas = abs(xs[e_idx] - xs[s_idx])
            n_samples = max(4, min(40, int(block_width_canvas / strip_target_canvas) + 2))
            samples = sample_block(s_idx, e_idx, n_samples)
            for k in range(len(samples) - 1):
                xa, da = samples[k]
                xb, db = samples[k + 1]
                color, _ = color_for((da + db) / 2.0)
                shapes.append({
                    "type": "polygon",
                    "points": [[xa, BAND_Y - line_half], [xb, BAND_Y - line_half],
                               [xb, BAND_Y + line_half], [xa, BAND_Y + line_half]],
                    "fill": color, "stroke": color, "fill_style": "solid",
                })
        for i in range(s_idx, e_idx + 1):
            e = events[i]
            d = event_density[i]
            norm = min(1.0, max(0.0, d / reference)) if reference > 0 else 0.0
            summary_rows.append({"line": e["line"], "tool": e["tool"], "t": e["t"],
                                  "density": d, "density_norm": norm})

    payload = {
        "title": "Toolchange Heatmap",
        "canvas": {"x_max": CANVAS_X_UNITS, "y_max": CANVAS_Y_UNITS, "pad": CANVAS_PAD, "max_size": CANVAS_MAX_SIZE},
        "shapes": shapes,
        "targets": resolve_svg_targets(cfg),
    }
    hottest = max(summary_rows, key=lambda r: r["density"], default=None)
    summary = {
        "total_time_seconds": total_time,
        "kernel_sigma_seconds": sigma,
        "density_scale_reference": reference,
        "events": summary_rows,
        "hottest": hottest,
    }
    return payload, summary


def process(gcode_path: str) -> None:
    p = pathlib.Path(gcode_path)
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    cfg = load_config()
    global _notice_cfg
    _notice_cfg = cfg

    events = find_toolchanges(lines)
    raw_count = len(events)
    # Captured before the initial-toolchange pop below, and before any
    # timing gets computed: naive_time_scale_factor() needs the full set
    # of toolchange line positions (including the initial selection,
    # which costs real dock time too) to know how much of the naive-vs-
    # real gap is already-known toolchange overhead vs. generic move-
    # time error -- see that function's docstring.
    toolchange_line_idxs = sorted(e["line"] for e in events)

    # The very first toolchange in the file is the printer's initial tool
    # selection at the start of the print, not a swap mid-print -- there's
    # nothing before it to have been "close together" with, and treating
    # it as a real event was what made it (and only it) the leftmost thing
    # on the timeline, which is misleading rather than informative.
    ignored_first = None
    if cfg.get("ignore_first_toolchange", True) and events:
        ignored_first = events.pop(0)

    debug_data = {"file": friendly_filename(p), "toolchange_count": len(events),
                  "toolchange_count_including_first": raw_count,
                  "ignored_first_toolchange": ignored_first}

    min_toolchanges = int(cfg.get("min_toolchanges") or 2)
    if len(events) < min_toolchanges:
        print_notice("info", "Toolchange Heatmap",
                     f"Only {len(events)} toolchange(s) found -- nothing meaningful to visualize.")
        write_debug_dump("toolchange_heatmap", cfg.get("debug", {}), debug_data, SCRIPT_PATH)
        return

    # calibrate_timeline() is the same call tool_temperature_graph.py
    # makes -- see helpers/timeline_scale.py's module docstring for why
    # that matters: both processors need the exact same tool_change_time,
    # print_duration, and naive_time_scale_factor() calibration so a
    # given g-code line lands at the same real-seconds timestamp in
    # either one's timeline. toolchange_line_idxs is the FULL set
    # (captured above, before the ignore_first_toolchange pop), which is
    # what the calibration itself needs even though the ignored first
    # event no longer appears in `events` below.
    timeline = calibrate_timeline(lines, cfg, toolchange_line_idxs)
    tool_change_time = timeline.tool_change_time
    print_duration = timeline.print_duration
    naive_scale = timeline.naive_scale

    # Naive move time only gets you the g-code's own moves -- an actual
    # toolchange line is treated as free/instantaneous by that model (see
    # helpers/time_estimator.py's consume()), since the real swap/purge/
    # park dwell isn't move time at all. timeline.time_at() adds it back
    # in: N toolchanges have already happened strictly before a given
    # line, each costing tool_change_time seconds the move model never
    # saw. Deliberately NOT calibrate_ratios()/global_ratio -- see the
    # module docstring for why that's a different, narrower tool this
    # processor shouldn't have been reusing.
    for e in events:
        e["t"] = timeline.time_at(e["line"])

    payload, summary = build_svg_payload(events, cfg, print_duration_seconds=print_duration)
    print_svg_payload(payload)

    debug_data.update(summary)
    debug_data["tool_change_time_seconds"] = tool_change_time
    debug_data["print_duration_seconds"] = print_duration
    debug_data["naive_time_scale_factor"] = naive_scale
    write_debug_dump("toolchange_heatmap", cfg.get("debug", {}), debug_data, SCRIPT_PATH)

    total = summary["total_time_seconds"]
    hottest = summary["hottest"]
    count_note = " (excluding the initial tool selection)" if ignored_first is not None else ""
    if hottest and hottest["density_norm"] > 0.05:
        msg = (f"{len(events)} toolchanges{count_note} over {format_duration(total)}. Busiest cluster around "
               f"T{hottest['tool']} at {format_duration(hottest['t'])}.")
    else:
        msg = f"{len(events)} toolchanges{count_note} over {format_duration(total)}, no significant clustering."
    print_notice("info", "Toolchange Heatmap", msg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python toolchange_heatmap.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1])
