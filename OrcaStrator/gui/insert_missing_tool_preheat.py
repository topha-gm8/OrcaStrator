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
Settings-form spec for insert_missing_tool_preheat.json.

Its one real setting, target_lead_seconds, lives in the SHARED
tool_preheat.json -- see gui/tool_preheat.py, the form for THAT file --
so this one has an empty SECTIONS list. It exists purely to give this
processor a landing-page home for its own opt-in Debug section (see
config_editor.py's _debug_section_for(), which auto-builds that section
for any config shaped like helpers/debug_dump.py expects -- zero extra
GUI code needed here beyond registering the file).

See gui/orcastrator.py for the full walkthrough of this convention.
"""

TITLE = "Insert Missing Tool Preheat"
SUBTITLE = "First-use preheat lead-time check. Debug logging only -- see Tool Preheat for its actual setting."
CONFIG = "insert_missing_tool_preheat.json"

SECTIONS = []
