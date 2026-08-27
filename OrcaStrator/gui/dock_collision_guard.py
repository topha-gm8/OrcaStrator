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
Settings-form spec for configs/dock_collision_guard.json.

This is still nominally a "rich" config (KIND="rich" below), but as of
this module, "rich" means only one thing left: it has a live SVG
preview. Every actual field -- including the Dock Boundary table, via
the generic "point_table" field kind -- is now plain data in SECTIONS
below, discovered automatically by config_editor.pyw's
discover_gui_specs(), with zero code in config_editor.pyw devoted to
dock collision specifically. See CLAUDE.md for what's left of the
"rich" vs "simple" distinction (at this point, just HAS_PREVIEW).

HAS_PREVIEW / PREVIEW_CONTROLS / build_preview_payload() below are the
generic "rich preview" contract any processor can implement -- see
CLAUDE.md. config_editor.pyw builds PREVIEW_CONTROLS into generic Tk
controls, tracks their values, and calls build_preview_payload(cfg,
controls) on every change, feeding the SVG_PAYLOAD-shaped dict it
returns into orcastrator.py's own already-generic canvas renderer
(_TkProgressUI._draw_payload -- the SAME renderer a real export's
progress window uses for ANY processor's SVG_PAYLOAD, so nothing about
the renderer itself is dock-collision-specific either).

The Dock Boundary section's own validate_boundary_points() below is a
genuine piece of dock-collision domain logic (it encodes an assumption
find_closest_approach() in the processor itself relies on) -- it's
passed into the generic "point_table" field kind as validate_rows,
which has no idea what makes a row valid, only that this config's
Dock Boundary section does.
"""
import sys as _sys
import pathlib as _pathlib

_GUI_DIR = _pathlib.Path(__file__).resolve().parent
if str(_GUI_DIR) not in _sys.path:
    _sys.path.insert(0, str(_GUI_DIR))
from _plugin_support import get_in, load_processor_module

# This processor's own module -- for build_preview_payload() below to call
# its build_boundary()/max_z_allowed()/build_svg_payload(). A plugin
# depending on its own processor is correct coupling; see
# gui/_plugin_support.py's docstring.
_checker = load_processor_module("dock_collision_guard")

TITLE = "Dock Collision Guard"
SUBTITLE = "Dock boundary + visualization settings, with a live SVG preview."
CONFIG = "dock_collision_guard.json"
KIND = "rich"
HAS_PREVIEW = True

# Preview-only controls -- NOT persisted to dock_collision_guard.json,
# purely inputs to build_preview_payload() below, used to pick which
# synthetic sample scenario gets rendered. config_editor.pyw builds
# these into generic Tk controls (see _build_preview_controls) and
# passes their live values into build_preview_payload() as a plain
# {var: value} dict on every change.
PREVIEW_CONTROLS = [
    dict(kind="choice_buttons", var="scenario", label="Preview scenario:", default="collision",
         options=[("no_collision", "No collision"), ("near_miss", "Near miss"), ("collision", "Collision")]),
    dict(kind="bool", var="multi_object", label="2 objects", default=False),
    dict(kind="bool", var="simulate_hop_fail", label="Simulate failing hop", default=False),
]

SHAPES = ["cross", "circle", "square", "diamond", "triangle"]
FILL_STYLES = ["solid", "dithered"]


def validate_boundary_points(points: list) -> dict:
    """
    Validates boundary points in the sorted-by-Y order build_boundary()
    actually uses (ties broken by original list order, matching Python's
    stable sort -- same tie-break build_boundary relies on).

    A point is invalid if, versus the point before it in that order:
      - Z decreases. max_z_allowed() must be monotonically non-decreasing
        as Y increases for find_closest_approach()'s closest-approach-only
        shortcut to be valid (see its docstring) -- a "notch" where Z dips
        back down would silently produce wrong pass/fail results, not a
        crash, so this is the check that matters most.
      - it's an exact duplicate (same Y and Z) of the point before it.
        A tied Y with a *higher* Z is fine -- that's a legitimate vertical
        step in the dock wall, handled by _collapse_ties() at the
        collision-check end.

    `points` is a list of dicts with at least "y", "z", and "_row" (an
    opaque identifier echoed back so the caller can map an error to the
    UI row that produced it -- order/content otherwise unused here).

    Returns {row_id: message} for every offending row.

    This is passed into the generic "point_table" field kind's
    validate_rows below -- config_editor.pyw's engine has no idea what
    makes a boundary point valid, it just calls whatever this field
    spec hands it.
    """
    errors = {}
    ordered = sorted(points, key=lambda p: p["y"])
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        if curr["y"] == prev["y"] and curr["z"] == prev["z"]:
            errors[curr["_row"]] = (
                f"Duplicate point (Y={curr['y']:g}, Z={curr['z']:g}) -- "
                f"same as another point once sorted by Y.")
        elif curr["z"] < prev["z"]:
            errors[curr["_row"]] = (
                f"Z can't decrease vs. the previous point in Y order "
                f"(Y {prev['y']:g}->{curr['y']:g}, Z {prev['z']:g}->{curr['z']:g}).")
    return errors


_DISPLAY_TT = ("Where this outcome shows up: off **|** printer **|** pc **|** both."
               "\n'printer' = printer console only."
               "\n'pc' = OrcaStrator's own progress window (or the standalone browser popup if that's enabled)."
               "\n'both' shows it in both places.")
_MARKER_POINT_TT = ("shape is one of cross **|** circle **|** square **|** diamond **|** triangle"
                    "\nsize_px is a FIXED on-screen pixel size (unlike an 'outline' zone, which is real mm geometry that "
                     "scales with canvas_clip zoom) -- stays legible even with thousands of markers.")
_ZONE_MODE_TT = ("off | highlight | outline. 'highlight' draws the zone's own min-Y/max-Y envelope as a "
                  "separate filled polygon on top. 'outline' recolors the object's OWN silhouette edge over "
                  "that Z-range instead -- capped at the top, open at the bottom where it blends into the "
                  "rest of the object, like a spotlight beam converging onto the zone from above. Only the "
                  "sub-config matching the current mode is actually used.")
_ZONE_HIGHLIGHT_TT = ("Fill/stroke for the zone's min-Y/max-Y envelope polygon -- same field shape as "
                      "colors.no_go. Only used when this zone's mode is set to 'highlight'.")
_ZONE_OUTLINE_STROKE_TT = ("Color the object's own silhouette edge is recolored to over this zone's Z-range. "
                           "Only used when this zone's mode is set to 'outline'.")
_ZONE_OUTLINE_WIDTH_TT = ("Stroke width (px) for the recolored edge. Defaults to 1.5, matching every other "
                          "shape's own fixed stroke width, so the default reads as recoloring that stretch "
                          "rather than a visibly thicker overlay -- raise it for a bolder 'spotlight'.")

SECTIONS = [
    ("Dock Boundary", [
        dict(kind="number", label="Safe Y (mm)", path=("safe_y",), min=0, max=2000, step=1),
        dict(kind="point_table", label="Boundary points (Y -> max safe Z):", path=("boundary",),
             columns=[("y", "Y (mm)"), ("z", "Z (mm)")],
             min_rows=2, min_rows_message="Boundary needs at least 2 points.",
             parse_error_message="row(s) have non-numeric Y/Z and are being ignored.",
             validate_rows=validate_boundary_points, add_label="+ Add point"),
    ], ("boundary",)),
    ("Status Display", [
        dict(kind="choice", label="No collision ->", path=("svg", "display", "no_collision"),
             options=["off", "printer", "pc", "both"], tooltip=_DISPLAY_TT),
        dict(kind="choice", label="Near miss ->", path=("svg", "display", "near_miss"),
             options=["off", "printer", "pc", "both"], tooltip=_DISPLAY_TT),
        dict(kind="choice", label="Collision ->", path=("svg", "display", "collision"),
             options=["off", "printer", "pc", "both"], tooltip=_DISPLAY_TT),
        dict(kind="bool", label="Standalone browser popup", path=("svg", "show_on_pc")),
        dict(kind="bool", label="Auto-open popup in browser", path=("svg", "pc_svg_open"),
             show_if=[(("svg", "show_on_pc"), True)]),
        dict(kind="number", label="Near-miss margin (mm)", path=("svg", "near_miss_margin"),
             min=0, max=100, step=0.5),
    ], "Where each outcome shows up (console, PC popup, or both) -- see each row's own tooltip for exactly "
       "what 'printer'/'pc'/'both' mean. The two checkboxes below control the STANDALONE browser popup "
       "specifically, on top of that: both must be on for one to appear at all."),
    ("Canvas", [
        dict(kind="choice", label="Canvas clip", path=("svg", "canvas_clip"),
             options=["tight", "normal", "full"]),
        dict(kind="number", label="Z cap (mm)", path=("svg", "z_cap"), min=0, max=2000, step=1),
        dict(kind="number", label="Box padding (mm)", path=("svg", "box_pad"), min=0, max=50, step=0.5),
        dict(kind="number", label="Max on-screen size (px)", path=("svg", "max_size"),
             min=80, max=1000, step=10, is_int=True),
        dict(kind="text", label="Title", path=("svg", "title"), width=28),
    ]),
    ("Travel Moves", [
        dict(kind="bool", label="Show travel moves", path=("svg", "travel_moves", "show"),
             tooltip="Off by default -- purely an optional debug overlay, not required for clean silhouettes "
                     "(that fix lives elsewhere and needs no config)."),
        dict(kind="color", label="Fill", path=("svg", "travel_moves", "fill"),
             default="rgba(200,200,200,0.20)", tooltip="Only visible when 'Show travel moves' is on.",
             show_if=[(("svg", "travel_moves", "show"), True)]),
        dict(kind="color", label="Stroke", path=("svg", "travel_moves", "stroke"),
             default="rgba(200,200,200,0.55)", tooltip="Only visible when 'Show travel moves' is on.",
             show_if=[(("svg", "travel_moves", "show"), True)]),
        dict(kind="choice", label="Fill style", path=("svg", "travel_moves", "fill_style"), options=FILL_STYLES,
             tooltip="Only visible when 'Show travel moves' is on.",
             show_if=[(("svg", "travel_moves", "show"), True)]),
    ]),
    ("Printable Area (canvas_clip = full only)", [
        dict(kind="number", label="Fallback Y depth (mm)", path=("svg", "printable_area", "fallback_y_depth"),
             min=0, max=2000, step=1,
             tooltip="Used ONLY if the bed size can't be read from the g-code comments OrcaSlicer appends. "
                     "Defaults match a 350x344 bed with a 325mm max height. Only affects how far the canvas "
                     "is sized when 'full' clip is selected -- no outline is drawn for it.",
             show_if=[(("svg", "canvas_clip"), ["full"])]),
        dict(kind="number", label="Fallback Z height (mm)", path=("svg", "printable_area", "fallback_z_height"),
             min=0, max=2000, step=1,
             tooltip="Used ONLY if the bed size can't be read from the g-code comments OrcaSlicer appends. "
                     "Defaults match a 350x344 bed with a 325mm max height. Only affects how far the canvas "
                     "is sized when 'full' clip is selected -- no outline is drawn for it.",
             show_if=[(("svg", "canvas_clip"), ["full"])]),
    ]),
    ("Toolchange Collision Guard", [
        dict(kind="bool", label="Enabled", path=("svg", "toolchange_collision_guard", "enabled")),
        dict(kind="number", label="Toolchange hop (mm)", path=("svg", "toolchange_collision_guard", "toolchange_hop_mm"),
             min=0, max=100, step=0.5, show_if=[(("svg", "toolchange_collision_guard", "enabled"), True)]),
        dict(kind="number", label="Restore hop (mm)", path=("svg", "toolchange_collision_guard", "restore_hop_mm"),
             min=0, max=100, step=0.5, show_if=[(("svg", "toolchange_collision_guard", "enabled"), True)]),
        dict(kind="color", label="Fail color (hop check failure)", path=("svg", "toolchange_markers", "fail_color"),
             default="rgba(255,0,0,0.95)",
             tooltip="Shown regardless of 'Show' in Toolchange Markers below -- a point failing Toolchange "
                     "Collision Guard is ALWAYS drawn in this color, since that's a hard-fail export block, "
                     "not a display preference. Only actually irrelevant if hop checking itself is off.",
             show_if=[(("svg", "toolchange_collision_guard", "enabled"), True)]),
    ],("svg", "toolchange_collision_guard")),
    ("Toolchange Markers", [
        dict(kind="choice", label="Show", path=("svg", "toolchange_markers", "show"),
             options=["off", "no_go_only", "all"]),
        dict(kind="choice", label="Toolchange shape", path=("svg", "toolchange_markers", "toolchange", "shape"),
             options=SHAPES, tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
        dict(kind="color", label="Toolchange color", path=("svg", "toolchange_markers", "toolchange", "color"),
             default="rgba(255,140,0,0.85)", tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
        dict(kind="number", label="Toolchange size (px)", path=("svg", "toolchange_markers", "toolchange", "size_px"),
             min=2, max=30, step=1, is_int=True, tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
        dict(kind="choice", label="Restore shape", path=("svg", "toolchange_markers", "restore", "shape"),
             options=SHAPES, tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
        dict(kind="color", label="Restore color", path=("svg", "toolchange_markers", "restore", "color"),
             default="rgba(0,220,120,0.85)", tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
        dict(kind="number", label="Restore size (px)", path=("svg", "toolchange_markers", "restore", "size_px"),
             min=2, max=30, step=1, is_int=True, tooltip=_MARKER_POINT_TT,
             show_if=[(("svg", "toolchange_markers", "show"), ["no_go_only", "all"])]),
    ]),
    ("Clearance Zones -- Shared", [
        dict(kind="number", label="Gap-merge (mm)", path=("svg", "clearance_zones", "gap_merge_mm"),
             min=0, max=50, step=0.5),
    ]),
    ("Clearance Zones -- Collision", [
        dict(kind="choice", label="Mode", path=("svg", "clearance_zones", "collision", "mode"),
             options=["off", "highlight", "outline"], tooltip=_ZONE_MODE_TT),
        dict(kind="color", label="Highlight fill", path=("svg", "clearance_zones", "collision", "highlight", "fill"),
             default="rgba(255,230,0,0.35)", tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "collision", "mode"), ["highlight"])]),
        dict(kind="color", label="Highlight stroke", path=("svg", "clearance_zones", "collision", "highlight", "stroke"),
             default="rgba(255,230,0,0.95)", tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "collision", "mode"), ["highlight"])]),
        dict(kind="choice", label="Highlight fill style", path=("svg", "clearance_zones", "collision", "highlight", "fill_style"),
             options=FILL_STYLES, tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "collision", "mode"), ["highlight"])]),
        dict(kind="color", label="Outline stroke", path=("svg", "clearance_zones", "collision", "outline", "stroke"),
             default="rgba(255,230,0,0.95)", tooltip=_ZONE_OUTLINE_STROKE_TT,
             show_if=[(("svg", "clearance_zones", "collision", "mode"), ["outline"])]),
        dict(kind="number", label="Outline width (px)", path=("svg", "clearance_zones", "collision", "outline", "width_px"),
             min=1, max=5, step=0.5, tooltip=_ZONE_OUTLINE_WIDTH_TT,
             show_if=[(("svg", "clearance_zones", "collision", "mode"), ["outline"])]),
    ], ("svg", "clearance_zones", "collision_and_near_miss")),  # actual JSON comment key, see near miss below
    ("Clearance Zones -- Near Miss", [
        dict(kind="choice", label="Mode", path=("svg", "clearance_zones", "near_miss", "mode"),
             options=["off", "highlight", "outline"], tooltip=_ZONE_MODE_TT),
        dict(kind="color", label="Highlight fill", path=("svg", "clearance_zones", "near_miss", "highlight", "fill"),
             default="rgba(255,140,0,0.30)", tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "near_miss", "mode"), ["highlight"])]),
        dict(kind="color", label="Highlight stroke", path=("svg", "clearance_zones", "near_miss", "highlight", "stroke"),
             default="rgba(255,140,0,0.85)", tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "near_miss", "mode"), ["highlight"])]),
        dict(kind="choice", label="Highlight fill style", path=("svg", "clearance_zones", "near_miss", "highlight", "fill_style"),
             options=FILL_STYLES, tooltip=_ZONE_HIGHLIGHT_TT,
             show_if=[(("svg", "clearance_zones", "near_miss", "mode"), ["highlight"])]),
        dict(kind="color", label="Outline stroke", path=("svg", "clearance_zones", "near_miss", "outline", "stroke"),
             default="rgba(255,140,0,0.85)", tooltip=_ZONE_OUTLINE_STROKE_TT,
             show_if=[(("svg", "clearance_zones", "near_miss", "mode"), ["outline"])]),
        dict(kind="number", label="Outline width (px)", path=("svg", "clearance_zones", "near_miss", "outline", "width_px"),
             min=1, max=5, step=0.5, tooltip=_ZONE_OUTLINE_WIDTH_TT,
             show_if=[(("svg", "clearance_zones", "near_miss", "mode"), ["outline"])]),
    ], ("svg", "clearance_zones", "collision_and_near_miss")),  # dock_collision_guard.json documents Collision +
       # Near Miss together under one "_collision_and_near_miss" comment, not "_near_miss" individually
    ("Colors -- No-Go Zone", [
        dict(kind="color", label="Fill", path=("svg", "colors", "no_go", "fill"), default="rgba(255,60,60,0.30)",
             tooltip="Fill/stroke/fill_style for the no-go zone shape."),
        dict(kind="color", label="Stroke", path=("svg", "colors", "no_go", "stroke"), default="rgba(255,60,60,0.9)",
             tooltip="Fill/stroke/fill_style for the no-go zone shape."),
        dict(kind="choice", label="Fill style", path=("svg", "colors", "no_go", "fill_style"), options=FILL_STYLES,
             tooltip="'solid' (flat, opaque) | 'dithered' (translucent, textured)."),
    ]),
    ("Colors -- Object Silhouettes", [
        dict(kind="hex_color", label="Color", path=("svg", "colors", "silhouette", "color"), default="#50aaff",
             tooltip="Starting color for every object silhouette. With one object (or object labeling not "
                     "enabled in OrcaSlicer), this is exactly the color drawn. With 2+ objects, this is object "
                     "#1's color and every other object fans out from its hue (golden-ratio spaced, steered "
                     "away from no-go/support/marker/zone colors) -- one object and several objects always go "
                     "through the same coloring path, so there's nothing to look inconsistent between them."),
        dict(kind="number", label="Saturation", path=("svg", "colors", "silhouette_palette", "saturation"),
             min=0, max=1, step=0.05, tooltip="Saturation of every object silhouette color. 0.0-1.0."),
        dict(kind="number", label="Value", path=("svg", "colors", "silhouette_palette", "value"),
             min=0, max=1, step=0.05, tooltip="Brightness of every object silhouette color. 0.0-1.0."),
        dict(kind="number", label="Fill alpha", path=("svg", "colors", "silhouette_palette", "fill_alpha"),
             min=0, max=1, step=0.05, tooltip="Fill opacity of every object silhouette. 0.0-1.0."),
        dict(kind="number", label="Stroke alpha", path=("svg", "colors", "silhouette_palette", "stroke_alpha"),
             min=0, max=1, step=0.05, tooltip="Outline opacity of every object silhouette. 0.0-1.0."),
        dict(kind="choice", label="Fill style", path=("svg", "colors", "silhouette", "fill_style"), options=FILL_STYLES,
             tooltip="'solid' (flat, opaque) | 'dithered' (translucent, textured). Applies to every object "
                     "silhouette regardless of count."),
    ]),
    ("Colors -- Support", [
        dict(kind="color", label="Fill", path=("svg", "colors", "support", "fill"), default="rgba(80,200,80,0.30)",
             tooltip="ONE fixed color shared by every object's supports (unlike the per-object auto palette "
                     "above). Requires OrcaSlicer's 'Detailed G-code comments' AND 'Label objects' settings."),
        dict(kind="color", label="Stroke", path=("svg", "colors", "support", "stroke"), default="rgba(80,200,80,0.9)",
             tooltip="ONE fixed color shared by every object's supports."),
        dict(kind="choice", label="Fill style", path=("svg", "colors", "support", "fill_style"), options=FILL_STYLES,
             tooltip="ONE fixed color shared by every object's supports."),
    ]),
]


# ---------------------------------------------------------------------------
# Preview scenario generation -- see module docstring for the "rich"
# preview mechanism. scenario/multi_object/simulate_hop_fail arrive as
# controls["scenario"] / controls["multi_object"] /
# controls["simulate_hop_fail"], read generically off PREVIEW_CONTROLS
# above by config_editor.pyw's engine, which has no idea what these
# particular controls mean.
# ---------------------------------------------------------------------------

def _build_preview_data(cfg, scenario, multi_object, simulate_hop_fail):
    """
    Builds a mostly-rectangular column -- constant Y footprint at every
    layer, like an actual (simplified) printed part -- placed with its
    near edge right at the boundary's tightest point. The scenario knob
    only changes ONE thing: how tall that column is printed. Since the
    Y footprint is constant almost everywhere, every layer shares the
    exact same dock clearance limit, so the column is safe up to that
    height and then cleanly "too tall" above it -- collision/near-miss
    zones show up as a clean band across the top of the object, which
    is what a real print reaching too close to the dock would actually
    look like.

    The one deliberate exception is a concave notch cut into the near
    (dock-facing) face partway up -- like a bridge/overhang cavity in a
    real part -- with the demo support fitted inside it. A support that
    shares the object's own footprint is invisible in the preview (the
    object silhouette is drawn on top of it, by design -- see
    build_svg_payload's z-order comment), so there'd be nothing to look
    at when adjusting colors.support. Sitting the support inside a
    notch instead means the object genuinely has no geometry over that
    patch, so the support reads clearly against the background at
    exactly the spot a real one would.
    """
    pts, safe_y = _checker.build_boundary(cfg)
    margin = float(get_in(cfg, ("svg", "near_miss_margin"), 5.0))

    # Deepest boundary point -- always in range regardless of how the rest
    # of the boundary is edited, so this stays representative mid-edit.
    y_near = pts[0][0]
    obj_width = 50.0
    y_far = y_near + obj_width

    limit = _checker.max_z_allowed(y_near, pts, safe_y)
    if limit == float("inf"):
        limit = safe_y  # degenerate boundary edge case, just pick something

    if scenario == "no_collision":
        height = max(20.0, limit - margin * 2)
    elif scenario == "near_miss":
        height = max(20.0, limit - margin * 0.3)
    else:  # "collision"
        height = limit + margin * 1.5

    def make_column(y0, y1, z_top, z_bottom=0.0, steps=60, extra_zs=()):
        # Union of even steps with explicit checkpoints (the near_miss/
        # collision threshold Zs) so the classification boundary is
        # always captured by an actual sample, never straddled past by
        # step granularity.
        zs = {z_bottom + (z_top - z_bottom) * i / steps for i in range(steps + 1)}
        zs.update(z for z in extra_zs if z_bottom <= z <= z_top)
        points = []
        for z in sorted(zs):
            points.append((y0, z))
            points.append((y1, z))
        return points

    checkpoints = (max(0.0, limit - margin), limit)

    # A tiny Z gap between adjacent bands so each keeps its own distinct
    # footprint instead of the two blending into one averaged step (see
    # group_min_max_y_by_z's Z rounding in the .py) -- small enough to
    # be visually a sharp corner, not a slope.
    NOTCH_EPS = 0.02
    notch_z0 = height * 0.28
    notch_z1 = height * 0.52
    notch_ok = (notch_z1 - notch_z0) >= 4.0 and notch_z0 >= 2.0

    if notch_ok:
        # How far the near face steps back during the notch band --
        # capped so a real wall is always left standing behind it.
        notch_inset = min(18.0, obj_width * 0.45)
        y_notch_far = y_near + notch_inset
        object_pts = (
            make_column(y_near, y_far, notch_z0 - NOTCH_EPS, steps=6)  # base, full footprint
            + make_column(y_notch_far, y_far, notch_z1 - NOTCH_EPS,    # notch band, cut back on the near side
                          z_bottom=notch_z0 + NOTCH_EPS, steps=6)
            + make_column(y_near, y_far, height,                       # overhang, full footprint resumes
                          z_bottom=notch_z1 + NOTCH_EPS, steps=54, extra_zs=checkpoints)
        )
    else:
        # Column too short to fit a legible notch (a small no_collision
        # height on a shallow boundary) -- fall back to the plain shape.
        object_pts = make_column(y_near, y_far, height, extra_zs=checkpoints)

    object_points = {"Test Object": object_pts}

    if notch_ok:
        # Fitted just inside the notch's own Y extent (a small margin on
        # each side) so its edges read as distinct from the object's
        # cut. Starts from the bed (Z=0), the way a real support does,
        # and rises to just under the notch's own top so it reads as
        # propping up the resumed overhang above -- not floating
        # mid-air the way a support confined to the notch band alone
        # would.
        gap = 1.5
        sy0, sy1 = y_near + gap, y_notch_far - gap
        sz1 = notch_z1 - gap
        support_steps = max(10, int(sz1 / 3))
        support_pts = make_column(sy0, sy1, sz1, z_bottom=0.0, steps=support_steps) if (sy1 > sy0 and sz1 > 0) else []
    else:
        # Original fallback: a short support block at the base, same
        # footprint as the object -- only meaningful while there's
        # enough height under the danger zone to put one. Will render
        # hidden behind the object (see build_svg_payload's z-order
        # comment), same as a real support sharing its object's exact
        # footprint would.
        support_h = min(15.0, max(0.0, limit - margin - 3))
        support_pts = make_column(y_near, y_far, support_h, steps=10) if support_h > 2 else []

    support_points = {"Test Object": support_pts}

    # Second object's height is fixed across all three scenarios -- always
    # equal to whatever Test Object's own height would be in the
    # near_miss scenario specifically (not whatever the currently
    # selected scenario is). That keeps Second Object a stable reference
    # point while Test Object's height swings around it: below Second
    # Object in no_collision, right at it in near_miss, above it in
    # collision -- so the travel move's height (which tracks the
    # *shorter* of the two objects, below) visibly shifts as the
    # scenario knob changes.
    near_miss_height = max(20.0, limit - margin * 0.3)
    second_obj_height = near_miss_height
    y2_near = safe_y + 60  # always defined so travel-move math below can use it

    if multi_object:
        # A second column safely beyond safe_y (never at any dock risk,
        # regardless of scenario) purely to demonstrate the auto-hue
        # palette across multiple objects.
        object_points["Second Object"] = make_column(y2_near, y2_near + obj_width, second_obj_height, steps=20)

    positions = object_points["Test Object"]  # same trace feeds the pass/fail classifier

    # Travel move sits *behind* the object(s), never straying outside
    # their combined Y footprint: min Y tracks Test Object's near edge,
    # max Y tracks the far edge of whichever object reaches furthest out
    # (Test Object alone, or Second Object once it's added). Z runs from
    # the bed up to the shorter of the objects involved, since a real
    # toolhead can only travel at a layer height both objects have
    # actually reached -- this is what an accumulated real travel
    # silhouette (thousands of layers, each contributing travel at that
    # Z) would fold down into, so a single segment spanning the full
    # bed-to-shortest-object range gives build_object_silhouette_polygon
    # two distinct Z levels (0 and the top) to fold into a real filled
    # rectangle, instead of the near-zero-thickness sliver a single flat
    # Z segment produces.
    travel_max_y = (y2_near + obj_width) if multi_object else y_far
    travel_top_z = min(height, second_obj_height) if multi_object else height
    travel_segments = [
        (y_near, 0.0, travel_max_y, travel_top_z),
    ]

    # Toolchange markers sit inset from the object's own edges -- never
    # outside it -- at the object's current height.
    inset = min(8.0, obj_width * 0.25)
    toolchange_points = [{
        "toolchange": (y_near + inset, height),
        "restore": (y_far - inset, height),
    }]
    tc_failing_keys = {(0, "toolchange")} if simulate_hop_fail else set()

    return dict(
        pts=pts, safe_y=safe_y, positions=positions,
        object_points=object_points, support_points=support_points,
        travel_segments=travel_segments, toolchange_points=toolchange_points,
        tc_failing_keys=tc_failing_keys,
    )


def build_preview_payload(cfg, controls):
    """
    The generic half of the "rich preview" contract -- see module
    docstring. config_editor.pyw calls this on every field/control
    change and feeds the SVG_PAYLOAD-shaped dict straight into
    orcastrator.py's own generic canvas renderer, with no idea what's
    inside it.
    """
    scenario = controls.get("scenario", "collision")
    multi_object = bool(controls.get("multi_object", False))
    simulate_hop_fail = bool(controls.get("simulate_hop_fail", False))
    d = _build_preview_data(cfg, scenario, multi_object, simulate_hop_fail)
    return _checker.build_svg_payload(
        cfg, d["pts"], d["safe_y"], d["positions"],
        d["object_points"], d["support_points"], d["travel_segments"],
        targets=["pc"], bed_extents=None,
        toolchange_points=d["toolchange_points"], tc_failing_keys=d["tc_failing_keys"],
    )
