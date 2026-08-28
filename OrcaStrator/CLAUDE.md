# slicer_postprocessing

Post-processing pipeline that runs at OrcaSlicer export time (on the PC,
not on the printer). `orcastrator.py` -- "OrcaStrator" -- is the pipeline
runner; it discovers and runs every processor in `post_processors/`, then
embeds their combined output into the g-code file for the printer-side
Klipper macro (`ORCASTRATOR_RENDER`) to read and render.

**The Klipper macro has zero knowledge of what any individual processor
does.** It only understands two generic stdout conventions, documented
below. A new processor that follows them needs no changes anywhere else
in this system -- not to OrcaStrator, not to the `.cfg` macro.

## Layout

```
OrcaStrator/
├── orcastrator.py                       ← the pipeline runner ("OrcaStrator"), do not rename
├── config_editor.pyw                    ← OrcaStrator Settings GUI engine, see below
├── assets/                              ← static assets, not code
│   ├── icon.png                           ← app/window icon
│   ├── success.wav                        ← played on a successful run
│   └── error.wav                          ← played on a failed run
├── configs/                             ← every processor's .json config lives here, see below
│   ├── orcastrator.json                   ← config: orcastrator.py's own settings
│   ├── tool_preheat.json                  ← config, shared by 2 processors, see below
│   ├── insert_missing_tool_preheat.json   ← config, debug block only (its real setting is above)
│   ├── disable_unused_tool_temps.json     ← config
│   ├── restore_pos_fix.json               ← config, debug block only (no other settings)
│   ├── dock_collision_guard.json          ← config
│   ├── gcode_template_notice.json         ← config, user-authored templates live here
│   ├── tool_temperature_graph.json        ← config, see "Timeline SVG processors" below
│   ├── toolchange_heatmap.json            ← config, see "Timeline SVG processors" below
│   ├── gui_state.json                     ← NOT a processor config -- window-geometry memory
│   │                                          for config_editor.pyw, see gui/_window_anchor.py
│   └── backups/                           ← auto-saved pre-edit snapshots, written by
│                                              config_editor.pyw itself, not hand-maintained
├── gui/                                 ← every processor's settings-FORM spec lives here,
│   │                                        auto-discovered by config_editor.pyw -- see below
│   ├── orcastrator.py                     ← form spec for configs/orcastrator.json
│   ├── tool_preheat.py                    ← form spec for configs/tool_preheat.json
│   ├── insert_missing_tool_preheat.py     ← form spec for configs/insert_missing_tool_preheat.json
│   │                                          (empty SECTIONS -- debug-only, see below)
│   ├── disable_unused_tool_temps.py       ← form spec for configs/disable_unused_tool_temps.json
│   ├── restore_pos_fix.py                 ← form spec for configs/restore_pos_fix.json
│   │                                          (empty SECTIONS -- debug-only, see below)
│   ├── dock_collision_guard.py            ← form spec for configs/dock_collision_guard.json,
│   │                                          INCLUDING its live SVG preview -- see below
│   ├── gcode_template_notice.py           ← form spec for configs/gcode_template_notice.json,
│   │                                          INCLUDING its live template-preview panel
│   ├── gcode_template_notice_sample.gcode ← sample g-code fed to that live preview, not code
│   ├── tool_temperature_graph.py          ← form spec for configs/tool_temperature_graph.json
│   ├── toolchange_heatmap.py              ← form spec for configs/toolchange_heatmap.json
│   ├── _window_anchor.py                  ← shared window-geometry memory (reads/writes
│   │                                          configs/gui_state.json) -- leading underscore
│   │                                          keeps it out of auto-discovery, see below
│   └── _plugin_support.py                 ← shared helpers for any gui/*.py needing a live
│                                              preview (get_in/set_in, load_processor_module) --
│                                              leading underscore keeps it out of auto-discovery,
│                                              see below
└── post_processors/
    ├── disable_unused_tool_temps.py
    ├── insert_missing_tool_preheat.py
    ├── restore_pos_fix.py
    ├── dock_collision_guard.py
    ├── gcode_template_notice.py
    ├── tool_temperature_graph.py
    ├── toolchange_heatmap.py
    ├── logs/                             ← debug-dump output, written by helpers/debug_dump.py,
    │                                        not hand-maintained -- see Debug dumps below
    └── helpers/
        ├── poly_tools.py                  ← unused utility library, kept for future
        │                                     rasterization needs -- see below
        ├── time_estimator.py              ← shared naive time-estimation/calibration
        │                                     model, used by insert_missing_tool_preheat.py
        │                                     and disable_unused_tool_temps.py
        ├── timeline_scale.py              ← shared timeline-SVG calibration (tool_change_time/
        │                                     print_duration/naive_time_scale_factor) and canvas-
        │                                     geometry math, used by tool_temperature_graph.py
        │                                     and toolchange_heatmap.py so their x-axes stay in
        │                                     sync -- see "Timeline SVG processors" below
        ├── run_guard.py                   ← shared "did I already run against this
        │                                     file" check, see Idempotency below
        ├── debug_dump.py                  ← shared debug-dump writer, see
        │                                     Debug dumps below
        ├── notice.py                      ← shared per-processor NOTICE console-visibility
        │                                     flag
        └── placeholders.py                ← shared placeholder registry + small whitelist
                                               expression language for user-authored templates,
                                               used by gcode_template_notice.py -- deliberately
                                               not Python eval, see that module's docstring
```

Every `gui/*.py` file is named after the **config** it edits, matching its
`CONFIG` value 1:1 (`gui/dock_collision_guard.py` -> `configs/dock_collision_
guard.json`) -- this happens to also match `post_processors/dock_collision_
guard.py`'s own name, but that's a coincidence of this one processor, not a
rule: `post_processors/` files are named after a **processor**, `gui/` files
after a **config**, and nothing requires them to line up.

## Settings GUI

`config_editor.pyw` ("OrcaStrator Settings") is a standalone Tk app for
editing every `.json` config in one place -- checkboxes for booleans,
dropdowns for enums, color pickers for colors, and a validated
Auto/override control for nullable settings, instead of hand-editing
JSON. `python3 config_editor.pyw` opens a landing page listing every known
config; `python3 config_editor.pyw <path>` jumps straight into whichever
one matches. Any `.json` file found in `configs/` that isn't claimed by
a `gui/*.py` spec still shows up on the landing page automatically (as
a raw-JSON editor, JSON-syntax validated on save) -- so a brand-new
processor's config is reachable here immediately even before someone
writes it a proper settings form. See
**Giving a new processor settings, and hooking it into the GUI** below
for the full walkthrough.

The landing page also has a "Debug Logs" card -- a read-only viewer over
whatever `*_debug.json` dumps exist, see **Debug dumps** below. It's the
one remaining `CONFIG_REGISTRY` entry hand-wired directly in
`config_editor.pyw` (`kind="logviewer"`, `path=None`) rather than something
`gui/*.py` auto-discovery produces, since it isn't a config editor at all --
every actual processor's entry, `dock_collision_guard.json` included, comes
from `gui/*.py` now (see `discover_gui_specs()`).

The progress window itself normally stays open, waiting on a
Continue/Close click, any time it has something worth a look -- an SVG
preview, or a failure. `orcastrator.json`'s `auto_close.enabled`/`.seconds`
(OrcaStrator Settings -> Progress Window) can optionally close it on its
own a few seconds after a FULLY successful run instead, even with an SVG
preview on screen -- never on a failed run, that always still needs a
manual Close regardless of this setting. Off by default. Clicking
anywhere on the window while it's counting down cancels it for that run
-- see `_TkProgressUI._start_auto_close_countdown()` in `orcastrator.py`.

`dock_collision_guard.json` additionally gets a live SVG preview
(re-rendered on every change using the actual rendering code, not a
reimplementation) with No collision/Near miss/Collision scenario buttons
-- see `HAS_PREVIEW`/`PREVIEW_CONTROLS`/`build_preview_payload()` in
`gui/dock_collision_guard.py` for how that synthetic scenario is generated,
and **Giving a new processor a live preview** below for the generic
contract any processor can implement the same way. A new config's
`gui/*.py` spec gets `KIND="simple"` (a generic field-spec form, the
common case, and also the default if left unset) or `KIND="none"` for a
processor with nothing to configure; `KIND="rich"` means it ALSO
implements `HAS_PREVIEW` -- unlike `"simple"`/`"none"`, this is still an
opt-in a new processor writes real Python for (see below), but it's
entirely inside that processor's own `gui/*.py` file now, same as
everything else -- `config_editor.pyw` itself has no per-processor code
left in it at all, `dock_collision_guard.json` included.

Every config's own JSON comments double as GUI tooltips automatically:
a sibling `"_key"` string next to a real key is picked up by
`lookup_comment()` with zero extra code; only fields whose only
documentation is a comment shared across several sibling keys need an
explicit `tooltip=` in their field spec (see `gui/orcastrator.py` for an
example of the automatic path, `config_editor.pyw`'s `SECTIONS` for
examples of the explicit one). Follow this same sibling-comment
convention in any new config's JSON and its tooltips need no extra
plumbing either.

Every `.json` config lives in `configs/`, one folder up from
`post_processors/` and grouped together instead of scattered next to
whichever script happened to introduce it -- keeps the top level of
`orcastrator/` itself down to just the runnable/registered
scripts (`orcastrator.py`, `config_editor.pyw`) plus
the `configs/` and `post_processors/` folders.

Only the top level of `post_processors/` is auto-scanned (`*.py`,
non-recursive) -- subfolders are invisible to it. Anything that isn't a
runnable processor (shared libraries, config files) belongs in
`post_processors/helpers/`, `configs/`, or one level up next to
OrcaStrator -- any of these keeps it out of the scan with zero extra
config, no `denylist` entry needed (that still exists as an override in
`orcastrator.json` -- editable from OrcaStrator Settings' Processor
Selection section -- if you ever DO need to exclude something sitting
directly in `post_processors/`, but it's empty by default now).

Companion `.json` configs (like `dock_collision_guard.json`) are looked
for in exactly one place: `configs/`, next to OrcaStrator.

See `load_config()` in `dock_collision_guard.py`, or
`find_config_path()` in `helpers/time_estimator.py` (used by every
processor that reads a config by explicit filename rather than its own
script name -- the two sharing `tool_preheat.json`, and any processor
whose config is debug-block-only, like `restore_pos_fix.json`), for the
pattern if you want the same lookup in a new processor.

## The processor contract

A processor is a normal standalone script:

```
<python> post_processors/your_script.py <gcode_path>
```

- Read/modify the file at `<gcode_path>` in place.
- Exit `0` on success, non-zero on failure. An uncaught exception is
  fine -- Python already exits non-zero for you, and see "auto-abort"
  below for what happens next.
- That's the entire contract. `restore_pos_fix.py` and
  `disable_unused_tool_temps.py` predate this whole system and needed
  zero changes to slot in.

Every processor runs under `sys.executable` -- i.e. whatever Python is
running OrcaStrator itself. Point OrcaSlicer at the one interpreter
that has whatever packages your processors need and every processor
inherits it automatically. No per-script interpreter mismatches.

## Talking to the printer: two stdout conventions

Both are optional. Print as many lines of either kind as you want, or
none at all.

### `SVG_PAYLOAD:{...}`

One compact JSON object per line. Rendered on the printer as its own
console message (title + canvas), via Klipper's `_SVG_TOOLS`.

```json
{
  "title": "My check",
  "canvas": {"x_max": 120.0, "y_max": 260.0, "pad": 5.0, "max_size": 260},
  "shapes": [
    {"type": "polygon", "points": [[0,0],[10,0],[10,10]],
     "fill": "rgba(255,60,60,0.30)", "stroke": "rgba(255,60,60,0.9)",
     "fill_style": "hatch", "hatch_angle": 45, "hatch_spacing": 4},
    {"type": "crosshair", "x": 5.0, "y": 5.0, "color": "yellow", "size": 4.0}
  ]
}
```

- `canvas.x_max`/`y_max` define the coordinate space; keep it tight to
  whatever's actually relevant. **Don't just use the full print's
  extents** -- if your shapes only matter in a small region (like "near
  the dock"), a canvas sized to the whole build volume squeezes your
  actual content into a corner. `dock_collision_guard.py`'s
  `canvas_clip` handling in `build_svg_payload()` is the pattern to
  copy: only include points that are actually in-scope before computing
  extents.
- `fill_style: "hatch"` needs `hatch_angle`/`hatch_spacing`; `"solid"`
  needs neither.
- Optional `"targets": ["printer", "pc"]` controls where a given payload
  actually shows up. `"printer"` means OrcaStrator embeds it in the
  g-code for `ORCASTRATOR_RENDER` to pick up; `"pc"` means the
  OrcaStrator's own tkinter progress window renders it directly, right
  there at export time. Include either, both, or (by emitting the line
  at all) at least one -- there's no reason to print an `SVG_PAYLOAD`
  line for a payload with neither. Omitting `"targets"` entirely defaults
  to both, matching the original (pre-targeting) behavior. See
  `dock_collision_guard.py`'s `svg.display` config and
  `classify_status()`/`resolve_targets()` for the pattern of mapping your
  own result states onto this -- it's generic, OrcaStrator has zero
  special-cased knowledge of what "collision" or "near_miss" mean.
- Need a silhouette polygon from a raw Y/Z toolpath trace? Prefer the
  layer-based technique in `dock_collision_guard.py`
  (`group_min_max_y_by_z` + `build_object_silhouette_polygon`) over
  `poly_tools.py`'s rasterization -- bucket points by Z (round to a few
  decimals to absorb float noise), keep min/max Y per bucket, then walk
  the min-Y side up in ascending Z, cross at the top, walk the max-Y
  side back down. Pure Python, no numpy/scipy, and it's exact per-layer
  geometry rather than an approximation of one (the only real
  imprecision is a genuine same-Z gap in an object's Y-extent getting
  bridged over, which the rasterized approach below approximates too, just
  differently). `post_processors/helpers/poly_tools.py`
  (`rasterize_lines`/`raster_outline`) still exists and still works if
  you have a case this doesn't fit -- currently no processor actually
  imports it (`dock_collision_guard.py` uses the layer-based technique
  instead) -- e.g. you actually need the X extent too, not just Y/Z --
  but for anything shaped like "toolhead position over Z", reach for the
  layer technique first.

### `NOTICE:{...}`

One compact JSON object per line.

```json
{"level": "info", "title": "Dock check OK", "message": "no collisions detected."}
```

`level` is one of:

| level     | printer-side effect                                              |
|-----------|-------------------------------------------------------------------|
| `info`    | shown as a plain console message. Non-fatal.                     |
| `warning` | shown as a plain console message. Non-fatal. (Currently rendered the same as `info` -- distinguish further in the macro if you need visually different treatment.) |
| `abort`   | **print refused.** The Klipper macro raises an error and the file never starts printing. Multiple `abort` notices (from the same or different processors) get combined into one final message, so none of them get silently dropped. |

You do not need to add a `"source"` field -- OrcaStrator tags every
notice with the emitting processor's filename automatically before
embedding it.

**Auto-abort on unexplained failure:** if your processor exits non-zero
and didn't emit its own `abort`-level notice, OrcaStrator
synthesizes one for you ("`'your_script.py' exited with code N and
didn't explain why`"). You don't need to wrap every possible exception in
a try/except that emits a notice just to be safe -- an uncaught exception
already produces the fail-safe outcome. Emit your own `abort` notice when
you want the printer-side message to actually explain *why*, which you
should do for anything you expect to legitimately trigger.

## Execution order

1. Anything listed in `EXPLICIT_ORDER` in `orcastrator.py`,
   in that exact order.
2. Everything else in `post_processors/*.py`, alphabetically.
3. Anything listed in `EXPLICIT_ORDER_LAST` in `orcastrator.py`,
   in that exact order.

**Check for ordering dependencies before adding a new processor.**
`restore_pos_fix.py` has to run before `disable_unused_tool_temps.py`
because the latter's idle-cooldown/reactivation-preheat feature shares a
naive time-estimation model (`post_processors/helpers/time_estimator.py`)
with `insert_missing_tool_preheat.py`, and that model resyncs its X/Y/Z
tracking from the `T3 X=.. Y=.. Z=..` annotations `restore_pos_fix.py`
adds to toolchange lines -- run it before and lead-time placement gets
less accurate (dock-move distance leaks into the estimate instead of
getting resynced away). That's why those two are pinned in
`EXPLICIT_ORDER`, in that order. `insert_missing_tool_preheat.py` has the
same dependency but doesn't need its own `EXPLICIT_ORDER` entry --
anything not listed there always runs after everything that is (see
`discover_processors()`), so as long as `restore_pos_fix.py` stays pinned,
it's covered regardless of alphabetical position.

If your new processor reads or depends on a specific line format that an
earlier processor might rewrite (T-lines, comments, anything), check what
runs before it and either add yourself to `EXPLICIT_ORDER` or confirm the
existing order already protects you. When in doubt, test both orderings
directly against a small synthetic file and diff the output -- that's how
the dependencies above were actually found and confirmed, not by
reasoning about it in the abstract.

## Timeline SVG processors

**Any processor that draws a time-based timeline SVG (`tool_temperature_
graph.py`, `toolchange_heatmap.py`, and any future one) MUST calibrate
its timing and canvas geometry through `post_processors/helpers/
timeline_scale.py`** -- `calibrate_timeline(lines, cfg,
toolchange_line_idxs)` for `tool_change_time`/`print_duration`/
`naive_time_scale_factor` (returns a `Timeline` with a `time_at(line_idx)`
method), and `resolve_canvas_dims(cfg, default_width_px,
default_height_px)` for the canvas-unit math.

This exists because independently calibrating this logic per-processor
is an easy way to drift apart: `naive_time_scale_factor()` has a subtle
input that's easy to get wrong -- whether `toolchange_line_idxs`/
`tool_change_time` are passed (subtracting known toolchange overhead
before turning the residual into a ratio) or not. Skip that argument in
one caller and not the other and the same g-code line lands at a
different calibrated timestamp depending on which processor you asked,
even though both are graphing the exact same print. Going through one
shared call site makes that class of drift
impossible: every timeline processor gets the same `tool_change_time`,
`print_duration`, and `naive_scale` for a given file, so their x-axes can
be compared or overlaid directly.

`toolchange_line_idxs` passed into `calibrate_timeline()` must be the
FULL, sorted set of every toolchange's line index -- including any event
a processor ignores for its own display purposes (e.g.
`toolchange_heatmap.py`'s `ignore_first_toolchange`) -- since
`naive_time_scale_factor()` needs the complete set to separate known
toolchange overhead from generic move-time error.

**Pinning a processor last instead of first.** `EXPLICIT_ORDER_LAST` is
for the opposite case -- a processor that needs to run after everything
else, e.g. a read-only reporting step that wants the final state of the
g-code once every other processor has already had its turn. The
alternative -- listing every OTHER processor ahead of it in
`EXPLICIT_ORDER` -- silently goes stale the moment
someone adds a new processor and forgets to add it there too.
`EXPLICIT_ORDER_LAST` doesn't have that problem -- anything not listed
in either list already runs in between (see "Execution order" above), so
a processor pinned in `EXPLICIT_ORDER_LAST` stays last regardless of what
gets added later. If a name ends up in both lists, `EXPLICIT_ORDER` wins
and `discover_processors()` prints a note -- that combination almost
certainly isn't intentional. Both lists are editable from the settings
GUI (OrcaStrator -> Processor Selection -> "Runs first"/"Runs last"),
same pick-from-disk pickers, not free-text.

**Sharing a config value across processors.** `tool_preheat.json`'s
`target_lead_seconds` is intentionally read by both
`insert_missing_tool_preheat.py` (lead time before a tool's first use) and
`disable_unused_tool_temps.py` (default idle-gap threshold + reactivation-preheat
lead time) -- one knob so the two can't drift out of sync. It's named
`tool_preheat.json` rather than after either individual script for that
reason -- a config file used by more than one processor shouldn't look
like it belongs to just one of them. If a new processor wants to share a
config value like this rather than defining its own, read the other
script's `.json` by name (same dual-location lookup, via
`find_config_path()` in `helpers/time_estimator.py`) instead of creating a
duplicate config with its own default that can go stale.

## Idempotency

A processor may run more than once against the same file in practice --
someone manually re-running `orcastrator.py` over an
already-processed export while testing, most commonly. Two ways to
handle that, and both are worth having:

**1. Make the processor idempotent on its own merits, if that's
straightforward.** `insert_missing_tool_preheat.py` does this: before
touching a tool, it checks whether that tool's *current* lead time
(from any preheat already there) already meets the target, and skips if
so. Re-running settles instead of stacking duplicate inserts.

**2. Skip outright if OrcaStrator's own run log shows you already
ran, via `helpers/run_guard.py`'s `already_processed(lines, script_path)`.**
This checks for an `; {your_script_name}: ...` line in
`ORCASTRATOR_LOG` (see `orcastrator.py`'s
`prepend_run_log()`) -- a coarser, cheaper check than #1 ("did I run
against this file at all" rather than "is there still something left to
do"), and a useful backstop when a processor's own per-item idempotency
logic is harder to get airtight (`disable_unused_tool_temps.py` uses
this, alongside its own per-tool logic, for exactly that reason). Always
pass your own `script_path` through rather than hardcoding your
filename as a string -- if the script ever gets renamed, a hardcoded
name silently stops matching.

Note what #2 does NOT cover: a processor invoked standalone and
repeatedly outside OrcaStrator (e.g. running a `.py` file by hand
against the same file twice while developing it) -- nothing writes that
log tag except OrcaStrator itself, so that workflow still needs a
fresh copy of the file each time, same as always.

## Debug dumps: a universal opt-in feature

Any processor can optionally write a structured JSON snapshot of its own
last run, for a "why didn't this do what I expected" question to be
answered from real, exact data instead of a screenshot or a description
of one. `helpers/debug_dump.py` provides this generically, so any
processor can opt in the same way. Nothing about this is required -- a
processor with no `debug` key in its config simply doesn't have the
feature.

**To opt in:**

1. Add a `debug` block to your processor's own `configs/your_script.json`,
   same shape `dock_collision_guard.json` uses:
   ```json
   "_debug": "What this run's dump captures and why it's useful.",
   "debug": {
       "_enabled": "Whether to write a debug dump on every run.",
       "enabled": true,
       "_path": "Optional override for THIS processor's dump location only. Empty = use the central Debug Log Directory in OrcaStrator Settings, or next to this processor's script if that's also empty.",
       "path": ""
   }
   ```
   `config_editor.pyw` auto-detects any config with a `debug.enabled` key
   (specifically that key, not just a `debug` key -- see
   `_debug_section_for()`'s docstring for why, it's what stops this
   colliding with `orcastrator.json`'s own unrelated `debug.dir` setting)
   and builds a standard "Debug" GUI section for it -- a single enable
   toggle, tooltipped, all with zero GUI code of your own. This is on
   top of whatever `SECTIONS` your `gui/your_script.py` already defines,
   not instead of it.

2. Call `write_debug_dump(processor_name, debug_cfg, data, script_path)`
   at the end of a run, with whatever dict is useful to dump. `debug_cfg`
   is that processor's own `debug` sub-object read from its config;
   `processor_name` becomes the output filename
   (`<processor_name>_debug.json` -- always per-processor, never a fixed
   name, since more than one processor's dump can now land in the same
   directory and a fixed name would mean the second one silently
   clobbers the first's on every run).

**Where the file lands** (see `write_debug_dump()`'s own docstring for
the full detail): the central `debug.dir` in `configs/orcastrator.json`
(OrcaStrator Settings → Debug), if set → next to the processor's own
script in `post_processors/`, the original no-configuration-needed
default. The central directory is one shared setting so every opted-in
processor's dumps land together and are easy to find/compare -- and so
OrcaStrator Settings' "Debug Logs" landing-page card (a read-only
viewer, click a file to preview it) has one place to look. There used
to also be a per-processor `debug.path` override ahead of both of
these; it's gone now -- it let a processor's dumps land somewhere the
Log Viewer (and the historical-log pickers in
`gui/tool_temperature_graph.py`/`gui/toolchange_heatmap.py`) never
looked, so removing it means every opted-in processor's dumps are
always findable in one of exactly two places.

`helpers/debug_dump.py` deliberately does NOT import `orcastrator.py` to
read the central directory -- it re-reads `configs/orcastrator.json`
directly instead. `orcastrator.py` already imports/discovers processors,
not the other way around, and every processor still has to stay
runnable standalone (see the processor contract above) -- importing it
back from a processor would risk a circular import and drags tkinter
into a process that has no business needing it.

## Filenames: don't trust the path argument

OrcaSlicer invokes processors against a temp upload file with a
meaningless name (e.g. `.OrcaSlicer.upload.fd82-4837-be65-c56d`), not the
real output filename -- that only gets applied afterward. For anything
user-facing (alert popups, notice messages, embedded titles), use:

```python
import os, pathlib

def friendly_filename(p: pathlib.Path) -> str:
    real = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if real:
        return pathlib.Path(real).name
    return p.name
```

(copy this pattern -- see `dock_collision_guard.py` for the
working version). `SLIC3R_PP_OUTPUT_NAME` is a PrusaSlicer-lineage
convention OrcaSlicer inherited; it holds the real intended name/path.

## You shouldn't need a native OS alert popup

OrcaSlicer's own post-processing-failure dialog is a fixed, generic
string ("Post-processing script X on file Y failed. Error code: N") --
it cannot be customized, and doesn't surface your notice text. Older
versions of `dock_collision_guard.py` popped a second, separate
native dialog (`ctypes.windll`/`osascript`/`notify-send`) to work around
that. That's gone now -- OrcaStrator's own tkinter progress window
already stays open (Close button, no auto-dismiss) whenever a processor
fails OR emits a `"pc"`-targeted `SVG_PAYLOAD`, which covers the same
need without a redundant second popup. Give an `abort` notice a plain,
human-readable `message` (no raw coordinates or other numbers a person
can't quickly picture -- if you have a `SVG_PAYLOAD` to point at, say so
and let the visualization carry the specifics) and the window handles
making sure it's actually seen.

If you're doing something unusual enough that you genuinely need a
popup independent of OrcaStrator (e.g. a processor meant to run
completely standalone, outside this pipeline), the pattern is still
straightforward -- wrap `ctypes.windll.user32.MessageBoxW` (Windows) /
`osascript -e 'display alert ...'` (macOS) / `notify-send` (Linux) in a
broad `except Exception: pass`, same as any other best-effort display
mechanism here.

## Testing a new processor

Standalone, against a synthetic file:

```bash
python post_processors/your_script.py test.gcode
cat test.gcode        # inspect what it changed
echo $?                # check exit code
```

Through the full pipeline (catches ordering issues, confirms your
`SVG_PAYLOAD`/`NOTICE` lines get picked up and embedded correctly):

```bash
python orcastrator.py test.gcode
grep -A2 "NOTICE:" test.gcode
grep -A2 "SVG_PAYLOAD:" test.gcode
```

If you're testing an ordering dependency specifically, run your new
processor both before and after whatever it might conflict with, on the
same input, and diff the two outputs -- don't just reason about whether
it *should* be fine.

### Testing a GUI layout change for real (not by reasoning about grid math)

`config_editor.pyw` and `orcastrator.py`'s progress window are both
Tkinter, and this sandbox has no display by default (`import tkinter`
fails outright) -- previous sessions had no way to verify a layout fix
actually worked and had to reason about `pack`/`grid` behavior
theoretically, which is exactly how the `right_list.pack()`
default-center bug (see the picker-alignment fixes above) went
unnoticed for as long as it did: the theoretical reasoning said it
should already be aligned, and it was wrong.

It's now possible to get a **real, live Tk render** in this sandbox:

```bash
apt-get install -y python3-tk        # tkinter itself isn't preinstalled
# Xvfb (virtual framebuffer) was already present, no install needed --
# check with `pgrep Xvfb` / `which Xvfb` first before assuming you need it
Xvfb :99 -screen 0 1300x1000x24 >/tmp/xvfb.log 2>&1 &
sleep 2                               # give it a moment before connecting
DISPLAY=:99 python3 your_script.py
```

Gotchas that cost real time working this out:

- **Xvfb dies between separate `bash_tool` calls.** Each call may be a
  fresh shell, killing anything backgrounded with a bare `&` in a
  previous call. Start Xvfb and run/inspect the app within the SAME
  `bash_tool` invocation, or accept you'll need to restart it
  (`pgrep Xvfb || (Xvfb :99 ... &)`) at the top of each subsequent call.
- **`.pyw` won't load via `importlib.util.spec_from_file_location`** --
  it returns `None` (no recognized loader for that extension). Use
  `importlib.machinery.SourceFileLoader(name, path)` +
  `importlib.util.spec_from_loader(...)` instead.
- **Don't just screenshot -- read real widget geometry.** Once a
  `SettingsApp`/`_TkProgressUI` instance exists and `.update()` has
  run, `widget.winfo_x()` / `winfo_rootx()` / `winfo_width()` etc. are
  ACTUAL pixel values from the real Tk layout engine, not a guess.
  Walking `winfo_children()` recursively and dumping class/geometry
  for everything is enough to catch a misalignment precisely (e.g.
  comparing `winfo_rootx()` across the four picker listboxes caught
  the `x=102` offset directly, no eyeballing needed).
- **Screenshots need `imagemagick`'s `import -window root` against the
  Xvfb display** (`xwd` isn't installed and isn't worth adding).
  `DISPLAY=:99 import -window root /tmp/shot.png`. Give the app a few
  real seconds to map before capturing -- a screenshot taken
  immediately after launch (or right as the process exits) comes back
  as a near-empty few-hundred-byte PNG instead of real content; a
  `time.sleep(10)` in the script itself with the capture happening
  from another shell mid-sleep is the reliable pattern.
- This is genuinely new capability, not something available in every
  session by default -- always check `python3 -c "import tkinter"` and
  `pgrep Xvfb` / `which Xvfb` before assuming either needs installing
  from scratch, but don't assume they're unavailable either.

## Minimal template

```python
#!/usr/bin/env python3
"""
<what this checks/changes and why>
"""
import json
import pathlib
import sys


def print_notice(level: str, title: str, message: str) -> None:
    print("NOTICE:" + json.dumps({"level": level, "title": title, "message": message}, separators=(",", ":")))


def process(gcode_path: str) -> None:
    p = pathlib.Path(gcode_path)
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()

    # ... inspect / modify `lines` ...

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print_notice("info", "Your check name", "Everything looked fine.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python your_script.py <gcode_file>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[-1])
```

For anything that should be able to block a print, replace the final
`print_notice(...)` call with your real logic:

```python
    if problem_detected:
        print_notice("abort", "Your check name", f"Specific, actionable reason: {details}")
        sys.exit(1)
    print_notice("info", "Your check name", "Everything looked fine.")
```

## Giving a new processor settings, and hooking it into the GUI

Skip this whole section if your processor has no real settings AND
isn't opting into the debug-dump feature below -- it still shows up in
the GUI automatically, with just a title/subtitle and an info blurb.
The one-file pattern to copy is `KIND = "none"`, no `CONFIG`, no
`SECTIONS`:

```python
TITLE = "Your Processor"
SUBTITLE = "One-line description shown on the landing page."
KIND = "none"
INFO = ("This processor has no configurable settings -- it always runs the same way. "
        "Listed here so every processor has a central home, even ones with nothing to tune.")
```

(No processor currently ships with nothing at all to configure --
`restore_pos_fix.py` and `insert_missing_tool_preheat.py` look like
they'd qualify, but both opted into the debug-dump feature below, which
needs a `CONFIG` + empty `SECTIONS` instead. See either one for that
narrower "nothing to tune, but debug-dump-capable" pattern.)

**1. Write the config JSON, in `configs/`.** Name it after your script
(`your_script.json`) unless you're deliberately sharing one config
across multiple processors like `tool_preheat.json` does. Document every
key with a sibling `"_key"` string comment right next to it -- this is
what makes tooltips in the GUI automatic, zero extra code:

```json
{
    "_comment": ["One or more lines describing the config as a whole."],

    "_your_setting": "What this controls, and what changing it does.",
    "your_setting": 42
}
```

A top-level `"_comment"` (string, or list of strings for multiple
paragraphs) becomes the description panel shown above a simple editor's
fields. It's read via `_joined_comment()` and `cfg.get("_comment")` in
`config_editor.pyw`. Anything documented ONLY by a comment that's shared
across several sibling keys at once (rather than one comment per key)
needs an explicit `tooltip=` in that field's spec instead of relying on
the automatic lookup -- see `lookup_comment()`'s docstring in
`config_editor.pyw` for exactly which shape it can and can't find on its
own, and `gui/orcastrator.py` vs. `config_editor.pyw`'s `SECTIONS` list
for examples of both the automatic and explicit paths.

Section HEADINGS (the `Labelframe` titles, not individual fields) get
tooltips the same automatic way, via `lookup_section_comment()` --
resolved from whichever object all of that section's fields live inside
(their deepest shared path). It accepts either comment style already in
use across these files: a sibling `"_<key>"` next to the object (dock_
collision_guard.json's convention, used at every level, e.g. `"_svg"`
documenting `"svg"`), OR an internal `"_comment"` key living INSIDE the
object itself (orcastrator.json's convention for its `window`/`on_error`/
`theme` sub-objects). Use whichever reads better for a given object --
both are picked up with zero extra code, same as field tooltips. If a
section's fields don't share one single nested object (a flat toggle
grouped alongside a nested sub-object's fields, say), the automatic
lookup won't find anything useful on its own -- give that section tuple
a third element instead: either a literal string (used as-is) or a path
tuple to look up directly, e.g. `("Progress Window", [...], ("window",))`
or `("Status Display", [...], "some hand-written summary")`. See either
`config_editor.pyw`'s `SECTIONS` (the `Clearance Zones -- Collision`/
`-- Near Miss` entries, an explicit-path example since dock_collision_
guard.json happens to document both under one shared
`"_collision_and_near_miss"` key) or `gui/orcastrator.py`
(`"Progress Window"`, an explicit-path example because it mixes a flat
toggle with a nested sub-object) for the pattern.

**2. Load it with the standard lookup.** Reuse `find_config_path()` from
`helpers/time_estimator.py` (or copy `load_config()` from
`dock_collision_guard.py` if you'd rather not import from
`helpers/`) so your processor picks up `configs/your_script.json` the
same way every other processor does. Missing file should mean "use
built-in defaults", not a crash -- see either existing implementation
for the pattern, and validate each field independently with its own
fallback (same reasoning as `orcastrator.py`'s
`load_orcastrator_config()`: a single bad value should never take out
the whole config, let alone the whole export).

**3. Write its settings-form spec as a new file in `gui/`, named after
the config it edits** (`gui/your_script.py` for `configs/your_script.
json`). This is a plain Python data file -- no tkinter import, no
dependency on `config_editor.pyw` -- picked up automatically by
`discover_gui_specs()` at startup, so this step is the ENTIRE GUI
integration; nothing in `config_editor.pyw` itself needs editing for a
plain settings form. `gui/orcastrator.py` is the fullest worked example
(multiple sections, `show_if`, a section-heading tooltip override, a
live preview mockup); `gui/tool_preheat.py` is the minimal one (one
section, one field). A module needs:

- `TITLE`, `SUBTITLE` -- shown on the landing page and as the editor's title.
- `CONFIG` -- the filename in `configs/` this form edits (its presence
  is also what makes `config_editor.pyw` load/save/validate against that
  file; omit only for a `KIND="none"` entry with nothing to load).
- `SECTIONS` -- the field-spec list, same `(section title, [field spec,
  ...])` tuple shape `_add_field()`/`_add_section_fields()` already
  handle for every other config. Field `kind`s available: `bool`,
  `choice`, `number`, `text`, `color` (rgba, with alpha), `hex_color`
  (no alpha, e.g. theme colors), `nullable_number` (an Auto checkbox
  alongside the spinbox, for a setting that can legitimately be `null`),
  `point_table` (an editable list of numeric-column rows -- see
  `gui/dock_collision_guard.py`'s "Dock Boundary" section for the fullest
  worked example, including its own `validate_rows` callable).
  Presence of `SECTIONS` is what implies `KIND="simple"` -- no need to
  set `KIND` explicitly for the common case.
- `PREVIEW` -- optional, currently only `"theme"` is meaningful (see
  `gui/orcastrator.py`); gives the editor a live mockup preview. Not to
  be confused with `HAS_PREVIEW` below -- unrelated despite the similar
  name, and only meaningful for a `KIND="simple"` config.
- `KIND` -- optional explicit override. `"rich"` means this config ALSO
  sets `HAS_PREVIEW = True` (a live SVG preview, like the collision
  checker) -- see **Giving a new processor a live preview** below for
  the full contract. Still real Python, not something auto-discovery
  builds for you, but it lives entirely in your own `gui/*.py` file now,
  same as everything else -- nothing to add to `config_editor.pyw`
  itself. Most processors will never need it.

If one setting only matters depending on another (a color that's only
visible when its shape is `outline`, a lead time that's only used when
some mode isn't `off`, etc.), add `show_if=[(path, expected), ...]` to
that field's spec instead of just noting the dependency in its tooltip --
`path` is the governing field's own path tuple, `expected` is either the
exact value that must match or a list of acceptable ones. All pairs must
hold (AND) for the field to show; leave it off entirely for a field
that's always relevant. This hides the row outright (not just
grays it out) any time the condition is false, live as the governing
field changes -- see the `mode`-gated point/outline fields under
`Clearance Zones -- Collision`/`-- Near Miss` in `config_editor.pyw`'s
`SECTIONS`, or `gui/orcastrator.py`'s `show_progress_ui`-gated Progress
Window fields, for real examples. A hidden field's position relative to
its own section's other fields never changes -- it's `grid()`-managed
(via `grid_remove()`/`grid()`), and Tkinter's grid manager always
restores a widget to its original row -- so a co-dependent field always
reappears in the same place, not wherever it happens to land.

If EVERY field in a section ends up conditional (no unconditional field
left to interact with), the whole section heading hides along with them
once they're all false -- see `config_editor.pyw`'s `Printable Area`,
entirely gated on `canvas_clip == "full"`. A section frame like that is
`pack()`-managed rather than `grid()`-managed, and plain `pack()` has no
memory of sibling order -- a naive hide/reshow would silently drop it
back in at the *bottom* of the settings screen instead of its declared
spot. `_pack_section_in_order()` exists specifically to avoid that: it
walks `self._section_frame_order` (every section frame on the current
screen, in declared order) to find the next sibling that's actually on
screen and packs before it, so a reshown section always lands back next
to whichever settings it's actually co-dependent with. This is all
generic machinery in `_add_section_fields()`/`_apply_visibility()`/
`_pack_section_in_order()`; a new processor's fields just need the
`show_if` key, nothing else to wire up.

You don't strictly have to do step 3 at all -- an unregistered `.json` in
`configs/` still shows up on the landing page automatically as a raw-JSON
editor (syntax-validated on save). Writing a `gui/your_script.py` just
gets you the proper form with typed, validated, tooltipped fields
instead. Where it lands in the landing-page list is controlled by
`gui_order` in `configs/orcastrator.json` -- editable from OrcaStrator
Settings -> Settings Landing Page (same "pick from what's on disk"
picker as `explicit_order`/`denylist`), purely cosmetic, same idea as
`explicit_order` for run order. `GUI_EXPLICIT_ORDER` in `config_editor.pyw`
is just the hardcoded fallback used until someone saves a `gui_order`
of their own; a new file needs no entry in either one, it just sorts
after the ones that are listed.

## Giving a new processor a live preview

Optional, and most processors won't need it -- a plain `SECTIONS` form
covers the common case fine. Worth doing if editing the config is hard
to reason about without seeing its effect (the collision checker's dock
boundary, or anything else where "is this setting right?" really means
"what would this look like on a real print?").

`gui/dock_collision_guard.py` is the reference implementation; a new
processor's `gui/your_script.py` copies the same three pieces:

- **`HAS_PREVIEW = True`** and **`KIND = "rich"`** -- `KIND` needs
  setting explicitly here (unlike a plain `SECTIONS` form, where it's
  inferred), since `SECTIONS` being present would otherwise default it
  to `"simple"`.
- **`PREVIEW_CONTROLS`** -- a declarative list of preview-only controls
  (NOT persisted to your config -- purely inputs to your own synthetic
  sample data, e.g. "which scenario to render"). Two kinds are
  supported today, handled generically by `_build_preview_panel()` in
  `config_editor.pyw`:
  - `dict(kind="choice_buttons", var="scenario", label="...", default="...",
    options=[(value, label), ...])` -- a row of Radiobuttons.
  - `dict(kind="bool", var="multi_object", label="...", default=False)`
    -- a Checkbutton.
  Adding a third kind is possible (that loop is the only place that
  would need to grow), but nothing needs one yet.
- **`build_preview_payload(cfg, controls)`** -- a function, `cfg` (the
  live in-editor config, same object the form fields are editing) and a
  plain `{control_var: value}` dict (read off `PREVIEW_CONTROLS`' live
  Tk state, one entry per `var` name) in, an `SVG_PAYLOAD`-shaped dict
  out (see **Talking to the printer** above for that shape) -- the SAME
  shape and generator your processor's real run-time
  `build_svg_payload()`-equivalent produces, so what you see in the
  editor is exactly what a real export would show, not an
  approximation. `config_editor.pyw` calls this on every field/control
  change and feeds the result straight into `orcastrator.py`'s own
  already-generic canvas renderer (`_TkProgressUI._draw_payload` --
  the SAME renderer a real export's progress window uses for ANY
  processor's `SVG_PAYLOAD`, so nothing about the renderer itself is
  processor-specific either). `config_editor.pyw` has zero knowledge of
  what's inside the payload or what any control means -- see
  `_build_preview_panel()`/`refresh_preview()` there if you want to
  trace exactly how generic that path is.

Your `build_preview_payload()` will naturally need to call into your
OWN processor's own rendering functions (`build_boundary()`,
`build_svg_payload()`, etc. for the collision checker) -- that coupling
is correct and expected, a plugin knowing about its own processor is
fine, it's `config_editor.pyw` knowing about a specific processor that
isn't. Use `gui/_plugin_support.py`'s `load_processor_module(name)` to
load `post_processors/<name>.py` for this rather than re-deriving the
sys.path/importlib dance yourself -- it also exports `get_in()`/
`set_in()`, the same nested-dict path helpers `config_editor.pyw` itself
uses, shared here so both sides read/write a config's paths identically.

A `point_table` field (see the field-kind list above) that needs its
own domain-specific row validation -- like the collision checker's
"Z must never decrease as Y increases" boundary rule -- passes a
`validate_rows` callable in its field spec:
`validate_rows(points) -> {row_id: message}`, where `points` is the
list of currently-entered rows (each a dict of your declared columns
plus an opaque `_row` id to echo back). `config_editor.pyw`'s
`point_table` handling has no idea what makes a row valid for YOUR
config; see `validate_boundary_points()` in `gui/dock_collision_guard.py`
for the pattern, including why it matters (it encodes an assumption
`find_closest_approach()` in the processor itself actually relies on --
this isn't just cosmetic input validation).

## What you should never need to touch

- `orcastrator.py`'s core loop, `ORCASTRATOR_RENDER` (the
  Klipper macro), or the `SVG_PAYLOAD`/`NOTICE` parsing logic on either
  side. If you find yourself wanting to, that's a signal the generic
  contract above is missing something -- fix the contract, not one
  processor's special case.
- `EXPLICIT_ORDER` / `EXPLICIT_ORDER_LAST` / `DENYLIST` are the only
  OrcaStrator-level things a new processor might legitimately need added
  to. `GUI_EXPLICIT_ORDER` (display order on the settings landing page)
  is the GUI-side equivalent, and is likewise optional.
- `config_editor.pyw`'s scroll/tooltip/visibility plumbing --
  `_make_scroll_area()` (themed, auto-hiding scrollbars scoped to
  whichever panel the mouse is actually over), `_labelframe()` (section
  headings that can carry a tooltip), and `_add_section_fields()` /
  `_apply_visibility()` (the `show_if` engine above). A new processor's
  section list just uses these already; there's nothing to opt into or
  configure per-config.
- `discover_gui_specs()` itself, or `CONFIG_REGISTRY`'s construction --
  a new processor's settings form is a new file in `gui/`, never an edit
  to how those files get found and turned into landing-page entries.
- `_debug_section_for()` or the "Debug Logs" `logviewer` entry -- a new
  processor opts into debug dumps by adding a `debug` block to its own
  config (see **Debug dumps** above), never by touching
  `config_editor.pyw` itself.
- `config_editor.pyw` never hard-imports any specific processor's own
  module, dock collision included -- if your `HAS_PREVIEW` config needs
  one (see **Giving a new processor a live preview** above), load it via
  `gui/_plugin_support.py`'s `load_processor_module()` from your own
  `gui/*.py`, not via `config_editor.pyw`. This is also why deleting a
  processor's `post_processors/*.py` + `gui/*.py` + `configs/*.json` as
  a unit is safe -- `config_editor.pyw` just loses that landing-page
  entry, nothing else breaks.

## Delivering finished files

A new (or changed) processor is always 2-3 files spread across three
different folders -- `post_processors/*.py`, `configs/*.json`, and
usually `gui/*.py`. Hand them back as a single zip with that same
folder structure rooted at `OrcaStrator/` (i.e. `OrcaStrator/
post_processors/foo.py`, `OrcaStrator/configs/foo.json`, `OrcaStrator/
gui/foo.py`), not as flat files -- that way it unzips straight into an
existing install and every file lands where it belongs without the
person having to sort three same-looking-but-different-purpose files
into the right folders by hand.
