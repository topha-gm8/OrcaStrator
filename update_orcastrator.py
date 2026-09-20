#!/usr/bin/env python3
"""
update_orcastrator.py -- one command to update OrcaStrator (git pull) and
merge any config changes into your live settings without touching values
you've already set.

HOW IT WORKS
------------
OrcaStrator's config files pair every real setting `foo` with a doc key
`_foo` right next to it (e.g. `_safe_y` / `safe_y`). This script uses that
convention as the merge rule:

  - Keys starting with "_"  -> documentation. Always taken from the new
    defaults, since these are just comments, not settings.
  - Every other key         -> a real setting.
      * Missing from your live config  -> added from defaults (a new
        feature/setting that shipped since you last updated).
      * Present in both, both dicts    -> merged recursively (so e.g.
        dock_collision_guard.json's nested "svg" block only pulls in the
        specific new sub-keys, not the whole block).
      * Present in both, not both dicts -> YOUR value wins, untouched.
      * Present in your config but gone from defaults -> left alone, but
        reported, in case it's a renamed/removed setting you should look at.

USAGE
-----
One command, any time, including the very first time:

    python update_orcastrator.py

It runs the whole cycle:

  1. Make sure setup is in place -- seed `configs/_defaults/` if it doesn't
     exist yet, and skip-worktree any live config that isn't protected yet
     (a fresh clone, or a config file added since you last ran this).
  2. `git pull`.
  3. Merge any changes `_defaults/` picked up from that pull into your live
     `configs/*.json` -- new settings added, doc strings refreshed, your
     own values untouched.

So "update OrcaStrator" is just this one command, start to finish. Nothing
else to remember, no separate git pull step.

This ONLY does the git pull part if your OrcaStrator folder is an actual
git clone. If you downloaded a ZIP or copied the folder some other way,
the script detects that up front, prints instructions for switching to a
proper clone -- and then still does the config-merge part anyway (the
same thing `--merge-only` does), reading whatever's currently in
`configs/_defaults/` and reconciling it into your live configs. That part
never overwrites a value you've set, so it's safe to just do rather than
ask first; the only thing skipped is the git pull itself, since there's
no repo to pull.

Protection status is read straight from git (`git ls-files -v`, which tags
skip-worktree files with `S`) rather than tracked separately, so it can't
drift out of sync with reality.

The one manual step, and only once: after the very first run, commit
`configs/_defaults/` -- that's the only new thing git needs to know about
before pulls can update it.

FLAGS
-----
--init        only seed configs/_defaults/, skip everything else
--protect     only set skip-worktree on live configs, skip everything else
--merge-only  only reconcile configs from current _defaults/, skip git pull
--no-pull     do setup + merge but skip the `git pull` step
--no-pause    don't pause for a keypress before exiting on Windows
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CONFIGS_DIR = REPO_ROOT / "OrcaStrator" / "configs"
DEFAULTS_DIR = CONFIGS_DIR / "_defaults"
IS_WINDOWS = os.name == "nt"


def git_available() -> bool:
    return shutil.which("git") is not None


def in_git_repo() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _detect_origin_url() -> str | None:
    """
    Best-effort detection of THIS repo's own remote URL -- never
    hardcoded, so forking this project needs zero changes to this
    script. The clone instructions in print_no_git_instructions() are
    only ever shown when in_git_repo() is already False, so there's no
    guarantee any of this finds anything; that's expected for a genuine
    ZIP download or hand-copied folder, which carries no record of
    where it came from no matter how hard anything here looks -- see
    that function's own fallback for what happens then.

    Still worth trying, in order, because it isn't ALWAYS a dead end:
    someone could be re-running this from a folder with a valid `.git`
    directory whose remote is misconfigured or whose branch has no
    upstream tracking (which is what in_git_repo() actually gates on,
    not "is there a .git folder at all") -- or git itself might simply
    not be installed even though a perfectly good `.git` folder exists.

      1. `git remote get-url origin` -- git installed, "origin" configured
      2. `git remote get-url <first remote>` -- origin renamed/absent,
         but some other remote is
      3. Read .git/config directly as plain text -- covers "no git
         binary" specifically, since this needs no subprocess call at all

    Returns None if none of the above find anything.
    """
    git_dir = REPO_ROOT / ".git"

    if git_available() and git_dir.exists():
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        remotes = subprocess.run(
            ["git", "remote"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        first_remote = next((line.strip() for line in remotes.stdout.splitlines() if line.strip()), None)
        if first_remote:
            result = subprocess.run(
                ["git", "remote", "get-url", first_remote],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

    config_path = git_dir / "config"
    if config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        in_remote_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_remote_section = stripped.startswith('[remote "')
                continue
            if in_remote_section and stripped.startswith("url"):
                _, _, value = stripped.partition("=")
                value = value.strip()
                if value:
                    return value

    return None


def print_no_git_instructions(reason: str) -> None:
    print("=" * 70)
    print("This isn't set up as a git repository, so the git-pull part of")
    print("updating can't be automated safely -- git commands here would")
    print("either fail outright or silently do nothing. (The config-merge")
    print("part still runs on its own below, using whatever's currently in")
    print("configs/_defaults/ -- it just can't fetch new changes for you.)")
    print()
    print(f"Reason: {reason}")
    print()
    print("This usually means the OrcaStrator folder was downloaded as a")
    print("ZIP or copied by hand, rather than cloned with git.")
    print()
    print("To fix it (keeps your existing settings):")
    print("  1. Rename this OrcaStrator folder, e.g. to 'OrcaStrator_old'.")

    origin_url = _detect_origin_url()
    if origin_url:
        print("  2. Clone the real repo in its place:")
        print(f"       git clone {origin_url} OrcaStrator")
    else:
        print("  2. Clone the repository fresh in its place -- check its GitHub")
        print("     page or README for the correct URL for wherever you got")
        print("     OrcaStrator from (your own fork, if you're using one):")
        print("       git clone <the repository's URL> OrcaStrator")

    print("  3. Copy your old config files over the fresh ones:")
    print("       OrcaStrator_old/OrcaStrator/configs/*.json")
    print("       -> OrcaStrator/OrcaStrator/configs/")
    print("     (skip the _defaults subfolder if your old copy has one --")
    print("     let the fresh clone's copy of that stay as-is)")
    print("  4. Copy this script into the new OrcaStrator folder too, then")
    print("     run it again from there.")
    print("=" * 70)


def pause_before_exit(no_pause: bool) -> None:
    if IS_WINDOWS and not no_pause:
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


def handle_no_git(reason: str, no_pause: bool) -> int:
    """
    Called when the automated (git pull + merge) route isn't available --
    either git isn't installed, or this folder isn't a git repo -- and the
    caller didn't already ask for --merge-only specifically.

    Explains that up front via print_no_git_instructions(), but doesn't
    just stop there: it then does the config merge anyway, reading
    whatever's currently sitting in configs/_defaults/ and reconciling it
    into configs/*.json. That's exactly what --merge-only does, and it's
    safe to run unprompted here -- see merge()'s own docstring/logic, but
    the short version is it never overwrites a value the user has already
    set, it only ever adds keys that are missing and flags ones that
    vanished upstream. There's no destructive outcome to ask permission
    for, so this doesn't prompt; it just does the one part of "update"
    that's still possible without git and reports what it found.

    Only actually merges if configs/_defaults/ exists. If it doesn't,
    there's nothing to merge yet -- e.g. a fresh ZIP/copy that has never
    had a _defaults folder dropped into it -- so this leaves that alone
    rather than seeding new defaults on someone's behalf; cmd_update()
    would just print its own "run --init first" message for that case,
    which doesn't apply to a non-repo anyway.
    """
    print_no_git_instructions(reason)

    if not DEFAULTS_DIR.exists():
        print(f"(No {DEFAULTS_DIR} yet either, so there's nothing to merge")
        print("right now -- that'll be created the first time defaults ship")
        print("with a copy of this folder, or via --init.)")
        pause_before_exit(no_pause)
        return 1

    print()
    print("(git aside: still checking whatever's currently in")
    print(f"{DEFAULTS_DIR} against your live configs, in case you")
    print("copied in an updated version of that folder by hand --")
    print("same merge --merge-only does, just running automatically:)")
    print()
    rc = cmd_update()
    pause_before_exit(no_pause)
    return rc


def is_doc_key(key: str) -> bool:
    return key.startswith("_")



def merge(defaults: dict, live: dict, path: str = "") -> tuple[dict, list[str]]:
    """
    Returns (merged_dict, change_log).
    `defaults` order wins for key ordering (keeps each _foo/foo pair adjacent
    the way the shipped file intends), live's own extra keys are appended
    after.
    """
    changes: list[str] = []
    merged: dict = {}

    for key, new_val in defaults.items():
        here = f"{path}.{key}" if path else key

        if key not in live:
            merged[key] = new_val
            if not is_doc_key(key):
                changes.append(f"+ added new setting: {here}")
            continue

        old_val = live[key]

        if is_doc_key(key):
            # Always refresh documentation, even if the user edited it.
            merged[key] = new_val
            continue

        if isinstance(new_val, dict) and isinstance(old_val, dict):
            sub_merged, sub_changes = merge(new_val, old_val, here)
            merged[key] = sub_merged
            changes.extend(sub_changes)
        else:
            # Real, non-dict setting: the user's value always wins.
            merged[key] = old_val

    # Anything the user has that defaults no longer mention (renamed/removed
    # upstream, or a user's own scratch key) -- keep it, but flag it.
    for key, old_val in live.items():
        if key not in defaults:
            merged[key] = old_val
            if not is_doc_key(key):
                changes.append(f"? no longer in defaults (kept as-is): {path + '.' if path else ''}{key}")

    return merged, changes


def cmd_update() -> int:
    if not DEFAULTS_DIR.exists():
        print(f"No {DEFAULTS_DIR} found. Run with --init first (see the top of this file).")
        return 1

    default_files = sorted(DEFAULTS_DIR.glob("*.json"))
    if not default_files:
        print(f"{DEFAULTS_DIR} is empty -- nothing to merge.")
        return 0

    any_changes = False
    for default_path in default_files:
        live_path = CONFIGS_DIR / default_path.name
        new_defaults = json.loads(default_path.read_text(encoding="utf-8"))

        if not live_path.exists():
            # Brand new config file shipped upstream -- just drop it in.
            live_path.write_text(json.dumps(new_defaults, indent=4), encoding="utf-8")
            print(f"{default_path.name}: new file, added.")
            any_changes = True
            continue

        live = json.loads(live_path.read_text(encoding="utf-8"))
        merged, changes = merge(new_defaults, live)

        if changes:
            any_changes = True
            print(f"{default_path.name}:")
            for c in changes:
                print(f"    {c}")
        # Always rewrite even with no value changes, since doc strings (_foo
        # keys) may have been refreshed silently.
        live_path.write_text(json.dumps(merged, indent=4), encoding="utf-8")

    if not any_changes:
        print("All configs already up to date.")
    return 0


def cmd_init() -> int:
    if not CONFIGS_DIR.exists():
        print(f"Couldn't find {CONFIGS_DIR}. Run this from the repo root.")
        return 1
    DEFAULTS_DIR.mkdir(exist_ok=True)
    copied = 0
    for f in CONFIGS_DIR.glob("*.json"):
        if f.name == "gui_state.json":
            continue  # runtime-only, gitignored -- never a shipped default
        dest = DEFAULTS_DIR / f.name
        if dest.exists():
            continue
        shutil.copy2(f, dest)
        copied += 1
    print(f"Seeded {DEFAULTS_DIR} with {copied} file(s).")
    print("Commit this folder to git -- it's the tracked 'upstream' copy that")
    print("will keep receiving updates on every future git pull.")
    return 0


def live_config_files() -> list[Path]:
    # gui_state.json is config_editor.pyw's own auto-written window/
    # update-check state (see gui/_window_anchor.py and
    # gui/_update_check.py) -- it's gitignored, never committed, so it
    # can never be a git-tracked file `skip-worktree` applies to, and
    # it's not a user setting to merge from _defaults/ either.
    return [
        f for f in CONFIGS_DIR.glob("*.json")
        if f.parent == CONFIGS_DIR and f.name != "gui_state.json"
    ]



def protected_status(files: list[Path]) -> dict[Path, bool]:
    """
    Ask git directly which of these files already have skip-worktree set,
    via `git ls-files -v` (skip-worktree files are tagged 'S'). Returns
    {file: is_protected}; a file git doesn't know about (not tracked yet)
    counts as not protected.
    """
    if not files:
        return {}
    rels = [str(f.relative_to(REPO_ROOT)) for f in files]
    result = subprocess.run(
        ["git", "ls-files", "-v", "--"] + rels,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    tagged: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # format: "<tag> <path>"
        tag, _, rel_path = line.partition(" ")
        tagged[rel_path] = tag

    status = {}
    for f, rel in zip(files, rels):
        tag = tagged.get(rel, "")
        status[f] = tag.upper() == "S"
    return status


def cmd_protect() -> int:
    if not CONFIGS_DIR.exists():
        print(f"Couldn't find {CONFIGS_DIR}. Run this from the repo root.")
        return 1
    targeted = live_config_files()
    if not targeted:
        print("No top-level config files found to protect.")
        return 0

    status = protected_status(targeted)
    unprotected = [f for f, ok in status.items() if not ok]
    if not unprotected:
        print("All live config files already protected (skip-worktree).")
        return 0

    for f in unprotected:
        rel = f.relative_to(REPO_ROOT)
        result = subprocess.run(
            ["git", "update-index", "--skip-worktree", str(rel)],
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            print(f"Warning: couldn't set skip-worktree on {rel}")
    print(f"Marked {len(unprotected)} config file(s) as skip-worktree.")
    print("`git pull` will no longer touch your live settings in these files.")
    print("(configs/_defaults/ was left alone -- it should stay normally tracked.)")
    return 0


def ensure_setup() -> int:
    """Seed _defaults/ if missing, protect anything unprotected. Idempotent."""
    first_run = not DEFAULTS_DIR.exists()

    if first_run:
        print("First run: setting things up.")
        rc = cmd_init()
        if rc != 0:
            return rc
        rc = cmd_protect()
        if rc != 0:
            return rc
        print()
        print("One-time manual step: `git add configs/_defaults && git commit`")
        print("so the upstream copy is tracked going forward.")
        print()
        return 0

    targeted = live_config_files()
    status = protected_status(targeted)
    if any(not ok for ok in status.values()):
        cmd_protect()
    return 0


def cmd_pull() -> int:
    result = subprocess.run(["git", "pull"], cwd=REPO_ROOT)
    if result.returncode != 0:
        print("`git pull` failed -- resolve that first, then re-run this script")
        print("to merge any config changes it brought in.")
    return result.returncode


def _clear_cached_update_flag() -> None:
    """
    Clears the "update available" flag this script's own successful
    pull just resolved -- otherwise config_editor.pyw's landing page
    and orcastrator.py's own console notice (see gui/_update_check.py)
    would both keep reporting an update as pending even after this
    script already fetched and merged it, until their own next
    from-scratch check happens to run days later.

    Deliberately self-contained rather than importing
    gui/_update_check.py: this script is meant to keep working as a
    standalone file even if someone's dropped it somewhere the gui/
    package isn't reachable from, so it reads/writes the exact same
    configs/gui_state.json schema directly instead of adding that
    dependency. Best-effort and silent on any failure, matching how
    gui/_update_check.py's own state read/writes behave -- a state
    file this script's own protection intentionally never touches
    isn't worth failing an otherwise-successful update over.
    """
    state_path = CONFIGS_DIR / "gui_state.json"
    try:
        full = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(full, dict):
            return
        section = full.get("update_check")
        if not isinstance(section, dict) or not section.get("update_available"):
            return  # nothing cached as pending -- nothing to clear
        section["update_available"] = False
        section["remote_sha"] = None
        full["update_check"] = section
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
        tmp.replace(state_path)
    except Exception:
        pass


def cmd_auto(do_pull: bool) -> int:
    """Full cycle: ensure setup -> git pull -> merge."""
    rc = ensure_setup()
    if rc != 0:
        return rc

    if do_pull:
        rc = cmd_pull()
        if rc != 0:
            return rc
        # Only after an ACTUAL successful pull -- see the function's
        # own docstring for why --no-pull correctly skips this.
        _clear_cached_update_flag()

    return cmd_update()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--init", action="store_true", help="only seed configs/_defaults/ from your current configs/")
    parser.add_argument("--protect", action="store_true", help="only git skip-worktree every live config file")
    parser.add_argument("--merge-only", action="store_true", help="only reconcile from current _defaults/, skip setup + git pull")
    parser.add_argument("--no-pull", action="store_true", help="do setup + merge, but skip the git pull step")
    parser.add_argument("--no-pause", action="store_true", help="don't pause for a keypress before exiting on Windows")
    args = parser.parse_args()

    # --merge-only never touches git, so it's the one mode that still works
    # on a plain copied folder (no repo needed at all). For every other
    # mode, a missing/unusable git repo doesn't just stop here anymore --
    # see handle_no_git()'s docstring for why it still attempts the config
    # merge on its own before giving up.
    if not args.merge_only:
        if not git_available():
            return handle_no_git("git isn't installed, or isn't on your PATH.", args.no_pause)
        if not in_git_repo():
            return handle_no_git(f"'{REPO_ROOT}' isn't a git repository.", args.no_pause)

    if args.init:
        rc = cmd_init()
    elif args.protect:
        rc = cmd_protect()
    elif args.merge_only:
        rc = cmd_update()
    else:
        rc = cmd_auto(do_pull=not args.no_pull)

    pause_before_exit(args.no_pause)
    return rc


if __name__ == "__main__":
    sys.exit(main())
