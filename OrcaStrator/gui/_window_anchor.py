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
Shared "grow toward center" window-anchoring logic, used by both
orcastrator.py's progress window and config_editor.pyw's settings
window for their auto-resize passes (SVG panel appearing, scrollable
content changing height, etc).

Leading underscore keeps this out of gui/*.py auto-discovery, same
convention as _plugin_support.py -- this isn't a config's field spec,
it's shared plumbing.

The core idea: a resize should feel like every other Windows app --
grow right/down from the top-left corner, shrink back toward it --
which is what compute_anchor defaults to on both axes. The ONLY time
an axis deviates from that is when growing in the default direction
would push the window off the edge of its current monitor; only then
does that axis's anchor flip to the far edge instead, so the window
grows the other way (left/up) into the room that's actually available,
rather than running off-screen or getting silently clamped with a
chunk of content cut off. Each axis is evaluated independently, and
every resize recomputes this fresh from the window's current position
-- there's no anchor state to track between resizes, it just falls
back to top-left again the moment there's room to.

This generalizes the existing corner-anchor idea in orcastrator.py's
_position_for (which only knows about 4 fixed named corners on the
PRIMARY screen) to any position on any monitor, while keeping the
common case (plenty of room, growing right/down) matching ordinary
app behavior instead of always computing a "distance from center"
that most resizes don't actually need.

No Tk import here on purpose -- this is pure geometry, testable without
a live window, and reusable by anything that just has x/y/w/h numbers.
"""
import sys
import ctypes
import json
import pathlib


def get_monitor_work_area(x: int, y: int):
    """
    Work-area rect (left, top, right, bottom) -- taskbar/dock chrome
    already excluded -- of whichever monitor contains screen point
    (x, y). Returns None on non-Windows or if the Win32 call fails for
    any reason; callers should fall back to the primary screen's full
    geometry (same fallback shape as orcastrator.py's
    _usable_screen_height) in that case.

    MONITOR_DEFAULTTONEAREST means a point that's technically just
    outside every monitor (rare, but possible mid-drag) still resolves
    to the nearest one instead of failing outright.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        MONITOR_DEFAULTTONEAREST = 2
        pt = wintypes.POINT(int(x), int(y))
        hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        r = info.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def compute_anchor(win_x: int, win_y: int, win_w: int, win_h: int,
                    new_w: int, new_h: int, monitor: tuple) -> dict:
    """
    Per axis independently: default anchor is the near edge (left for
    X, top for Y), i.e. grow right/down, shrink back toward top-left --
    ordinary app behavior. An axis only flips to the far edge (right
    for X, bottom for Y) when growing the default way at `new_w`/
    `new_h` would push past `monitor`'s far bound; in that case the far
    edge holds at its CURRENT position (clamped onto the monitor, in
    case the window was already hanging off it slightly) and growth
    extends backward (left/up) instead.

    `new_w`/`new_h` are needed here (unlike a pure "which corner is
    this" query) because whether the default direction still has room
    depends on how big the window is about to become, not just where
    it currently sits.

    Returns {"anchor_x": "left"|"right", "anchor_y": "top"|"bottom",
             "anchor_px_x": <absolute x of that edge>,
             "anchor_px_y": <absolute y of that edge>}.
    """
    left, top, right, bottom = monitor

    if win_x + new_w <= right:
        anchor_x, anchor_px_x = "left", win_x
    else:
        anchor_x, anchor_px_x = "right", min(win_x + win_w, right)

    if win_y + new_h <= bottom:
        anchor_y, anchor_px_y = "top", win_y
    else:
        anchor_y, anchor_px_y = "bottom", min(win_y + win_h, bottom)

    return {
        "anchor_x": anchor_x, "anchor_y": anchor_y,
        "anchor_px_x": anchor_px_x, "anchor_px_y": anchor_px_y,
    }


def geometry_for_new_size(anchor: dict, new_w: int, new_h: int, monitor: tuple):
    """
    Given an anchor (from compute_anchor) and a new width/height,
    returns the (x, y) top-left corner that keeps the anchored edge
    pixel-fixed and grows/shrinks the opposite edge -- so resizing
    always extends toward, or pulls back from, that monitor's center.

    Clamped fully inside `monitor`'s work area: if new_w/new_h is
    bigger than the room available on the near side, the whole window
    slides back onto the monitor rather than hanging off the far edge.
    This is the direct replacement for orcastrator.py's _position_for
    + config_editor.pyw's screen_h clamp in _autosize_to_scroll_content.
    """
    left, top, right, bottom = monitor
    x = anchor["anchor_px_x"] if anchor["anchor_x"] == "left" else anchor["anchor_px_x"] - new_w
    y = anchor["anchor_px_y"] if anchor["anchor_y"] == "top" else anchor["anchor_px_y"] - new_h

    x = max(left, min(x, right - new_w))
    y = max(top, min(y, bottom - new_h))
    return int(x), int(y)


def resize_toward_center(win_x: int, win_y: int, win_w: int, win_h: int, new_w: int, new_h: int):
    """
    One-call convenience: given a window's current rect and a desired
    new size, returns (x, y) for the new top-left corner, handling the
    monitor lookup and fallback internally. This is what the two
    production auto-resize call sites (_finalize_svg_size,
    _autosize_to_scroll_content) will actually call.
    """
    win_cx = win_x + win_w / 2
    win_cy = win_y + win_h / 2
    monitor = get_monitor_work_area(int(win_cx), int(win_cy))
    if monitor is None:
        # Non-Windows, or the Win32 call failed -- fall back to treating
        # the whole virtual screen as one "monitor" via Tk's own (less
        # accurate, taskbar-included) screen size query. Callers on
        # Windows should essentially never hit this path.
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        sw, sh = probe.winfo_screenwidth(), probe.winfo_screenheight()
        probe.destroy()
        monitor = (0, 0, sw, sh)

    anchor = compute_anchor(win_x, win_y, win_w, win_h, new_w, new_h, monitor)
    return geometry_for_new_size(anchor, new_w, new_h, monitor)


# ---------------------------------------------------------------------------
# Persisted window geometry -- configs/gui_state.json
# ---------------------------------------------------------------------------
# Deliberately separate from every other configs/*.json: this one is
# auto-written only, never meant for hand-editing, so it carries none
# of the _comment/_field convention the rest of configs/ uses. Lives
# one level up from this file (gui/_window_anchor.py -> configs/), the
# same "single configs/ location" every other config in this project
# already uses.

def _state_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "configs" / "gui_state.json"


def load_state() -> dict:
    """
    Reads configs/gui_state.json in full. Missing, empty, or corrupt
    all resolve to {} rather than raising -- same tolerance as every
    other config load in this project; a broken state file can never
    stop a window from opening, it just loses its remembered geometry
    for this run.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_window_geometry(key: str, x: int, y: int, w: int, h: int) -> None:
    """
    Persists one window's geometry under `key` (e.g. "progress_window",
    "config_editor"), read-modify-write so saving one window's entry
    never clobbers another's. Call this once, on an explicit user
    close/continue -- never from an automatic dismiss (auto-close
    countdown, a fully-silent successful run) -- so what gets
    remembered is always somewhere the person actually left the window
    themselves.

    Records the CURRENT monitor's work-area bounds alongside x/y/w/h --
    restore_geometry() uses that to detect a stale save (resolution
    changed, that monitor disconnected) rather than trusting
    coordinates that may no longer mean anything.

    Best-effort: any failure (unwritable configs/ dir, etc) is
    swallowed silently, same as every other write in this project that
    must never be able to break the app around it. Written via a
    temp-file-then-replace so a crash mid-write can't leave a corrupt,
    unparseable gui_state.json behind for next launch.
    """
    try:
        monitor = get_monitor_work_area(int(x + w / 2), int(y + h / 2))
        state = load_state()
        state[key] = {
            "x": int(x), "y": int(y), "width": int(w), "height": int(h),
            "monitor": list(monitor) if monitor is not None else None,
        }
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def restore_geometry(key: str):
    """
    Returns (x, y, width, height) previously saved for `key`, or None
    if there's nothing saved OR the save looks stale: the monitor at
    the saved anchor point no longer matches the monitor recorded at
    save time (resolution changed, or that monitor's no longer
    connected). None is the caller's signal to fall back to its own
    default placement instead of trusting coordinates that may not
    correspond to anything on screen anymore.

    On non-Windows (get_monitor_work_area always returns None there),
    a saved entry is trusted as-is rather than treated as unverifiable
    -- staleness detection is a Windows-only refinement, not a
    requirement for restoring at all.
    """
    entry = load_state().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        x, y = int(entry["x"]), int(entry["y"])
        w, h = int(entry["width"]), int(entry["height"])
        saved_monitor = entry.get("monitor")
    except Exception:
        return None

    current_monitor = get_monitor_work_area(int(x + w / 2), int(y + h / 2))
    if current_monitor is None:
        return (x, y, w, h)
    if saved_monitor is None or list(current_monitor) != list(saved_monitor):
        return None
    return (x, y, w, h)
