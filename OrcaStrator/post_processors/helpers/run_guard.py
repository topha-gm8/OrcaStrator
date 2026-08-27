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
Shared helper: detect whether OrcaStrator has already run this exact
processor against this exact gcode file, via its own ORCASTRATOR_LOG
block (see orcastrator.py's prepend_run_log(), which
prepends one "; {script_name}: {status} ({ms}ms)" line per processor that
ran, every time OrcaStrator completes a pass -- success or failure).

This is a coarse, cheap idempotency guard: "did I already run against
this file, at all" -- not "is there still something left for me to do".
Prefer making a processor idempotent on its own merits first (checking
its own previously-inserted markers directly -- e.g.
insert_missing_tool_preheat.py's "current lead already >= target" check)
wherever that's straightforward. Reach for this when that's awkward, or
as a backstop alongside a processor's own idempotency logic.

Only catches a full OrcaStrator re-run over an already-processed file --
the realistic case being a re-export, or manually re-running
orcastrator.py against output that already carries a log
block. It does NOT protect a processor invoked standalone and repeatedly
outside OrcaStrator (e.g. `python3 some_processor.py file.gcode` run
twice by hand during development) -- nothing writes this tag except the
OrcaStrator itself, so that workflow needs a fresh copy of the file each
time same as always.
"""


def already_processed(lines, script_path) -> bool:
    """
    True if `lines` already contains an ORCASTRATOR_LOG entry for this
    exact script.

    `script_path` should be the same pathlib.Path your script's
    __main__ block already passes into process() (e.g.
    `pathlib.Path(__file__).resolve()`) -- so the name being checked for
    always comes from the actual file on disk, never a hardcoded string
    that could drift out of sync if the script gets renamed.
    """
    marker = f"; {script_path.name}: "
    return any(ln.startswith(marker) for ln in lines)
