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
Post-processor: insert_missing_tool_preheat.py

OrcaSlicer's ooze-prevention feature preheats an idle tool a fixed number
of seconds (its configured "preheat time") before it's actually needed, by
inserting a lead-time M104 during the *previous* tool's printing. Two
related problems show up around a tool's FIRST use in a print:

  1. If the first use comes too early for that fixed lead time to fit
     (nothing printed yet to hide the preheat inside), OrcaSlicer just
     skips the preheat entirely -- the tool change falls straight through
     to a blocking M109 that waits from idle temp.

  2. If the first use comes later in the print, OrcaSlicer still only
     gives it its configured lead time (typically ~30-40s) -- the same
     amount it'd give a routine mid-print tool switch. There's often far
     more print time available before a tool's first use than that, and a
     cold-ish heater (idle temp -> full temp, e.g. 170C -> 250C) can
     genuinely benefit from more head start than a routine switch does.

This processor finds every tool's first use in the file and, if it isn't
already getting at least `target_lead_seconds` of lead time (configurable,
see the companion tool_preheat.json file), moves its preheat M104 earlier
-- inserting one from scratch if it's missing, or relocating an existing
one further back if there's room.

Lead time is estimated, not exact: there's no way to know real firmware
timing without simulating acceleration, so a simple distance/feedrate
model is calibrated against OrcaSlicer's own stated preheat times found
elsewhere in the same file (per tool, since a given tool tends to print
similar-shaped regions on every layer). Expect it to land in the right
ballpark, not to the second.

Naturally idempotent: re-running with the same target changes nothing
further once every tool has reached it; raising the target in the config
and re-running will push preheats back further still. As a backstop, it
also skips outright if OrcaStrator's own ORCASTRATOR_LOG already
shows this exact script ran against this file (see helpers/run_guard.py).

tool_preheat.json's `target_lead_seconds` is also read by
disable_unused_tool_temps.py for its idle-cooldown/reactivation-preheat
feature -- one knob controls both "how early to preheat a tool's first
use" here and "how early to reheat a tool coming back from an idle
cooldown" there, so they can't drift out of sync with each other. It's
named tool_preheat.json rather than after either individual script for
that reason. Looked for next to this script or next to OrcaStrator
(see find_config_path in helpers/time_estimator.py) -- ships at the
OrcaStrator level by default.

    python3 insert_missing_tool_preheat.py /path/to/file.gcode
"""
import json
import os
import pathlib
import re
import sys

from helpers.time_estimator import (
    TOLERANCE_SECONDS,
    PAT_TOOLCHANGE,
    PAT_M104,
    build_cumulative_time,
    calibrate_ratios,
    find_insert_index_for_lead,
    find_existing_preheat,
    find_target_temp,
    find_config_path,
)
from helpers.run_guard import already_processed
from helpers.debug_dump import write_debug_dump as _write_debug_dump
from helpers.notice import display_flag as _notice_display_flag

DEFAULT_TARGET_LEAD_SECONDS = 90.0
CONFIG_FILENAME = "tool_preheat.json"
# Separate from CONFIG_FILENAME above on purpose: tool_preheat.json is
# shared with disable_unused_tool_temps.py (that's the whole point of
# it being named after neither script individually), so a "debug"
# block living there would be ambiguous about which of the two
# processors it's actually toggling. This one's just for this script's
# own opt-in debug dump -- see helpers/debug_dump.py -- and has nothing
# else in it.
DEBUG_CONFIG_FILENAME = "insert_missing_tool_preheat.json"

# Matches the slicer-emitted `print_start ...` line (see the
# `### slicer start G-code ###` template in the Klipper macro file) so we can
# append/replace its UNDERHEATED_TOOLS=... param. Anchored to the start of
# the line (allowing leading whitespace) so we don't match e.g. a comment
# that happens to mention print_start elsewhere in the file.
PAT_PRINT_START_LINE = re.compile(r'^\s*print_start\b', re.IGNORECASE)
PAT_UNDERHEATED_PARAM = re.compile(r'\bUNDERHEATED_TOOLS=\S*')


def friendly_filename(p: pathlib.Path) -> str:
    """
    OrcaSlicer invokes post-processing scripts against a temp file with a
    meaningless name, not the file's real/intended output name. Prefer
    SLIC3R_PP_OUTPUT_NAME (set by OrcaSlicer) for anything shown to a person.
    """
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


# Set once process() has loaded DEBUG_CONFIG_FILENAME (this script's
# own config -- see helpers/notice.py's docstring). Starts empty
# (reads as "display on", the default) so nothing before that point
# -- e.g. load_debug_config()'s own read-failure notice below -- is
# ever embedded with display=false by a config it hasn't read yet.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


def load_config(script_path: pathlib.Path) -> dict:
    cfg_path = find_config_path(CONFIG_FILENAME, script_path)
    if cfg_path is None:
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print_notice(
            "warning",
            "Preheat lead-time config error",
            f"Couldn't read {cfg_path.name} ({exc}) -- using default target of "
            f"{DEFAULT_TARGET_LEAD_SECONDS:.0f}s.",
        )
        return {}


def load_debug_config(script_path: pathlib.Path) -> dict:
    """
    Separate from load_config() above because it reads a different
    file (DEBUG_CONFIG_FILENAME, not the shared CONFIG_FILENAME) --
    see the comment on DEBUG_CONFIG_FILENAME for why. Missing file is
    the normal/expected case (debug logging is opt-in and this file
    has nothing else in it), so that's silent; only an actually
    unreadable/malformed file gets a notice.
    """
    cfg_path = find_config_path(DEBUG_CONFIG_FILENAME, script_path)
    if cfg_path is None:
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print_notice("warning", "Debug config error", f"Couldn't read {cfg_path.name} ({exc}) -- debug logging off.")
        return {}


# ---------------------------------------------------------------------------
# Gcode structure helpers local to this script
# ---------------------------------------------------------------------------

def find_print_start_line(lines):
    """Index of the slicer-emitted `print_start ...` line, or None if absent
    (e.g. a Klipper config that doesn't use this convention at all)."""
    for idx, ln in enumerate(lines):
        if PAT_PRINT_START_LINE.match(ln):
            return idx
    return None


def apply_underheated_param(line, tools):
    """
    Append (or, on a manual re-run outside OrcaStrator, replace)
    UNDERHEATED_TOOLS=... on the print_start line. Always written -- even
    empty -- so PRINT_START can tell "processor ran, list is empty" apart
    from "processor never touched this file, fall back to heating every
    used tool at idle" (see PRINT_START's per-tool loop).
    """
    value = ",".join(str(t) for t in tools)
    param = f"UNDERHEATED_TOOLS={value}"
    if PAT_UNDERHEATED_PARAM.search(line):
        return PAT_UNDERHEATED_PARAM.sub(param, line, count=1)
    return line.rstrip() + " " + param


def find_first_uses(lines):
    """tool number -> index of its first tool-change line in the file."""
    first_use = {}
    for idx, ln in enumerate(lines):
        m = PAT_TOOLCHANGE.match(ln)
        if m:
            t = int(m.group(1))
            if t not in first_use:
                first_use[t] = idx
    return first_use


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(gcode_path: str, script_path: pathlib.Path) -> None:
    cfg = load_config(script_path)
    target_seconds = float(cfg.get("target_lead_seconds", DEFAULT_TARGET_LEAD_SECONDS))
    own_cfg = load_debug_config(script_path)
    global _notice_cfg
    _notice_cfg = own_cfg
    debug_cfg = own_cfg.get("debug", {}) or {}

    p = pathlib.Path(gcode_path)
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    fname = friendly_filename(p)

    # Builds up as the run progresses -- written at every exit point
    # below (including the early-skip ones), not just on a successful
    # patch, since "why did this skip" is exactly the kind of question
    # this feature exists to answer. See dock_collision_guard.py /
    # helpers/debug_dump.py for the shared convention this follows.
    debug_data = {
        "file": fname,
        "config": {"target_lead_seconds": target_seconds},
        "result": None,
    }

    if already_processed(lines, script_path):
        debug_data["result"] = "skipped_already_processed"
        _write_debug_dump("insert_missing_tool_preheat", debug_cfg, debug_data, script_path)
        print_notice(
            "info", "Preheat lead-time check skipped",
            f"Found an existing '{script_path.name}' entry in ORCASTRATOR_LOG -- "
            f"{fname} has already been through this processor, skipping.",
        )
        return

    first_use = find_first_uses(lines)
    if not first_use:
        debug_data["result"] = "skipped_no_toolchanges"
        _write_debug_dump("insert_missing_tool_preheat", debug_cfg, debug_data, script_path)
        print_notice("info", "Preheat lead-time check", "No tool changes found -- nothing to check.")
        return

    insert_at = min(first_use.values())
    cum = build_cumulative_time(lines)
    ratio_by_tool, global_ratio = calibrate_ratios(lines, cum)

    to_comment = set()  # indices of old preheat lines being relocated -- commented out, not deleted
    to_insert = {}      # idx -> [(tool, line_text), ...]
    report_new, report_extended, report_clamped, report_skipped = [], [], [], []

    for tool in sorted(first_use):
        use_idx = first_use[tool]
        ratio = ratio_by_tool.get(tool, global_ratio)

        existing_idx = find_existing_preheat(lines, tool, insert_at, use_idx)
        current_lead = (cum[use_idx] - cum[existing_idx]) * ratio if existing_idx is not None else 0.0

        if current_lead >= target_seconds - TOLERANCE_SECONDS:
            continue  # already close enough, leave alone

        temp = find_target_temp(lines, tool, use_idx)
        if temp is None:
            report_skipped.append(tool)
            continue

        new_idx, achieved, clamped = find_insert_index_for_lead(cum, use_idx, insert_at, target_seconds, ratio)

        if existing_idx is not None:
            to_comment.add(existing_idx)
            note = f"moved earlier, ~{achieved:.0f}s lead (target {target_seconds:.0f}s)"
        else:
            note = f"inserted, ~{achieved:.0f}s lead (target {target_seconds:.0f}s)"

        line_text = f"M104 S{temp} T{tool} ; preheat T{tool} ({note})"
        to_insert.setdefault(new_idx, []).append((tool, line_text))

        if clamped:
            report_clamped.append((tool, achieved))
        elif existing_idx is not None:
            report_extended.append((tool, current_lead, achieved))
        else:
            report_new.append((tool, achieved))

    # Tools that couldn't reach the target lead time even pushed as early as
    # possible in the body -- PRINT_START's own per-tool loop is these tools'
    # only real shot at any head start, so flag them via UNDERHEATED_TOOLS on
    # the print_start line. The initial tool is excluded: PRINT_START already
    # heats it to full temp unconditionally, regardless of this param.
    initial_tool = min(first_use, key=first_use.get)
    underheated_tools = sorted(t for t, _ in report_clamped if t != initial_tool)

    print_start_idx = find_print_start_line(lines)
    if print_start_idx is None and underheated_tools:
        print_notice(
            "warning", "Couldn't attach UNDERHEATED_TOOLS",
            f"{fname}: no 'print_start ...' line found -- "
            f"T{', T'.join(str(t) for t in underheated_tools)} won't get an early "
            f"heat at PRINT_START and will rely on in-body preheat only.",
        )

    debug_data["first_uses"] = {str(t): idx for t, idx in first_use.items()}
    debug_data["decisions"] = {
        "new": [{"tool": t, "achieved_seconds": round(s, 1)} for t, s in report_new],
        "extended": [{"tool": t, "before_seconds": round(a, 1), "after_seconds": round(b, 1)}
                     for t, a, b in report_extended],
        "clamped": [{"tool": t, "achieved_seconds": round(s, 1)} for t, s in report_clamped],
        "skipped_no_temp": list(report_skipped),
    }

    if not to_insert and not report_skipped and print_start_idx is None:
        debug_data["result"] = "skipped_no_change"
        debug_data["underheated_tools"] = underheated_tools
        _write_debug_dump("insert_missing_tool_preheat", debug_cfg, debug_data, script_path)
        print_notice(
            "info", "Preheat lead-time check",
            f"Every tool already has >= {target_seconds:.0f}s of lead time -- nothing to change.",
        )
        return

    if print_start_idx is not None:
        value = ",".join(str(t) for t in underheated_tools)
        desc = f"T{', T'.join(str(t) for t in underheated_tools)}" if underheated_tools else "none"
        print_notice(
            "info", "UNDERHEATED_TOOLS set on print_start",
            f"{fname}: print_start line updated with UNDERHEATED_TOOLS={value} ({desc}).",
        )

    if to_insert or print_start_idx is not None:
        out = []
        for i, ln in enumerate(lines):
            if i == print_start_idx:
                ln = apply_underheated_param(ln, underheated_tools)
            if i in to_insert:
                for _, text in sorted(to_insert[i]):
                    out.append(text)
            if i in to_comment:
                out.append(f"; {ln} \u2190  moved earlier by INSERT_MISSING_TOOL_PREHEAT")
            else:
                out.append(ln)
        if len(lines) in to_insert:
            for _, text in sorted(to_insert[len(lines)]):
                out.append(text)
        p.write_text("\n".join(out) + "\n", encoding="utf-8")

    debug_data["result"] = "patched"
    debug_data["underheated_tools"] = underheated_tools
    _write_debug_dump("insert_missing_tool_preheat", debug_cfg, debug_data, script_path)

    msgs = []
    if report_new:
        tool_list = ", ".join(f"T{t} (~{s:.0f}s)" for t, s in report_new)
        msgs.append(f"inserted for {tool_list}")
    if report_extended:
        tool_list = ", ".join(f"T{t} ({a:.0f}s->~{b:.0f}s)" for t, a, b in report_extended)
        msgs.append(f"extended {tool_list}")
    if msgs:
        print_notice(
            "info", "Preheat lead time extended",
            f"{fname}: target {target_seconds:.0f}s -- " + "; ".join(msgs) + ".",
        )
    report_clamped_warn = [(t, s) for t, s in report_clamped if t != initial_tool]
    if report_clamped_warn:
        tool_list = ", ".join(f"T{t} (~{s:.0f}s)" for t, s in report_clamped_warn)
        print_notice(
            "warning", "Preheat lead time limited by available print time",
            f"{fname}: {tool_list} pushed as early as possible in the print but couldn't "
            f"reach the {target_seconds:.0f}s target -- not enough print time before first use.",
        )
    if report_skipped:
        tool_list = ", ".join(f"T{t}" for t in report_skipped)
        print_notice(
            "warning", "Preheat lead-time check incomplete",
            f"{fname}: couldn't determine a target temp for {tool_list} "
            f"(no matching M109 or *_TEMP= param found) -- left as-is.",
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python insert_missing_tool_preheat.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1], pathlib.Path(__file__).resolve())
