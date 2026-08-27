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
Post-processor: restore_pos_fix.py

Annotates every toolchange (T#) line with the X/Y/Z position the print
will resume at once the toolchange finishes -- e.g. `T3 X=120.5 Y=80.25`.
Other processors (insert_missing_tool_preheat.py,
disable_unused_tool_temps.py) read these annotations to resync their
own position tracking after a dock move, instead of having to model the
dock move's travel distance themselves.

The restore position is the first printing move (extrusion + X/Y) found
after the toolchange. Any pure travel moves between the toolchange and
that first printing move are redundant once the toolchange itself carries
the destination, so they're commented out rather than left duplicated.

    python3 restore_pos_fix.py /path/to/file.gcode
"""

import sys
import os
import pathlib
import re
import json

from helpers.time_estimator import find_config_path
from helpers.debug_dump import write_debug_dump as _write_debug_dump
from helpers.notice import display_flag as _notice_display_flag

CONFIG_FILENAME = "restore_pos_fix.json"


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

    Same helper as the other three processors (dock_collision_guard.py,
    insert_missing_tool_preheat.py, disable_unused_tool_temps.py) --
    this one just didn't have it yet, which is why its debug dumps were
    showing the meaningless temp name instead of the real filename.
    """
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name


# --- regexes -------------------------------------------------------

PAT_T    = re.compile(r"^\s*(T(\d+))\s*$", re.IGNORECASE)
PAT_E    = re.compile(r"\bE([-+]?\d*\.?\d+)", re.IGNORECASE)
PAT_AXES = re.compile(r"\b([XYZ])([-+]?\d*\.?\d+)", re.IGNORECASE)


class ModalPosition:
    __slots__ = ("x", "y", "z")

    def __init__(self, x=None, y=None, z=None):
        self.x = x
        self.y = y
        self.z = z

    def copy(self):
        return ModalPosition(self.x, self.y, self.z)

    def update_from_move(self, line: str) -> None:
        """Update modal coordinates from a G0/G1 line."""
        for axis, val in PAT_AXES.findall(line):
            v = float(val)
            if axis in ("X", "x"):
                self.x = v
            elif axis in ("Y", "y"):
                self.y = v
            elif axis in ("Z", "z"):
                self.z = v


class Coord:
    __slots__ = ("x", "y", "z")

    def __init__(self, x=None, y=None, z=None):
        self.x = x
        self.y = y
        self.z = z

    @staticmethod
    def _fmt_value(v: float) -> str:
        s = f"{v:.4f}"
        s = s.rstrip("0").rstrip(".")
        return s

    @classmethod
    def from_anchor(cls, anchor: ModalPosition, pre: ModalPosition):
        if anchor is None or anchor.x is None or anchor.y is None:
            return None
        if anchor.z is not None:
            z_val = anchor.z
        else:
            z_val = pre.z
        return cls(anchor.x, anchor.y, z_val)

    def to_gcode_suffix(self) -> str:
        parts = []
        if self.x is not None:
            parts.append(f"X={self._fmt_value(self.x)}")
        if self.y is not None:
            parts.append(f"Y={self._fmt_value(self.y)}")
        if self.z is not None:
            parts.append(f"Z={self._fmt_value(self.z)}")
        return " ".join(parts)


# Set once patch_file() has loaded this processor's own config -- see
# helpers/notice.py's docstring. Starts empty (reads as "display on",
# the default) so nothing before that point is ever embedded with
# display=false by a config it hasn't read yet.
_notice_cfg: dict = {}


def print_notice(level: str, title: str, message: str) -> None:
    payload = {"level": level, "title": title, "message": message,
               "display": _notice_display_flag(level, _notice_cfg)}
    print("NOTICE:" + json.dumps(payload, separators=(",", ":")))


def load_config(script_path: pathlib.Path) -> dict:
    """
    This processor has no settings of its own -- it always runs the
    same way -- so the only thing this file (if present at all) can
    contain is an opt-in "debug" block for helpers/debug_dump.py's
    shared feature. Missing file is the normal/expected case, same
    tolerance as every other processor's config loader.
    """
    cfg_path = find_config_path(CONFIG_FILENAME, script_path)
    if cfg_path is None:
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print_notice("warning", "Debug config error", f"Couldn't read {cfg_path.name} ({exc}) -- debug logging off.")
        return {}


def is_comment(line: str) -> bool:
    return line.lstrip().startswith(";")

def is_move(line: str) -> bool:
    ls = line.lstrip()
    return ls.startswith("G0") or ls.startswith("G1")

def has_extrusion(line: str) -> bool:
    return bool(PAT_E.search(line))

def has_any_xyz(line: str) -> bool:
    return bool(PAT_AXES.search(line))

def find_anchor(lines, start_index: int, pre_modal: ModalPosition):
    modal = pre_modal.copy()
    travel_indices = []

    for idx in range(start_index, len(lines)):
        ln = lines[idx]

        if is_comment(ln):
            continue

        if PAT_T.match(ln):
            break

        if is_move(ln):
            if has_extrusion(ln):
                if has_any_xyz(ln):
                    # First printing move with XY — we've found the destination
                    break
                else:
                    # Pure retract/unretract (G1 E±n) — skip it, keep scanning
                    continue
            if has_any_xyz(ln):
                travel_indices.append(idx)
            modal.update_from_move(ln)
        # Non-move, non-comment lines (M-codes, macros, blank) — just continue

    if modal.x is None or modal.y is None:
        return None, []
    return modal, travel_indices

def patch_file(path_str: str, script_path: pathlib.Path = None) -> tuple[int, int]:
    """Patch the file in place. Returns (patched_count, skipped_count)
    counting toolchange (T#) lines that were / weren't annotated with
    restore coordinates."""
    full_cfg = load_config(script_path) if script_path is not None else {}
    global _notice_cfg
    _notice_cfg = full_cfg
    debug_cfg = full_cfg.get("debug", {}) or {}

    p = pathlib.Path(path_str)
    text = p.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    modal = ModalPosition()      # global modal position
    comment_indices = set()      # travel moves we will replace with comments

    patched_count = 0
    skipped_count = 0
    debug_events = []  # one entry per toolchange encountered, in file order

    out_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if i in comment_indices:
            out_lines.append(f"; {line} ←  removed by RESTORE_POS_FIX")
            i += 1
            continue

        if is_move(line):
            modal.update_from_move(line)

        mT = PAT_T.match(line)
        if mT:
            pre_modal = modal.copy()

            anchor_modal, travel_to_comment = find_anchor(lines, i + 1, pre_modal)

            coord = Coord.from_anchor(anchor_modal, pre_modal)
            tool_name = mT.group(1)  # e.g. "T3"

            if coord is not None:
                comment_indices.update(travel_to_comment)

                suffix = coord.to_gcode_suffix()

                if suffix:
                    out_lines.append(f"{tool_name} {suffix}")
                else:
                    out_lines.append(tool_name)
                modal = ModalPosition(coord.x, coord.y, coord.z)
                patched_count += 1
                debug_events.append({
                    "line_index": i, "tool": tool_name, "resolved": True,
                    "coord": {"x": coord.x, "y": coord.y, "z": coord.z},
                    "travel_lines_commented": sorted(travel_to_comment),
                })
            else:
                out_lines.append(line)
                skipped_count += 1
                debug_events.append({
                    "line_index": i, "tool": tool_name, "resolved": False,
                    "coord": None,
                    "reason": "no printing move with an X/Y position found before or after the toolchange",
                })

            i += 1
            continue

        out_lines.append(line)
        i += 1

    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    if script_path is not None:
        debug_data = {
            "file": friendly_filename(p),
            "result": {"patched": patched_count, "skipped": skipped_count},
            "toolchanges": debug_events,
        }
        _write_debug_dump("restore_pos_fix", debug_cfg, debug_data, script_path)

    return patched_count, skipped_count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python restore_pos_fix.py input_file")
        sys.exit(1)

    patched, skipped = patch_file(sys.argv[1], pathlib.Path(__file__).resolve())

    if patched == 0 and skipped == 0:
        print_notice("info", "Restore pos fix", "No toolchanges found -- nothing to patch.")
    elif skipped == 0:
        print_notice("info", "Restore pos fix",
                     f"Patched {patched} toolchange(s) with restore coordinates.")
    else:
        print_notice("warning", "Restore pos fix",
                     f"Patched {patched} toolchange(s); {skipped} left unpatched "
                     f"(no usable X/Y position found before or after the toolchange).")
