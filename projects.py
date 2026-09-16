#!/usr/bin/env python3
"""Backend for the Omarchy ``projects`` bar widget.

Two jobs live in this file.

``report`` (the default command)
    Merge three sources into one JSON document on stdout: the project
    directories found on disk, the priority lanes recorded in the state file,
    and live Herdr workspace state. Cheap git signals are folded in on top.

``open <name>``
    Focus a project's Herdr workspace when one already exists, otherwise build
    a fresh one from the project's ``.herdr/layout.toml`` (or a default
    single-pane layout), then raise the Herdr terminal window.

The report path never raises. Every failure is reported as ``ok: false`` with
an ``error`` string, so a broken backend degrades to an error pill in the bar
instead of taking the shell down with it.

Only the priority lanes are stored; everything else on screen is derived, so a
project created tomorrow shows up without being registered anywhere.
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

LANES: tuple[str, ...] = ("focus", "active", "next", "paused")
UNSORTED = "unsorted"
DIRTY_LANES = frozenset({"focus", "active"})
DEFAULT_ROOT = "~/data/_Github"
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
    """Return a fresh state document with every lane empty.

    Returns
    -------
    dict
        Seed state. ``_Archive`` is hidden by default because it is a holding
        pen rather than a project.
    """
    return {
        "version": 1,
        "lanes": {lane: [] for lane in LANES},
        "notes": {},
        "hidden": ["_Archive"],
    }


def load_state(path: Path) -> dict:
    """Read and normalise the priority state file.

    Parameters
    ----------
    path : pathlib.Path
        Location of the state file.

    Returns
    -------
    dict
        A state document guaranteed to carry every lane key, a ``notes``
        mapping and a ``hidden`` list. A missing or malformed file yields the
        default state rather than an error: priorities are a convenience, and
        losing them must never block opening a project.
    """
    state = default_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state

    lanes = raw.get("lanes")
    if isinstance(lanes, dict):
        for lane in LANES:
            values = lanes.get(lane)
            if isinstance(values, list):
                state["lanes"][lane] = [v for v in values if isinstance(v, str)]

    notes = raw.get("notes")
    if isinstance(notes, dict):
        state["notes"] = {
            k: str(v) for k, v in notes.items() if isinstance(k, str) and v is not None
        }

    hidden = raw.get("hidden")
    if isinstance(hidden, list):
        state["hidden"] = [v for v in hidden if isinstance(v, str)]

    return state


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


def git_info(path: Path, want_dirty: bool) -> dict:
    """Collect cheap git signals for one project.

    Parameters
    ----------
    path : pathlib.Path
        Project directory.
    want_dirty : bool
        Whether to run ``git status``. Reserved for the lanes actually being
        worked on, since a status call over dozens of repositories on every
        bar refresh is wasteful.

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

    if want_dirty:
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
        Priority state document.

    Returns
    -------
    dict
        Report with ``lanes``, ``counts`` and metadata. Herdr being down is
        reported in ``herdrError`` and leaves the rest of the report intact,
        so the project list still works without a running session.
    """
    herdr_error = ""
    workspaces: dict[str, dict] = {}
    try:
        workspaces = herdr_workspaces()
    except HerdrError as exc:
        herdr_error = str(exc)

    names = discover(root, state["hidden"])
    on_disk = set(names)

    lane_of: dict[str, str] = {}
    ordered: dict[str, list[str]] = {lane: [] for lane in LANES}
    for lane in LANES:
        for name in state["lanes"][lane]:
            if name in lane_of:
                continue
            lane_of[name] = lane
            ordered[lane].append(name)

    unsorted = [name for name in names if name not in lane_of]

    entries: list[dict] = []
    for lane in LANES:
        for name in ordered[lane]:
            entries.append({"name": name, "lane": lane})
    for name in unsorted:
        entries.append({"name": name, "lane": UNSORTED})

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

        if missing:
            entry.update({"git": False, "ageDays": None, "dirty": None, "branch": ""})
        else:
            entry.update(git_info(path, entry["lane"] in DIRTY_LANES))
        return entry

    if entries:
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(hydrate, entries))

    # Freshest first is the only ordering that means anything for projects
    # nobody has triaged yet.
    def age_key(entry: dict) -> tuple[int, int]:
        age = entry["ageDays"]
        return (1, 0) if age is None else (0, age)

    unsorted_entries = sorted(
        (e for e in entries if e["lane"] == UNSORTED), key=age_key
    )
    by_lane = {lane: [e for e in entries if e["lane"] == lane] for lane in LANES}
    by_lane[UNSORTED] = unsorted_entries

    counts = {
        "total": len(entries),
        "open": sum(1 for e in entries if e["open"]),
        "working": sum(1 for e in entries if e["agentStatus"] == "working"),
        "blocked": sum(1 for e in entries if e["agentStatus"] == "blocked"),
        "dirty": sum(1 for e in entries if (e["dirty"] or 0) > 0),
        "focus": len(by_lane["focus"]),
        "unsorted": len(unsorted_entries),
        "missing": sum(1 for e in entries if e["missing"]),
    }

    return {
        "ok": True,
        "error": "",
        "herdrError": herdr_error,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "statePath": str(state_path()),
        "counts": counts,
        "lanes": [
            {"name": lane, "projects": by_lane[lane]}
            for lane in (*LANES, UNSORTED)
        ],
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
                        choices=["report", "open", "init", "state"])
    parser.add_argument("name", nargs="?", default="")
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
            "lanes": [],
        }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
