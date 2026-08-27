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
Post-processor: disable_unused_tool_temps.py

Two related things, both driven off the gap between a tool's uses:

  1. PERMANENT SHUTOFF: a tool's very last use in the print gets an
     `M104 S0` right after the next toolchange -- no point keeping it
     hot for the rest of the print. If that toolchange is also the LAST
     one in the whole file (nothing to attach after), the cooldown lands
     right before OrcaSlicer's own `; EXECUTABLE_BLOCK_END` marker
     instead of at raw end-of-file -- keeps it inside the actual
     executable g-code rather than tacked onto the filament-use/config-
     dump comment block OrcaSlicer appends past that marker. Falls back
     to true EOF if the marker isn't present.

  2. IDLE-CYCLE SHUTOFF + REACTIVATION PREHEAT: a tool that's NOT done
     for the print, but won't be needed again for a while, gets the
     same `M104 S0` treatment right after the next toolchange -- and then,
     ahead of its actual next use, an `M104 S{temp}` reheat gets inserted
     (or an existing preheat relocated) so it's back up to temp in time.

     "A while" is configurable two ways, in the companion
     disable_unused_tool_temps.json:

       - `idle_shutoff_minutes`: an explicit fixed threshold, in minutes
         of estimated real idle time. Set this if you just want "don't
         bother cooling anything idle for under N minutes" independent
         of preheat lead-time tuning.
       - unset (the default): AUTO mode -- the threshold becomes
         tool_preheat.json's `target_lead_seconds` PLUS its
         `target_cooldown_seconds`, so a tool only gets cycled off if the
         idle gap is long enough to actually finish cooling down AND
         still let a reheat regain its full configured lead time
         afterward. Without the cooldown term, a gap barely longer than
         the lead time alone could get cycled even though the heater
         never really had time to drop before it's asked to climb back
         up -- wasted cycling for no real thermal benefit.

     Either way, the reheat's own lead time (how early to start warming
     back up before the tool's next use) always comes from
     tool_preheat.json's `target_lead_seconds` -- `idle_shutoff_minutes`
     only controls the shutdown decision, not the reheat timing.
     `target_cooldown_seconds` likewise only feeds the AUTO threshold
     decision above; it plays no part in the reheat-lead calculation.

Gaps are estimated with the same naive distance/feedrate model
insert_missing_tool_preheat.py uses (see helpers/time_estimator.py),
calibrated against OrcaSlicer's own stated preheat times found in the
file. Ballpark, not exact -- see that module for the full rationale.

Depends on restore_pos_fix.py's `X=/Y=/Z=` toolchange annotations for
accurate dock-move-free position resync in the time model, so this runs
AFTER restore_pos_fix.py in EXPLICIT_ORDER (see CLAUDE.md for the
processor-ordering rules, and the tool-change regex below, which matches
both bare and parameterized T-lines).

Naturally idempotent in the same sense as insert_missing_tool_preheat.py:
re-running settles once every idle gap already has its cooldown+reheat in
place -- but as a backstop (this script's own idempotency logic doesn't
cover every edge as tidily as insert_missing_tool_preheat.py's single
"current lead already sufficient" check does), it also skips outright if
OrcaStrator's own ORCASTRATOR_LOG already shows this exact script
ran against this file (see helpers/run_guard.py) -- e.g. if the whole
OrcaStrator gets manually re-run over an already-processed export.

    python3 disable_unused_tool_temps.py /path/to/file.gcode
"""
import json
import os
import pathlib
import sys

from helpers.time_estimator import (
    TOLERANCE_SECONDS,
    PAT_TOOLCHANGE,
    build_cumulative_time,
    calibrate_ratios,
    find_insert_index_for_lead,
    find_existing_preheat,
    find_target_temp,
    find_config_path,
    find_executable_block_end,
)
from helpers.run_guard import already_processed
from helpers.debug_dump import write_debug_dump as _write_debug_dump
from helpers.notice import display_flag as _notice_display_flag

DEFAULT_TARGET_LEAD_SECONDS = 90.0
# Fallback for a tool_preheat.json missing this key entirely -- 0
# reproduces this script's cooldown-unaware behavior exactly (AUTO
# threshold == target_lead_seconds, unchanged).
DEFAULT_TARGET_COOLDOWN_SECONDS = 0.0
SHARED_CONFIG_FILENAME = "tool_preheat.json"
OWN_CONFIG_FILENAME = "disable_unused_tool_temps.json"


def friendly_filename(p: pathlib.Path) -> str:
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


# Set once process() has loaded this processor's own config -- see
# helpers/notice.py's docstring. Starts empty ({} reads as "notice
# display on", the default) so any print_notice() call before that
# point (e.g. load_json_config()'s own read-failure notice below) is
# always embedded with display=true -- never accidentally hidden by
# a config it hasn't even read yet.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


def load_json_config(filename: str, script_path: pathlib.Path, error_title: str) -> dict:
    cfg_path = find_config_path(filename, script_path)
    if cfg_path is None:
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print_notice("warning", error_title, f"Couldn't read {cfg_path.name} ({exc}) -- using defaults.")
        return {}


def find_toolchange_events(lines):
    """Ordered list of (line_idx, tool) for every toolchange in the file."""
    events = []
    for idx, ln in enumerate(lines):
        m = PAT_TOOLCHANGE.match(ln)
        if m:
            events.append((idx, int(m.group(1))))
    return events


def find_next_occurrence(events):
    """
    events-list-index -> events-list-index of the next occurrence of the
    SAME tool later in the list, or None if this is that tool's last use.
    """
    next_occ = {}
    last_seen = {}
    for k in range(len(events) - 1, -1, -1):
        _, tool = events[k]
        next_occ[k] = last_seen.get(tool)
        last_seen[tool] = k
    return next_occ


def process(gcode_path: str, script_path: pathlib.Path) -> None:
    shared_cfg = load_json_config(SHARED_CONFIG_FILENAME, script_path, "Idle-cooldown config error")
    target_seconds = float(shared_cfg.get("target_lead_seconds", DEFAULT_TARGET_LEAD_SECONDS))
    cooldown_seconds = float(shared_cfg.get("target_cooldown_seconds", DEFAULT_TARGET_COOLDOWN_SECONDS))

    own_cfg = load_json_config(OWN_CONFIG_FILENAME, script_path, "Idle shutoff config error")
    global _notice_cfg
    _notice_cfg = own_cfg
    debug_cfg = own_cfg.get("debug", {}) or {}
    idle_shutoff_minutes = own_cfg.get("idle_shutoff_minutes")
    if idle_shutoff_minutes is not None:
        idle_threshold_seconds = float(idle_shutoff_minutes) * 60.0
        threshold_label = f"{idle_threshold_seconds:.0f}s fixed ({idle_shutoff_minutes:g}min)"
    else:
        idle_threshold_seconds = target_seconds + cooldown_seconds
        threshold_label = (
            f"{idle_threshold_seconds:.0f}s auto (= tool_preheat.json "
            f"target_lead_seconds {target_seconds:.0f}s + target_cooldown_seconds {cooldown_seconds:.0f}s)"
        )

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
        "config": {
            "target_lead_seconds": target_seconds,
            "cooldown_seconds": cooldown_seconds,
            "idle_shutoff_minutes": idle_shutoff_minutes,
            "idle_threshold_seconds": idle_threshold_seconds,
            "threshold_label": threshold_label,
        },
        "result": None,
    }

    if already_processed(lines, script_path):
        debug_data["result"] = "skipped_already_processed"
        _write_debug_dump("disable_unused_tool_temps", debug_cfg, debug_data, script_path)
        print_notice(
            "info", "Idle tool cooldown skipped",
            f"Found an existing '{script_path.name}' entry in ORCASTRATOR_LOG -- "
            f"{fname} has already been through this processor, skipping.",
        )
        return

    events = find_toolchange_events(lines)
    if not events:
        debug_data["result"] = "skipped_no_toolchanges"
        _write_debug_dump("disable_unused_tool_temps", debug_cfg, debug_data, script_path)
        print_notice("info", "Idle tool cooldown", "No tool changes found -- nothing to check.")
        return

    cum = build_cumulative_time(lines)
    ratio_by_tool, global_ratio = calibrate_ratios(lines, cum)
    next_occ = find_next_occurrence(events)

    # Where a cooldown for the LAST toolchange in the whole file lands,
    # since there's no next toolchange line to attach it after (see
    # cooldown_line below). Anchored on OrcaSlicer's own
    # `; EXECUTABLE_BLOCK_END` marker rather than raw EOF, so the
    # inserted M104 lands right after the actual machine-end g-code
    # instead of after the entire filament-use/config-dump comment
    # block OrcaSlicer appends past it -- inserting real g-code after
    # that marker would be misleading even though harmless (nothing
    # after it ever executes). Falls back to true EOF if the marker's
    # missing (older OrcaSlicer, or a non-Orca file), matching the old
    # behavior exactly in that case.
    exec_block_end = find_executable_block_end(lines)
    end_of_file_insert_idx = exec_block_end if exec_block_end is not None else len(lines)

    to_comment = set()   # indices of existing preheat lines being relocated
    to_insert = {}        # line idx -> [(sort_key, text), ...], inserted before that line
    report_permanent = []                    # tools shut off for good
    report_cycled = []                       # (tool, idle_seconds) cooled + reheat scheduled
    report_clamped = []                      # (tool, achieved) reheat couldn't fully reach target
    report_skipped_no_temp = []              # tools where reheat temp couldn't be determined

    for k, (idx, tool) in enumerate(events):
        # Where the cooldown line lands: right after the very next
        # toolchange in the file (whichever tool that is). If this is
        # the last toolchange in the whole file, there's nothing to attach
        # to -- land it at end_of_file_insert_idx instead (harmless,
        # print's basically done) rather than silently dropping the
        # cooldown.
        cooldown_line = events[k + 1][0] + 1 if k + 1 < len(events) else end_of_file_insert_idx

        nxt = next_occ[k]
        if nxt is None:
            # Last use of this tool in the print -- permanent shutoff.
            to_insert.setdefault(cooldown_line, []).append(
                (tool, f"M104 S0 T{tool} ; auto-off unused")
            )
            report_permanent.append(tool)
            continue

        # Not the last use -- decide whether the gap to its next use is
        # long enough to be worth cycling the heater off and back on.
        next_use_idx = events[nxt][0]
        ratio = ratio_by_tool.get(tool, global_ratio)
        naive_gap = cum[next_use_idx] - cum[idx]
        calibrated_gap = naive_gap * ratio

        if calibrated_gap < idle_threshold_seconds:
            continue  # short break, leave it hot

        temp = find_target_temp(lines, tool, next_use_idx)
        if temp is None:
            # Can't safely cool it without knowing what to reheat it to --
            # leave this occurrence alone rather than risk a cold tool
            # blocking on a full M109 wait later.
            report_skipped_no_temp.append(tool)
            continue

        to_insert.setdefault(cooldown_line, []).append(
            (tool, f"M104 S0 T{tool} ; auto-off, idle ~{calibrated_gap:.0f}s until next use")
        )

        existing_idx = find_existing_preheat(lines, tool, cooldown_line, next_use_idx)
        new_idx, achieved, clamped = find_insert_index_for_lead(
            cum, next_use_idx, cooldown_line, target_seconds, ratio
        )

        if existing_idx is not None:
            to_comment.add(existing_idx)
            note = f"reheat moved earlier, ~{achieved:.0f}s lead (target {target_seconds:.0f}s)"
        else:
            note = f"reheat inserted, ~{achieved:.0f}s lead (target {target_seconds:.0f}s)"

        to_insert.setdefault(new_idx, []).append(
            (tool, f"M104 S{temp} T{tool} ; {note}")
        )

        if clamped:
            report_clamped.append((tool, achieved))
        else:
            report_cycled.append((tool, calibrated_gap))

    debug_data["toolchange_events"] = [{"line_index": idx, "tool": tool} for idx, tool in events]
    debug_data["decisions"] = {
        "permanent_shutoff": sorted(set(report_permanent)),
        "cycled": [{"tool": t, "idle_seconds": round(s, 1)} for t, s in report_cycled],
        "clamped": [{"tool": t, "achieved_seconds": round(s, 1)} for t, s in report_clamped],
        "skipped_no_temp": sorted(set(report_skipped_no_temp)),
    }

    if not to_insert:
        debug_data["result"] = "skipped_no_change"
        _write_debug_dump("disable_unused_tool_temps", debug_cfg, debug_data, script_path)
        print_notice(
            "info", "Idle tool cooldown",
            "No tool had a long-enough idle gap or final unused stretch -- nothing to change.",
        )
        return

    out = []
    for i, ln in enumerate(lines):
        if i in to_insert:
            for _, text in sorted(to_insert[i]):
                out.append(text)
        if i in to_comment:
            out.append(f"; {ln} \u2190  moved earlier by DISABLE_UNUSED_TOOL_TEMPS")
        else:
            out.append(ln)
    if len(lines) in to_insert:
        for _, text in sorted(to_insert[len(lines)]):
            out.append(text)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")

    debug_data["result"] = "patched"
    _write_debug_dump("disable_unused_tool_temps", debug_cfg, debug_data, script_path)

    msgs = []
    if report_permanent:
        tool_list = ", ".join(f"T{t}" for t in sorted(set(report_permanent)))
        msgs.append(f"permanently off after last use: {tool_list}")
    if report_cycled:
        tool_list = ", ".join(f"T{t} (~{s:.0f}s idle)" for t, s in report_cycled)
        msgs.append(f"cycled off/on: {tool_list}")
    if msgs:
        print_notice(
            "info", "Idle tool cooldown",
            f"{fname}: idle threshold {threshold_label}, reheat lead target {target_seconds:.0f}s -- "
            + "; ".join(msgs) + ".",
        )
    if report_clamped:
        tool_list = ", ".join(f"T{t} (~{s:.0f}s)" for t, s in report_clamped)
        print_notice(
            "warning", "Reactivation preheat limited by available print time",
            f"{fname}: {tool_list} reheat pushed as early as possible after the cooldown but "
            f"couldn't reach the {target_seconds:.0f}s target -- not enough print time between "
            f"cooldown and next use.",
        )
    if report_skipped_no_temp:
        tool_list = ", ".join(f"T{t}" for t in sorted(set(report_skipped_no_temp)))
        print_notice(
            "warning", "Idle tool cooldown incomplete",
            f"{fname}: couldn't determine a reheat temp for {tool_list} "
            f"(no matching M109 or *_TEMP= param found) -- left hot rather than risk a cold "
            f"heater blocking later.",
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python disable_unused_tool_temps.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1], pathlib.Path(__file__).resolve())
