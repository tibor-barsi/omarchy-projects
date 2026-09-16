#!/usr/bin/env python3
"""Backend for the Omarchy ``projects`` bar widget.

Two jobs live in this file.

``report`` (the default command)
    Merge three sources into one JSON document on stdout: the project
    directories found on disk, the tags recorded in the state file, and live
    Herdr workspace state. Cheap git signals are folded in on top.

``open <name>``
    Focus a project's Herdr workspace when one already exists, otherwise build
    a fresh one from the project's ``.herdr/layout.toml`` (or a default
    single-pane layout), then raise the Herdr terminal window.

``tag <name> <tag>``
    Move a project into one tag's box, or untag it with ``-``.

The report path never raises. Every failure is reported as ``ok: false`` with
an ``error`` string, so a broken backend degrades to an error pill in the bar
instead of taking the shell down with it.

Only the tags are stored; everything else on screen is derived, so a project
created tomorrow shows up without being registered anywhere. A project carries
exactly one tag, which decides which box it sits in; its position within that
box is its priority.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 2
UNSORTED = "unsorted"
UNSORTED_LABEL = "Unsorted"
UNSORTED_GLYPH = "\U000F0765"

# The boxes in the popup, in order. A project carries exactly one tag, which
# decides its box; its position within that box is its priority. Tags are data
# rather than code so the set can be renamed or extended in the state file.
# Glyphs are Nerd Font Material Design icons, which live above U+FFFF and so
# need the 8-digit \U escape; the 4-digit \u form silently truncates them.
DEFAULT_TAGS: list[dict] = [
    {"name": "running", "label": "Running", "glyph": "\U000F040A"},
    {"name": "waiting", "label": "Waiting", "glyph": "\U000F051F"},
    {"name": "next", "label": "Next", "glyph": "\U000F0054"},
    {"name": "paused", "label": "Paused", "glyph": "\U000F03E4"},
    {"name": "idea", "label": "Ideas", "glyph": "\U000F0335"},
    {"name": "done", "label": "Done", "glyph": "\U000F012C"},
]

# The tag that claims a project is being worked on right now. Carrying it
# without an open Herdr workspace is what marks a project stale.
DEFAULT_LIVE_TAG = "running"

# Tags are set by hand, so they can drift from reality. v1 lanes map onto the
# v2 tag set like this.
V1_LANE_MAP = {"focus": "running", "active": "running",
               "next": "next", "paused": "paused"}

DEFAULT_ROOT = "~/data/projects"
GIT_TIMEOUT = 5
HERDR_TIMEOUT = 20
AGENT_RE = re.compile(r"[^a-z0-9_-]+")


class HerdrError(RuntimeError):
    """Raised when the Herdr CLI cannot be reached or refuses a command."""


class LayoutError(RuntimeError):
    """Raised when a project's layout file cannot be read or understood."""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def state_path() -> Path:
    """Return the path of the priority state file.

    Returns
    -------
    pathlib.Path
        ``$XDG_STATE_HOME/omarchy-projects/state.json``, falling back to
        ``~/.local/state`` when the variable is unset. The file deliberately
        lives outside the plugin directory so ``omarchy plugin update`` cannot
        overwrite hand-curated priorities.
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy-projects" / "state.json"


def default_state() -> dict:
    """Return a fresh state document with every tag empty.

    Returns
    -------
    dict
        Seed state. ``_Archive`` is hidden by default because it is a holding
        pen rather than a project.
    """
    return {
        "version": STATE_VERSION,
        "liveTag": DEFAULT_LIVE_TAG,
        "tags": [dict(tag) for tag in DEFAULT_TAGS],
        "projects": {tag["name"]: [] for tag in DEFAULT_TAGS},
        "notes": {},
        "hidden": ["_Archive"],
    }


def normalise_tags(raw: object) -> list[dict]:
    """Coerce a stored tag list into well-formed tag definitions.

    Parameters
    ----------
    raw : object
        The ``tags`` value as found in the state file.

    Returns
    -------
    list of dict
        Tags carrying ``name``, ``label`` and ``glyph``, in file order and
        without duplicates. Falls back to the defaults when nothing usable is
        present, so a mangled tag list cannot empty the popup.
    """
    tags: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name or name == UNSORTED or name in seen:
                continue
            seen.add(name)
            tags.append({
                "name": name,
                "label": str(entry.get("label") or name.title()),
                "glyph": str(entry.get("glyph") or ""),
            })
    return tags or [dict(tag) for tag in DEFAULT_TAGS]


def load_state(path: Path) -> dict:
    """Read and normalise the tag state file.

    Parameters
    ----------
    path : pathlib.Path
        Location of the state file.

    Returns
    -------
    dict
        A state document carrying ``tags``, a ``projects`` mapping of tag name
        to an ordered list of project names, ``notes``, ``hidden`` and
        ``liveTag``. A missing or malformed file yields the default state
        rather than an error: tags are a convenience, and losing them must
        never block opening a project.

    Notes
    -----
    Version 1 files stored priority ``lanes`` instead of tags. They are
    migrated in memory on read through :data:`V1_LANE_MAP`; the file is only
    rewritten when something actually writes to it.
    """
    state = default_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state

    state["tags"] = normalise_tags(raw.get("tags"))
    names = {tag["name"] for tag in state["tags"]}
    state["projects"] = {name: [] for name in names}

    stored = raw.get("projects")
    if not isinstance(stored, dict) and isinstance(raw.get("lanes"), dict):
        # v1: fold the old priority lanes onto the tag set.
        stored = {}
        for lane, values in raw["lanes"].items():
            target = V1_LANE_MAP.get(lane)
            if target and isinstance(values, list):
                stored.setdefault(target, []).extend(values)

    if isinstance(stored, dict):
        placed: set[str] = set()
        for name in state["projects"]:
            values = stored.get(name)
            if not isinstance(values, list):
                continue
            for value in values:
                # One tag per project: the first tag that claims a name wins.
                if isinstance(value, str) and value and value not in placed:
                    placed.add(value)
                    state["projects"][name].append(value)

    live = str(raw.get("liveTag") or DEFAULT_LIVE_TAG)
    state["liveTag"] = live if live in names else ""

    notes = raw.get("notes")
    if isinstance(notes, dict):
        state["notes"] = {
            k: str(v) for k, v in notes.items() if isinstance(k, str) and v is not None
        }

    hidden = raw.get("hidden")
    if isinstance(hidden, list):
        state["hidden"] = [v for v in hidden if isinstance(v, str)]

    state["version"] = STATE_VERSION
    return state


def set_tag(state: dict, project: str, tag: str) -> bool:
    """Move a project into one tag, or untag it entirely.

    Parameters
    ----------
    state : dict
        State document, modified in place.
    project : str
        Project name.
    tag : str
        Target tag name, or ``"-"`` / ``""`` / ``"unsorted"`` to untag.

    Returns
    -------
    bool
        Whether the state changed.

    Raises
    ------
    ValueError
        When ``tag`` names a tag that does not exist.

    Notes
    -----
    A project is removed from every list before being appended to the target,
    which is what keeps the one-tag-per-project rule true no matter what the
    file looked like beforehand. It lands at the end of its new box, the
    lowest priority there, since arriving somewhere says nothing about how
    urgent it is.
    """
    clearing = tag in ("-", "", UNSORTED)
    names = {t["name"] for t in state["tags"]}
    if not clearing and tag not in names:
        raise ValueError(f"unknown tag {tag!r}; known: {', '.join(sorted(names))}")

    before = {name: list(values) for name, values in state["projects"].items()}
    for name in state["projects"]:
        state["projects"][name] = [
            v for v in state["projects"][name] if v != project
        ]
    if not clearing:
        state["projects"].setdefault(tag, []).append(project)
    return state["projects"] != before


def write_state(path: Path, state: dict) -> None:
    """Write the state document, creating parent directories as needed.

    Parameters
    ----------
    path : pathlib.Path
        Destination file.
    state : dict
        State document to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Disk and git
# --------------------------------------------------------------------------

def discover(root: Path, hidden: list[str]) -> list[str]:
    """List candidate project directories.

    Parameters
    ----------
    root : pathlib.Path
        Directory holding one subdirectory per project.
    hidden : list of str
        Directory names to skip.

    Returns
    -------
    list of str
        Sorted directory names, excluding dotfiles and hidden entries.
    """
    skip = set(hidden)
    try:
        entries = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [name for name in entries if not name.startswith(".") and name not in skip]


def _git(path: Path, *args: str) -> str | None:
    """Run a git command inside ``path`` and return trimmed stdout.

    Parameters
    ----------
    path : pathlib.Path
        Repository working directory.
    *args : str
        Arguments passed after ``git -C <path>``.

    Returns
    -------
    str or None
        Command output, or ``None`` when git failed or timed out.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_info(path: Path) -> dict:
    """Collect cheap git signals for one project.

    Parameters
    ----------
    path : pathlib.Path
        Project directory.

    Returns
    -------
    dict
        Keys ``git`` (bool), ``ageDays`` (int or None), ``dirty`` (int or
        None) and ``branch`` (str).
    """
    info: dict = {"git": False, "ageDays": None, "dirty": None, "branch": ""}
    if not (path / ".git").exists():
        return info
    info["git"] = True

    stamp = _git(path, "log", "-1", "--format=%ct")
    if stamp and stamp.isdigit():
        age = (time.time() - int(stamp)) / 86400.0
        info["ageDays"] = max(0, int(age))

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        info["branch"] = branch

    status = _git(path, "status", "--porcelain")
    if status is not None:
        info["dirty"] = len([line for line in status.splitlines() if line.strip()])

    return info


# --------------------------------------------------------------------------
# Herdr
# --------------------------------------------------------------------------

def herdr_call(
    args: list[str], timeout: int = HERDR_TIMEOUT, expect_json: bool = True
) -> dict:
    """Invoke the Herdr CLI and return the ``result`` payload.

    Parameters
    ----------
    args : list of str
        Arguments after the ``herdr`` executable.
    timeout : int, optional
        Seconds to wait before giving up.
    expect_json : bool, optional
        Whether a JSON response is required. A few mutating commands, notably
        ``pane run``, succeed silently with no output at all; for those the
        exit status is the only signal available.

    Returns
    -------
    dict
        The ``result`` object of the JSON response, or an empty dict.

    Raises
    ------
    HerdrError
        When Herdr is absent, unreachable, or returns a failure.

    Notes
    -----
    The CLI talks to the Herdr server over its socket and works from outside a
    Herdr pane, which is what lets the Quickshell bar drive it.
    """
    if not shutil.which("herdr"):
        raise HerdrError("herdr is not installed")
    try:
        proc = subprocess.run(
            ["herdr", *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise HerdrError(f"herdr {' '.join(args)} timed out") from None
    except OSError as exc:
        raise HerdrError(str(exc)) from None

    if not expect_json:
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip()
                      or f"exited {proc.returncode}")
            raise HerdrError(detail.splitlines()[0][:200])
        return {}

    payload = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break

    if payload is None:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "no response")
        raise HerdrError(detail.splitlines()[0][:200])

    error = payload.get("error")
    if error:
        raise HerdrError(str(error)[:200])
    if proc.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args)} exited {proc.returncode}")

    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def herdr_workspaces() -> dict[str, dict]:
    """Return live Herdr workspaces keyed by label.

    Returns
    -------
    dict
        Mapping of workspace label to the workspace object, which carries
        ``workspace_id``, ``agent_status``, ``focused``, ``pane_count`` and
        ``tab_count``.
    """
    result = herdr_call(["workspace", "list"])
    out: dict[str, dict] = {}
    for workspace in result.get("workspaces") or []:
        label = str(workspace.get("label") or "")
        if label:
            out[label] = workspace
    return out


def herdr_ready() -> bool:
    """Report whether the Herdr server socket answers.

    Returns
    -------
    bool
        True when a workspace listing succeeds.
    """
    try:
        herdr_workspaces()
    except HerdrError:
        return False
    return True


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def build_report(root: Path, state: dict) -> dict:
    """Assemble the JSON document consumed by the bar widget.

    Parameters
    ----------
    root : pathlib.Path
        Directory holding the projects.
    state : dict
        Tag state document.

    Returns
    -------
    dict
        Report with ``boxes``, ``counts`` and metadata. One box per tag in the
        state file's order, plus a trailing ``unsorted`` box for anything on
        disk that carries no tag. Herdr being down is reported in
        ``herdrError`` and leaves the rest of the report intact, so the roster
        still works without a running session.
    """
    herdr_error = ""
    workspaces: dict[str, dict] = {}
    try:
        workspaces = herdr_workspaces()
    except HerdrError as exc:
        herdr_error = str(exc)

    names = discover(root, state["hidden"])
    on_disk = set(names)
    live_tag = state["liveTag"]

    tagged: list[dict] = []
    seen: set[str] = set()
    for tag in state["tags"]:
        for name in state["projects"].get(tag["name"], []):
            if name in seen:
                continue
            seen.add(name)
            tagged.append({"name": name, "tag": tag["name"]})

    entries = tagged + [
        {"name": name, "tag": UNSORTED} for name in names if name not in seen
    ]

    def hydrate(entry: dict) -> dict:
        name = entry["name"]
        path = root / name
        missing = name not in on_disk
        entry["path"] = str(path)
        entry["missing"] = missing
        entry["note"] = state["notes"].get(name, "")
        entry["hasLayout"] = (path / ".herdr" / "layout.toml").is_file()

        workspace = workspaces.get(name)
        entry["open"] = workspace is not None
        entry["workspaceId"] = str(workspace.get("workspace_id")) if workspace else ""
        entry["agentStatus"] = str(workspace.get("agent_status") or "") if workspace else ""
        entry["focused"] = bool(workspace.get("focused")) if workspace else False

        # Tags are set by hand, so they drift. A project claiming to be the
        # live tag with nothing actually open is the drift worth surfacing;
        # the widget marks it rather than silently correcting it.
        entry["stale"] = bool(
            live_tag and entry["tag"] == live_tag and not entry["open"] and not missing
        )

        if missing:
            entry.update({"git": False, "ageDays": None, "dirty": None, "branch": ""})
        else:
            entry.update(git_info(path))
        return entry

    if entries:
        with ThreadPoolExecutor(max_workers=12) as pool:
            entries = list(pool.map(hydrate, entries))

    by_tag: dict[str, list[dict]] = {}
    for entry in entries:
        by_tag.setdefault(entry["tag"], []).append(entry)

    # Freshest first is the only ordering that means anything for projects
    # nobody has tagged yet. Tagged boxes keep file order, which is the
    # priority the user set.
    def age_key(entry: dict) -> tuple[int, int]:
        age = entry["ageDays"]
        return (1, 0) if age is None else (0, age)

    boxes = [
        {
            "name": tag["name"],
            "label": tag["label"],
            "glyph": tag["glyph"],
            "projects": by_tag.get(tag["name"], []),
        }
        for tag in state["tags"]
    ]
    boxes.append({
        "name": UNSORTED,
        "label": UNSORTED_LABEL,
        "glyph": UNSORTED_GLYPH,
        "projects": sorted(by_tag.get(UNSORTED, []), key=age_key),
    })

    counts = {
        "total": len(entries),
        "open": sum(1 for e in entries if e["open"]),
        "working": sum(1 for e in entries if e["agentStatus"] == "working"),
        "blocked": sum(1 for e in entries if e["agentStatus"] == "blocked"),
        "dirty": sum(1 for e in entries if (e["dirty"] or 0) > 0),
        "stale": sum(1 for e in entries if e["stale"]),
        "tagged": len(tagged),
        "unsorted": len(by_tag.get(UNSORTED, [])),
        "missing": sum(1 for e in entries if e["missing"]),
    }

    return {
        "ok": True,
        "error": "",
        "herdrError": herdr_error,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "statePath": str(state_path()),
        "liveTag": live_tag,
        "counts": counts,
        "boxes": boxes,
    }


# --------------------------------------------------------------------------
# Layouts
# --------------------------------------------------------------------------

def load_layout(target: Path, default_agent: str) -> dict:
    """Load a project's Herdr layout, or synthesise a default one.

    Parameters
    ----------
    target : pathlib.Path
        Project directory.
    default_agent : str
        Agent kind to start in projects that ship no layout file. Empty means
        open a plain shell.

    Returns
    -------
    dict
        A layout with a non-empty ``tabs`` list.

    Raises
    ------
    LayoutError
        When the file exists but cannot be parsed.

    Notes
    -----
    Layouts live in the project at ``.herdr/layout.toml`` rather than in this
    plugin, so they travel with the repository to other machines and survive
    plugin updates.
    """
    path = target / ".herdr" / "layout.toml"
    if path.is_file():
        try:
            spec = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise LayoutError(f"{path}: {exc}") from None
        tabs = spec.get("tabs")
        if isinstance(tabs, list) and tabs:
            return {"tabs": [t for t in tabs if isinstance(t, dict)]}
        raise LayoutError(f"{path}: no [[tabs]] entries")

    tab: dict = {"label": "main", "cwd": "."}
    if default_agent:
        tab["agent"] = default_agent
    return {"tabs": [tab]}


def resolve_cwd(target: Path, spec: dict) -> str:
    """Resolve a layout entry's working directory against the project.

    Parameters
    ----------
    target : pathlib.Path
        Project directory.
    spec : dict
        Tab or pane specification, optionally carrying ``cwd``.

    Returns
    -------
    str
        An absolute path. A relative ``cwd`` that does not exist falls back to
        the project root rather than failing the whole layout.
    """
    raw = str(spec.get("cwd") or ".").strip()
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = target / candidate
    return str(candidate if candidate.is_dir() else target)


def agent_name(project: str, index: int) -> str:
    """Derive a valid Herdr agent name from a project name.

    Parameters
    ----------
    project : str
        Project directory name.
    index : int
        Ordinal of this agent within the layout, used to keep names unique.

    Returns
    -------
    str
        A name matching Herdr's ``[a-z][a-z0-9_-]{0,31}`` requirement.
    """
    slug = AGENT_RE.sub("-", project.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "p" + slug
    slug = slug[:26]
    return slug if index == 0 else f"{slug}-{index}"


def _start_in_pane(
    spec: dict, pane_id: str, project: str, counter: list[int], warnings: list[str]
) -> None:
    """Start whatever a layout entry asks for inside an existing pane.

    Parameters
    ----------
    spec : dict
        Tab or pane specification; ``agent`` wins over ``cmd``.
    pane_id : str
        Target pane.
    project : str
        Project name, used to derive agent names.
    counter : list of int
        Single-element mutable counter tracking agents started so far.
    warnings : list of str
        Collects non-fatal failures so a partial layout still opens.
    """
    if not pane_id:
        return

    kind = str(spec.get("agent") or "").strip()
    if kind:
        name = agent_name(project, counter[0])
        counter[0] += 1
        try:
            herdr_call(
                ["agent", "start", name, "--kind", kind, "--pane", pane_id],
                timeout=45,
            )
        except HerdrError as exc:
            warnings.append(f"agent {kind} in {pane_id}: {exc}")
        return

    command = spec.get("cmd")
    if not command:
        return
    words = command if isinstance(command, list) else shlex.split(str(command))
    words = [str(w) for w in words if str(w)]
    if not words:
        return
    try:
        herdr_call(["pane", "run", pane_id, *words], expect_json=False)
    except HerdrError as exc:
        warnings.append(f"cmd in {pane_id}: {exc}")


def _populate_tab(
    target: Path,
    tab_spec: dict,
    root_pane: str,
    project: str,
    counter: list[int],
    warnings: list[str],
) -> None:
    """Split a tab's extra panes and start their processes.

    Parameters
    ----------
    target : pathlib.Path
        Project directory.
    tab_spec : dict
        Tab specification, optionally carrying a ``panes`` list.
    root_pane : str
        Pane id Herdr created with the tab.
    project : str
        Project name.
    counter : list of int
        Shared agent-name counter.
    warnings : list of str
        Collects non-fatal failures.
    """
    _start_in_pane(tab_spec, root_pane, project, counter, warnings)

    current = root_pane
    for pane in tab_spec.get("panes") or []:
        if not isinstance(pane, dict):
            continue
        base = root_pane if str(pane.get("from") or "").lower() == "root" else current
        if not base:
            continue
        direction = str(pane.get("direction") or "right").lower()
        if direction not in ("right", "down"):
            direction = "right"
        args = [
            "pane", "split",
            "--pane", base,
            "--direction", direction,
            "--cwd", resolve_cwd(target, pane),
            "--no-focus",
        ]
        ratio = pane.get("ratio")
        if isinstance(ratio, (int, float)) and 0 < float(ratio) < 1:
            args += ["--ratio", str(float(ratio))]
        try:
            result = herdr_call(args)
        except HerdrError as exc:
            warnings.append(f"split {direction}: {exc}")
            continue
        current = str((result.get("pane") or {}).get("pane_id") or "")
        _start_in_pane(pane, current, project, counter, warnings)


def apply_layout(project: str, target: Path, spec: dict, warnings: list[str]) -> str:
    """Build a Herdr workspace for a project from its layout.

    Parameters
    ----------
    project : str
        Project name, used as the workspace label so the widget can match it
        back to the directory on the next refresh.
    target : pathlib.Path
        Project directory.
    spec : dict
        Layout with a ``tabs`` list.
    warnings : list of str
        Collects non-fatal failures.

    Returns
    -------
    str
        The new workspace id.

    Raises
    ------
    HerdrError
        When the workspace itself cannot be created.
    """
    tabs = spec["tabs"]
    counter = [0]

    first = tabs[0]
    result = herdr_call([
        "workspace", "create",
        "--cwd", resolve_cwd(target, first),
        "--label", project,
        "--focus",
    ])
    workspace_id = str((result.get("workspace") or {}).get("workspace_id") or "")
    tab_id = str((result.get("tab") or {}).get("tab_id") or "")
    root_pane = str((result.get("root_pane") or {}).get("pane_id") or "")

    label = str(first.get("label") or "").strip()
    if label and tab_id:
        try:
            herdr_call(["tab", "rename", tab_id, label])
        except HerdrError as exc:
            warnings.append(f"rename tab: {exc}")

    _populate_tab(target, first, root_pane, project, counter, warnings)

    for tab in tabs[1:]:
        args = ["tab", "create", "--workspace", workspace_id,
                "--cwd", resolve_cwd(target, tab), "--no-focus"]
        label = str(tab.get("label") or "").strip()
        if label:
            args += ["--label", label]
        try:
            created = herdr_call(args)
        except HerdrError as exc:
            warnings.append(f"tab {label or '?'}: {exc}")
            continue
        pane_id = str((created.get("root_pane") or {}).get("pane_id") or "")
        _populate_tab(target, tab, pane_id, project, counter, warnings)

    return workspace_id


# --------------------------------------------------------------------------
# Window focus
# --------------------------------------------------------------------------

def focus_herdr_window() -> bool:
    """Raise the Hyprland window hosting the Herdr session.

    Returns
    -------
    bool
        True when a matching window was found and focused.

    Notes
    -----
    Herdr sets its terminal title from ``window_title`` in ``config.toml``,
    which defaults to ``{hostname}: {workspace}``. Matching on that prefix is
    best effort: a customised title simply means the window is not raised, and
    the workspace switch still happened.
    """
    try:
        proc = subprocess.run(
            ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5
        )
        clients = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False

    prefix = f"{socket.gethostname()}: "
    for client in clients:
        title = str(client.get("title") or "")
        address = str(client.get("address") or "")
        if title.startswith(prefix) and address:
            try:
                subprocess.run(
                    ["hyprctl", "dispatch", "focuswindow", f"address:{address}"],
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return True
    return False


def launch_herdr_terminal() -> None:
    """Start a terminal attached to the persistent Herdr session."""
    launcher = shutil.which("omarchy-launch-terminal-herdr")
    command = [launcher] if launcher else ["xdg-terminal-exec", "herdr"]
    try:
        subprocess.Popen(
            command,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def open_project(root: Path, name: str, default_agent: str) -> dict:
    """Focus or build the Herdr workspace for one project.

    Parameters
    ----------
    root : pathlib.Path
        Directory holding the projects.
    name : str
        Project directory name.
    default_agent : str
        Agent kind for projects without a layout file.

    Returns
    -------
    dict
        Result document with ``ok``, ``error``, ``action`` and ``warnings``.
    """
    target = root / name
    if not target.is_dir():
        return {"ok": False, "error": f"{target} is not a directory", "warnings": []}

    if not herdr_ready():
        launch_herdr_terminal()
        deadline = time.time() + 15
        while time.time() < deadline and not herdr_ready():
            time.sleep(0.5)
        if not herdr_ready():
            return {
                "ok": False,
                "error": "Herdr is not running and did not start in time",
                "warnings": [],
            }

    warnings: list[str] = []
    try:
        existing = herdr_workspaces().get(name)
        if existing:
            herdr_call(["workspace", "focus", str(existing.get("workspace_id"))])
            action = "focused"
        else:
            apply_layout(name, target, load_layout(target, default_agent), warnings)
            action = "created"
    except (HerdrError, LayoutError) as exc:
        return {"ok": False, "error": str(exc), "warnings": warnings}

    focus_herdr_window()
    return {"ok": True, "error": "", "action": action, "warnings": warnings}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Parameters
    ----------
    argv : list of str, optional
        Argument vector; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="report",
                        choices=["report", "open", "tag", "tags", "init", "state"])
    parser.add_argument("name", nargs="?", default="")
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--default-agent", default="")
    args = parser.parse_args(argv)

    root = Path(os.path.expanduser(args.root)).resolve()
    path = state_path()

    if args.command == "state":
        print(path)
        return 0

    if args.command == "init":
        if not path.exists():
            write_state(path, default_state())
        print(path)
        return 0

    if args.command == "tags":
        state = load_state(path)
        for tag in state["tags"]:
            marker = " (live)" if tag["name"] == state["liveTag"] else ""
            print(f"{tag['name']}{marker}")
        print(UNSORTED)
        return 0

    if args.command == "tag":
        if not args.name:
            print(json.dumps({"ok": False, "error": "no project given"}))
            return 2
        state = load_state(path)
        try:
            changed = set_tag(state, args.name, args.value)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
        if changed:
            write_state(path, state)
        print(json.dumps({"ok": True, "error": "", "changed": changed,
                          "project": args.name, "tag": args.value or UNSORTED}))
        return 0

    if args.command == "open":
        if not args.name:
            print(json.dumps({"ok": False, "error": "no project given"}))
            return 2
        result = open_project(root, args.name, args.default_agent.strip())
        print(json.dumps(result))
        return 0 if result["ok"] else 1

    try:
        if not path.exists():
            write_state(path, default_state())
        report = build_report(root, load_state(path))
    except Exception as exc:  # noqa: BLE001 - the bar must never see a traceback
        report = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "herdrError": "",
            "counts": {},
            "boxes": [],
        }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
