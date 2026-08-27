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
Settings-form spec for tool_preheat.json -- shared by
insert_missing_tool_preheat.py and disable_unused_tool_temps.py.

See gui/orcastrator.py for the full walkthrough of this convention.
"""

TITLE = "Tool Preheat"
SUBTITLE = "Shared by insert_missing_tool_preheat.py and disable_unused_tool_temps.py."
CONFIG = "tool_preheat.json"

SECTIONS = [
    ("Preheat Lead Time (shared by 2 processors)", [
        dict(kind="number", label="Target lead time (seconds)", path=("target_lead_seconds",),
             min=0, max=600, step=5,
             tooltip="How many seconds before a tool needs heat its heater should start ramping. Used by "
                     "BOTH insert_missing_tool_preheat.py (lead time before a tool's first use) and "
                     "disable_unused_tool_temps.py (reheat lead time, and default idle-gap threshold unless "
                     "overridden there) -- one shared knob so the two settings can't drift out of sync."),
    ]),
    ("Idle Cooldown (disable_unused_tool_temps.py only)", [
        dict(kind="number", label="Target cooldown time (seconds)", path=("target_cooldown_seconds",),
             min=0, max=600, step=5,
             tooltip="How many seconds it takes a tool's heater to actually cool down once commanded off. "
                     "Only used by disable_unused_tool_temps.py's AUTO idle-gap threshold (i.e. when its own "
                     "idle_shutoff_minutes is left unset) -- the threshold becomes target lead time above PLUS "
                     "this, so a tool only gets cycled off if the idle gap is long enough to both finish cooling "
                     "down and still regain a full reheat lead time before it's needed again. Has no effect if "
                     "idle_shutoff_minutes is set, and insert_missing_tool_preheat.py never reads it."),
    ]),
]
