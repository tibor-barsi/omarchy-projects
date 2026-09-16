# Projects — an Omarchy bar widget

A tagged project roster in the Omarchy bar, wired to
[Herdr](https://herdr.dev). The pill counts what is open and flags anything
blocked; the popup is the roster, one box per tag. Left-click a project to open
it — its existing Herdr workspace is focused if there is one, otherwise a fresh
workspace is built from the project's layout file. Right-click to move it to
the next box.

## The idea

Only one thing here is stored: **the tags**. Everything else on screen is
derived on every refresh, so a project you create tomorrow appears without
being registered anywhere:

| Shown | Source |
|---|---|
| Open right now | `herdr workspace list` |
| Agent working / blocked | `agent_status` from the same call |
| Staleness | `git log -1` |
| Uncommitted changes | `git status --porcelain` |
| Exists at all | directory scan of the projects folder |

## Install

```bash
omarchy plugin add https://github.com/tibor-barsi/omarchy-projects.git --enable
```

Or, for local development, put this directory at
`~/.config/omarchy/plugins/io.github.tibor-barsi.projects/` and run:

```bash
omarchy plugin enable io.github.tibor-barsi.projects left
```

Note that Omarchy refuses symlinks *inside* a plugin folder, so the plugin
directory itself has to be a real directory.

## Settings

Configured through the Omarchy settings panel, or directly in
`~/.config/omarchy/shell.json`:

| Key | Default | Meaning |
|---|---|---|
| `projectsDir` | `~/data/projects` | Folder holding one subdirectory per project |
| `refreshIntervalSec` | `120` | How often the roster refreshes |
| `defaultAgent` | `none` | Agent started for projects with no layout file |

## Tags

A project carries exactly one tag, which decides which box it sits in. Its
position within that box is its priority — first in the list is the most
important. Anything on disk without a tag lands in **Unsorted**, freshest
first, so new work is never silently missed.

| Tag | For |
|---|---|
| Running | being worked on right now |
| Waiting | blocked on someone or something outside your control |
| Next | queued up, starting soon |
| Paused | deliberately parked |
| Ideas | not started, someday |
| Done | finished, kept for reference |

Set a tag by **right-clicking** a project row, which moves it to the next box
and wraps around through Unsorted. For anything bulkier — reordering within a
box, renaming tags, notes — press `e` to open the state file, or use the CLI.

State lives in `~/.local/state/omarchy-projects/state.json`, deliberately
outside the plugin folder so `omarchy plugin update` cannot overwrite it:

```json
{
  "version": 2,
  "liveTag": "running",
  "tags": [
    { "name": "running", "label": "Running", "glyph": "󰐊" },
    { "name": "waiting", "label": "Waiting", "glyph": "󰔟" }
  ],
  "projects": {
    "running": ["my-project", "my-project"],
    "waiting": []
  },
  "notes":  { "my-project": "finish the EMA release" },
  "hidden": ["_Archive"]
}
```

The `tags` array defines the boxes and their order, so the tag set can be
renamed, reordered or extended without touching any code. Tag glyphs are Nerd
Font Material Design icons — if you add one in Python source, note they live
above U+FFFF and need the 8-digit `\U000FXXXX` escape; the 4-digit `\u` form
silently truncates and renders as garbage.

`liveTag` names the tag that claims a project is being worked on. Carrying it
with no open Herdr workspace marks the row with `⚠` — tags are set by hand, so
they drift, and the widget flags the drift rather than silently correcting it.
Set `liveTag` to `""` to switch the marker off.

Anything listed under a tag but no longer on disk shows as `missing` rather
than being dropped, so a rename is visible instead of silent.

## Layouts

Give a project a predefined Herdr layout by adding `.herdr/layout.toml` to it.
Layouts live in the project rather than in this plugin, so they travel with the
repository to other machines and survive plugin updates. See
`layout.example.toml`.

```toml
[[tabs]]
label = "agent"
cwd = "."
agent = "claude"

  [[tabs.panes]]
  direction = "right"
  ratio = 0.35
  cmd = "git status"

[[tabs]]
label = "run"
cwd = "codebase"
```

Each `[[tabs]]` entry becomes a Herdr tab; its own `cmd` or `agent` runs in the
tab's first pane. Each `[[tabs.panes]]` entry splits a new pane off the
previous one, or off the tab's first pane with `from = "root"`. A pane-level
failure is collected as a warning rather than aborting the layout, so a partial
workspace still opens.

Projects without a layout file get a single pane in the project root, running
`defaultAgent` if one is configured.

## Scripting

The widget registers an IPC target, so the roster can be bound to a key:

```bash
omarchy-shell projects toggle
omarchy-shell projects open my-project
omarchy-shell projects tag my-project waiting
omarchy-shell projects cycle my-project
omarchy-shell projects reload
```

The backend is also usable on its own:

```bash
python3 projects.py report --root ~/data/projects   # JSON roster
python3 projects.py open my-project                     # focus or build
python3 projects.py tag my-project waiting              # move to a box
python3 projects.py tag my-project -                    # untag
python3 projects.py tags                           # list known tags
python3 projects.py state                          # print the state file path
```

## Known limitations

- **IPC targets register only at shell start.** After editing this file, hot
  reload picks up UI changes immediately, but a new or renamed IPC target needs
  `omarchy restart shell`.
- **Raising the Herdr window is best effort.** It matches the terminal title
  against Herdr's default `window_title` of `{hostname}: {workspace}`. A
  customised title just means the window is not raised; the workspace switch
  still happens.
- **One popup across monitors.** The widget is instantiated per monitor, so the
  IPC target resolves to whichever bar registered first. Clicking the pill
  always works on the monitor you clicked.
- **Herdr has no native layout support** as of 0.8.2, so layouts are scripted
  over its CLI. If that lands upstream, the driver in `projects.py` is the only
  part that needs to go.

## License

MIT
