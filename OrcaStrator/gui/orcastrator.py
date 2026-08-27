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
Settings-form spec for orcastrator.json -- OrcaStrator's own settings
(progress window, error handling, color theme).

Auto-discovered by config_editor.py: every *.py in this folder is loaded
and turned into a landing-page entry with zero edits to config_editor.py
itself. See CLAUDE.md's "Giving a new processor settings" section for
the full convention this file follows -- what each of these module-level
names means, the field-spec shape, `show_if`, section-heading tooltips,
etc. This file is a working example of all of it, not just orcastrator's
own settings.
"""

# Shown on the landing page and as this config's editor title.
TITLE = "OrcaStrator"
SUBTITLE = "The pipeline runner itself: progress window, error handling, color theme."

# The JSON file in configs/ this form edits.
CONFIG = "orcastrator.json"

# Optional: "theme" gives this config's editor a live mockup preview of
# the progress window using its own Theme Colors fields (see
# _refresh_theme_preview() in config_editor.py). Omit for a plain form.
PREVIEW = "theme"

WINDOW_POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right", "center"]

# A list of (section title, [field spec, ...]) tuples, or (section title,
# [field spec, ...], tooltip_override) if the section heading needs an
# explicit tooltip -- see CLAUDE.md for when the automatic lookup can't
# find one on its own. Each field spec is a dict; see _add_field() in
# config_editor.py for every supported "kind" and option.
SECTIONS = [
    ("Progress Window", [
        dict(kind="bool", label="Show progress window", path=("show_progress_ui",)),
        dict(kind="bool", label="Remember last position", path=("window", "remember_position"),
             tooltip="On: reopens wherever you last left it (saved when you click Close/Continue -- "
                     "an auto-closed or auto-dismissed window doesn't count). Off: always uses the "
                     "fixed Window position below. If nothing's been saved yet, or the saved spot was "
                     "on a monitor that's no longer connected, falls back to Window position either way.",
             show_if=[(("show_progress_ui",), True)]),
        dict(kind="choice", label="Window position", path=("window", "position"), options=WINDOW_POSITIONS,
             show_if=[(("show_progress_ui",), True), (("window", "remember_position"), False)]),
        dict(kind="number", label="Screen edge margin (px)", path=("window", "margin"),
             min=0, max=1000, step=5, is_int=True, tooltip="Ignored for the 'center' position -- only the corner "
                     "presets (top-left/top-right/bottom-left/bottom-right) use a margin.",
             show_if=[(("show_progress_ui",), True),
                      (("window", "remember_position"), False),
                      (("window", "position"), ["top-left", "top-right", "bottom-left", "bottom-right"])]),
        dict(kind="bool", label="Auto-close after a successful run", path=("auto_close", "enabled"),
             tooltip="Closes the progress window on its own a few seconds after every processor "
                     "succeeds -- even if an SVG preview kept it open. A failed run always still needs "
                     "a manual Close. Clicking anywhere on the window while it's counting down cancels "
                     "it for that run.",
             show_if=[(("show_progress_ui",), True)]),
        dict(kind="number", label="Auto-close after (seconds)", path=("auto_close", "seconds"),
             min=1, max=3600, step=1, is_int=True,
             show_if=[(("show_progress_ui",), True), (("auto_close", "enabled"), True)]),
    ], ("window",)),
    ("Error Handling", [
        dict(kind="bool", label="Stop pipeline on first failure", path=("on_error", "stop_on_error")),
        dict(kind="bool", label="Auto-abort print on unexplained failure",
             path=("on_error", "auto_abort_on_unexplained_failure")),
    ]),
    ("Processor Selection", [
        dict(kind="processor_order", label="Runs first", path=("explicit_order",)),
        dict(kind="processor_order", label="Runs last", path=("explicit_order_last",), order_position="last"),
        dict(kind="processor_denylist", label="Denylist", path=("denylist",)),
    ], "All three pickers build their lists from whatever's actually in post_processors/ right now, "
       "rather than free-text entry -- there's no way to end up with a typo'd or stale script name in "
       "any of them. \"Runs first\"/\"Runs last\" only need to list the processors that actually care "
       "about being pinned -- anything left off either list still runs, in between, alphabetically. If "
       "a processor somehow ends up in both, \"Runs first\" wins."),
    ("Settings Landing Page", [
        dict(kind="gui_order", label="Card order", path=("gui_order",)),
    ], "Purely cosmetic -- reorders the cards on this settings app's own landing page (the screen "
       "you land on when you open OrcaStrator Settings, or hit \"\u25c0 All configs\"). OrcaStrator's "
       "own card always stays first no matter what's picked here. Takes effect the moment you save "
       "and go back to that screen -- no restart needed."),
    ("Debug", [
        dict(kind="text", label="Debug log directory", path=("debug", "dir"), width=40,
             tooltip="Default location for any processor's debug dump, when that processor's own "
                     "\"debug.enabled\" is on and it hasn't set its own path override (see each "
                     "processor's own Debug section). Empty = each processor falls back to writing "
                     "next to its own script. Browse dumps that land here from the \"Debug Logs\" "
                     "card on the settings landing page."),
        dict(kind="choice", label="Debug log mode", path=("debug", "mode"), options=["single", "multiple"],
             tooltip="single (default) = every processor's own debug dump overwrites the same "
                     "<processor>_debug.json every run, exactly as this always worked. multiple = "
                     "each run writes a new, separately-timestamped file for that processor instead, "
                     "so a history of past runs is kept rather than just the latest. Applies the same "
                     "way to every processor's opt-in debug dump -- one central switch, not something "
                     "set per processor."),
        dict(kind="nullable_number", label="Keep how many logs per processor", path=("debug", "cap"),
             is_int=True, min=1, max=1000, step=1, default=10, auto_placeholder=10, auto_label="Unlimited",
             tooltip="Only used when Debug log mode is \"multiple\". Once a processor has more than "
                     "this many saved dumps, the oldest are deleted automatically right after each new "
                     "one is written. \"Unlimited\" keeps every dump ever written -- nothing is ever "
                     "deleted.",
             show_if=[(("debug", "mode"), "multiple")]),
    ]),
    ("Sounds", [
        dict(kind="bool", label="Play sound when blocked", path=("sounds", "on_error", "enabled")),
        dict(kind="text", label="Blocked sound file", path=("sounds", "on_error", "file"), width=24,
             tooltip="Name to look for in assets/, no extension needed -- \"error\" matches "
                     "assets/error.wav, assets/error.mp3, assets/error.ogg, anything. Plays whenever "
                     "this run had at least one processor failure, which includes a print-abort.",
             show_if=[(("sounds", "on_error", "enabled"), True)]),
        dict(kind="bool", label="Play sound when all clear", path=("sounds", "on_success", "enabled")),
        dict(kind="text", label="All-clear sound file", path=("sounds", "on_success", "file"), width=24,
             tooltip="Name to look for in assets/, no extension needed -- same matching as the "
                     "blocked sound above. Plays whenever every processor in the run succeeded.",
             show_if=[(("sounds", "on_success", "enabled"), True)]),
    ], "Both off by default. Drop a file of any format into the assets/ folder (next to icon.png) "
       "named to match the setting below -- there's no fixed extension to match, whatever's found "
       "wins, so swapping the sound is just replacing the file, no config edit needed unless you're "
       "also renaming it."),
    ("Theme Colors", [
        dict(kind="hex_color", label="Background", path=("theme", "bg"), default="#2b2b2b"),
        dict(kind="hex_color", label="Panel background", path=("theme", "panel_bg"), default="#1e1e1e"),
        dict(kind="hex_color", label="Border", path=("theme", "border"), default="#3f3f3f"),
        dict(kind="hex_color", label="Text", path=("theme", "fg"), default="#e6e6e6"),
        dict(kind="hex_color", label="Dim text", path=("theme", "fg_dim"), default="#9a9a9a"),
        dict(kind="hex_color", label="Accent", path=("theme", "accent"), default="#00A886"),
        dict(kind="hex_color", label="Accent (hover)", path=("theme", "accent_hover"), default="#1DC2A4"),
        dict(kind="hex_color", label="Text on accent", path=("theme", "accent_fg"), default="#ffffff"),
        dict(kind="hex_color", label="Titlebar", path=("theme", "titlebar"), default="#2b2b2b",
             tooltip="Color of the actual OS window titlebar (Windows only -- see apply_dark_titlebar() "
                     "in orcastrator.py). Every window in the app gets this. This settings window's own "
                     "titlebar updates live as you type (that's a plain OS color, not a themed widget, so "
                     "it's safe to push straight to the real window) -- everywhere else (the progress "
                     "window, other dialogs) picks it up next time that window opens."),
        dict(kind="hex_color", label="Text on titlebar", path=("theme", "titlebar_fg"), default="#e6e6e6"),
    ]),
]
