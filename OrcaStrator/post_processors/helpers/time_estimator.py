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
Shared naive relative-time model, used by any processor that needs to
estimate how much print time separates two points in the g-code (lead-time
placement, idle-gap detection, etc).

Shared here (rather than duplicated per-script) so
disable_unused_tool_temps.py's idle-cooldown/reactivation-preheat feature
and insert_missing_tool_preheat.py's lead-time placement use the exact
same calibrated model and can't drift out of sync with each other. See
insert_missing_tool_preheat.py's module docstring for the full rationale
on why this is relative/calibrated rather than absolute.
"""
import bisect
import math
import re
import statistics

DEFAULT_FALLBACK_RATIO = 25.0  # used only if the file has zero calibration data at all
TOLERANCE_SECONDS = 3.0  # slack around a target so re-runs settle instead of churning on rounding

PAT_TOOLCHANGE = re.compile(r'^\s*T(\d+)\b(.*)$', re.IGNORECASE)
PAT_XYZ_KV = re.compile(r'\b([XYZ])=(-?[\d.]+)', re.IGNORECASE)
PAT_AXIS = re.compile(r'(?<![A-Za-z0-9_])([XYZE])(-?[\d.]+)', re.IGNORECASE)
PAT_F = re.compile(r'(?<![A-Za-z0-9_])F(-?[\d.]+)', re.IGNORECASE)
PAT_G4 = re.compile(r'^\s*G4\s+(?:P(?P<p>[\d.]+)|S(?P<s>[\d.]+))', re.IGNORECASE)
PAT_M104 = re.compile(r'^\s*M104\s+S(\d+)\s+T(\d+)\b', re.IGNORECASE)
PAT_M109 = re.compile(r'^\s*M109\s+S(\d+)\s+T(\d+)\b', re.IGNORECASE)
PAT_PARAM_TEMP = re.compile(r'\bT(\d+)_TEMP=(\d+)\b')
PAT_STATED_PREHEAT = re.compile(r'preheat T(\d+) time:\s*([\d.]+)s', re.IGNORECASE)
PAT_EXECUTABLE_BLOCK_END = re.compile(r'^\s*;\s*EXECUTABLE_BLOCK_END\s*$', re.IGNORECASE)

M109_LOOKAHEAD = 15          # lines to scan forward from a tool change for its M109
STATED_LOOKAHEAD = 2000      # lines to scan forward from a stated preheat comment for its toolchange


# ---------------------------------------------------------------------------
# Naive relative-time model
# ---------------------------------------------------------------------------

class TimeEstimator:
    """
    Rough, UNcalibrated relative-time model: walks the gcode accumulating
    seconds from naive (distance / feedrate) move times plus explicit G4
    dwells. It ignores acceleration entirely, so short zig-zag infill moves
    come out far faster than they really run -- which is why this is only
    ever used in *relative, per-tool-calibrated* form (see calibrate_ratios),
    never as an absolute time by itself.

    Toolchange dock moves resync X/Y/Z from the `X=/Y=/Z=` annotation that
    restore_pos_fix.py adds to each T-line -- so any processor using this
    model needs to run AFTER restore_pos_fix.py, or dock-move distance will
    get billed into the naive estimate (which calibrate_ratios will mostly
    absorb into the ratio anyway, but resynced is more accurate).
    """

    def __init__(self):
        self.x = self.y = self.z = None
        self.feed = 3000.0  # mm/min, sane placeholder until a real F is seen
        self.t = 0.0

    def consume(self, line: str) -> None:
        s = line.strip()
        if not s or s.startswith(';'):
            return

        mt = PAT_TOOLCHANGE.match(s)
        if mt:
            # A toolchange dock move is "free" in this model -- it's a fixed
            # part of the toolchange routine, not something preheat timing
            # should be billed against. Just resync modal position from the
            # X=/Y=/Z= restore_pos_fix annotation, if present.
            for axis, val in PAT_XYZ_KV.findall(mt.group(2)):
                v = float(val)
                if axis.upper() == 'X':
                    self.x = v
                elif axis.upper() == 'Y':
                    self.y = v
                elif axis.upper() == 'Z':
                    self.z = v
            return

        cmd = s[:2].upper()
        if cmd == 'G4':
            m = PAT_G4.match(s)
            if m:
                if m.group('p'):
                    self.t += float(m.group('p')) / 1000.0
                elif m.group('s'):
                    self.t += float(m.group('s'))
            return

        if cmd not in ('G0', 'G1'):
            return

        mf = PAT_F.search(s)
        if mf:
            try:
                self.feed = float(mf.group(1))
            except ValueError:
                pass

        axes = {}
        for axis, val in PAT_AXIS.findall(s):
            au = axis.upper()
            if au in ('X', 'Y', 'Z', 'E'):
                axes[au] = float(val)

        dx = (axes['X'] - self.x) if ('X' in axes and self.x is not None) else 0.0
        dy = (axes['Y'] - self.y) if ('Y' in axes and self.y is not None) else 0.0
        dz = (axes['Z'] - self.z) if ('Z' in axes and self.z is not None) else 0.0
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist == 0.0 and 'E' in axes:
            # Pure retract/prime move (relative-E, per this pipeline's M83
            # convention) -- no XY/Z travel, but it still takes real time.
            dist = abs(axes['E'])

        if 'X' in axes:
            self.x = axes['X']
        if 'Y' in axes:
            self.y = axes['Y']
        if 'Z' in axes:
            self.z = axes['Z']

        if dist > 0 and self.feed > 0:
            self.t += dist / self.feed * 60.0


def build_cumulative_time(lines):
    """cum[i] = naive-model seconds elapsed from the start of the list up to
    (but not including) line i. Length len(lines) + 1."""
    est = TimeEstimator()
    cum = [0.0] * (len(lines) + 1)
    for i, ln in enumerate(lines):
        cum[i] = est.t
        est.consume(ln)
    cum[len(lines)] = est.t
    return cum


def calibrate_ratios(lines, cum):
    """
    Find every OrcaSlicer-authored "; preheat Tn time: Xs" comment, pair it
    with that tool's next tool-change line, and compare OrcaSlicer's own
    stated lead time against our naive model's estimate for that same span.
    The ratio (stated / naive) is a per-tool correction factor for how much
    slower that tool's typical surrounding gcode really runs compared to
    this simplistic model.

    Returns (ratio_by_tool: {tool: float}, global_ratio: float).
    """
    by_tool = {}
    for i, ln in enumerate(lines):
        m = PAT_STATED_PREHEAT.search(ln)
        if not m:
            continue
        tool = int(m.group(1))
        stated = float(m.group(2))
        end = min(len(lines), i + 1 + STATED_LOOKAHEAD)
        for j in range(i + 1, end):
            m2 = PAT_TOOLCHANGE.match(lines[j])
            if m2 and int(m2.group(1)) == tool:
                naive = cum[j] - cum[i]
                if naive > 0.01:
                    by_tool.setdefault(tool, []).append(stated / naive)
                break

    ratio_by_tool = {t: statistics.median(v) for t, v in by_tool.items()}
    pooled = [r for v in by_tool.values() for r in v]
    global_ratio = statistics.median(pooled) if pooled else DEFAULT_FALLBACK_RATIO
    return ratio_by_tool, global_ratio


def find_insert_index_for_lead(cum, use_idx, floor_idx, target_seconds, ratio):
    """
    Earliest line index (>= floor_idx) that gives at least target_seconds of
    calibrated lead time before use_idx. Returns (index, achieved_seconds, clamped).
    """
    target_naive = target_seconds / ratio if ratio > 0 else target_seconds
    target_cum = cum[use_idx] - target_naive

    lo, hi = floor_idx, use_idx
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] >= target_cum:
            hi = mid
        else:
            lo = mid + 1

    achieved = (cum[use_idx] - cum[lo]) * ratio
    clamped = lo <= floor_idx and achieved < target_seconds - TOLERANCE_SECONDS
    return lo, achieved, clamped


# ---------------------------------------------------------------------------
# Gcode structure helpers
# ---------------------------------------------------------------------------

def find_executable_block_end(lines):
    """Line index of OrcaSlicer's `; EXECUTABLE_BLOCK_END` marker, which
    sits right after the machine end g-code -- everything from there to
    EOF is filament-use/config-dump comments that never cost real time.
    This is the anchor for "where did actual machine motion stop",
    independent of whatever the user's own machine_end_gcode/print_end
    macro happens to contain (which OrcaStrator doesn't control and
    shouldn't assume the shape of).

    Returns None if the marker isn't present (older OrcaSlicer, or a
    file from a different slicer) -- callers should fall back to
    treating EOF as the anchor in that case.
    """
    for i, ln in enumerate(lines):
        if PAT_EXECUTABLE_BLOCK_END.match(ln):
            return i
    return None


def naive_time_scale_factor(cum, lines, real_duration_seconds,
                             toolchange_line_idxs=None, tool_change_time=0.0):
    """Ratio to multiply naive per-line MOVE time by so the point where
    real machine motion stops (find_executable_block_end(), or EOF if
    that marker's missing) lines up with OrcaSlicer's own stated total
    print time.

    IMPORTANT: this only corrects the residual gap left AFTER known
    per-toolchange overhead is accounted for -- pass toolchange_line_idxs
    and tool_change_time (the same values a caller feeds into its own
    adjusted_time()) so this function can subtract
    `(toolchanges before the anchor) * tool_change_time` from the real
    duration before computing the ratio.

    This matters a lot on toolchange-heavy files: build_cumulative_time()
    treats a toolchange line as taking zero time (the real swap/purge/
    park dwell isn't a move at all -- see consume()'s toolchange branch),
    so on a print with hundreds of toolchanges, the bulk of the naive-
    vs-real gap is that missing overhead, NOT generic acceleration/jerk
    modeling error in ordinary moves. A first version of this function
    computed the ratio from the raw, un-adjusted naive total, which
    produced huge scale factors (4x+ seen on a 172-toolchange file) that
    then got applied to EVERY event's move-time component uniformly --
    dragging genuinely early/mid-print toolchanges artificially far
    later, since most of the "gap" it was correcting for had nothing to
    do with move-time at all. Subtracting the already-known toolchange
    overhead first isolates just the smaller, genuinely proportional
    move-time error before turning it into a ratio.

    Returns 1.0 (no scaling) if there isn't enough to calibrate from --
    callers should treat that as "use the raw naive numbers", not an
    error.
    """
    if not real_duration_seconds or real_duration_seconds <= 0 or not cum:
        return 1.0
    anchor = find_executable_block_end(lines)
    anchor_idx = anchor if anchor is not None else len(lines) - 1
    anchor_idx = max(0, min(anchor_idx, len(cum) - 1))
    naive_anchor_time = cum[anchor_idx]
    if naive_anchor_time <= 0:
        return 1.0
    toolchange_overhead = 0.0
    if toolchange_line_idxs and tool_change_time:
        n_before_anchor = bisect.bisect_left(toolchange_line_idxs, anchor_idx)
        toolchange_overhead = n_before_anchor * tool_change_time
    residual_real = real_duration_seconds - toolchange_overhead
    if residual_real <= 0:
        # Known toolchange overhead alone already accounts for the
        # entire real duration (or more) -- nothing left to attribute to
        # generic move-time error, so don't scale at all.
        return 1.0
    return residual_real / naive_anchor_time


def find_existing_preheat(lines, tool, floor_idx, use_idx):
    """Index of the last M104 targeting `tool` before `use_idx`, if any
    (searching back no further than floor_idx)."""
    for j in range(use_idx - 1, floor_idx - 1, -1):
        m = PAT_M104.match(lines[j])
        if m and int(m.group(2)) == tool:
            return j
    return None


def find_target_temp(lines, tool, use_idx):
    """The temp `tool` is expected to be at by `use_idx`: prefer a nearby
    M109 (the blocking wait-for-temp OrcaSlicer emits right at the tool
    change), fall back to a `Tn_TEMP=` param seen earlier in the file."""
    end = min(len(lines), use_idx + 1 + M109_LOOKAHEAD)
    for ln in lines[use_idx + 1:end]:
        m = PAT_M109.match(ln)
        if m and int(m.group(2)) == tool:
            return int(m.group(1))
    for ln in lines[:use_idx]:
        for m in PAT_PARAM_TEMP.finditer(ln):
            if int(m.group(1)) == tool:
                return int(m.group(2))
    return None


def find_config_path(filename: str, script_path):
    """
    Config lookup for a companion .json config, by explicit filename
    rather than the calling script's own name -- lets a config be shared
    by more than one processor (e.g. tool_preheat.json). Looked up in
    configs/, next to OrcaStrator. Returns None if it doesn't exist, so
    callers can decide whether a missing config means "use defaults" or
    is actually fatal.
    """
    p = script_path.parent.parent / "configs" / filename
    return p if p.exists() else None
