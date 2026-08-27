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
Dock collision guard — OrcaSlicer post-processing script
=========================================================================
Runs at slicer EXPORT time, on your desktop PC — not on the printer, and
not inside a Klipper macro. This is the slow, heavy version of the check
and it is fine for it to take a while here, because nothing on the
printer is waiting on it.

The no-go check and the silhouette are both layer-based: every point in
the file's Y/Z trace is bucketed by its exact Z (including z-hop-induced
values, not just real print layers), keeping the closest (minimum-Y)
approach per Z for the collision check and both extremes (min/max-Y) per
Z per object for the silhouette. No numpy/scipy, no poly_tools.py, no
rasterization -- see find_closest_approach()/build_object_silhouette_polygon()
in this file for the actual technique.

The result is embedded as a small comment block near the top of the
g-code file:

    ; STEALTHCHANGER_CHECK_START
    ; STATUS:OK
    ; SVG_PAYLOAD:{...compact json...}
    ; STEALTHCHANGER_CHECK_END

The companion Klipper macro (STEALTHCHANGER_RENDER, in
stealthchanger_render.cfg) does a single bounded read of the first ~64KB
of the file at print start, pulls this block out with a couple of
string.find() calls, and renders it via _SVG_TOOLS. No subprocess, no
regex sweep of the whole file, no numpy/scipy on the printer's host —
all of that already happened here.

On a detected collision:
  - STATUS is set to VIOLATION in the embedded block
  - a RESPOND + CANCEL_PRINT_BASE block is ALSO injected at the very top
    of the file, as defense-in-depth in case this file is ever printed
    without going through STEALTHCHANGER_RENDER's gate
  - this script exits with code 1, which OrcaSlicer treats as a
    post-processing failure and will surface as an error at export time —
    you should never even get as far as having a file to upload
"""

import colorsys
import json
import os
import pathlib
import re
import sys
import tempfile
import webbrowser

from helpers.debug_dump import write_debug_dump as _write_debug_dump
from helpers.notice import display_flag as _notice_display_flag


def friendly_filename(p: pathlib.Path) -> str:
    """
    OrcaSlicer invokes post-processing scripts against a temp file with a
    meaningless name (e.g. '.OrcaSlicer.upload.fd82-4837-be65-c56d'), not
    the file's real/intended output name -- that only gets applied
    afterward. It does, however, set the SLIC3R_PP_OUTPUT_NAME environment
    variable to the real name/path (this is a PrusaSlicer-lineage
    convention OrcaSlicer inherited), so prefer that for anything shown
    to a person -- alerts, embedded titles, etc. Falls back to the raw
    path's basename if the env var isn't set for some reason.
    """
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


def render_svg_html(payload: dict) -> str:
    """
    Turns the same {title, canvas, shapes} payload that gets embedded as
    SVG_PAYLOAD for the printer into a real, standalone SVG -- rendered
    right here, since this whole script already runs on the PC. Returns a
    small self-contained HTML document (dark background to match the
    Mainsail console look) rather than a bare .svg file, so the title
    shows up as an actual heading instead of being lost.

    Deliberately independent from Klipper's _SVG_TOOLS -- there's no way
    to share code across that boundary, so this is a from-scratch
    implementation of the same canvas/shape schema. Keep the two in sync
    by hand if the schema ever changes.
    """
    canvas = payload.get("canvas", {}) or {}
    x_max = float(canvas.get("x_max", 100.0)) or 1.0
    y_max = float(canvas.get("y_max", 100.0)) or 1.0
    pad = float(canvas.get("pad", 5.0))
    max_size = float(canvas.get("max_size", 260))

    scale = (max_size - 2 * pad) / max(x_max, y_max, 1e-6)
    width = x_max * scale + 2 * pad
    height = y_max * scale + 2 * pad

    def to_svg(x: float, y: float):
        # mm -> px, with the Z axis flipped (SVG y grows downward, we
        # want z=0 to sit at the bottom of the canvas like the printer
        # console does).
        return (pad + x * scale, height - pad - y * scale)

    defs = []
    body = []

    for i, shape in enumerate(payload.get("shapes", [])):
        kind = shape.get("type")

        if kind == "polygon":
            pts = shape.get("points", [])
            svg_pts = " ".join(f"{sx:.2f},{sy:.2f}" for sx, sy in (to_svg(x, y) for x, y in pts))
            stroke = shape.get("stroke", "black")
            fill = shape.get("fill", "none")
            # fill_style only distinguishes "solid" vs "dithered" in the Tk
            # preview (see orcastrator.py's _draw_payload) -- Tk has no real
            # alpha, so it needs an explicit flat-vs-textured choice. Real
            # SVG rendering has true alpha compositing regardless, so it
            # just uses the shape's own fill color here.
            fill_attr = fill

            body.append(f'<polygon points="{svg_pts}" fill="{fill_attr}" stroke="{stroke}" stroke-width="1.5"/>')

        elif kind == "path":
            # Open, unfilled line -- e.g. a single travel move's own
            # start->end segment. Deliberately just 2 points per shape
            # (see collect_motion_data's travel_segments) rather than one
            # shared polyline strung across unrelated moves.
            pts = shape.get("points", [])
            svg_pts = " ".join(f"{sx:.2f},{sy:.2f}" for sx, sy in (to_svg(x, y) for x, y in pts))
            color = shape.get("color", "rgba(200,200,200,0.4)")
            width = shape.get("width_px", 1)
            body.append(f'<polyline points="{svg_pts}" fill="none" stroke="{color}" stroke-width="{width}"/>')

        elif kind == "crosshair":
            cx, cy = to_svg(float(shape.get("x", 0)), float(shape.get("y", 0)))
            size = float(shape.get("size", 4.0)) * scale
            color = shape.get("color", "yellow")
            body.append(
                f'<g stroke="{color}" stroke-width="2">'
                f'<line x1="{cx - size:.2f}" y1="{cy:.2f}" x2="{cx + size:.2f}" y2="{cy:.2f}"/>'
                f'<line x1="{cx:.2f}" y1="{cy - size:.2f}" x2="{cx:.2f}" y2="{cy + size:.2f}"/>'
                f'</g>'
            )

        elif kind == "marker":
            # Toolchange/restore-position markers (see collect_toolchange_points).
            # Deliberately fixed PIXEL size, unlike crosshair's mm-based size
            # above -- these shouldn't shrink/grow as canvas_clip zooms in
            # or out, since with potentially thousands of them on one file,
            # a consistent on-screen size is what keeps them legible.
            cx, cy = to_svg(float(shape.get("x", 0)), float(shape.get("y", 0)))
            r = float(shape.get("size_px", 6))
            color = shape.get("color", "yellow")
            mshape = shape.get("shape", "cross")
            if mshape == "circle":
                body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{color}"/>')
            elif mshape == "square":
                body.append(
                    f'<rect x="{cx - r:.2f}" y="{cy - r:.2f}" width="{2 * r:.2f}" height="{2 * r:.2f}" fill="{color}"/>'
                )
            elif mshape == "diamond":
                pts_str = f"{cx:.2f},{cy - r:.2f} {cx + r:.2f},{cy:.2f} {cx:.2f},{cy + r:.2f} {cx - r:.2f},{cy:.2f}"
                body.append(f'<polygon points="{pts_str}" fill="{color}"/>')
            elif mshape == "triangle":
                pts_str = f"{cx:.2f},{cy - r:.2f} {cx + r:.2f},{cy + r:.2f} {cx - r:.2f},{cy + r:.2f}"
                body.append(f'<polygon points="{pts_str}" fill="{color}"/>')
            else:  # "cross"
                body.append(
                    f'<g stroke="{color}" stroke-width="2">'
                    f'<line x1="{cx - r:.2f}" y1="{cy:.2f}" x2="{cx + r:.2f}" y2="{cy:.2f}"/>'
                    f'<line x1="{cx:.2f}" y1="{cy - r:.2f}" x2="{cx:.2f}" y2="{cy + r:.2f}"/>'
                    f'</g>'
                )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}" height="{height:.2f}" '
        f'viewBox="0 0 {width:.2f} {height:.2f}">'
        f'<defs>{"".join(defs)}</defs>'
        f'<rect width="{width:.2f}" height="{height:.2f}" fill="#1a1a1a"/>'
        f'{"".join(body)}'
        f'</svg>'
    )

    title = payload.get("title", "StealthChanger dock check")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>body{background:#111;color:#eee;font-family:sans-serif;"
        "display:flex;flex-direction:column;align-items:center;padding:24px}"
        "h1{font-size:18px;font-weight:600}</style></head><body>"
        f"<h1>{title}</h1>{svg}</body></html>"
    )


# write_debug_dump() lives in helpers/debug_dump.py, generalized so any
# processor can opt in the same way -- see that module's docstring
# for the full path-resolution rules (central configs/orcastrator.json
# debug.dir -> next to this script, same fallback as always). Imported
# below as _write_debug_dump; every call
# site here passes "dock_collision_guard" as the processor name, which
# is what the dump file gets named after (dock_collision_guard_debug.json)
# -- see that module's docstring for why a fixed name isn't safe once a
# shared central directory exists.


def show_svg_on_pc(payload: dict, svg_cfg: dict, friendly_name: str) -> None:
    """
    Writes the rendered SVG/HTML to disk and opens it in the default
    browser. This is the standalone/no-OrcaStrator fallback -- when run
    through orcastrator.py (the normal setup), its own
    progress window already renders "pc"-targeted payloads directly, so
    this would just be a redundant second popup.

    Callers only reach this when "pc" is already in the resolved targets
    for the current status (see svg.display in the config); show_on_pc
    is a second, independent gate on top of that for turning the
    standalone browser popup off even when "pc" is otherwise targeted
    (e.g. because you're relying on OrcaStrator window instead).

    Best-effort: a display mechanism failing must never break the actual
    check.
    """
    if not svg_cfg.get("show_on_pc", True):
        return
    try:
        html = render_svg_html(payload)
        out_path = svg_cfg.get("pc_svg_path", "")
        if out_path:
            path = pathlib.Path(out_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = pathlib.Path(tempfile.gettempdir()) / "stealthchanger_dock_check.html"
        path.write_text(html, encoding="utf-8")

        if svg_cfg.get("pc_svg_open", True):
            webbrowser.open(path.resolve().as_uri())
    except Exception as exc:
        print(f"[dock_collision] could not display SVG on PC: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# G-code parsing (unchanged from the printer-side version)
# ---------------------------------------------------------------------------

PAT_MOVE = re.compile(r"^\s*G[01]\b", re.IGNORECASE)
PAT_AXES = re.compile(r"\b([XYZ])([-+]?\d*\.?\d+)", re.IGNORECASE)
PAT_E = re.compile(r"\bE([-+]?\d*\.?\d+)", re.IGNORECASE)
# Matches both a bare tool-change ("T1") and one already annotated by
# restore_pos_fix.py ("T1 X=10.5 Y=20.5 Z=30.5") -- group(2) is the
# optional suffix, empty/None for a bare line.
PAT_TOOLCHANGE = re.compile(r"^\s*T(\d+)(?:\s+(.*\S))?\s*$", re.IGNORECASE)
# restore_pos_fix.py writes its suffix with "=" (Coord.to_gcode_suffix),
# unlike ordinary G-code axis params -- needs its own pattern.
PAT_TOOLCHANGE_SUFFIX = re.compile(r"\b([XYZ])=([-+]?\d*\.?\d+)", re.IGNORECASE)
# OrcaSlicer's structured object-labeling commands (requires "Label
# objects" enabled in the slicer). Deliberately matching these rather
# than the free-text "; printing object ..." comments that accompany
# them -- same information, but the name is a proper parameter here
# instead of embedded in a sentence, so it's more robust to reword.
PAT_EXCLUDE_START = re.compile(r"^\s*EXCLUDE_OBJECT_START\s+NAME=(\S+)", re.IGNORECASE)
PAT_EXCLUDE_END = re.compile(r"^\s*EXCLUDE_OBJECT_END\b", re.IGNORECASE)


def collect_motion_data(lines: list) -> dict:
    """
    Single pass over the file gathering everything downstream needs.
    Returns:

      "positions": the full, UNFILTERED chronological (y, z) trace --
        every G0/G1 move, print or travel, in or out of any
        object/support block. This is what find_closest_approach() (the
        actual safety check) and classify_z_layers() (clearance-zone
        highlighting) both use -- one entry per matching line, yielded
        only once both Y and Z are known. Never filtered by
        object/support/travel status; that would silently narrow what
        gets safety-checked.

      "object_points": {object_name: [(y,z), ...]} -- PRINT-only
        (extruding) moves attributed to each object's own BODY (i.e. NOT
        Support-typed). Falls back to a single {"": [...]} bucket if
        the file has no EXCLUDE_OBJECT markers at all.

      "support_points": {object_name: [(y,z), ...]} -- PRINT-only moves
        that were Support-typed while that object was active. Keyed by
        the SAME object name as object_points, so a support shape is
        always linked to its parent object. Object labeling ("Label
        objects") and feature-type comments ("Detailed G-code comments")
        are two independent OrcaSlicer settings -- both are needed for
        this to populate.

      "travel_segments": [(y0, z0, y1, z1), ...] -- one entry per travel
        move that actually changed position, each move's own start->end
        pair. Deliberately kept as individual segments rather than one
        flat point list/polyline -- stringing unrelated travels together
        in chronological order would reproduce the exact same
        bleeding-together artifact objects had, just for travel instead.

      "position_owners": [(y, z, object_name_or_None, is_support), ...] --
        parallel to "positions" (same points, same order), tagging each
        with whichever object was active at that moment and whether it
        was support material -- INCLUDING travel moves, unlike
        object_points/support_points above, since a collision can easily
        be a travel/Z-hop move rather than an extruding one, and "no
        object was attributable" is still useful information. This is
        PURELY for attributing a violation to an object name in the
        abort message (see group_min_y_by_z_with_owner) -- it must never
        be used as an input to the actual safety check, only to explain
        one after the fact.

    Support detection is via the standard slicer ";TYPE:Support" comment.
    Like EXCLUDE_OBJECT_START/END, it's modal: it applies to every move
    until the next ";TYPE:" comment changes it. The two are orthogonal and
    independently tracked -- a single object's block can switch between
    body and support moves many times per layer.

    Print-vs-travel classification reuses this file's existing
    has_E-and-has_XY convention (see _find_restore_anchor) for
    consistency: a move counts as "print" if it has both an E parameter
    and an XY(Z) change. A combined travel+retraction move (E negative,
    XY also present) will be misclassified as "print" by this same
    simplification -- a known, accepted trade-off already made elsewhere
    in this file, not something newly introduced here.
    """
    y = z = None
    active_object = None
    active_is_support = False
    saw_any_object_marker = False

    positions = []
    position_owners = []
    object_points = {}
    support_points = {}
    all_print_points = []  # fallback bucket if no EXCLUDE_OBJECT markers exist at all
    travel_segments = []

    for line in lines:
        if PAT_MOVE.match(line):
            prev_y, prev_z = y, z
            has_e = bool(PAT_E.search(line))
            has_xyz_line = bool(PAT_AXES.search(line))
            for axis, val in PAT_AXES.findall(line):
                v = float(val)
                a = axis.upper()
                if a == "Y":
                    y = v
                elif a == "Z":
                    z = v

            if y is not None and z is not None:
                positions.append((y, z))  # unfiltered -- see docstring
                position_owners.append((y, z, active_object, active_is_support))

                if has_xyz_line:
                    if has_e:
                        all_print_points.append((y, z))
                        if active_object is not None:
                            bucket = support_points if active_is_support else object_points
                            bucket.setdefault(active_object, []).append((y, z))
                    elif prev_y is not None and prev_z is not None:
                        travel_segments.append((prev_y, prev_z, y, z))
            continue

        ls = line.strip()

        if ls.startswith(";TYPE:"):
            active_is_support = (ls[len(";TYPE:"):].strip() == "Support")
            continue

        m = PAT_EXCLUDE_START.match(ls)
        if m:
            active_object = m.group(1)
            saw_any_object_marker = True
            continue
        if PAT_EXCLUDE_END.match(ls):
            active_object = None
            saw_any_object_marker = True
            continue

    if not saw_any_object_marker:
        object_points = {"": all_print_points} if all_print_points else {}
        support_points = {}

    return {
        "positions": positions,
        "position_owners": position_owners,
        "object_points": object_points,
        "support_points": support_points,
        "travel_segments": travel_segments,
    }


def parse_bed_extents(lines: list):
    """
    OrcaSlicer appends a full config block as comments at the very end of
    every exported g-code file, including the bed shape and max print
    height -- e.g.:

        ; printable_area = 0x0,350x0,350x344,0x344
        ; printable_height = 325

    printable_area is a comma-separated list of "XxY" corner points; what
    canvas_clip="full" actually needs is just the Y-depth (this script's
    canvas is a Y/Z plane, not the bed's X/Y plane) and printable_height
    directly gives the Z. Returns (y_depth, z_height) in mm, or None if
    either key is missing/unparseable -- callers fall back to a
    configured constant in that case.

    The config block is always the last few hundred lines regardless of
    file size, so this only looks at the tail rather than scanning a
    file that could be hundreds of MB of toolpath.
    """
    y_depth = None
    z_height = None
    for line in lines[-2000:]:
        s = line.strip()
        if s.startswith("; printable_area"):
            try:
                raw = s.split("=", 1)[1].strip()
                ys = [float(tok.strip().split("x", 1)[1]) for tok in raw.split(",") if "x" in tok]
                if ys:
                    y_depth = max(ys) - min(ys)
            except Exception:
                pass
        elif s.startswith("; printable_height"):
            try:
                z_height = float(s.split("=", 1)[1].strip())
            except Exception:
                pass
    if y_depth is not None and z_height is not None:
        return (y_depth, z_height)
    return None


def _find_restore_anchor(lines: list, start_index: int, pre_x, pre_y, pre_z):
    """
    Deliberate, faithful port of restore_pos_fix.py's find_anchor(): scans
    forward from a BARE (unannotated) T{n} line for the first subsequent
    extruding move with XY -- that's the restore destination. Pure
    retract/unretract-only lines (G1 E±n with no XY) are skipped rather
    than treated as the destination -- that distinction was the original
    bug fix in restore_pos_fix.py, so it matters to reproduce it exactly
    here rather than getting a subtly different answer from a
    reimplementation.

    NON-OBVIOUS BUT INTENTIONAL: when the destination line itself is
    found, its own X/Y/Z is NOT applied to the running position -- only
    the travel-only moves seen along the way are (see find_anchor: it
    calls modal.update_from_move() for travel moves, but break()s on the
    destination line before ever calling it there). So the returned
    point is "wherever the last travel move landed", which is normally
    at or extremely close to the destination anyway (slicers travel
    straight to the resume point, then prime/extrude a hair further).
    Reproducing this exactly -- rather than "improving" it -- is what
    keeps this calculation consistent with what restore_pos_fix.py
    itself would annotate the line with if it ran on this file.

    Returns (x, y, z) or None if no destination is found before EOF or
    the next T-line -- matches restore_pos_fix.py leaving a bare line
    bare when it can't find an anchor either.
    """
    x, y, z = pre_x, pre_y, pre_z
    for idx in range(start_index, len(lines)):
        ln = lines[idx]
        if ln.lstrip().startswith(";"):
            continue
        if PAT_TOOLCHANGE.match(ln):
            break
        ls = ln.lstrip()
        if ls.startswith("G0") or ls.startswith("G1"):
            has_e = bool(PAT_E.search(ln))
            has_xyz = bool(PAT_AXES.search(ln))
            if has_e:
                if has_xyz:
                    break  # destination found -- deliberately NOT applied, see docstring
                else:
                    continue  # pure retract/unretract -- keep scanning
            if has_xyz:
                for axis, val in PAT_AXES.findall(ln):
                    v = float(val)
                    a = axis.upper()
                    if a == "X":
                        x = v
                    elif a == "Y":
                        y = v
                    elif a == "Z":
                        z = v
        # non-move, non-comment lines (M-codes, macros, blank): keep going
    if x is None or y is None:
        return None
    return (x, y, z)


def collect_toolchange_points(lines: list) -> list:
    """
    Walks the file once, tracking modal Y/Z the same basic way
    collect_motion_data does, and for every T{n} tool-change line records:
      - "toolchange": modal Y/Z right BEFORE that line -- wherever the
        toolhead was when the swap was triggered. None if the file has
        no prior Y/Z (a toolchange before the first real move).
      - "restore": parsed straight from the line's "X=.. Y=.. Z=.."
        suffix if restore_pos_fix.py already ran, otherwise calculated
        with _find_restore_anchor() so an unannotated file gets the
        SAME answer restore_pos_fix.py itself would have written, not a
        second, possibly-divergent guess. None if no destination could
        be determined either way.

    Deliberately only Y/Z -- X is tracked internally (both here and in
    _find_restore_anchor) because the lookahead logic needs it to stay a
    faithful port, but this script's whole collision model is a Y/Z
    plane and has no use for X.

    Returns a list of {"tool": int, "toolchange": (y,z)|None, "restore": (y,z)|None}.
    """
    results = []
    x = y = z = None

    for i, line in enumerate(lines):
        if PAT_MOVE.match(line):
            for axis, val in PAT_AXES.findall(line):
                v = float(val)
                a = axis.upper()
                if a == "X":
                    x = v
                elif a == "Y":
                    y = v
                elif a == "Z":
                    z = v
            continue

        if line.lstrip().startswith(";"):
            continue

        m = PAT_TOOLCHANGE.match(line)
        if not m:
            continue

        tool_num = int(m.group(1))
        suffix = m.group(2) or ""
        toolchange_point = (y, z) if (y is not None and z is not None) else None

        restore_point = None
        if suffix:
            sx = sy = sz = None
            for axis, val in PAT_TOOLCHANGE_SUFFIX.findall(suffix):
                v = float(val)
                a = axis.upper()
                if a == "X":
                    sx = v
                elif a == "Y":
                    sy = v
                elif a == "Z":
                    sz = v
            if sy is not None:
                restore_point = (sy, sz if sz is not None else z)
        else:
            anchor = _find_restore_anchor(lines, i + 1, x, y, z)
            if anchor is not None:
                _, ay, az = anchor
                if ay is not None:
                    restore_point = (ay, az if az is not None else z)

        results.append({"tool": tool_num, "toolchange": toolchange_point, "restore": restore_point})

    return results


def check_toolchange_hop_clearance(toolchange_points: list, pts: list, safe_y: float,
                                    toolchange_hop_mm: float, restore_hop_mm: float):
    """
    A check the main safety check CANNOT do: for every toolchange/restore
    point, adds the configured (assumed) hop height to its observed Z and
    tests the resulting peak against the no-go boundary. The toolchange
    trigger point's raw (Y, Z) is already covered by find_closest_approach
    (it's just wherever the toolhead was before "T{n}", part of the
    ordinary continuous trace) -- what's NEW here is the hypothetical
    peak during the actual physical hop, a height that never appears as
    a literal commanded coordinate anywhere in the g-code, since the real
    printer synthesizes that motion from its own macro at print time.

    toolchange_hop_mm and restore_hop_mm are independent, deliberately --
    the outbound (into-the-dock) and inbound (return-to-print) hops can
    genuinely differ depending on the macro. Y >= safe_y points are
    skipped (max_z_allowed returns inf there, same as everywhere else).

    Returns (toolchange_min_z_clearance, worst_entry, failing_keys, all_checked):
      - toolchange_min_z_clearance: the worst (smallest) clearance found
        across every point checked, or None if nothing was in reach of
        the dock at all.
      - worst_entry: {"index", "tool", "role", "y", "z" (the
        ASSUMED/hopped Z, not the raw observed one), "limit",
        "clearance"} for that worst point, or None.
      - failing_keys: set of (index, role) for every point with
        clearance < 0 -- not just the worst one, so every offending
        point can be marked, not only the single worst. Keyed by INDEX
        into toolchange_points (that specific occurrence), NOT by tool
        number -- the same tool is typically used for dozens of separate
        toolchange events throughout a file, on different objects, at
        different heights; keying by tool number alone would mark every
        one of them as failing the moment any single occurrence does.
        Callers must enumerate() the same toolchange_points list to line
        indices back up correctly.
      - all_checked: every point actually evaluated (Y < safe_y), as
        plain dicts -- for write_debug_dump, so the FULL picture is
        inspectable, not just the worst point.
    """
    worst_clearance = None
    worst_entry = None
    failing_keys = set()
    all_checked = []

    for idx, entry in enumerate(toolchange_points):
        for role, hop_mm in (("toolchange", toolchange_hop_mm), ("restore", restore_hop_mm)):
            pt = entry.get(role)
            if pt is None:
                continue
            y, z = pt
            assumed_z = z + hop_mm
            limit = max_z_allowed(y, pts, safe_y)
            if limit == float("inf"):
                continue
            clearance = limit - assumed_z
            checked_entry = {
                "index": idx, "tool": entry["tool"], "role": role,
                "observed_y": y, "observed_z": z, "hop_mm": hop_mm,
                "assumed_z": assumed_z, "limit": limit, "clearance": clearance,
                "fails": clearance < 0,
            }
            all_checked.append(checked_entry)
            if clearance < 0:
                failing_keys.add((idx, role))
            if worst_clearance is None or clearance < worst_clearance:
                worst_clearance = clearance
                worst_entry = {
                    "index": idx, "tool": entry["tool"], "role": role,
                    "y": y, "z": assumed_z, "limit": limit, "clearance": clearance,
                }

    return worst_clearance, worst_entry, failing_keys, all_checked


def simplify_polyline(points: list, epsilon: float) -> list:
    """
    Ramer-Douglas-Peucker simplification of an open polyline. Endpoints
    are always kept; an interior point survives only if it's more than
    epsilon away from the straight line between its neighbors -- i.e.
    it's dropped when it's on (or close enough to) a straight run, kept
    when it marks a real bend.

    Applied per-edge (the silhouette's "up" walk and "down" walk
    simplified separately, see build_object_silhouette_polygon) rather
    than on the whole closed loop at once, so each edge's own start/end
    corner -- the actual top/bottom extent of the shape -- is always
    preserved exactly rather than risking getting smoothed away by a
    whole-loop pass.

    epsilon is in the same units as the points (mm here). Silhouettes
    are a debug/preview overlay, not a precision trace (see
    build_object_silhouette_polygon's own docstring on that trade-off),
    so a small epsilon that's invisible at preview scale but prunes the
    long straight runs a real object's vertical walls produce is the
    right target -- not zero.

    Iterative (explicit stack), not the textbook recursive form -- a
    tall print can have well over a thousand layers, and a pathological
    input (near-monotonic curve) can make recursive RDP's call depth
    approach the point count itself, past Python's default recursion
    limit. An explicit stack has no such ceiling.
    """
    n = len(points)
    if n < 3:
        return list(points)

    keep = bytearray(n)  # 0/1 flags, index-parallel to points
    keep[0] = 1
    keep[-1] = 1
    stack = [(0, n - 1)]

    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue

        x1, y1 = points[start]
        x2, y2 = points[end]
        dx, dy = x2 - x1, y2 - y1
        seg_len = (dx * dx + dy * dy) ** 0.5

        max_dist = -1.0
        split_idx = -1
        for i in range(start + 1, end):
            px, py = points[i]
            if seg_len == 0:
                dist = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
            else:
                dist = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / seg_len
            if dist > max_dist:
                max_dist = dist
                split_idx = i

        if max_dist > epsilon:
            keep[split_idx] = 1
            stack.append((start, split_idx))
            stack.append((split_idx, end))

    return [p for p, k in zip(points, keep) if k]


def build_object_silhouette_polygon(min_max_by_z: dict, simplify_epsilon_mm: float = 0.15) -> list:
    """
    Builds a closed (y, z) polygon from {z: (min_y, max_y)} the same way
    build_no_go_polygon builds the no-go zone: walk the near-dock
    (min_y) edge in ascending Z, cross to the far (max_y) edge at the
    top, walk back down the max_y edge in descending Z, close at the
    bottom. Pure Python, no rasterization/scipy needed.

    Can over-include real gaps for an object/layer with a genuine hole
    in its Y-extent (e.g. a letter "H" shape, where the middle isn't
    actually printed) -- an accepted visual approximation, not a
    precision trace.

    Each edge (the min_y walk, the max_y walk) is independently run
    through simplify_polyline at simplify_epsilon_mm -- one point per
    printed layer is otherwise the norm here (400+ layers is a normal
    print), and most of an ordinary object's height is straight walls
    where consecutive layers barely move in Y, so those points are pure
    redundancy at preview scale. Pass 0 to disable and keep one point
    per layer exactly, e.g. for a caller that needs the unsimplified
    trace for something other than the SVG preview.
    """
    if not min_max_by_z:
        return []
    zs = sorted(min_max_by_z.keys())
    up = [[round(min_max_by_z[z][0], 2), round(z, 2)] for z in zs]
    down = [[round(min_max_by_z[z][1], 2), round(z, 2)] for z in reversed(zs)]
    if simplify_epsilon_mm > 0:
        up = simplify_polyline(up, simplify_epsilon_mm)
        down = simplify_polyline(down, simplify_epsilon_mm)
    return up + down


# Last-resort fallback only -- used if a caller invokes
# object_silhouette_color() without going through build_svg_payload (so
# there's no svg_cfg to derive real bands from), or if hue-derivation
# somehow yields nothing. Mirrors this script's factory-default colors.
FALLBACK_AVOID_HUE_BANDS = [
    (0, 14),
    (33, 14),
    (54, 14),
    (120, 16),
    (153, 16),
    (211, 16),
]


def _hue_from_color(color_str, min_saturation: float = 0.12):
    """
    Extracts a 0-360 hue from an 'rgba(r,g,b,a)' / 'rgb(r,g,b)' / '#rrggbb'
    color string. Returns None for near-achromatic colors (grays, e.g.
    the default travel-move color) below min_saturation -- hue is
    undefined/unstable there, and there's nothing meaningful for the
    auto-assigned object hues to avoid.
    """
    if not color_str:
        return None
    color_str = color_str.strip()
    if color_str.startswith("#"):
        hexs = color_str.lstrip("#")
        if len(hexs) < 6:
            return None
        r, g, b = int(hexs[0:2], 16), int(hexs[2:4], 16), int(hexs[4:6], 16)
    else:
        nums = re.findall(r"[-\d.]+", color_str)
        if len(nums) < 3:
            return None
        r, g, b = float(nums[0]), float(nums[1]), float(nums[2])
    h, s, _v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if s < min_saturation:
        return None
    return h * 360.0


def derive_avoid_hue_bands(svg_cfg: dict, half_width: float = 15.0) -> list:
    """
    Reads every OTHER fixed-meaning color actually in effect in svg_cfg
    (config override if present, else the same default string literals
    build_svg_payload/build_marker_shapes/build_zone_shapes themselves
    fall back to) and turns each into a hue-avoidance band. This is what
    lets object_silhouette_color's auto-assigned per-object hues
    self-adjust whenever a user recolors no_go/support/silhouette/
    travel/toolchange_markers/clearance_zones in the JSON config -- no
    code change required, the bands just follow whatever colors are
    actually on screen. printable_area no longer contributes a color
    here -- it only sizes the canvas now, it draws no outline.

    A color belonging to a switched-off feature (travel_moves.show:
    false, toolchange_markers.show: 'off', a clearance_zones.<type>.
    mode: 'off') is skipped, since nothing on screen actually wears
    that color. no_go and support are always
    included (always drawn), as is toolchange_markers.fail_color (can
    appear regardless of the "show" setting -- see build_marker_shapes).
    Object silhouettes are deliberately NOT in this list any more: the
    base silhouette color is now itself the seed for the auto-assigned
    per-object palette (see object_silhouette_color), so there's nothing
    separate left for the palette to avoid clashing with.
    """
    colors = svg_cfg.get("colors", {}) or {}
    travel_cfg = svg_cfg.get("travel_moves", {}) or {}
    tc_cfg = svg_cfg.get("toolchange_markers", {}) or {}
    zones_cfg = svg_cfg.get("clearance_zones", {}) or {}

    candidates = []

    no_go_style = _shape_style(colors, "no_go", "rgba(255,60,60,0.30)", "rgba(255,60,60,0.9)", "dithered")
    candidates.append(no_go_style["stroke"])

    support_style = _shape_style(colors, "support", "rgba(80,200,80,0.30)", "rgba(80,200,80,0.9)", "solid")
    candidates.append(support_style["stroke"])

    if travel_cfg.get("show", False):
        candidates.append(travel_cfg.get("color", "rgba(200,200,200,0.4)"))

    candidates.append(tc_cfg.get("fail_color", "rgba(255,0,0,0.95)"))
    if tc_cfg.get("show", "off") != "off":
        tc_style = tc_cfg.get("toolchange", {}) or {}
        rs_style = tc_cfg.get("restore", {}) or {}
        candidates.append(tc_style.get("color", "rgba(255,140,0,0.85)"))
        candidates.append(rs_style.get("color", "rgba(0,220,120,0.85)"))

    for zone_type in ("collision", "near_miss"):
        type_cfg = zones_cfg.get(zone_type, {}) or {}
        mode = type_cfg.get("mode", "off")
        if mode == "highlight":
            style = type_cfg.get("highlight", {}) or {}
            candidates.append(style.get("stroke", "rgba(255,230,0,0.95)"))
        elif mode == "outline":
            style = type_cfg.get("outline", {}) or {}
            candidates.append(style.get("stroke", "rgba(255,230,0,0.95)"))

    bands = []
    for c in candidates:
        hue = _hue_from_color(c)
        if hue is not None:
            bands.append((hue, half_width))
    return bands


def _push_hue_outside_bands(hue_deg: float, bands) -> float:
    for _ in range(4):
        moved = False
        for center, half_width in bands:
            diff = (hue_deg - center + 180) % 360 - 180
            if abs(diff) < half_width:
                edge = half_width + 1.0
                hue_deg = (center + edge) % 360 if diff >= 0 else (center - edge) % 360
                moved = True
        if not moved:
            break
    return hue_deg


def object_silhouette_color(index: int, colors_cfg: dict, avoid_bands: list = None):
    """
    Auto-generates a visually distinct fill/stroke color per object
    using golden-ratio hue spacing (a standard trick for well-separated
    hues without needing to know the total object count up front) --
    seeded from colors.silhouette.color so index 0 lands on exactly the
    color picked in the GUI, and every other index fans out from it.
    Saturation/value/alpha come from colors.silhouette_palette so the
    look can still be tuned; only the hue itself is auto-assigned. This
    is the single color path for every object count -- one object and
    several objects both go through here, so there's no separate
    "single object" look to fall out of sync with the palette.

    The golden-ratio hue is then nudged away from avoid_bands (normally
    derive_avoid_hue_bands()'s output, passed in by build_svg_payload) so
    an auto-generated object color never lands on or near a fixed-meaning
    color used elsewhere in the render -- otherwise a viewer could
    mistake "this object" for a no-go zone, a support, a collision
    marker, etc. Index 0 is exempt from this nudge -- it's the color the
    user actually picked, so it's used exactly as-is regardless of what
    it's close to. colors.silhouette_palette.avoid_hues, if set,
    overrides this entirely with a user-specified list instead.
    """
    sil_cfg = colors_cfg.get("silhouette", {}) or {}
    palette_cfg = colors_cfg.get("silhouette_palette", {}) or {}
    sat = float(palette_cfg.get("saturation", 0.65))
    val = float(palette_cfg.get("value", 0.95))
    fill_alpha = float(palette_cfg.get("fill_alpha", 0.30))
    stroke_alpha = float(palette_cfg.get("stroke_alpha", 0.9))
    custom_bands = palette_cfg.get("avoid_hues")
    if custom_bands:
        bands = [(float(c), float(w)) for c, w in custom_bands]
    elif avoid_bands:
        bands = avoid_bands
    else:
        bands = FALLBACK_AVOID_HUE_BANDS

    base_color = sil_cfg.get("color", "#50aaff")
    base_hue_deg = _hue_from_color(base_color, min_saturation=0.0)
    if base_hue_deg is None:
        base_hue_deg = 0.0

    hue_deg = (base_hue_deg + (index * 0.61803398875) * 360.0) % 360.0
    if index != 0:
        # Index 0 is the user's own picked color, exactly -- exempt from
        # collision-avoidance so what you pick is always what you get.
        # Every other index is auto-generated, so those still get pushed
        # away from fixed-meaning colors.
        hue_deg = _push_hue_outside_bands(hue_deg, bands)
    hue = hue_deg / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    r, g, b = round(r * 255), round(g * 255), round(b * 255)
    return f"rgba({r},{g},{b},{fill_alpha})", f"rgba({r},{g},{b},{stroke_alpha})"


# ---------------------------------------------------------------------------
# Config + boundary (unchanged)
# ---------------------------------------------------------------------------

def load_config(script_path: pathlib.Path) -> dict:
    """
    Looks for the companion config in configs/, next to OrcaStrator
    (one level up from post_processors/).
    """
    cfg_name = script_path.with_suffix(".json").name
    cfg_path = script_path.parent.parent / "configs" / cfg_name
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    raise FileNotFoundError(f"Companion config '{cfg_name}' not found. Looked in:\n  {cfg_path}")


def build_boundary(cfg: dict):
    raw = cfg.get("boundary", [])
    if not raw:
        raise ValueError("'boundary' list is empty in config.")

    pts = sorted(
        ((float(p["y"]), float(p["z"])) for p in raw),
        key=lambda p: p[0],
    )
    safe_y = float(cfg.get("safe_y", pts[-1][0]))
    return pts, safe_y


Z_ROUND_DECIMALS = 3  # groups Z values this close together as "the same layer" -- just
                       # enough to absorb float noise (e.g. two z-hops that should be
                       # identical differing by 1e-10), nowhere near enough to merge
                       # genuinely different layers/hop heights.

TRAVEL_SILHOUETTE_HEIGHT_MM = 0.2  # minimum Z thickness a travel silhouette entry is
                                   # given, grown upward from the move's own lowest Z, so a
                                   # move with (nearly) zero Z rise still reads as a visible
                                   # sliver rather than a zero-height line. Not the real
                                   # swept toolhead-body height -- just enough for the shape
                                   # to render; real layer spacing (commonly >=0.1mm) then
                                   # naturally fills in the rest as adjacent layers stack.


def group_min_y_by_z(points: list, z_round: int = Z_ROUND_DECIMALS) -> dict:
    """
    Groups (y, z) points by Z (rounded, see Z_ROUND_DECIMALS) and keeps
    only the minimum Y seen at each Z. This is the core insight behind
    both the collision check and the silhouette: to get from one Y to
    another at a given Z, the toolhead necessarily passes through every
    Y value in between, so the closest approach (minimum Y, since lower
    Y means closer to the dock) is all that matters -- everything else
    at that Z can be safely ignored.

    Handles Z-hop for free: every distinct Z actually commanded becomes
    its own bucket, whether it's a real print layer or a hop-induced
    transient height -- no special-casing needed.

    Returns {rounded_z: min_y}.
    """
    by_z = {}
    for y, z in points:
        rz = round(z, z_round)
        if rz not in by_z or y < by_z[rz]:
            by_z[rz] = y
    return by_z


def group_min_max_y_by_z(points: list, z_round: int = Z_ROUND_DECIMALS) -> dict:
    """Same idea as group_min_y_by_z, but keeps both extremes -- the
    silhouette needs the far (max-Y) edge too, not just the near one."""
    by_z = {}
    for y, z in points:
        rz = round(z, z_round)
        if rz not in by_z:
            by_z[rz] = [y, y]
        else:
            entry = by_z[rz]
            if y < entry[0]:
                entry[0] = y
            if y > entry[1]:
                entry[1] = y
    return {z: (mn, mx) for z, (mn, mx) in by_z.items()}


import bisect


def snap_z_to_nearest_layer(z: float, sorted_layer_zs: list) -> float:
    """
    Snaps z to whichever value in sorted_layer_zs (must be pre-sorted)
    it's numerically closest to. Used to pull a travel move's own Z back
    onto a real print layer's Z before it's folded into the travel
    silhouette -- see split_cluster_by_z_structure's docstring for why
    that matters (Z-hop ramping).
    """
    if not sorted_layer_zs:
        return z
    idx = bisect.bisect_left(sorted_layer_zs, z)
    if idx == 0:
        return sorted_layer_zs[0]
    if idx == len(sorted_layer_zs):
        return sorted_layer_zs[-1]
    before, after = sorted_layer_zs[idx - 1], sorted_layer_zs[idx]
    return before if (z - before) <= (after - z) else after



AUTO_CLUSTER_GAP_SAFETY_FACTOR = 0.4  # fraction of the tightest real inter-object Y
                                       # gap in THIS file that cluster_gap_mm is allowed
                                       # to use -- keeps real margin against ever
                                       # bridging two objects the print itself keeps
                                       # apart, even a fairly close pair.
AUTO_CLUSTER_GAP_FLOOR_MM = 0.5   # never go below this -- still needs to absorb
                                  # ordinary float noise between unrelated nearby moves
                                  # (e.g. one ends at y=39.98, another starts at y=40.01).
AUTO_CLUSTER_GAP_CEILING_MM = 5.0  # never go above this -- once objects are already
                                    # comfortably far apart there's no benefit to a
                                    # larger tolerance, and an unbounded one risks
                                    # merging things for unrelated reasons.


def compute_auto_cluster_gap_mm(filtered_object_points: dict, filtered_support_points: dict) -> float:
    """
    Derives cluster_gap_mm from THIS file's own object layout instead of
    using one fixed value across every print. A single constant is
    inherently a compromise: big enough to merge ordinary local jitter
    within one region risks being big enough to also merge two genuinely
    separate objects that just happen to sit close together in Y (which
    silently reintroduces the false-bridging bug that
    cluster_travel_segments_by_y exists to prevent -- just triggered by
    physical proximity instead of a stray connector move). The right
    tolerance depends on how tightly THIS print's objects are actually
    spaced, which varies file to file, so there's no one constant that's
    safe for every print without needlessly fragmenting the least
    tightly-spaced ones.

    Combines each named object's own points with any support points
    sharing that name into one Y-range per object, finds every pair's
    real Y gap (max(lo1, lo2) - min(hi1, hi2); ignores pairs that
    overlap in Y, e.g. two objects side-by-side in X, since there's no
    gap to measure there), and uses AUTO_CLUSTER_GAP_SAFETY_FACTOR of
    the TIGHTEST one found -- keeping real margin below the closest
    real objects ever get in this specific file, whatever that number
    turns out to be. Falls back to the ceiling when there's nothing to
    measure (fewer than two objects, or every pair overlaps in Y).
    """
    names = set(filtered_object_points) | set(filtered_support_points)
    y_ranges = []
    for name in names:
        pts = filtered_object_points.get(name, []) + filtered_support_points.get(name, [])
        if not pts:
            continue
        ys = [y for y, _z in pts]
        y_ranges.append((min(ys), max(ys)))

    positive_gaps = []
    for i in range(len(y_ranges)):
        lo1, hi1 = y_ranges[i]
        for lo2, hi2 in y_ranges[i + 1:]:
            gap = max(lo1, lo2) - min(hi1, hi2)
            if gap > 0:
                positive_gaps.append(gap)

    if not positive_gaps:
        return AUTO_CLUSTER_GAP_CEILING_MM

    gap_mm = min(positive_gaps) * AUTO_CLUSTER_GAP_SAFETY_FACTOR
    return max(AUTO_CLUSTER_GAP_FLOOR_MM, min(AUTO_CLUSTER_GAP_CEILING_MM, gap_mm))


def cluster_travel_segments_by_y(travel_segments: list, gap_mm: float,
                                  sparse_coverage_max: int = 2) -> tuple:
    """
    Groups travel_segments into separate clusters by Y connectivity, so
    two physically disjoint travel regions (e.g. moves shuttling between
    two front-of-bed objects, and separately between two back-of-bed
    objects printed with a different tool -- never directly connected to
    each other) don't get folded into one polygon that bridges the empty
    gap between them.

    Y is the only axis that matters here (same reasoning as everywhere
    else in this file) -- X position doesn't affect dock risk, so two
    travel regions that share a Y band are correctly treated as one
    cluster even if they're on opposite sides of the bed in X.

    Works by building a Y-axis coverage function (a sweep over every
    segment's [lo, hi] span, +1 entering, -1 leaving) and looking for
    "deserts" -- contiguous Y-runs where coverage stays at or below
    sparse_coverage_max (default 2: at most a couple of segments are
    ever passing through, as opposed to the dozens/hundreds that cover a
    real dense region, since travel recurs on nearly every layer there),
    wider than gap_mm. A segment whose span fully crosses a desert is a
    connector -- rare, low-count evidence bridging two regions, not
    proof they're actually one. Excluding connector segments and
    re-running a plain span-overlap sweep on what's left lets genuine
    separate regions fall apart on their own.

    This groups by Y alone, ignoring Z -- deliberately coarse. A cluster
    formed here can still internally be two Z-disjoint sub-regions that
    only start overlapping in Y partway up the print (see
    split_cluster_by_z_structure, applied to each cluster afterward,
    which is what actually detects and un-bridges that case). Splitting
    that apart requires per-Z structure this function doesn't look at;
    trying to do both jobs in one pass is what made the Z-aware attempt
    here fragile against ordinary per-layer jitter in complex geometry --
    a real object's own travel pattern can fluctuate between 1-3 locally
    disjoint pieces layer to layer for reasons having nothing to do with
    genuine long-term disconnection, and naive per-Z track matching
    treated every such flicker as a hard split. Two clean passes (coarse
    Y grouping here, then a noise-tolerant per-cluster structural check)
    is more robust than one pass trying to be precise about both at once.

    Returns (dense_clusters, connectors):
      dense_clusters -- list of segment-lists, each a genuinely connected
        Y-region.
      connectors -- segments that fully crossed a desert -- real motion,
        but not part of any region's dense envelope. Caller decides
        whether/how to draw these.
    """
    if not travel_segments:
        return [], []

    spans = []
    for y0, z0, y1, z1 in travel_segments:
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        spans.append((lo, hi))

    events = sorted(
        [(lo, 1) for lo, hi in spans] + [(hi, -1) for lo, hi in spans]
    )

    coverage_intervals = []  # (lo, hi, coverage)
    coverage = 0
    prev_y = events[0][0]
    i, n = 0, len(events)
    while i < n:
        y = events[i][0]
        if y > prev_y:
            coverage_intervals.append((prev_y, y, coverage))
        while i < n and events[i][0] == y:
            coverage += events[i][1]
            i += 1
        prev_y = y

    deserts = []
    m = len(coverage_intervals)
    j = 0
    while j < m:
        if coverage_intervals[j][2] <= sparse_coverage_max:
            start = j
            while j < m and coverage_intervals[j][2] <= sparse_coverage_max:
                j += 1
            end = j - 1
            desert_lo, desert_hi = coverage_intervals[start][0], coverage_intervals[end][1]
            if (desert_hi - desert_lo) > gap_mm:
                deserts.append((desert_lo, desert_hi))
        else:
            j += 1

    connectors = []
    normal_segments = []
    for seg, (lo, hi) in zip(travel_segments, spans):
        if any(lo <= d_lo and hi >= d_hi for d_lo, d_hi in deserts):
            connectors.append(seg)
        else:
            normal_segments.append(seg)

    if not normal_segments:
        return [], list(travel_segments)

    indexed = sorted(
        range(len(normal_segments)),
        key=lambda i: min(normal_segments[i][0], normal_segments[i][2]),
    )
    dense_clusters = []
    current_indices = [indexed[0]]
    seg = normal_segments[indexed[0]]
    current_hi = max(seg[0], seg[2])

    for i in indexed[1:]:
        seg = normal_segments[i]
        lo, hi = min(seg[0], seg[2]), max(seg[0], seg[2])
        if lo <= current_hi + gap_mm:
            current_indices.append(i)
            current_hi = max(current_hi, hi)
        else:
            dense_clusters.append([normal_segments[k] for k in current_indices])
            current_indices = [i]
            current_hi = hi
    dense_clusters.append([normal_segments[k] for k in current_indices])

    return dense_clusters, connectors


def split_cluster_by_z_structure(cluster_segments: list, gap_mm: float,
                                  travel_height_mm: float = TRAVEL_SILHOUETTE_HEIGHT_MM,
                                  z_round: int = Z_ROUND_DECIMALS, layer_zs: list = None,
                                  min_run: int = 5) -> list:
    """
    Takes one Y-cluster from cluster_travel_segments_by_y and checks
    whether it actually contains a persistent internal Z-structure that
    the Y-only pass couldn't see -- e.g. two objects that share no
    travel for most of their height (so they'd form separate clusters
    on their own), but share a tool -- and therefore direct connecting
    travel -- for their last few layers, at which point their Y-ranges
    genuinely do overlap and cluster_travel_segments_by_y correctly
    lumps everything into one Y-cluster. Folding that whole cluster with
    a single flat {z: (min_y, max_y)} would bridge the low-Z portion
    where the two objects were never actually connected -- this
    function detects that and splits it back apart, but ONLY when the
    split is a real, sustained structural feature, not per-layer noise.

    Per real Z, computes the disjoint Y-intervals present (merging only
    what's within gap_mm of each other at that Z) and counts them. This
    count is often noisy in real files -- a complex object's own travel
    pattern can flicker between 1-3 locally separate pieces from one
    layer to the next for reasons having nothing to do with a genuine
    long-term split (transient per-layer routing detail, not two
    actually-disconnected regions). So the raw per-Z count is smoothed
    by collapsing any run shorter than min_run consecutive Z's into
    whichever neighboring run is longer -- a count that only holds for a
    few layers is noise; a count that holds for min_run+ layers is
    structure.

    If the smoothed count is constant across the whole cluster (the
    common case), returns the cluster as a single flat-folded track,
    identical to the pre-existing simple behavior. If it finds one or
    more STABLE transitions (e.g. steady at 2 for the first 165 layers,
    then steady at 1 for the last 35), it splits the Z-range at each
    transition and, within any multi-group stretch, re-clusters just
    that stretch's own segments by Y (reusing
    cluster_travel_segments_by_y, which handles finding the right
    number of groups and their positions far more robustly than trying
    to track per-Z identity frame-by-frame) -- each resulting sub-group
    becomes its own track.

    Returns a list of {z: (min_y, max_y)} track dicts.
    """
    if not cluster_segments:
        return []

    per_z_raw: dict = {}
    for y0, z0, y1, z1 in cluster_segments:
        sz0 = snap_z_to_nearest_layer(z0, layer_zs) if layer_zs else z0
        sz1 = snap_z_to_nearest_layer(z1, layer_zs) if layer_zs else z1
        y_lo, y_hi = (y0, y1) if y0 <= y1 else (y1, y0)
        z_lo, z_hi = (sz0, sz1) if sz0 <= sz1 else (sz1, sz0)
        if z_hi - z_lo < travel_height_mm:
            z_hi = z_lo + travel_height_mm
        for z in (round(z_lo, z_round), round(z_hi, z_round)):
            per_z_raw.setdefault(z, []).append((y_lo, y_hi))

    per_z_intervals: dict = {}
    for z, ivals in per_z_raw.items():
        ivals.sort()
        merged = [list(ivals[0])]
        for lo, hi in ivals[1:]:
            if lo <= merged[-1][1] + gap_mm:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        per_z_intervals[z] = [tuple(iv) for iv in merged]

    zs = sorted(per_z_intervals.keys())
    raw_counts = [len(per_z_intervals[z]) for z in zs]

    # Smooth: collapse any run shorter than min_run into a neighbor.
    # Simple iterative pass -- repeatedly find the shortest run and
    # merge it into whichever adjacent run is longer, until every run
    # meets min_run or only one run remains.
    runs = []  # [count, length]
    for c in raw_counts:
        if runs and runs[-1][0] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])

    def _shortest_run_idx():
        idx, best = None, None
        for i, (_, length) in enumerate(runs):
            if length < min_run and (best is None or length < best):
                idx, best = i, length
        return idx

    while len(runs) > 1:
        i = _shortest_run_idx()
        if i is None:
            break
        left_len = runs[i - 1][1] if i > 0 else -1
        right_len = runs[i + 1][1] if i < len(runs) - 1 else -1
        if left_len < 0 and right_len < 0:
            break
        merge_left = right_len < 0 or (left_len >= 0 and left_len >= right_len)
        if merge_left:
            runs[i - 1][1] += runs[i][1]
            del runs[i]
        else:
            runs[i + 1][1] += runs[i][1]
            del runs[i]
        # Re-merge adjacent runs that now share the same count.
        merged_runs = []
        for r in runs:
            if merged_runs and merged_runs[-1][0] == r[0]:
                merged_runs[-1][1] += r[1]
            else:
                merged_runs.append(r)
        runs = merged_runs

    # Expand smoothed runs back over the z sequence to get segment
    # boundaries (as z-index ranges into `zs`).
    segments_idx = []  # (start_idx, end_idx_exclusive, smoothed_count)
    pos = 0
    for count, length in runs:
        segments_idx.append((pos, pos + length, count))
        pos += length

    if len(segments_idx) == 1:
        # No stable structural split -- simple flat fold, same as the
        # original, proven behavior.
        flat: dict = {}
        for z in zs:
            los = [lo for lo, hi in per_z_intervals[z]]
            his = [hi for lo, hi in per_z_intervals[z]]
            flat[z] = (min(los), max(his))
        return [flat]

    tracks = []
    for start_idx, end_idx, count in segments_idx:
        seg_zs = set(zs[start_idx:end_idx])
        if count <= 1:
            flat = {}
            for z in seg_zs:
                los = [lo for lo, hi in per_z_intervals[z]]
                his = [hi for lo, hi in per_z_intervals[z]]
                flat[z] = (min(los), max(his))
            tracks.append(flat)
        else:
            # Stable multi-group stretch -- re-cluster just this
            # stretch's own segments by Y to robustly recover the
            # sub-groups and their positions. Restrict to segments whose
            # OWN raw (unsmoothed) Z-bucket count actually matches this
            # run's target count -- using the run's Z-range as a simple
            # min/max bound would also catch the run-length-smoothing's
            # own absorbed noise Z's (the min_run-collapsed blips), which
            # can carry genuinely different (e.g. much wider) raw data
            # that would recontaminate the very re-clustering this step
            # exists to do.
            confident_zs = {z for z in seg_zs if len(per_z_intervals[z]) == count}

            def _touches_confident_z(seg):
                y0, z0, y1, z1 = seg
                sz0 = snap_z_to_nearest_layer(z0, layer_zs) if layer_zs else z0
                sz1 = snap_z_to_nearest_layer(z1, layer_zs) if layer_zs else z1
                zlo, zhi = (sz0, sz1) if sz0 <= sz1 else (sz1, sz0)
                if zhi - zlo < travel_height_mm:
                    zhi = zlo + travel_height_mm
                return (round(zlo, z_round) in confident_zs
                        or round(zhi, z_round) in confident_zs)

            sub_segments = [s for s in cluster_segments if _touches_confident_z(s)]
            sub_clusters, sub_connectors = cluster_travel_segments_by_y(sub_segments, gap_mm)
            for sub in sub_clusters:
                flat = {}
                for y0, z0, y1, z1 in sub:
                    sz0 = snap_z_to_nearest_layer(z0, layer_zs) if layer_zs else z0
                    sz1 = snap_z_to_nearest_layer(z1, layer_zs) if layer_zs else z1
                    y_lo, y_hi = (y0, y1) if y0 <= y1 else (y1, y0)
                    z_lo2, z_hi2 = (sz0, sz1) if sz0 <= sz1 else (sz1, sz0)
                    if z_hi2 - z_lo2 < travel_height_mm:
                        z_hi2 = z_lo2 + travel_height_mm
                    for z in (round(z_lo2, z_round), round(z_hi2, z_round)):
                        if z not in seg_zs:
                            continue
                        if z not in flat:
                            flat[z] = [y_lo, y_hi]
                        else:
                            flat[z][0] = min(flat[z][0], y_lo)
                            flat[z][1] = max(flat[z][1], y_hi)
                if flat:
                    tracks.append({z: tuple(v) for z, v in flat.items()})
            for y0, z0, y1, z1 in sub_connectors:
                y_lo, y_hi = (y0, y1) if y0 <= y1 else (y1, y0)
                z_lo2, z_hi2 = (z0, z1) if z0 <= z1 else (z1, z0)
                if z_hi2 - z_lo2 < travel_height_mm:
                    z_hi2 = z_lo2 + travel_height_mm
                tracks.append({round(z_lo2, z_round): (y_lo, y_hi),
                               round(z_hi2, z_round): (y_lo, y_hi)})

    return tracks





def group_min_y_by_z_with_owner(position_owners: list, z_round: int = Z_ROUND_DECIMALS) -> dict:
    """
    Same technique as group_min_y_by_z, but position_owners carries
    (object_name, is_support) alongside each point (see
    collect_motion_data), and this tracks which owner produced the
    minimum Y at each Z. PURELY for attributing a violation to an object
    in the abort message -- the actual safety check always uses the
    plain, unowned group_min_y_by_z on the unfiltered positions list;
    this function is never consulted for the pass/fail decision itself.

    Because find_closest_approach's worst point is chosen from that same
    plain grouping, looking up the identical (rounded) Z here is
    guaranteed to land on the exact same point -- no fuzzy/nearest-match
    lookup needed, just a same-key dict lookup.

    Returns {rounded_z: (min_y, object_name_or_None, is_support)}.
    """
    by_z = {}
    for y, z, owner, is_support in position_owners:
        rz = round(z, z_round)
        if rz not in by_z or y < by_z[rz][0]:
            by_z[rz] = (y, owner, is_support)
    return by_z


def worst_violation_by_owner(points_by_owner: dict, pts: list, safe_y: float):
    """
    Runs find_closest_approach() independently for each owner's own point
    list (e.g. each object's body points, or each object's support
    points) and returns the worst VIOLATING (clearance < 0) result found
    across all owners -- or None if none of them violate.

    This exists for priority-based collision attribution: unlike the
    single global find_closest_approach() call on the full unfiltered
    trace, this tells us whether a *specific category* (object bodies,
    support material) has a violation of its own, independent of whether
    some other category's point happens to be numerically worse. See
    check_file()'s object/support/toolchange priority order.

    Restricting each owner's check to just their own points is safe here
    (doesn't miss anything a combined check would catch) precisely
    because each owner's point set is a SUBSET of the full positions
    trace already covered by the main find_closest_approach() call --
    this function only ever runs to decide priority/attribution among
    violations the main check (or the toolchange hop check) already
    knows exist, never as its own independent pass/fail authority.

    Returns (owner_name, y, z, limit, clearance) for the worst
    violation, or None.
    """
    worst = None
    for name, owner_points in points_by_owner.items():
        if not owner_points:
            continue
        approach = find_closest_approach(owner_points, pts, safe_y)
        if approach is None:
            continue
        y, z, limit, clearance = approach
        if clearance < 0 and (worst is None or clearance < worst[4]):
            worst = (name, y, z, limit, clearance)
    return worst


def friendly_object_name(name: str) -> str:
    """
    OrcaSlicer's EXCLUDE_OBJECT names look like 'Cube_id_0_copy_0' or
    'sc_chip.step_id_0_copy_0' -- strips the '_id_N_copy_M' suffix for a
    human-facing message, leaving 'Cube' / 'sc_chip.step'. Falls back to
    the raw name unchanged if it doesn't match that pattern.
    """
    m = re.match(r"^(.*)_id_\d+_copy_\d+$", name)
    return m.group(1) if m else name



def _collapse_ties(pts: list) -> list:
    """
    Collapses consecutive points that share the same Y (a vertical step in
    the boundary -- e.g. the dock wall jumping straight up) into a single
    point holding the higher Z. Once Y has reached that point the boundary
    has already stepped up to its new value; there's no segment to
    interpolate across a zero-width (Y1 - Y0 == 0) gap, and leaving the tie
    in would divide by zero below. Boundary validation (config_editor.py)
    guarantees Z is non-decreasing as Y increases, so within a tied-Y run
    the last point seen is always the highest Z -- safe to just keep it.
    Expects pts already sorted by Y (build_boundary does this).
    """
    collapsed = []
    for y, z in pts:
        if collapsed and collapsed[-1][0] == y:
            collapsed[-1] = (y, z)
        else:
            collapsed.append((y, z))
    return collapsed


def max_z_allowed(y: float, pts: list, safe_y: float) -> float:
    pts = _collapse_ties(pts)
    if y >= safe_y:
        return float("inf")
    if y <= pts[0][0]:
        return pts[0][1]
    if y >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        y0, z0 = pts[i]
        y1, z1 = pts[i + 1]
        if y0 <= y <= y1:
            t = (y - y0) / (y1 - y0)
            return z0 + t * (z1 - z0)
    return pts[-1][1]


def find_closest_approach(positions: list, pts: list, safe_y: float):
    """
    THE actual safety check -- this is what determines pass/fail.

    Groups the full motion trace by Z (group_min_y_by_z) and checks only
    the closest (minimum-Y) approach at each Z against the no-go
    boundary, instead of every single one of potentially hundreds of
    thousands of commanded points. This has EQUIVALENT coverage to
    checking every point, not an approximation of it -- PROVIDED the
    boundary's allowed Z (max_z_allowed) is monotonically non-decreasing
    as Y increases toward safe_y: if the closest point at a given Z
    clears the boundary, every other point at that Z is further from the
    dock and clears it too. That's true of this config's boundary and of
    any realistic physical dock shape (further from the dock = more
    headroom), but it's worth stating explicitly rather than leaving it
    implicit: a boundary shaped with a "notch" (allowed Z dipping back
    down at some larger Y) would need every point checked individually,
    not just the closest one per layer.

    Deliberately runs against the UNFILTERED, un-object-attributed
    positions trace -- purge lines, wipe tower moves, priming, and
    toolchange travel/Z-hop all still count, not just motion inside
    EXCLUDE_OBJECT blocks. Object attribution is a separate, purely
    visual concern (see collect_object_points) that must never narrow
    what this function considers.

    Same return interface as the exhaustive per-point version this
    replaces: (y, z, limit, clearance) for the worst point found, or
    None if nothing was ever within reach of the dock (safe_y).
    """
    by_z = group_min_y_by_z(positions)
    worst = None  # (clearance, y, z, limit)
    for z, y in by_z.items():
        limit = max_z_allowed(y, pts, safe_y)
        if limit == float("inf"):
            continue
        clearance = limit - z
        if worst is None or clearance < worst[0]:
            worst = (clearance, y, z, limit)
    if worst is None:
        return None
    clearance, y, z, limit = worst
    return y, z, limit, clearance


# ---------------------------------------------------------------------------
# Clearance-zone data layer -- purely descriptive, feeds the visualization
# only. NEVER used for the pass/fail decision above; find_closest_approach's
# single worst-point check remains the sole authority on that.
# ---------------------------------------------------------------------------

def classify_z_layers(positions: list, pts: list, safe_y: float, margin: float) -> dict:
    """
    Per-Z-layer version of the same classification find_closest_approach
    does for the single worst point -- for EVERY Z within reach of the
    dock (limit != inf), records min_y, max_y (both needed for zone
    outlines later), clearance, and a class: 'collision' (clearance < 0),
    'near_miss' (0 <= clearance <= margin), or 'safe'.

    Z values never within reach of the dock (limit == inf, i.e. even the
    closest point at that Z stayed at/beyond safe_y) are omitted
    entirely -- they're not part of any zone, safe or otherwise.

    Returns {z: {"min_y", "max_y", "clearance", "class"}}.
    """
    by_z = group_min_max_y_by_z(positions)
    out = {}
    for z, (min_y, max_y) in by_z.items():
        limit = max_z_allowed(min_y, pts, safe_y)
        if limit == float("inf"):
            continue
        clearance = limit - z
        if clearance < 0:
            cls = "collision"
        elif clearance <= margin:
            cls = "near_miss"
        else:
            cls = "safe"
        out[z] = {"min_y": min_y, "max_y": max_y, "clearance": clearance, "class": cls}
    return out


def build_clearance_zones(classified: dict, gap_merge_mm: float) -> list:
    """
    Groups classify_z_layers()'s per-Z data into zones: maximal runs of
    consecutive (in Z order) same-class entries, then optionally bridges
    two same-type zones separated by a single 'safe' zone, when the true
    Z-distance between the two same-type zones' own boundaries (not the
    safe zone's own internal span, which can be 0 for a single-point
    interruption) is no more than gap_merge_mm (see build_svg_payload's
    docs for a worked example of why). Only ever bridges 'safe' gaps -- a
    collision zone can never absorb a near_miss zone or vice versa, so
    the two types never overlap or get visually confused with each other.

    Returns a list of zones in Z order, each:
        {"type": "collision"|"near_miss"|"safe",
         "z_start", "z_end",
         "entries": [(z, min_y, max_y, clearance), ...] sorted by z}
    """
    if not classified:
        return []

    zs = sorted(classified.keys())

    # Pass 1: maximal runs of consecutive same-class Z values.
    raw = []
    current_type = None
    current_entries = []
    for z in zs:
        rec = classified[z]
        if rec["class"] != current_type:
            if current_entries:
                raw.append(_finish_zone(current_type, current_entries))
            current_type = rec["class"]
            current_entries = []
        current_entries.append((z, rec["min_y"], rec["max_y"], rec["clearance"]))
    if current_entries:
        raw.append(_finish_zone(current_type, current_entries))

    if gap_merge_mm <= 0:
        return raw

    # Pass 2: bridge collision-safe-collision / near_miss-safe-near_miss
    # triples where the safe zone's Z-span is <= gap_merge_mm. Repeat
    # until a full pass makes no changes, so a chain of several small
    # gaps (collision, safe, collision, safe, collision, ...) collapses
    # into one zone rather than needing to be hit twice.
    changed = True
    while changed:
        changed = False
        merged = []
        i = 0
        while i < len(raw):
            if (i + 2 < len(raw)
                    and raw[i]["type"] in ("collision", "near_miss")
                    and raw[i]["type"] == raw[i + 2]["type"]
                    and raw[i + 1]["type"] == "safe"
                    and (raw[i + 2]["z_start"] - raw[i]["z_end"]) <= gap_merge_mm):
                combined_entries = raw[i]["entries"] + raw[i + 1]["entries"] + raw[i + 2]["entries"]
                merged.append(_finish_zone(raw[i]["type"], combined_entries))
                i += 3
                changed = True
            else:
                merged.append(raw[i])
                i += 1
        raw = merged

    return raw


def _finish_zone(zone_type: str, entries: list) -> dict:
    z_start = entries[0][0]
    z_end = entries[-1][0]
    return {
        "type": zone_type,
        "z_start": z_start,
        "z_end": z_end,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# SVG visualization (unchanged — the part that's fine to be slow now)
# ---------------------------------------------------------------------------

def build_no_go_polygon(pts: list, safe_y: float, z_cap: float, y_min: float = 0.0):
    start_y = min(y_min, pts[0][0])
    poly = []
    if pts[0][0] > start_y:
        poly.append((start_y, pts[0][1]))
    poly.extend(pts)
    poly.append((safe_y, pts[-1][1]))
    poly.append((safe_y, z_cap))
    poly.append((start_y, z_cap))
    return poly


DISPLAY_MODE_TARGETS = {
    "off": [],
    "printer": ["printer"],
    "pc": ["pc"],
    "both": ["printer", "pc"],
}


def classify_status(is_violation: bool, clearance, margin: float) -> str:
    """
    Buckets the check result into exactly one of three named statuses,
    which is what svg.display's keys are keyed on. clearance is None
    when there was no motion in reach of the dock at all (still
    "no_collision", just with no meaningful near_miss check to speak of).
    """
    if is_violation:
        return "collision"
    if clearance is not None and clearance <= margin:
        return "near_miss"
    return "no_collision"


def resolve_targets(svg_cfg: dict, status: str) -> list:
    """
    Looks up svg.display[status] ('off' | 'printer' | 'pc' | 'both') and
    returns the destination list to embed on the payload itself as
    "targets". Unknown/missing values fall back to 'off' rather than
    silently defaulting to showing everything -- a typo in the config
    should mean "nothing shows", not "everything shows".
    """
    display_cfg = svg_cfg.get("display", {}) or {}
    mode = display_cfg.get(status, "off")
    return DISPLAY_MODE_TARGETS.get(mode, [])


def _shape_style(colors: dict, group: str, default_fill: str, default_stroke: str, default_fill_style: str):
    g = colors.get(group, {}) or {}
    return {
        "fill": g.get("fill", default_fill),
        "stroke": g.get("stroke", default_stroke),
        "fill_style": g.get("fill_style", default_fill_style),
    }


def build_marker_shapes(tc_cfg: dict, toolchange_points: list, pts: list, safe_y: float, failing_keys: set):
    """
    Turns collect_toolchange_points()'s output into "marker"-type shapes,
    filtered per tc_cfg["show"] ('off' | 'no_go_only' | 'all'). Filtering
    is per-POINT, not per-toolchange-event -- a toolchange point and its
    restore point are independent, and with 'no_go_only' one can show
    while the other doesn't. Reuses max_z_allowed(), the exact same test
    the actual collision check itself uses, so "under the no-go zone"
    means the same thing here as everywhere else in this script.

    failing_keys is a set of (tool, role) pairs that failed
    check_toolchange_hop_clearance() -- these are ALWAYS drawn and
    ALWAYS colored with tc_cfg["fail_color"], regardless of "show"
    (including "off"). A point that's the actual cause of a hard-fail
    export block isn't something the display config should be able to
    hide.

    Returns (marker_shapes, max_observed_y, max_observed_z) -- the two
    extents are for the caller to fold into the canvas size, since a
    'show: all' marker (or a forced-visible failing one) can easily sit
    outside whatever the no-go zone/object extent alone would have
    produced.
    """
    mode = tc_cfg.get("show", "off")
    if (mode == "off" or not toolchange_points) and not failing_keys:
        return [], None, None

    tc_style = tc_cfg.get("toolchange", {}) or {}
    rs_style = tc_cfg.get("restore", {}) or {}
    fail_color = tc_cfg.get("fail_color", "rgba(255,0,0,0.95)")
    role_defaults = {
        "toolchange": ("cross", "rgba(255,140,0,0.85)"),
        "restore": ("circle", "rgba(0,220,120,0.85)"),
    }

    shapes = []
    max_y = max_z = None
    for idx, entry in enumerate(toolchange_points):
        for role, style_cfg in (("toolchange", tc_style), ("restore", rs_style)):
            pt = entry.get(role)
            if pt is None:
                continue
            is_failing = (idx, role) in failing_keys
            py, pz = pt
            if not is_failing:
                if mode == "off":
                    continue
                if mode == "no_go_only" and not (py < safe_y and pz <= max_z_allowed(py, pts, safe_y)):
                    continue
            default_shape, default_color = role_defaults[role]
            shapes.append({
                "type": "marker",
                "x": round(py, 2), "y": round(pz, 2),
                "shape": style_cfg.get("shape", default_shape),
                "color": fail_color if is_failing else style_cfg.get("color", default_color),
                "size_px": style_cfg.get("size_px", 6),
            })
            max_y = py if max_y is None else max(max_y, py)
            max_z = pz if max_z is None else max(max_z, pz)

    return shapes, max_y, max_z


def build_zone_outline_polygon(entries: list) -> list:
    """
    Same technique as build_object_silhouette_polygon: walk the near-dock
    (min_y) edge up in ascending Z, cross to the far (max_y) edge at the
    top, walk back down in descending Z, close at the bottom. entries is
    a zone's (z, min_y, max_y, clearance) list, already Z-sorted.
    """
    if len(entries) < 1:
        return []
    up = [[round(min_y, 2), round(z, 2)] for z, min_y, _, _ in entries]
    down = [[round(max_y, 2), round(z, 2)] for z, _, max_y, _ in reversed(entries)]
    return up + down


def build_zone_shapes(zones: list, zones_cfg: dict) -> list:
    """
    Display layer: turns build_clearance_zones()'s data into shapes,
    independently configurable per zone type ('collision'/'near_miss').
    'safe' zones are pure connective tissue for the gap-merge logic and
    are never rendered here.

    Each type's config is {"mode": "off"|"highlight"|"outline",
    "highlight": {...}, "outline": {...}} -- mode picks which of the two
    style sub-configs (if either) actually gets used. 'highlight' draws
    a separate filled polygon over the zone's own min-Y/max-Y envelope.
    'outline' recolors the object's OWN silhouette edge over that
    Z-range instead of adding a shape on top of it -- drawn as two open
    polylines (near edge, far edge) capped with a third connecting them
    at the top only, open at the bottom where it blends straight into
    the rest of the object's own outline with no seam. Deliberately NOT
    closed into a full polygon the way 'highlight' is (that would add a
    bottom cap too, which isn't part of the object's real silhouette --
    its own edges continue past z_start, they don't cap off there). The
    open-bottom/capped-top shape reads like a spotlight beam converging
    onto the danger zone from above.
    """
    shapes = []
    for zone in zones:
        if zone["type"] not in ("collision", "near_miss"):
            continue
        type_cfg = zones_cfg.get(zone["type"], {}) or {}
        mode = type_cfg.get("mode", "off")
        if mode == "off":
            continue

        if mode == "highlight":
            style = type_cfg.get("highlight", {}) or {}
            poly = build_zone_outline_polygon(zone["entries"])
            if len(poly) >= 3:
                shapes.append({
                    "type": "polygon",
                    "points": poly,
                    "fill": style.get("fill", "rgba(255,230,0,0.35)"),
                    "stroke": style.get("stroke", "rgba(255,230,0,0.95)"),
                    "fill_style": style.get("fill_style", "solid"),
                })
        elif mode == "outline":
            style = type_cfg.get("outline", {}) or {}
            stroke = style.get("stroke", "rgba(255,230,0,0.95)")
            width_px = float(style.get("width_px", 1.5))
            near_edge = [[round(min_y, 2), round(z, 2)] for z, min_y, _, _ in zone["entries"]]
            far_edge = [[round(max_y, 2), round(z, 2)] for z, _, max_y, _ in zone["entries"]]
            for edge in (near_edge, far_edge):
                if len(edge) >= 2:
                    shapes.append({
                        "type": "path",
                        "points": edge,
                        "color": stroke,
                        "width_px": width_px,  # defaults to 1.5, matching every polygon's own fixed
                                               # stroke width, so a default-config edge reads as
                                               # recoloring that stretch, not a visibly thicker overlay
                    })
            # Capped at the TOP only (z_end), open at the bottom (z_start)
            # where it blends straight into the rest of the object's own
            # outline with no seam -- the open end reads as the zone
            # fading into the object, the capped end reads as a beam
            # converging onto it from above, like a spotlight.
            if near_edge and far_edge:
                shapes.append({
                    "type": "path",
                    "points": [near_edge[-1], far_edge[-1]],
                    "color": stroke,
                    "width_px": width_px,
                })
    return shapes


def build_svg_payload(cfg: dict, pts: list, safe_y: float, positions: list,
                       object_points: dict, support_points: dict, travel_segments: list,
                       targets: list, bed_extents, toolchange_points: list, tc_failing_keys: set):
    svg_cfg = cfg.get("svg", {}) or {}
    colors = svg_cfg.get("colors", {}) or {}
    canvas_clip = svg_cfg.get("canvas_clip", "normal")
    pa_cfg = svg_cfg.get("printable_area", {}) or {}
    travel_cfg = svg_cfg.get("travel_moves", {}) or {}

    if canvas_clip == "tight":
        # Only near-dock motion -- the original, most-zoomed-in behavior.
        # No more "runs"/spurious-connector concern here: the layer-based
        # silhouette buckets by Z regardless of chronological order, so
        # there's nothing for a simple point filter to accidentally join.
        filtered_object_points = {
            name: [(y, z) for y, z in pts_list if y < safe_y]
            for name, pts_list in object_points.items()
        }
        filtered_support_points = {
            name: [(y, z) for y, z in pts_list if y < safe_y]
            for name, pts_list in support_points.items()
        }
        # Keep any segment that touches the dock at all (either endpoint
        # inside safe_y), but -- unlike the object/support point filter,
        # which can just drop out-of-range points one at a time -- a
        # segment that CROSSES safe_y has to be clipped, not dropped,
        # or the far endpoint drags its full unclipped length back into
        # the render. Cap each Y coordinate at safe_y so the clipped
        # segment's far edge lands exactly on the dock boundary, the
        # same edge the no-go zone and canvas width are drawn at.
        filtered_travel_segments = [
            (min(y0, safe_y), z0, min(y1, safe_y), z1)
            for y0, z0, y1, z1 in travel_segments
            if y0 < safe_y or y1 < safe_y
        ]
    else:
        # "normal"/"full" both want each whole object's silhouette for
        # context, not just the sliver of motion near the dock.
        filtered_object_points = object_points
        filtered_support_points = support_points
        filtered_travel_segments = travel_segments

    all_used_points = (
        [pt for pts_list in filtered_object_points.values() for pt in pts_list]
        + [pt for pts_list in filtered_support_points.values() for pt in pts_list]
    )
    observed_max_y = max((y for y, _ in all_used_points), default=pts[-1][0])
    observed_max_z = max((z for _, z in all_used_points), default=pts[-1][1])

    z_cap = float(svg_cfg.get("z_cap", observed_max_z + 10.0))
    pad = float(svg_cfg.get("box_pad", 5.0))
    max_size = float(svg_cfg.get("max_size", 260))
    simplify_mm = float(svg_cfg.get("silhouette_simplify_mm", 0.15))

    no_go = build_no_go_polygon(pts, safe_y, z_cap)

    # One silhouette polygon per object/support, each object its own
    # auto-assigned hue; supports all share one fixed color (see
    # colors.support) regardless of which object they belong to -- they
    # read as "the same kind of thing" across the whole print, matching
    # how OrcaSlicer's own preview colors every support green regardless
    # of which body it's attached to. Dict insertion order (collect_
    # motion_data appends in first-seen order) makes the object color
    # assignment stable run-to-run for the same file.
    silhouettes = []
    for idx, (name, pts_list) in enumerate(filtered_object_points.items()):
        min_max_by_z = group_min_max_y_by_z(pts_list)
        poly = build_object_silhouette_polygon(min_max_by_z, simplify_mm)
        if len(poly) >= 3:
            silhouettes.append((name, poly))

    support_silhouettes = []
    for name, pts_list in filtered_support_points.items():
        min_max_by_z = group_min_max_y_by_z(pts_list)
        poly = build_object_silhouette_polygon(min_max_by_z, simplify_mm)
        if len(poly) >= 3:
            support_silhouettes.append((name, poly))

    if canvas_clip == "tight":
        # x_max is just safe_y -- everything in filtered_object_points is
        # < safe_y by construction, and the no-go polygon itself already
        # extends to safe_y, so that's the natural canvas width
        # regardless of whether the toolhead grazed right up against it.
        x_max = safe_y
        y_max = max(z_cap, observed_max_z)
    elif canvas_clip == "normal":
        # Widen to fit the whole object's observed extent, in addition to
        # the no-go zone itself.
        x_max = max(safe_y, observed_max_y)
        y_max = max(z_cap, observed_max_z)
    else:  # "full"
        fallback_y = float(pa_cfg.get("fallback_y_depth", 344.0))
        fallback_z = float(pa_cfg.get("fallback_z_height", 325.0))
        bed_y, bed_z = bed_extents if bed_extents is not None else (fallback_y, fallback_z)
        # max() with the observed extents too, defensively -- an object
        # or no-go zone should never exceed the bed, but a bad/fallback
        # bed reading should never be allowed to clip real geometry.
        # This is purely a canvas-sizing input -- no outline is drawn
        # for the bed boundary itself.
        x_max = max(safe_y, observed_max_y, bed_y)
        y_max = max(z_cap, observed_max_z, bed_z)

    no_go_style = _shape_style(colors, "no_go", "rgba(255,60,60,0.30)", "rgba(255,60,60,0.9)", "dithered")

    # Travel is drawn as a filled polygon -- the actual Y span the moves
    # cross by the actual Z span they cover, thickened up to
    # TRAVEL_SILHOUETTE_HEIGHT_MM if that Z span is thinner than that (a
    # level, non-hopping travel move would otherwise collapse to a
    # zero-height sliver). The thickening always grows UPWARD from the
    # move's own min Z, never down -- that's the direction the toolhead
    # body/gantry actually occupies above the nozzle tip, so it reads as
    # "the real swept clearance volume", not an arbitrary centered pad.
    # This also means travel renders as an ordinary "polygon" shape, so
    # it picks up on-printer (Klipper _SVG_TOOLS) rendering for free --
    # that macro doesn't implement a line-only "path" kind.
    #
    # Built as one silhouette polygon PER TRACK, not one shape per travel
    # segment -- a real print can have thousands of travel segments,
    # and drawing each as its own rectangle made travel routinely >99%
    # of the SVG payload's bytes for no visual gain, since overlapping/
    # adjacent segments (the normal case -- travel happens on nearly
    # every layer) just paint over each other anyway. Getting the
    # grouping right takes two passes: cluster_travel_segments_by_y
    # groups by Y alone (coarse, but robust -- correctly keeps physically
    # disjoint regions like two front-of-bed objects and two back-of-bed
    # objects, with different tools and no travel ever connecting them,
    # as separate clusters). But a Y-only pass can't see that ONE
    # resulting cluster might still internally be two Z-disjoint
    # sub-regions that only start overlapping in Y partway up the print
    # (e.g. two objects sharing no travel for most of their height, but
    # sharing a tool -- and therefore direct connecting travel -- for
    # their last few layers). split_cluster_by_z_structure checks each
    # cluster for that specific pattern, using run-length smoothing so it
    # only splits on a SUSTAINED structural change, not the ordinary
    # per-layer jitter real complex geometry produces.
    travel_shapes = []
    if travel_cfg.get("show", False) and filtered_travel_segments:
        travel_fill = travel_cfg.get("fill", "rgba(200,200,200,0.20)")
        travel_stroke = travel_cfg.get("stroke", "rgba(200,200,200,0.55)")
        travel_fill_style = travel_cfg.get("fill_style", "solid")
        cluster_gap_mm = compute_auto_cluster_gap_mm(filtered_object_points, filtered_support_points)

        def _travel_polygon_shape(min_max_by_z):
            poly = build_object_silhouette_polygon(min_max_by_z, simplify_mm)
            if len(poly) < 3:
                return None
            return {
                "type": "polygon",
                "points": poly,
                "fill": travel_fill,
                "stroke": travel_stroke,
                "fill_style": travel_fill_style,
            }

        # Real per-layer Z's (extrusion-only, so exactly one Z per layer,
        # no Z-hop involved) -- passed through so ramped/sloped Z-hop
        # travel waypoints snap back onto the layer they actually belong
        # to instead of fragmenting into their own near-duplicate Z
        # buckets. See split_cluster_by_z_structure's docstring.
        layer_zs = sorted(set(round(z, Z_ROUND_DECIMALS) for _, z in all_used_points))

        all_travel_y = []
        all_travel_z = []
        y_clusters, y_connectors = cluster_travel_segments_by_y(
            filtered_travel_segments, cluster_gap_mm
        )
        tracks = []
        for y_cluster in y_clusters:
            tracks.extend(split_cluster_by_z_structure(
                y_cluster, cluster_gap_mm, layer_zs=layer_zs
            ))
        for y0, z0, y1, z1 in y_connectors:
            y_lo, y_hi = (y0, y1) if y0 <= y1 else (y1, y0)
            z_lo, z_hi = (z0, z1) if z0 <= z1 else (z1, z0)
            if z_hi - z_lo < TRAVEL_SILHOUETTE_HEIGHT_MM:
                z_hi = z_lo + TRAVEL_SILHOUETTE_HEIGHT_MM
            tracks.append({round(z_lo, Z_ROUND_DECIMALS): (y_lo, y_hi),
                            round(z_hi, Z_ROUND_DECIMALS): (y_lo, y_hi)})

        for track_min_max_by_z in tracks:
            if not track_min_max_by_z:
                continue
            shape = _travel_polygon_shape(track_min_max_by_z)
            if shape is not None:
                travel_shapes.append(shape)
            all_travel_y.extend(y for lo, hi in track_min_max_by_z.values() for y in (lo, hi))
            all_travel_z.extend(track_min_max_by_z.keys())

        if all_travel_y and canvas_clip != "tight":
            # "tight" already pins x_max to safe_y/z_cap on purpose (see
            # above) -- letting travel grow the canvas here would silently
            # undo that clip the moment any travel segment reached past
            # the dock boundary, which is exactly the motion tight mode
            # exists to crop out.
            x_max = max(x_max, max(all_travel_y))
            y_max = max(y_max, max(all_travel_z))

    tc_cfg = svg_cfg.get("toolchange_markers", {}) or {}
    marker_shapes, marker_max_y, marker_max_z = build_marker_shapes(tc_cfg, toolchange_points, pts, safe_y, tc_failing_keys)
    if marker_max_y is not None:
        # A 'show: all' marker can easily sit outside the no-go
        # zone/object extent alone -- expand to fit rather than let
        # points silently render off-canvas.
        x_max = max(x_max, marker_max_y)
        y_max = max(y_max, marker_max_z)

    shapes = []
    shapes.append({
        "type": "polygon",
        "points": [[round(y, 2), round(z, 2)] for y, z in no_go],
        **no_go_style,
    })

    # Travel next -- drawn UNDER supports/objects on purpose, so it never
    # washes back over them regardless of color/opacity (a translucent
    # layer drawn LAST would visually re-create the exact bleeding
    # problem this whole thing exists to fix, just relabeled).
    shapes.extend(travel_shapes)

    # Supports before their object -- we only care that a support pokes
    # out beyond the object it's holding up (that's the actual extra
    # collision risk), and the object itself should always read clearly
    # on top rather than fighting with support geometry for attention.
    support_style = _shape_style(colors, "support", "rgba(80,200,80,0.30)", "rgba(80,200,80,0.9)", "solid")
    for name, poly in support_silhouettes:
        shapes.append({"type": "polygon", "points": poly, **support_style})

    # Single and multi-object cases now go through the exact same
    # coloring path -- object_silhouette_color(0, ...) is what a single
    # object gets, using colors.silhouette.color as its exact hue (see
    # that function's docstring). No more special-cased single-object
    # style, so there's nothing for the two cases to visually diverge on.
    avoid_bands = derive_avoid_hue_bands(svg_cfg)
    palette_fill_style = colors.get("silhouette", {}).get("fill_style", "solid")
    for idx, (name, poly) in enumerate(silhouettes):
        fill, stroke = object_silhouette_color(idx, colors, avoid_bands)
        shapes.append({
            "type": "polygon", "points": poly,
            "fill": fill, "stroke": stroke, "fill_style": palette_fill_style,
        })

    margin = float(svg_cfg.get("near_miss_margin", 5.0))
    classified = classify_z_layers(positions, pts, safe_y, margin)
    zones_cfg = svg_cfg.get("clearance_zones", {}) or {}
    gap_merge_mm = float(zones_cfg.get("gap_merge_mm", 1.0))
    zones = build_clearance_zones(classified, gap_merge_mm)
    shapes.extend(build_zone_shapes(zones, zones_cfg))

    shapes.extend(marker_shapes)

    return {
        "title": svg_cfg.get("title", "StealthChanger dock check"),
        "canvas": {
            "x_max": round(x_max, 2),
            "y_max": round(y_max, 2),
            "pad": pad,
            "max_size": max_size,
        },
        "shapes": shapes,
        "targets": targets,
    }


# ---------------------------------------------------------------------------
# Notice emission -- the generic mechanism the printer-side macro reacts
# to. This script has zero special-cased knowledge on the printer side
# anymore: it just prints NOTICE:{...} to stdout like any other processor
# could, and OrcaStrator/macro handle severity levels generically.
# ---------------------------------------------------------------------------

# Set once check_file() has loaded this processor's own config -- see
# helpers/notice.py's docstring. Starts empty (reads as "display on",
# the default) so nothing before that point is ever embedded with
# display=false by a config it hasn't read yet. "abort" (the
# dock-collision case itself) ignores this entirely regardless --
# see helpers/notice.py.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


_CANCEL_BLOCK = """\
; =========================================================
; STEALTHCHANGER DOCK COLLISION CHECK -- UNSAFE PRINT
; Z={z:.2f}mm at Y={y:.2f}mm exceeds dock limit of {limit:.2f}mm{object_note}
; Detected at slicer export time. This file should never have reached
; the printer -- if you are seeing this fire on the printer, the
; STEALTHCHANGER_RENDER pre-print gate was bypassed.
; =========================================================
RESPOND TYPE=error MSG="StealthChanger: dock collision risk! Z={z:.2f} at Y={y:.2f} exceeds limit {limit:.2f}mm{object_note}. Print cancelled."
CANCEL_PRINT_BASE
; =========================================================
"""


def insert_at_top(lines: list, block_lines: list) -> list:
    """
    Inserts block_lines before the first non-comment, non-empty line, so
    each block sits with the other header comments but ahead of any real
    motion command. Safe to call more than once in sequence -- later
    calls will simply insert after whatever comment blocks are already
    there.
    """
    insert_at = 0
    for idx, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped and not stripped.startswith(";"):
            insert_at = idx
            break
    return lines[:insert_at] + block_lines + lines[insert_at:]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check_file(gcode_path: str) -> None:
    script_path = pathlib.Path(__file__).resolve()
    p = pathlib.Path(gcode_path)

    if not p.exists():
        print(f"[dock_collision] File not found: {p}", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = load_config(script_path)
        global _notice_cfg
        _notice_cfg = cfg
        pts, safe_y = build_boundary(cfg)
    except Exception as exc:
        print(f"[dock_collision] Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    motion = collect_motion_data(lines)
    positions = motion["positions"]

    approach = find_closest_approach(positions, pts, safe_y)
    is_violation = approach is not None and approach[3] < 0
    clearance = approach[3] if approach is not None else None
    violation = (approach[0], approach[1], approach[2]) if is_violation else None

    # Object attribution for whatever the closest-approach point is,
    # violation or not -- purely descriptive, never consulted for the
    # pass/fail decision. Computed unconditionally (not just when there's
    # a violation) so it always ends up in the debug dump below, even on
    # an unremarkable OK run -- "why didn't this attribute an object"
    # is just as answerable from that as "why did this abort".
    approach_owner_name = None
    approach_owner_is_support = False
    if approach is not None:
        owner_map = group_min_y_by_z_with_owner(motion["position_owners"])
        _, approach_owner_name, approach_owner_is_support = owner_map.get(
            round(approach[1], Z_ROUND_DECIMALS), (None, None, False)
        )

    # Priority-based attribution: object > support > toolchange > travel.
    # Each of these is checked independently against each owner's OWN
    # points (see worst_violation_by_owner), rather than just trusting
    # whichever point happened to be the single global worst -- an
    # object can be sitting in the collision zone even when some other,
    # more severe point (a travel move, a toolchange hop) is what
    # find_closest_approach() picked out above. object_violation/
    # support_violation can only be non-None when is_violation is True,
    # since each owner's points are a strict subset of the full trace
    # find_closest_approach() already ran against.
    object_violation = worst_violation_by_owner(motion["object_points"], pts, safe_y) if is_violation else None
    support_violation = worst_violation_by_owner(motion["support_points"], pts, safe_y) if is_violation else None

    svg_cfg = cfg.get("svg", {}) or {}
    margin = float(svg_cfg.get("near_miss_margin", 5.0))

    # Toolchange hop-clearance check -- independent of, and in addition
    # to, the main observed-motion check above. See
    # check_toolchange_hop_clearance()'s docstring for why this catches
    # something the main check structurally cannot: the hypothetical
    # peak Z during a hop that's synthesized by the printer's own macro
    # at print time and never appears as a literal commanded coordinate
    # in this file.
    hop_cfg = svg_cfg.get("toolchange_collision_guard", {}) or {}
    hop_enabled = hop_cfg.get("enabled", True)
    tc_markers_cfg = svg_cfg.get("toolchange_markers", {}) or {}
    toolchange_points = []
    if hop_enabled or tc_markers_cfg.get("show", "off") != "off":
        toolchange_points = collect_toolchange_points(lines)

    toolchange_min_z_clearance = None
    tc_worst_entry = None
    tc_failing_keys = set()
    tc_all_checked = []
    toolchange_hop_mm = float(hop_cfg.get("toolchange_hop_mm", 0.0))
    restore_hop_mm = float(hop_cfg.get("restore_hop_mm", 10.0))
    if hop_enabled and toolchange_points:
        toolchange_min_z_clearance, tc_worst_entry, tc_failing_keys, tc_all_checked = check_toolchange_hop_clearance(
            toolchange_points, pts, safe_y, toolchange_hop_mm, restore_hop_mm,
        )
    toolchange_hard_fail = toolchange_min_z_clearance is not None and toolchange_min_z_clearance < 0

    overall_is_violation = is_violation or toolchange_hard_fail
    # Status/target resolution otherwise follows the main check's own
    # clearance exactly as before -- a toolchange-only failure simply
    # forces "collision" regardless of what the main check alone would
    # have classified this as.
    status = "collision" if overall_is_violation else classify_status(False, clearance, margin)
    targets = resolve_targets(svg_cfg, status)

    debug_cfg = cfg.get("debug", {}) or {}
    debug_data = {
        "file": friendly_filename(p),
        "config": {
            "safe_y": safe_y,
            "boundary": pts,
            "near_miss_margin": margin,
            "toolchange_collision_guard": {
                "enabled": hop_enabled,
                "toolchange_hop_mm": toolchange_hop_mm,
                "restore_hop_mm": restore_hop_mm,
            },
        },
        "counts": {
            "positions": len(positions),
            "toolchange_events": len(toolchange_points),
            "objects": list(motion["object_points"].keys()),
            "objects_with_support": [name for name, pts_ in motion["support_points"].items() if pts_],
            "travel_segments": len(motion["travel_segments"]),
            "travel_z_range": (
                [min(min(z0, z1) for _, z0, _, z1 in motion["travel_segments"]),
                 max(max(z0, z1) for _, z0, _, z1 in motion["travel_segments"])]
                if motion["travel_segments"] else None
            ),
            "object_z_range": {
                name: [min(z for _, z in pts_), max(z for _, z in pts_)]
                for name, pts_ in motion["object_points"].items() if pts_
            },
        },
        "main_check": {
            "is_violation": is_violation,
            "clearance": clearance,
            "closest_approach": (
                {"y": approach[0], "z": approach[1], "limit": approach[2]} if approach is not None else None
            ),
            "closest_approach_owner": approach_owner_name,
            "closest_approach_is_support": approach_owner_is_support,
            "object_violation": (
                {"owner": object_violation[0], "y": object_violation[1], "z": object_violation[2],
                 "limit": object_violation[3], "clearance": object_violation[4]}
                if object_violation is not None else None
            ),
            "support_violation": (
                {"owner": support_violation[0], "y": support_violation[1], "z": support_violation[2],
                 "limit": support_violation[3], "clearance": support_violation[4]}
                if support_violation is not None else None
            ),
        },
        "toolchange_collision_guard": {
            "enabled": hop_enabled,
            "toolchange_min_z_clearance": toolchange_min_z_clearance,
            "worst_entry": tc_worst_entry,
            "failing_count": len(tc_failing_keys),
            "all_checked": tc_all_checked,
        },
        "decision": {
            "overall_is_violation": overall_is_violation,
            "status": status,
            "targets": targets,
        },
    }

    svg_payload = None
    if targets:
        bed_extents = None
        if svg_cfg.get("canvas_clip", "normal") == "full":
            bed_extents = parse_bed_extents(lines)
        svg_payload = build_svg_payload(
            cfg, pts, safe_y, positions, motion["object_points"], motion["support_points"],
            motion["travel_segments"], targets, bed_extents, toolchange_points, tc_failing_keys,
        )

    if overall_is_violation:
        # Priority order when multiple things are simultaneously in the
        # collision zone: object body > support material > toolchange
        # hop > travel. Each category is checked independently (see
        # object_violation/support_violation above) rather than just
        # reporting whichever single point happened to have the worst
        # clearance -- an object sitting in the zone should never be
        # reported as "a toolchange" just because some other point was
        # numerically worse.
        if object_violation is not None:
            priority_category = "object"
            name, y, z, limit, _clearance = object_violation
            pretty_name = friendly_object_name(name) if name else None
            object_descriptor = f"'{pretty_name}'" if pretty_name else "the object"
            location_phrase = f"near {object_descriptor}"
            object_note = f" (near {object_descriptor})"

            stderr_msg = (
                f"[dock_collision] COLLISION DETECTED {location_phrase}: "
                f"Z={z:.2f}mm at Y={y:.2f}mm (limit at that Y: {limit:.2f}mm)"
            )
            abort_msg = (
                f"Dock clearance exceeded {location_phrase} -- see the visualization "
                f"for the exact location. Export blocked."
            )
        elif support_violation is not None:
            priority_category = "support"
            name, y, z, limit, _clearance = support_violation
            pretty_name = friendly_object_name(name) if name else None
            object_descriptor = f"'{pretty_name}' support material" if pretty_name else "support material"
            location_phrase = f"near {object_descriptor}"
            object_note = f" (near {object_descriptor})"

            stderr_msg = (
                f"[dock_collision] COLLISION DETECTED {location_phrase}: "
                f"Z={z:.2f}mm at Y={y:.2f}mm (limit at that Y: {limit:.2f}mm)"
            )
            abort_msg = (
                f"Dock clearance exceeded {location_phrase} -- see the visualization "
                f"for the exact location. Export blocked."
            )
        elif toolchange_hard_fail:
            priority_category = "toolchange"
            # No object/support point of its own is in the zone -- this
            # is a toolchange-hop-only failure. No object attribution
            # attempted here: the worst_entry's Z is a hypothetical
            # (hopped) height that never actually appears in the file's
            # own motion trace, so looking it up against
            # position_owners would either find nothing or, worse,
            # coincidentally match an unrelated point. Tool number +
            # role is already a precise pointer to the exact g-code
            # location (search for "T{tool}"), which is arguably more
            # actionable than a fuzzy object name here.
            y, z, limit = tc_worst_entry["y"], tc_worst_entry["z"], tc_worst_entry["limit"]
            tool = tc_worst_entry["tool"]
            role = tc_worst_entry["role"]
            hop_mm = toolchange_hop_mm if role == "toolchange" else restore_hop_mm
            role_label = "restore position" if role == "restore" else "toolchange position"
            object_note = ""

            stderr_msg = (
                f"[dock_collision] TOOLCHANGE HOP CLEARANCE EXCEEDED: T{tool} {role_label}, "
                f"assuming a {hop_mm:.1f}mm hop, reaches Z={z:.2f}mm at Y={y:.2f}mm "
                f"(limit at that Y: {limit:.2f}mm, clearance {toolchange_min_z_clearance:.2f}mm)"
            )
            abort_msg = (
                f"T{tool}'s {role_label} -- assuming a {hop_mm:.1f}mm hop -- would exceed the "
                f"dock clearance limit. Export blocked."
            )
        else:
            priority_category = "travel"
            # Lowest priority: none of object/support/toolchange have a
            # violation of their own, so whatever the main check tripped
            # on is a bare travel move, purge/prime, or object labeling
            # isn't enabled -- an honest "don't know", not a guess.
            y, z, limit = violation
            location_phrase = "at an unlabeled/travel location"
            object_note = ""

            stderr_msg = (
                f"[dock_collision] COLLISION DETECTED {location_phrase}: "
                f"Z={z:.2f}mm at Y={y:.2f}mm (limit at that Y: {limit:.2f}mm)"
            )
            abort_msg = (
                f"Dock clearance exceeded {location_phrase} -- see the visualization "
                f"for the exact location. Export blocked."
            )

        # CANCEL_PRINT_BASE injection stays -- this is this processor's
        # OWN defense-in-depth choice (a domain-specific safety action),
        # separate from the generic NOTICE mechanism below. The printer
        # doesn't need to know why it's there, only that an "abort"
        # notice means "don't start this print".
        cancel_block = _CANCEL_BLOCK.format(y=y, z=z, limit=limit, object_note=object_note).splitlines()
        new_lines = insert_at_top(lines, cancel_block)

        print(stderr_msg, file=sys.stderr)
        p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        if svg_payload is not None:
            print("SVG_PAYLOAD:" + json.dumps(svg_payload, separators=(",", ":")))
            if "pc" in targets:
                show_svg_on_pc(svg_payload, svg_cfg, friendly_filename(p))
        print_notice("abort", "StealthChanger dock collision", abort_msg)
        debug_data["messages"] = {"stderr": stderr_msg, "notice": abort_msg}
        debug_data["decision"]["priority_category"] = priority_category
        _write_debug_dump("dock_collision_guard", debug_cfg, debug_data, script_path)
        sys.exit(1)  # tells OrcaSlicer this post-processing step failed

    print("[dock_collision] OK -- no dock collisions detected.", file=sys.stdout)
    if svg_payload is not None:
        print("SVG_PAYLOAD:" + json.dumps(svg_payload, separators=(",", ":")))
        if "pc" in targets:
            show_svg_on_pc(svg_payload, svg_cfg, friendly_filename(p))
    info_msg = f"{friendly_filename(p)}: no dock collisions detected."
    print_notice("info", "Dock check OK", info_msg)
    debug_data["messages"] = {"stderr": "[dock_collision] OK -- no dock collisions detected.", "notice": info_msg}
    _write_debug_dump("dock_collision_guard", debug_cfg, debug_data, script_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {pathlib.Path(__file__).name} <gcode_file>", file=sys.stderr)
        sys.exit(1)
    # OrcaSlicer appends the output file path as the LAST argument, not
    # necessarily the only one (extra params can precede it if you added
    # any in the Post-processing Scripts field).
    check_file(sys.argv[-1])
