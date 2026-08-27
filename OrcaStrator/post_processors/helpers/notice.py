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
Shared per-processor NOTICE console-visibility flag.

Every processor's own local print_notice(level, title, message) wraps
this to decide the value of a "display" key embedded IN the NOTICE
payload itself -- e.g. NOTICE:{"level":"info","title":"...",
"message":"...","display":false} -- rather than deciding whether to
print the NOTICE line at all.

That distinction matters: the notice always gets embedded into the
g-code's ORCASTRATOR_LOG block either way. What changes is only whether
ORCASTRATOR_RENDER (the Klipper
macro, see OrcaStrator_render.cfg's NOTICE handling) chooses to show it
on the printer console. Deciding "don't show this on the console" at
EXPORT time by simply not printing the NOTICE line at all would mean the
notice vanishes everywhere -- gone from the file, gone from
orcastrator.py's own progress log, gone from anything that might read
ORCASTRATOR_LOG back later. Embedding the flag and filtering at
RENDER time instead keeps the notice's data intact (still inspectable
in the file, still whatever a future log-reading tool might want it
for) and only ever affects the one thing this feature is actually
about: printer console clutter.

Shape mirrors helpers/debug_dump.py's "debug": {"enabled": bool}
convention on purpose: config_editor.pyw's _notice_section_for() (the
sibling of that module's own _debug_section_for()) auto-detects a
top-level "notice": {"display": ...} block by shape and builds a
"Notices" GUI section for free -- zero gui/*.py code needed, same
"convention, not configuration" spirit as everything else here. That
config block is this module's ONLY input; it never reaches into the
g-code or the macro side itself.

SAFETY: "abort" level notices always get display=True, no matter what
the config says. An abort notice IS the mechanism
OrcaStrator_render.cfg uses to refuse to print a file -- muting it on
the console would defeat the refuse-to-print check's entire visible
purpose even though the abort itself (action_raise_error) still fires
regardless, since that path in the macro doesn't consult "display" at
all (see that macro's own comment on this). Embedding display=True
here anyway keeps the payload itself internally honest for any future
reader that DOES look at the flag. Call sites don't need to remember
any of this -- see display_flag() below, which enforces it centrally.

No "notice" block at all, or one missing "display", defaults to
showing -- opt-OUT, not opt-in, so upgrading OrcaStrator never changes
anyone's existing console output until they actually touch the new
setting.
"""


def notice_display_enabled(cfg: dict) -> bool:
    """
    cfg is a processor's own already-loaded config dict (the same one
    passed to helpers.debug_dump for its "debug" block). Returns
    whether this processor's info/warning NOTICEs should show on the
    printer console.
    """
    notice_cfg = cfg.get("notice") if isinstance(cfg, dict) else None
    if not isinstance(notice_cfg, dict):
        return True
    return bool(notice_cfg.get("display", True))


def display_flag(level: str, cfg: dict) -> bool:
    """
    The value to embed as this NOTICE's own "display" key. "abort" is
    always True regardless of cfg -- see this module's docstring for
    why that's non-negotiable. Everything else defers to
    notice_display_enabled(cfg).
    """
    if level == "abort":
        return True
    return notice_display_enabled(cfg)
