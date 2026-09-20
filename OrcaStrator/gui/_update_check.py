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
Shared update-check logic for OrcaStrator's git-clone workflow.

Deliberately has ZERO tkinter dependency, unlike every other gui/*.py in
this folder -- orcastrator.py (the pipeline runner itself) needs to read
this module's cached status on every export, and that must stay safe to
import from a plain script context with no GUI involved at all.

Two call patterns live here, both fine to use now:

  - config_editor.pyw's landing page calls check_for_updates() from a
    background thread, once per process (see its _start_update_check),
    respecting the default CHECK_INTERVAL_SECONDS unless the person
    explicitly asks for a recheck (force=True).

  - orcastrator.py's own pipeline run (see its run_all()) ALSO calls
    check_for_updates(), from its own background thread kicked off
    before any processor runs and joined (with a short bound) after
    they've all finished -- so the fetch overlaps real processing time
    instead of adding to it. It passes DAILY_CHECK_INTERVAL_SECONDS
    instead of the default, since a print can happen many times a day
    and doesn't need its own fetch each time -- and both callers share
    the exact same cached state, so whichever happens to check first in
    a given window satisfies the other for free.

Either way: this is a real `git fetch`, a network round trip, so it must
ALWAYS run in a background thread, never on a main/synchronous path --
Tk's main thread would visibly freeze the GUI, and orcastrator.py's main
thread is the same one writing the actual g-code output.

Reading the cached result back (get_cached_status() / should_notify()) is
cheap either way -- no network, no subprocess beyond a couple of instant
`git rev-parse` calls used for the initial is_git_repo() gate -- so those
two are fine to call synchronously from anywhere, including right before
printing a console notice.

Activity log: always on, no setting. Every check, apply, and settings
change is written to update_check.log in the central Debug Log Directory
(OrcaStrator Settings -> Debug) -- see the "Activity log" section below
for exactly how it rotates. Deliberately separate from the shared
helpers/debug_dump.py system every processor uses (no toggle, no
mode/cap), apart from sharing that directory and being listed in the
settings app's "Debug Logs" card: this is a plain-text, human-readable
timeline that's always there when something about updating seems off,
rather than an opt-in snapshot.

State lives in configs/gui_state.json under the "update_check" key,
alongside (not colliding with) gui/_window_anchor.py's own per-window
geometry keys in that same file -- see that module's docstring for why
this file is the shared, auto-written-only state store every GUI-adjacent
helper in this project already uses.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import traceback

# .../OrcaStrator/OrcaStrator (this file's package root, matches
# config_editor.pyw's own HERE) -- one level up from gui/.
HERE = pathlib.Path(__file__).resolve().parent.parent
# .../OrcaStrator (the actual git repo root, one level further up --
# matches update_orcastrator.py's own REPO_ROOT, which lives there).
REPO_ROOT = HERE.parent
CONFIGS_DIR = HERE / "configs"
STATE_PATH = CONFIGS_DIR / "gui_state.json"
UPDATE_SCRIPT = REPO_ROOT / "update_orcastrator.py"

# Don't re-fetch more than this often even if asked -- see
# check_for_updates()'s `force` and `min_interval_seconds` parameters for
# how callers can loosen or bypass it.
CHECK_INTERVAL_SECONDS = 6 * 3600
# orcastrator.py's own pipeline-run check (see run_all()'s docstring in
# orcastrator.py) uses this much coarser cap instead of
# CHECK_INTERVAL_SECONDS -- a print can happen many times a day, and a
# git fetch on every single one would be wasteful for a check that's only
# ever informational. Both callers share the exact same cached state
# either way, so whichever one happens to check first in a given window
# satisfies the other for free.
DAILY_CHECK_INTERVAL_SECONDS = 24 * 3600


# On Windows, config_editor.pyw / OrcaSlicer's post-processing hook run
# with no console of their own, so every console program spawned from
# here (git.exe, python.exe) would otherwise pop up a brief command
# window of its own. CREATE_NO_WINDOW suppresses that; child processes
# inherit the hidden console, so anything those spawn stays hidden too.
# Empty (a no-op) on every other platform.
_NO_WINDOW_KWARGS = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform.startswith("win") else {}
)


# ---------------------------------------------------------------------------
# Activity log (always on)
#
# Two plain-text files in the central Debug Log Directory (see
# _log_dir() for the exact resolution):
#   update_check.log            -- the CURRENT run
#   update_check.previous.log   -- the run before it
# A "run" starts each time a check actually gets going (i.e. gets past the
# "checked recently, skip" throttle -- see check_for_updates()): the
# current log is renamed over the previous one (whatever was there is
# discarded) and a fresh log begins. Everything else appends to the
# current log: the throttled checks that did nothing, "Ignore this
# update", the Updates/notice toggles, and applying an update. So the
# current log reads as "the last real check, and everything that has
# happened since", and the previous one is the check before that.
#
# Lines are written and flushed as they happen (not at the end), so a
# check that hangs or crashes still leaves its partial timeline behind.
# Everything here is best-effort: a logging failure must never be able to
# break the feature it's describing.
#
# maintainer note: documented in CLAUDE.md under "Update activity log" --
# keep the two in sync if the file names or rotation rule change.
# ---------------------------------------------------------------------------

_LOG_NAME = "update_check.log"
_LOG_PREVIOUS_NAME = "update_check.previous.log"
# Public: config_editor.pyw's "Debug Logs" card lists these two files
# (alongside the *_debug.json dumps) by reading this tuple.
LOG_FILE_NAMES = (_LOG_NAME, _LOG_PREVIOUS_NAME)
_MAX_OUTPUT_CHARS = 4000         # per git stdout/stderr
_MAX_APPLY_OUTPUT_CHARS = 20000  # update_orcastrator.py's full output matters more
_log_lock = threading.Lock()     # checks and applies run on background threads


def _log_dir() -> pathlib.Path:
    """
    Where the activity log lives: the central debug.dir from
    configs/orcastrator.json if set (a relative value resolves against the
    OrcaStrator root, exactly as helpers/debug_dump.py does), otherwise
    post_processors/ -- the same fallback debug dumps use. Re-read on every
    write so changing the setting takes effect immediately, and read
    straight from the JSON rather than importing orcastrator.py (this
    module has to stay tkinter-free). Unreadable/blank = the fallback.

    maintainer note: this deliberately mirrors helpers/debug_dump.py's
    central-directory rule rather than importing it, so the update log
    keeps following the same setting -- if that rule ever changes there,
    change it here too.
    """
    try:
        raw = json.loads((CONFIGS_DIR / "orcastrator.json").read_text(encoding="utf-8"))
        value = raw.get("debug", {}).get("dir")
        if isinstance(value, str) and value.strip():
            path = pathlib.Path(value.strip()).expanduser()
            return path if path.is_absolute() else HERE / path
    except Exception:
        pass
    return HERE / "post_processors"


def _clip(text, limit: int) -> str:
    text = (text or "").rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters truncated]"


def _format_entry(message: str, details: dict) -> list[str]:
    """One timestamped line; multi-line values (command output) go below it, indented."""
    who = f"{os.getpid()} {pathlib.Path(sys.argv[0]).name if sys.argv and sys.argv[0] else '?'}"
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    inline, blocks = [], []
    for key, value in details.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if "\n" in text:
            blocks.append((key, text))
        else:
            inline.append(f"{key}={text}")
    lines = [f"{stamp}  [{who}] {message}" + ("  " + "  ".join(inline) if inline else "")]
    for key, text in blocks:
        lines.append(f"    {key}:")
        lines.extend("        " + ln for ln in text.splitlines())
    return lines


def _write_log(lines: list[str], new_run: bool = False) -> None:
    with _log_lock:
        try:
            log_dir = _log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            current = log_dir / _LOG_NAME
            if new_run and current.exists():
                os.replace(current, log_dir / _LOG_PREVIOUS_NAME)  # overwrites the older one
            with open(current, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass


class _Trace:
    """
    Writes one operation's timeline to the activity log as it happens.
    new_run=True rotates the log first (see the section header); False
    appends to the current one.
    """

    def __init__(self, operation: str, new_run: bool = False, **context):
        self.outcome = None
        self._t0 = time.monotonic()
        header = f"=== {operation} ===" if new_run else f"--- {operation} ---"
        # Environment details only once per log, in its header -- appended
        # entries (toggles, apply) would just repeat them on every line.
        env = {"python": sys.executable, "platform": sys.platform, "repo_root": str(REPO_ROOT)} if new_run else {}
        details = {**env, **context}
        _write_log(_format_entry(header, details), new_run=new_run)

    def event(self, message: str, **details) -> None:
        _write_log(_format_entry(message, details))

    def git(self, args, timeout, result, seconds: float) -> None:
        details = {"seconds": round(seconds, 3), "timeout_s": timeout}
        if result is None:
            details["result"] = "no result (git missing, timed out, or repo folder missing)"
        else:
            details["exit"] = result.returncode
            if (result.stdout or "").strip():
                details["stdout"] = _clip(result.stdout, _MAX_OUTPUT_CHARS)
            if (result.stderr or "").strip():
                details["stderr"] = _clip(result.stderr, _MAX_OUTPUT_CHARS)
        self.event("git " + " ".join(args), **details)

    def state(self, label: str) -> None:
        """Snapshot of the persisted update state, e.g. label="before"."""
        try:
            self.event(f"state {label}", **_load_state())
        except Exception:
            pass

    def finish(self, outcome=None, **extra) -> None:
        if outcome is not None:
            self.outcome = outcome
        self.event("finished", outcome=self.outcome,
                   duration_s=round(time.monotonic() - self._t0, 3), **extra)


def _log_state_change(what: str, **details) -> None:
    """A settings change (toggle, ignore), appended to the current log."""
    trace = _Trace(what, **details)
    trace.state("after")


def _run_git(args: list[str], timeout: float = 10.0, trace: "_Trace | None" = None):
    """
    Returns a CompletedProcess, or None if git isn't installed, the
    command timed out, or REPO_ROOT doesn't exist yet. Never raises --
    every caller here treats None the same as "couldn't tell, assume no
    update", which is always the safe fallback for this feature.
    `trace`, when given, logs the command, exit code and output.
    """
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_NO_WINDOW_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        if trace is not None:
            trace.git(args, timeout, None, time.monotonic() - started)
            trace.event("git error", error=repr(exc))
        return None
    if trace is not None:
        trace.git(args, timeout, result, time.monotonic() - started)
    return result


def is_git_repo(trace: "_Trace | None" = None) -> bool:
    """
    Cheap, local, no-network check -- safe to call from orcastrator.py's
    own pipeline path if it ever needs to gate on this directly (it
    currently doesn't; should_notify() already implies it).
    """
    result = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=5, trace=trace)
    return bool(result) and result.returncode == 0 and result.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# State (configs/gui_state.json, "update_check" key)
# ---------------------------------------------------------------------------

def _load_full_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_state() -> dict:
    return _load_full_state().get("update_check", {})


def _save_state(patch: dict) -> None:
    """
    Read-modify-write of just the "update_check" key, same
    temp-file-then-replace pattern gui/_window_anchor.py's
    save_window_geometry() uses -- best-effort, a write failure here
    must never be able to break a caller (GUI or pipeline) around it.
    """
    try:
        full = _load_full_state()
        section = full.get("update_check", {})
        if not isinstance(section, dict):
            section = {}
        section.update(patch)
        full["update_check"] = section
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:
        pass


def get_cached_status() -> dict:
    """
    Zero-network read of the last background check's result.
    orcastrator.py's pipeline path should ONLY ever call this (or
    should_notify() below) -- never check_for_updates().

    Returns:
        {
            "update_available": bool,
            "remote_sha": str | None,   # only set while an update is pending
            "ignored_sha": str | None,  # last sha dismissed via "Ignore this update"
            "checked_at": float | None, # time.time() of the last real fetch
        }
    """
    state = _load_state()
    return {
        "update_available": bool(state.get("update_available", False)),
        "remote_sha": state.get("remote_sha"),
        "ignored_sha": state.get("ignored_sha"),
        "checked_at": state.get("checked_at"),
    }


def should_notify() -> bool:
    """
    True if there's a pending update that hasn't already been dismissed
    for this specific commit via "Ignore this update". This is the one
    function orcastrator.py's run_all() calls -- a dict lookup and a
    string compare, nothing that can meaningfully add latency to an
    export.
    """
    status = get_cached_status()
    if not status["update_available"]:
        return False
    return status["remote_sha"] != status["ignored_sha"]


# ---------------------------------------------------------------------------
# The actual network-touching calls -- config_editor.pyw only, always from
# a background thread.
# ---------------------------------------------------------------------------

def check_for_updates(force: bool = False, min_interval_seconds: float = CHECK_INTERVAL_SECONDS) -> dict:
    """
    Runs `git fetch` and compares HEAD against the upstream branch,
    caching the result either way. NETWORK CALL: only call this from a
    background thread -- config_editor.pyw's landing page and
    orcastrator.py's own pipeline run (see its run_all()) both do, each
    with its own min_interval_seconds against this SAME shared cache, so
    whichever happens to check first in a given window satisfies both --
    never call it from either's main-thread/synchronous path.

    `force=True` bypasses the interval entirely (used by an explicit
    user action, like the landing page's Updates toggle being switched back
    on); the automatic per-launch/per-run checks should leave it False.
    `min_interval_seconds` lets a caller require a DIFFERENT freshness
    than the default -- orcastrator.py's pipeline run passes
    DAILY_CHECK_INTERVAL_SECONDS, since a print can happen many times a
    day and doesn't need its own fetch each time.

    Returns the same shape as get_cached_status().
    """
    state = _load_state()
    if not force:
        last = state.get("checked_at")
        if isinstance(last, (int, float)) and (time.time() - last) < min_interval_seconds:
            # Nothing to do -- note it in the CURRENT log rather than
            # starting a new one, so a burst of skipped calls can't push
            # the last real check out of the two logs that are kept.
            trace = _Trace("check skipped (checked recently)", force=force,
                           min_interval_seconds=min_interval_seconds,
                           seconds_since_last_check=round(time.time() - last, 1))
            return get_cached_status()

    trace = _Trace("update check", new_run=True, force=force, min_interval_seconds=min_interval_seconds)
    trace.state("before")
    try:
        status = _check_for_updates(trace)
    except Exception as exc:
        trace.event("unexpected exception", error=repr(exc), traceback=traceback.format_exc())
        trace.finish("exception")
        raise
    trace.state("after")
    trace.finish(should_notify=should_notify())
    return status


def _check_for_updates(trace: _Trace) -> dict:
    """check_for_updates()'s network work; sets trace.outcome on every path out."""
    if not is_git_repo(trace):
        trace.outcome = "not a git repo"
        return get_cached_status()

    fetch = _run_git(["fetch", "--quiet"], timeout=20, trace=trace)
    if fetch is None or fetch.returncode != 0:
        # Offline, no remote configured, auth prompt refused, etc. --
        # leave whatever was already cached alone rather than clobbering
        # a real "update available" with a false negative from a
        # transient network hiccup.
        trace.outcome = "fetch failed"
        _save_state({"checked_at": time.time()})
        return get_cached_status()

    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=5, trace=trace)
    if upstream is None or upstream.returncode != 0:
        # No upstream tracking branch configured -- nothing to compare
        # against, so there's nothing to report as available.
        trace.outcome = "no upstream branch"
        _save_state({"checked_at": time.time(), "update_available": False, "remote_sha": None})
        return get_cached_status()
    upstream_ref = upstream.stdout.strip()

    local = _run_git(["rev-parse", "HEAD"], timeout=5, trace=trace)
    remote = _run_git(["rev-parse", upstream_ref], timeout=5, trace=trace)
    if local is None or remote is None or local.returncode or remote.returncode:
        trace.outcome = "rev-parse failed"
        return get_cached_status()

    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()
    update_available = local_sha != remote_sha
    trace.outcome = "update available" if update_available else "up to date"
    trace.event("compared", upstream=upstream_ref, local_sha=local_sha, remote_sha=remote_sha)

    _save_state({
        "checked_at": time.time(),
        "update_available": update_available,
        "remote_sha": remote_sha if update_available else None,
    })
    return get_cached_status()


def get_pending_commits(limit: int = 25) -> list[dict]:
    """
    [{sha, short_sha, summary, author, date}, ...] for every commit
    between HEAD and the cached remote_sha, newest first. Empty if
    there's nothing pending, or if the repo has moved on since the
    cached remote_sha was recorded (e.g. HEAD already merged it some
    other way) -- either way, an empty list is the correct thing to
    show, not an error.
    """
    status = get_cached_status()
    remote_sha = status.get("remote_sha")
    if not remote_sha:
        return []

    result = _run_git([
        "log", f"HEAD..{remote_sha}",
        f"--max-count={limit}",
        "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%ad",
        "--date=short",
    ], timeout=10)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return []

    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short_sha, summary, author, date = parts
        commits.append(dict(sha=sha, short_sha=short_sha, summary=summary, author=author, date=date))
    return commits


def ignore_update(sha: str) -> None:
    """Records `sha` as dismissed -- should_notify() stays False for
    this exact commit until a NEWER one shows up on a future check."""
    _save_state({"ignored_sha": sha})
    _log_state_change("Ignore this update", ignored_sha=sha)


# ---------------------------------------------------------------------------
# User-facing on/off toggles -- deliberately stored HERE (gui_state.json,
# gitignored, never git-tracked) rather than in a real config file like
# configs/orcastrator.json.
#
# The whole point of this feature is protecting a git-tracked config from
# being silently modified by a `git pull` before update_orcastrator.py's
# skip-worktree protection is in place for it. Shipping these two toggles
# AS NEW KEYS IN THAT SAME TRACKED CONFIG would hit exactly that problem
# for anyone who hasn't run update_orcastrator.py even once yet: the very
# commit that adds this feature would modify configs/orcastrator.json
# through a plain git pull, unprotected, before the feature meant to need
# protecting had a chance to be protected. Since these two settings are
# pure app/GUI behavior -- not real pipeline or processor settings -- they
# don't need to be git-tracked, shareable, or backed up at all, so there's
# no reason to accept that risk. gui_state.json is never git-tracked in
# the first place (see .gitignore), so there's nothing to protect here:
# the GUI just builds this key itself, the same way it already does for
# window geometry (see gui/_window_anchor.py) -- no pre-shipped default,
# no migration, nothing for a git pull to ever touch.
# ---------------------------------------------------------------------------

def get_check_enabled() -> bool:
    """Whether config_editor.pyw's landing page should ever run the
    background check at all. Defaults to True the first time this is
    read, same as every other gui_state.json-backed setting -- there's
    nothing to migrate FROM, since this key has never lived anywhere
    else."""
    return bool(_load_state().get("check_enabled", True))


def set_check_enabled(enabled: bool) -> None:
    """
    Flips the toggle. Turning it off also clears any already-cached
    "update available" state -- otherwise a stale prior result could
    keep should_notify() (and so orcastrator.py's console notice)
    reporting an update even after the person explicitly asked to stop
    hearing about it.
    """
    patch = {"check_enabled": bool(enabled)}
    if not enabled:
        patch.update({"update_available": False, "remote_sha": None})
    _save_state(patch)
    _log_state_change("Updates toggle", enabled=bool(enabled))


def get_notice_display_enabled() -> bool:
    """
    Whether an already-pending update (should_notify() True) should
    also show up as a console NOTICE during orcastrator.py's own
    pipeline run -- independent of get_check_enabled() above, which
    only gates the landing page's own background check. Defaults to
    True.
    """
    return bool(_load_state().get("notice_display", True))


def set_notice_display_enabled(enabled: bool) -> None:
    _save_state({"notice_display": bool(enabled)})
    _log_state_change("Console notice toggle", enabled=bool(enabled))


def apply_update() -> tuple[bool, str]:
    """
    Runs the actual pull + config merge by delegating to
    update_orcastrator.py at the repo root, so there's exactly one place
    that logic lives rather than a second copy embedded in the GUI.
    Blocking -- call from a background thread and marshal the result
    back via root.after(), same as check_for_updates().

    Returns (success, combined stdout+stderr) for the caller to show in
    an error dialog on failure.
    """
    trace = _Trace("apply update", update_script=str(UPDATE_SCRIPT))
    trace.state("before")
    if not UPDATE_SCRIPT.exists():
        trace.finish("update script missing")
        return False, f"update_orcastrator.py not found at {UPDATE_SCRIPT}"

    command = ["python", str(UPDATE_SCRIPT), "--no-pause"]
    trace.event("running", command=command, cwd=str(REPO_ROOT), timeout_s=180)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            **_NO_WINDOW_KWARGS,
        )
    except Exception as exc:
        trace.event("run failed", error=repr(exc), traceback=traceback.format_exc())
        trace.finish("exception")
        raise
    output = (result.stdout or "") + (result.stderr or "")
    details = {"exit": result.returncode, "seconds": round(time.monotonic() - started, 3)}
    if (result.stdout or "").strip():
        details["stdout"] = _clip(result.stdout, _MAX_APPLY_OUTPUT_CHARS)
    if (result.stderr or "").strip():
        details["stderr"] = _clip(result.stderr, _MAX_APPLY_OUTPUT_CHARS)
    trace.event("update script finished", **details)
    if result.returncode != 0:
        trace.finish("failed")
        return False, output

    _save_state({"update_available": False, "remote_sha": None, "ignored_sha": None})
    trace.state("after")
    trace.finish("applied")
    return True, output
