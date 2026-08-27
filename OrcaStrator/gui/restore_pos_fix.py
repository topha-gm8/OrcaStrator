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
Landing-page entry for restore_pos_fix.py. It has no settings of its
own to tune -- it always runs the same way -- but it CAN opt into
helpers/debug_dump.py's shared debug-dump feature (see
config_editor.py's _debug_section_for(), which auto-builds a Debug
section for any config shaped like that helper expects), so unlike a
truly config-less processor this one does get a (nearly empty) config
file and form, purely for that section. See gui/orcastrator.py for the
full walkthrough of the convention.
"""

TITLE = "Restore Position Fix"
SUBTITLE = "Annotates toolchange lines with restore coordinates. Debug logging only."
CONFIG = "restore_pos_fix.json"

SECTIONS = []

