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
gcode_template_notice.py -- OrcaSlicer post-processing script
=========================================================================
Renders user-authored template strings (see configs/gcode_template_notice
.json) against placeholders drawn from this file's own CONFIG_BLOCK --
every "; key = value" resolved-setting comment OrcaSlicer writes, ANY key,
not a curated subset -- plus a small set of computed placeholders
(total_number_toolchanges today, more later). Supports basic arithmetic /
comparisons / a ternary / function calls inside {...}, via helpers/
placeholders.py's small whitelist expression language -- deliberately not
Python eval, see that module's docstring for why.

Example template text (see the config for the actual JSON shape):
    Time spent on toolchanges: {time_format(machine_tool_change_time * total_number_toolchanges)}

Each configured template independently targets one or more destinations:
  - "notice": a NOTICE (see CLAUDE.md's stdout conventions) -- shown as a
    plain console message on the printer.
  - "gcode_comment": inserted as a plain "; <name>: <rendered>" line near
    the top of the file, inside its own OT_TEMPLATE_START/END block --
    deliberately separate markers from orcastrator.py's own
    ORCASTRATOR_LOG block, so the two can never collide or be mistaken
    for one another.
  - "abort": an "abort"-level NOTICE (see CLAUDE.md's NOTICE levels) --
    the SAME generic mechanism dock_collision_guard.py uses to refuse a
    print, just triggered here by a user-authored template instead of a
    built-in collision check. Always shows on the console regardless of
    this processor's own "notice": {"display": ...} setting (an abort
    is never something you'd want silently muted -- see helpers/
    notice.py), and OrcaStrator_render.cfg's action_raise_error fires
    on it exactly the same way, no macro-side changes needed for this
    to work.

A template can also carry an optional "condition": a boolean expression
(same {expr} grammar, just without the braces -- e.g.
"curr_bed_type != 'High Temp Plate'") gating the WHOLE template, every destination at
once. No condition (missing or blank) means "always fires". This is what
makes "abort" useful for the "watch a placeholder, refuse to print if
it's wrong" case: give the "abort" template a condition and it stays
silent on every normal print, only firing (and only rendering
"message") on the one that actually trips it. A condition that fails
to evaluate (unknown placeholder, a typo) is treated as false and
never fires -- see helpers/placeholders.py's evaluate_condition() --
plus a "warning" NOTICE surfaces the broken condition itself, so a
typo is visible rather than either silently never firing OR (worse,
for "abort") silently blocking every print.

A template TEXT that fails to fully resolve (unknown placeholder, a bad
expression, division by zero, whatever) never blocks the print on its
own -- this is informational metadata, not a correctness check, same
spirit as toolchange_heatmap.py. The unresolved piece renders inline as
<ERR:the_expression> and a "warning" NOTICE is emitted too, so the
problem is still visible rather than silently swallowed. (An "abort"
destination's own message can still render with an <ERR:...> piece in
it this way -- the print is refused because "abort" was selected and
the condition passed, not because the text happened to resolve
cleanly.)

No templates configured -> this processor does nothing at all (not even
touch the file), same "off by default, opt-in" shape as every other
processor here.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from helpers.notice import display_flag as _notice_display_flag
from helpers.placeholders import build_namespace, evaluate_condition, render_template
from helpers.time_estimator import find_config_path

SCRIPT_PATH = pathlib.Path(__file__).resolve()

TEMPLATE_START = "; OT_TEMPLATE_START"
TEMPLATE_END = "; OT_TEMPLATE_END"

DEFAULTS = {
    "templates": [],
    "notice": {},
}

# Set once process() has loaded this processor's own config -- same
# "starts empty, reads as display-on" reasoning as dock_collision_
# guard.py's own _notice_cfg (see helpers/notice.py's docstring).
# "abort" ignores this entirely regardless -- it always displays.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


def friendly_filename(p: pathlib.Path) -> str:
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["templates"] = list(DEFAULTS["templates"])
    cfg["notice"] = dict(DEFAULTS["notice"])
    path = find_config_path("gcode_template_notice.json", SCRIPT_PATH)
    if path is None:
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    if isinstance(raw.get("templates"), list):
        cfg["templates"] = raw["templates"]
    if isinstance(raw.get("notice"), dict):
        cfg["notice"] = raw["notice"]
    return cfg


def process(gcode_path: str) -> None:
    global _notice_cfg
    p = pathlib.Path(gcode_path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    cfg = load_config()
    _notice_cfg = cfg
    templates = cfg.get("templates") or []
    if not templates:
        return

    namespace = build_namespace(lines)

    comment_lines = []
    abort_fired = False

    for tmpl in templates:
        if not isinstance(tmpl, dict):
            continue
        name = str(tmpl.get("name") or "template").strip() or "template"
        raw_text = tmpl.get("text") or ""
        destinations = tmpl.get("destinations") or ["notice"]
        if not raw_text.strip():
            continue

        condition_src = tmpl.get("condition") or ""
        if condition_src.strip():
            passed, cond_error = evaluate_condition(condition_src, namespace)
            if cond_error:
                print_notice(
                    "warning",
                    f"Template '{name}' condition had a problem",
                    cond_error[:400],
                )
                continue
            if not passed:
                continue

        rendered, errors = render_template(raw_text, namespace)

        if errors:
            print_notice(
                "warning",
                f"Template '{name}' had a problem",
                "; ".join(errors)[:400],
            )

        if "abort" in destinations:
            print_notice("abort", name, rendered)
            abort_fired = True

        if "notice" in destinations:
            print_notice("info", name, rendered)

        if "gcode_comment" in destinations:
            comment_lines.append(f"; {name}: {rendered}")

    if comment_lines:
        block = [TEMPLATE_START] + comment_lines + [TEMPLATE_END]
        p.write_text("\n".join(block) + "\n" + text, encoding="utf-8")
    # notice-only templates need no file edit at all -- orcastrator.py's
    # own run log already embeds every NOTICE this processor printed.

    if abort_fired:
        # Same shape as dock_collision_guard.py's own sys.exit(1): this
        # is what makes OrcaSlicer treat the export itself as failed --
        # you never get as far as having a file to upload. The "abort"
        # NOTICE already printed above still gets embedded in
        # ORCASTRATOR_LOG by orcastrator.py (it reads our stdout before
        # checking our return code), so ORCASTRATOR_RENDER's print-time
        # gate remains as defense-in-depth for any file that somehow
        # gets printed without going through export again -- exactly
        # dock_collision_guard.py's own reasoning, just applied here to
        # a user-authored template condition instead of a collision.
        print("[gcode_template_notice] an 'abort' destination fired -- "
              "refusing to export.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gcode_template_notice.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1])
