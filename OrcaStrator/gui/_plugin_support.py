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
Shared helpers used by config_editor.pyw AND by gui/*.py plugin modules
that implement a live preview (HAS_PREVIEW = True, see
gui/dock_collision_guard.py for the full convention).

get_in()/set_in() are the plain nested-dict accessors every field spec
and every preview builder needs -- kept in one place so config_editor.pyw
and every plugin share the exact same behavior instead of each carrying
its own copy.

load_processor_module() is for a plugin's build_preview_payload(), which
naturally needs to call into its OWN processor's rendering functions --
that coupling is correct and expected (a plugin knowing about its own
processor is fine; it's config_editor.pyw knowing about a specific
processor that isn't). This just avoids every such plugin re-deriving
the same sys.path/importlib dance to get there.
"""
import importlib.util
import pathlib
import sys

GUI_DIR = pathlib.Path(__file__).resolve().parent
HERE = GUI_DIR.parent
POST_PROCESSORS_DIR = HERE / "post_processors"

if str(POST_PROCESSORS_DIR) not in sys.path:
    # Needed before loading any processor module below: a processor does
    # `from helpers.debug_dump import ...` (helpers/ lives inside
    # post_processors/), which only resolves for free when it's run
    # directly as a script (its own folder becomes sys.path[0]
    # automatically) -- importlib-loading it from outside that folder,
    # as we're doing here, doesn't get that for free.
    sys.path.insert(0, str(POST_PROCESSORS_DIR))


def load_processor_module(name):
    """
    Loads post_processors/<name>.py as a module, the same way
    config_editor.pyw loads orcastrator.py -- not a normal import, since
    this is a plain script layout, not an installed package. Guards its
    own entry point with `if __name__ == "__main__"`, so loading it here
    for its functions has no side effects.
    """
    path = POST_PROCESSORS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_in(cfg, path, default=None):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


def set_in(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value
