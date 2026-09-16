# Projects — an Omarchy bar widget

A prioritised project roster in the Omarchy bar, wired to
[Herdr](https://herdr.dev). The pill counts what is open and flags anything
blocked; the popup is the roster, grouped into priority lanes. Click a project
to open it — its existing Herdr workspace is focused if there is one, otherwise
a fresh workspace is built from the project's layout file.

## The idea

Only one thing here is stored: **the priority lanes**. Everything else on
screen is derived on every refresh, so a project you create tomorrow appears
without being registered anywhere:

| Shown | Source |
|---|---|
| Open right now | `herdr workspace list` |
| Agent working / blocked | `agent_status` from the same call |
| Staleness | `git log -1` |
| Uncommitted changes | `git status --porcelain` (focus and active lanes only) |
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

## Priorities

Lanes live in `~/.local/state/omarchy-projects/state.json` — deliberately
outside the plugin folder, so `omarchy plugin update` cannot overwrite them.

```json
{
  "version": 1,
  "lanes": {
    "focus":  ["my-project"],
    "active": ["my-project", "unified_calendar"],
    "next":   [],
    "paused": ["pysernal"]
  },
  "notes":  { "my-project": "finish the EMA release" },
  "hidden": ["_Archive"]
}
```

Order within a lane is the order in the list. Anything on disk but not in a
lane shows up under **Unsorted**, freshest first, so new work is never
silently missed. Anything in a lane but no longer on disk is shown as
`missing` rather than dropped, so a rename is visible.

Press `e` in the popup to open the file in your editor, `r` to refresh.

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
omarchy-shell projects reload
```

The backend is also usable on its own:

```bash
python3 projects.py report --root ~/data/projects   # JSON roster
python3 projects.py open my-project                     # focus or build
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
