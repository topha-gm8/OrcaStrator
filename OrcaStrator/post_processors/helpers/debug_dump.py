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
Shared debug-dump writer -- lets ANY processor optionally write a
structured JSON snapshot of its own last run, for a "why didn't this do
what I expected" question to be answered from real, exact data (share
the file) instead of a screenshot or a description of one.

To opt in, a processor needs:
  1. A "debug": {"enabled": bool} block in its own configs/*.json (see
     dock_collision_guard.json for the convention -- document each key
     with a sibling "_key" comment, same as everywhere else, so
     config_editor.py's tooltips pick it up). config_editor.py
     auto-detects this block by shape (see _debug_section() there) and
     builds a standard "Debug" GUI section for it -- zero extra code
     needed in gui/*.py.
  2. A call to write_debug_dump() below at the end of a run, with
     whatever data dict is useful to dump.

Nothing here is required. A processor with no "debug" key in its config
simply never has the feature, same as today.

read_debug_dump()/list_debug_dumps() read back what write_debug_dump()
last wrote, using the identical path resolution -- for a gui/*.py
HAS_PREVIEW module (see gui/toolchange_heatmap.py) that wants to render
a live preview against a processor's own real run(s) instead of
synthetic sample data, since there's no gcode file to re-derive anything
from at settings-edit time.

Log MODE (single vs. multiple) and, when multiple, the per-processor cap
are both read from ONE central place -- configs/orcastrator.json's own
"debug": {"mode": ..., "cap": ...} block -- rather than being a
per-processor setting. Every opted-in processor's dumps behave the same
way as a result; a processor's own "debug" block only ever controls
on/off, never the mode/cap/location. Reusing the SAME central block that
already held "dir" (see _central_debug_settings below) keeps this to one
setting to find and change, same as the save-location precedent it's
modeled on. WHERE dumps go is likewise NOT overridable per-processor
(no debug_cfg["path"] escape hatch) -- see write_debug_dump's
docstring for why -- so every opted-in processor's dumps land in the
same one place, full stop.
"""
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile

# "<processor_name>_debug_<timestamp>.json" -- the multi-log filename
# shape. Timestamp is sortable lexicographically (year..millisecond,
# zero-padded, no separators tk/OS-unsafe), and millisecond resolution
# is there specifically so two dumps written in the same wall-clock
# second (a fast-failing processor, or a pipeline re-run kicked off
# quickly) still each get their own distinct filename instead of one
# silently clobbering the other.
_TIMESTAMP_FMT = "%Y%m%d_%H%M%S_%f"  # %f is microseconds; trimmed to ms below
_MULTI_LOG_RE = re.compile(r"^(?P<processor>.+)_debug_(?P<ts>\d{8}_\d{6}_\d{3})\.json$")


def _timestamp_suffix():
    return dt.datetime.now().strftime(_TIMESTAMP_FMT)[:-3]


def debug_log_filename(processor_name: str, mode: str) -> str:
    """
    The one place the "<processor>_debug.json" (single) vs.
    "<processor>_debug_<timestamp>.json" (multiple) filename shapes are
    decided, so write_debug_dump() and any GUI code that needs to
    recognize/group these files (config_editor.py's Debug Logs viewer)
    can't drift apart from each other.
    """
    if mode == "multiple":
        return f"{processor_name}_debug_{_timestamp_suffix()}.json"
    return f"{processor_name}_debug.json"


def parse_debug_log_filename(filename: str):
    """
    Reverse of debug_log_filename(). Returns (processor_name, timestamp
    or None) for anything shaped like a debug dump this module could
    have written, or None if `filename` doesn't match either shape at
    all (e.g. something unrelated that happens to sit in the same
    folder). A multi-log match's timestamp is the raw string from the
    filename (still lexicographically sortable) -- callers that just
    need newest-first ordering can sort on it directly without parsing
    it into a datetime.
    """
    m = _MULTI_LOG_RE.match(filename)
    if m:
        return m.group("processor"), m.group("ts")
    if filename.endswith("_debug.json"):
        return filename[: -len("_debug.json")], None
    return None


def _central_debug_settings(script_path: pathlib.Path):
    """
    Reads the central debug.dir/mode/cap settings straight out of
    configs/orcastrator.json, WITHOUT importing orcastrator.py itself --
    processors have to stay runnable standalone (see CLAUDE.md's
    processor contract), and orcastrator.py already imports/discovers
    *them*, not the other way around, so importing it back here would
    risk a circular import for no reason (and would drag tkinter into
    every processor's process just to read one string).

    Missing/unreadable/malformed file, or a missing/blank key inside it,
    falls back field-by-field to ("dir"=None, "mode"="single",
    "cap"=None) -- "no central directory configured", "overwrite the one
    file, exactly as this always worked before multi-log existed", and
    "no cap" respectively. This never raises.

    cap is only meaningful when mode is "multiple", and None there means
    unlimited (see the GUI's "nullable_number" field for cap -- same
    "null = auto/unbounded" convention used elsewhere in this codebase).
    An invalid (non-int, non-null, or <1) cap value falls back to
    unlimited rather than guessing at a number.
    """
    orcastrator_root = script_path.parent.parent
    candidate = orcastrator_root / "configs" / "orcastrator.json"
    result = {"dir": None, "mode": "single", "cap": None}
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return result
    debug_cfg = raw.get("debug")
    if not isinstance(debug_cfg, dict):
        return result
    value = debug_cfg.get("dir")
    if isinstance(value, str) and value.strip():
        result["dir"] = value.strip()
    if debug_cfg.get("mode") == "multiple":
        result["mode"] = "multiple"
    cap = debug_cfg.get("cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
        result["cap"] = cap
    return result


def _enforce_cap(directory: pathlib.Path, processor_name: str, cap: int, just_written: pathlib.Path):
    """
    Deletes the oldest multi-log dumps for `processor_name` in
    `directory` beyond `cap`, newest-kept. Ordering is by the
    TIMESTAMP IN THE FILENAME (lexicographic == chronological, see
    _TIMESTAMP_FMT), not file mtime -- mtime can be disturbed by
    copying/syncing the debug directory (e.g. onto a network share or
    into a support ticket) in a way the filename never is, so this is
    the more reliable "oldest" signal.

    Best-effort: same philosophy as the write itself (see
    write_debug_dump's own docstring) -- a failure to prune must never
    be allowed to affect the actual processor run, so this only ever
    logs to stderr and moves on.
    """
    try:
        matches = []
        for f in directory.glob(f"{processor_name}_debug_*.json"):
            parsed = parse_debug_log_filename(f.name)
            if parsed and parsed[0] == processor_name and parsed[1] is not None:
                matches.append((parsed[1], f))
        matches.sort(key=lambda pair: pair[0])  # oldest first
        excess = len(matches) - cap
        if excess <= 0:
            return
        for _, f in matches[:excess]:
            if f.resolve() == just_written.resolve():
                continue  # never delete the dump this very call just wrote
            try:
                f.unlink()
            except Exception as exc:
                print(f"[{processor_name}] could not prune old debug dump {f}: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[{processor_name}] could not prune old debug dumps: {exc}", file=sys.stderr)


def write_debug_dump(processor_name: str, debug_cfg: dict, data: dict, script_path: pathlib.Path = None) -> None:
    """
    Writes `data` as indented JSON to a debug-dump file for this
    processor -- "<processor_name>_debug.json" in the default "single"
    log mode (overwritten every run, exactly as this always worked), or
    "<processor_name>_debug_<timestamp>.json" (a new file every run,
    oldest pruned past the configured cap) when the CENTRAL debug.mode
    in configs/orcastrator.json is "multiple". See
    _central_debug_settings()'s docstring -- mode and cap are a single
    setting shared by every opted-in processor, never a per-processor
    one.

    debug_cfg is that processor's OWN "debug" sub-object -- each
    processor's on/off switch (debug_cfg["enabled"]) always stays local
    to that processor's config; this function only ever decides WHERE
    the file lands, in this priority order:

      1. The central debug.dir in configs/orcastrator.json, if set --
         one shared directory every opted-in processor's dump lands in
         by default, so OrcaStrator Settings' Log Viewer has one place
         to look and dumps from the same run sit next to each other.
         A relative value here is resolved against the OrcaStrator root
         (one level up from post_processors/), not against
         post_processors/ itself -- it's a central, OrcaStrator-level
         setting, not a per-processor one.
      2. Next to the processor's own script (post_processors/) -- the
         original fixed, predictable, no-configuration-needed default
         dock_collision_guard.py always used, preserved as the fallback
         for anyone who hasn't set a central dir.
      3. The OS temp folder, only reached if script_path itself wasn't
         even provided.

    A per-processor debug_cfg["path"] override is deliberately NOT
    supported here, even though debug_cfg is accepted as a parameter --
    a single processor's dumps landing somewhere else entirely would put
    them outside the Log Viewer's reach (which only looks at this
    central/default location, never a custom override) and, worse, out
    of step with gui/tool_temperature_graph.py's and
    gui/toolchange_heatmap.py's own historical-log pickers, which are
    anchored to this same central/default location regardless of what a
    specific config's debug settings say. One location every opted-in
    processor actually uses avoids both failure modes. debug_cfg is
    still accepted here (and still only ever consulted for ["enabled"])
    so callers don't need a "path" key at all.

    In "single" mode the filename is always "<processor_name>_debug.json",
    never a fixed name -- once more than one processor can share the
    same central directory, a fixed name would mean the second
    processor's dump silently clobbers the first's on every run. In
    "multiple" mode a fresh timestamped filename is used every run
    instead, and once the write succeeds, any of this processor's own
    older dumps in that same directory beyond the configured cap are
    deleted (oldest first) -- see _enforce_cap(). No cap configured
    (cap is None) means every run's dump is kept forever.

    Best-effort, same philosophy as show_svg_on_pc/show_native_alert
    elsewhere in this codebase: a failure here must never be allowed to
    affect the actual processor run.
    """
    if not debug_cfg.get("enabled", True):
        return
    central = _central_debug_settings(script_path) if script_path is not None else {"dir": None, "mode": "single", "cap": None}
    mode = central["mode"]
    filename = debug_log_filename(processor_name, mode)
    try:
        if central["dir"]:
            path = pathlib.Path(central["dir"]).expanduser()
            if not path.is_absolute():
                path = script_path.parent.parent / path
            path = path / filename
        elif script_path is not None:
            path = script_path.parent / filename
        else:
            path = pathlib.Path(tempfile.gettempdir()) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"[{processor_name}] debug dump written to {path}", file=sys.stderr)
        if mode == "multiple" and central["cap"] is not None:
            _enforce_cap(path.parent, processor_name, central["cap"], path)
    except Exception as exc:
        print(f"[{processor_name}] could not write debug dump: {exc}", file=sys.stderr)


def _resolve_debug_dir(processor_name: str, debug_cfg: dict, script_path: pathlib.Path):
    """
    The DIRECTORY (not one specific file) this processor's dumps live
    in -- same priority order write_debug_dump() uses to decide where to
    write, just stopping at the folder rather than a filename. Used by
    list_debug_dumps()/read_debug_dump() below, and available to a
    HAS_PREVIEW gui/*.py module (see gui/toolchange_heatmap.py) that
    wants to enumerate a processor's own dump history for a picker,
    without duplicating this resolution logic a third time.

    debug_cfg is accepted (and unused) here purely so a HAS_PREVIEW
    gui/*.py module (or any other caller) can pass its config's "debug"
    block through without checking whether it contains a "path" key --
    see write_debug_dump()'s docstring for why that key isn't supported.
    """
    central = (_central_debug_settings(script_path) if script_path is not None
               else {"dir": None, "mode": "single", "cap": None})
    if central["dir"]:
        path = pathlib.Path(central["dir"]).expanduser()
        if not path.is_absolute():
            path = script_path.parent.parent / path
        return path
    if script_path is not None:
        return script_path.parent
    return pathlib.Path(tempfile.gettempdir())


def list_debug_dumps(processor_name: str, debug_cfg: dict, script_path: pathlib.Path = None):
    """
    Every existing debug dump for this processor at its resolved
    location (see _resolve_debug_dir()), as a list of (path,
    timestamp_or_None), sorted NEWEST FIRST. Never raises -- a missing
    directory or nothing found just returns [].

    In "single" log mode there's at most one entry (timestamp None --
    the filename doesn't carry one). In "multiple" mode there can be
    several, ordered by the timestamp encoded in each filename (see
    debug_log_filename()/parse_debug_log_filename()) -- lexicographic
    ordering on that string IS chronological ordering, by construction
    (_TIMESTAMP_FMT is zero-padded year..millisecond). If a leftover
    untimestamped file exists alongside timestamped ones (e.g. debug.mode
    was switched from "single" to "multiple" at some point, so an old
    file from before the switch is still sitting there), it's placed
    LAST -- "most recently written, but no exact time known" is a
    reasonable default rather than guessing where it belongs.

    debug_cfg["enabled"] is ignored here, same reasoning as
    read_debug_dump() below -- it only ever gated WRITING.
    """
    try:
        directory = _resolve_debug_dir(processor_name, debug_cfg or {}, script_path)
        if not directory.is_dir():
            return []
        results = []
        single_path = directory / f"{processor_name}_debug.json"
        if single_path.is_file():
            results.append((single_path, None))
        for f in directory.glob(f"{processor_name}_debug_*.json"):
            parsed = parse_debug_log_filename(f.name)
            if parsed and parsed[0] == processor_name and parsed[1] is not None:
                results.append((f, parsed[1]))
        timestamped = sorted((r for r in results if r[1] is not None), key=lambda r: r[1], reverse=True)
        untimestamped = [r for r in results if r[1] is None]
        return timestamped + untimestamped
    except Exception:
        return []


def read_debug_dump(processor_name: str, debug_cfg: dict, script_path: pathlib.Path = None):
    """
    Reads back the MOST RECENT debug dump write_debug_dump() produced
    for this processor -- in "single" mode that's the one overwritten
    file, in "multiple" mode the newest timestamped one (see
    list_debug_dumps()). Returns the parsed dict, or None if there's
    nothing yet or it can't be parsed -- never raises, so a caller
    (typically a HAS_PREVIEW gui/*.py module) can just treat None as
    "nothing to preview yet" rather than handling a specific exception
    type.

    Ignores debug_cfg.get("enabled") -- a dump written while debug was
    on stays readable even if it's since been turned off in the config
    being edited; that flag only ever gated WRITING, not reading back
    what already exists.
    """
    dumps = list_debug_dumps(processor_name, debug_cfg, script_path)
    if not dumps:
        return None
    path, _ts = dumps[0]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
