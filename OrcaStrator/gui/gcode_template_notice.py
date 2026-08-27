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
Settings-form spec for configs/gcode_template_notice.json.

KIND="rich" here means the same one thing it means for
gui/dock_collision_guard.py: a live preview. The difference is what kind
-- this processor's own build_preview_payload() below returns a
{"kind": "text", ...} payload instead of an SVG_PAYLOAD-shaped one, which
config_editor.pyw's refresh_preview() renders as a plain read-only text
box instead of feeding it into orcastrator.py's SVG canvas renderer (see
that generic branch in refresh_preview() -- it has no idea this is
template text specifically, just that "kind": "text" means "show this
string", same spirit as the SVG path having no idea what a collision
zone is).

The templates list itself is the generic "template_list" field kind
(see config_editor.pyw's _add_field()) -- name + multiline template text
+ destination checkboxes per row, add/remove, nothing dock-collision-
specific about it.

No PREVIEW_CONTROLS: the preview always renders EVERY configured
template at once, against one bundled synthetic sample g-code (SAMPLE_
LINES below) -- there's no meaningful "scenario" to pick the way dock
collision's no-collision/near-miss/collision buttons pick one, since a
template's whole job is to work across whatever real file eventually
runs it, not one specific case.
"""
import sys as _sys
import pathlib as _pathlib

_GUI_DIR = _pathlib.Path(__file__).resolve().parent
if str(_GUI_DIR) not in _sys.path:
    _sys.path.insert(0, str(_GUI_DIR))
from _plugin_support import get_in  # noqa: F401  (kept for parity/future use, same as other rich gui/*.py modules)

# _plugin_support's own import already put post_processors/ on sys.path
# (needed for `from helpers.X import ...` to resolve the same way it
# does when a processor runs standalone) -- see its docstring.
from helpers.placeholders import build_namespace, evaluate_condition, render_template, placeholder_catalog

TITLE = "G-code Template Notice"
SUBTITLE = "User-authored template strings rendered from this file's CONFIG_BLOCK, with a live preview."
CONFIG = "gcode_template_notice.json"
KIND = "rich"
HAS_PREVIEW = True
PREVIEW_CONTROLS = []

# Wider settings column -- open_stealthchanger_editor()'s default
# (minsize=440, see config_editor.pyw) is sized for dock_collision_
# guard's short numeric fields; a multiline template editor genuinely
# needs more room, since nothing else in the column would otherwise
# ask for more width for the template text box to render into. This is
# a floor, not a stretch target -- the column still won't grow further
# just because the window's wider (config_editor.pyw's settings scroll
# area sizes to its content's natural width, not the available canvas,
# same as every other rich config's settings column).
SETTINGS_MIN_WIDTH = 660

# A small, REAL sample file (trimmed from an actual export: every T<n>
# toolchange line plus the full, unedited CONFIG_BLOCK -- see
# gcode_template_notice_sample.gcode next to this module) rather than a
# hand-typed synthetic one. This is what the preview and the placeholder
# reference list below both render against, since the editor has no real
# g-code file of its own to read -- using a real CONFIG_BLOCK means the
# placeholder list reflects actual OrcaSlicer key names/shapes (including
# the comma-joined per-extruder ones) instead of a guess at what one
# might look like.
_SAMPLE_PATH = _GUI_DIR / "gcode_template_notice_sample.gcode"
SAMPLE_LINES = _SAMPLE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()

SECTIONS = [
    ("Templates", [
        dict(kind="template_list", label="Templates", path=("templates",),
             destination_options=[("notice", "Printer notice"), ("gcode_comment", "G-code comment"),
                                   ("abort", "Abort print")],
             add_label="+ Add template",
             tooltip="{expr} placeholders draw from this file's CONFIG_BLOCK (any key OrcaSlicer wrote) "
                     "plus computed values like total_number_toolchanges. {let name = expr} computes a "
                     "value once for reuse later in the same template. \"Only if:\" is an optional "
                     "condition (same grammar, no braces) that gates the whole template -- pair it with "
                     "the \"Abort print\" destination to watch a placeholder and refuse to print only "
                     "when it trips (e.g. curr_bed_type != \"High Temp Plate\"), staying silent "
                     "otherwise. See the live "
                     "preview -> for the full placeholder list and a resolved example, updated as you "
                     "type."),
    ]),
]


def build_preview_payload(cfg: dict, _controls: dict) -> dict:
    """Two separate fields, resolved preview above placeholder reference
    (config_editor.pyw's "sections" shape -- see its refresh_preview),
    rather than one blob a plugin has to visually fake a split inside
    of. controls is unused (no PREVIEW_CONTROLS) but still accepted --
    config_editor.pyw always calls build_fn(cfg, controls) regardless of
    whether a given plugin has any controls of its own."""
    namespace = build_namespace(SAMPLE_LINES)
    catalog = placeholder_catalog(SAMPLE_LINES)

    preview_lines = []
    templates = cfg.get("templates") or []
    if not templates:
        preview_lines.append("(no templates configured yet -- add one on the left)")
    for tmpl in templates:
        if not isinstance(tmpl, dict):
            continue
        name = tmpl.get("name") or "template"
        text = tmpl.get("text") or ""
        raw_dests = tmpl.get("destinations") or []
        dests = ", ".join(raw_dests) or "(no destination selected)"
        if "abort" in raw_dests:
            dests += "  \u26a0 REFUSES THE PRINT"
        preview_lines.append(f"[{name}] -> {dests}")

        condition_src = tmpl.get("condition") or ""
        if condition_src.strip():
            passed, cond_error = evaluate_condition(condition_src, namespace)
            if cond_error:
                preview_lines.append(f"  only if: {condition_src}  ->  \u26a0 {cond_error}")
                preview_lines.append("  (broken condition -- never fires, against the sample file)")
                preview_lines.append("")
                continue
            preview_lines.append(f"  only if: {condition_src}  ->  "
                                  f"{'true' if passed else 'false'} (against the sample file)")
            if not passed:
                preview_lines.append("  (condition false -- this template doesn't fire on the sample file)")
                preview_lines.append("")
                continue

        rendered, errors = render_template(text, namespace)
        preview_lines.append(f"  {rendered}")
        for err in errors:
            preview_lines.append(f"  \u26a0 {err}")
        preview_lines.append("")

    # catalog is already computed-first then config_block (sorted) --
    # see placeholder_catalog()'s own docstring -- so a single "category
    # just changed" check is enough to drop the divider in the one spot
    # between the two groups, without hard-coding either group's name.
    placeholder_lines = []
    prev_category = None
    for name, desc, category in catalog:
        if prev_category is not None and category != prev_category:
            placeholder_lines.append("-" * 40)
        prev_category = category
        placeholder_lines.append(f"  {{{name}}}  [computed]  {desc}" if category == "computed" else f"  {{{name}}}")
    placeholder_lines.append("")
    placeholder_lines.append("Helper functions: time_format(seconds), round(x, n), number_format(x, decimals), "
                              "pluralize(n, singular, plural=None), int(x), abs(x), min(...), max(...)")
    placeholder_lines.append("Also: {let name = expr} computes once and reuses that value in every {...} block "
                              "after it in the SAME template -- doesn't render anything itself.")
    placeholder_lines.append("\"Only if:\" takes the same grammar without the braces (e.g. "
                              "curr_bed_type != \"High Temp Plate\") and gates the whole template -- "
                              "pair it with the \"Abort print\" destination to refuse a print only "
                              "when it trips.")

    return {
        "kind": "text",
        "sections": [
            # "scroll": False pins this one in place (config_editor.pyw's
            # preview_fixed_holder) -- it's a handful of lines per
            # template, always short, and the one a user is actively
            # iterating on while they type, so it shouldn't disappear
            # off-screen once the (much longer, alphabetical) CONFIG_
            # BLOCK placeholder list below it needs to scroll.
            {"title": "Resolved preview (against the sample file, not a real export)",
             "text": "\n".join(preview_lines).rstrip(), "scroll": False},
            {"title": "Available placeholders (from the sample file below) "
                      "-- computed above the divider, this file's CONFIG_BLOCK keys below",
             "text": "\n".join(placeholder_lines).rstrip()},
        ],
    }
