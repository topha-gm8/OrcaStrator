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
Settings-form spec for disable_unused_tool_temps.json.

See gui/orcastrator.py for the full walkthrough of this convention.
"""

TITLE = "Disable Unused Tool Temps"
SUBTITLE = "Idle-cooldown threshold for tools sitting unused mid-print."
CONFIG = "disable_unused_tool_temps.json"

SECTIONS = [
    ("Idle Shutoff", [
        dict(kind="nullable_number", label="Idle shutoff threshold", path=("idle_shutoff_minutes",),
             min=1, max=180, step=1, is_int=True, unit="minutes", auto_placeholder=10,
             tooltip="How many minutes a tool must sit unused before it's worth cooling it down mid-print "
                     "(and scheduling a reheat ahead of its next use). Check 'Auto' to instead derive this "
                     "from tool_preheat.json's target lead time plus its target cooldown time -- a tool "
                     "then only gets cycled off if the idle gap is long enough to actually finish cooling "
                     "down and still regain its full reheat lead time before it's needed again."),
    ]),
]
