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

## Keyboard

Bind the roster to a key — `shell toggle` is the right route, because the shell
opens the copy on the *focused* screen, which the widget's own IPC cannot do:

```lua
-- ~/.config/hypr/bindings.lua
hl.unbind("SUPER + SHIFT + P")
o.bind("SUPER + SHIFT + P", "Projects", "omarchy-shell shell toggle io.github.tibor-barsi.projects")
```

With the roster open, everything works without the mouse:

| Key | Does |
|---|---|
| `↑` `↓` / `k` `j` | move the selection |
| `←` `→` / `h` `l` | jump to the previous or next box |
| `K` `J` | raise or lower the selected project's priority |
| `Enter` | open the selected project in Herdr |
| `t` | open the tag picker for the selected row |
| `1`–`9` | set the tag by box number |
| `0` / `x` | clear the tag |
| `c` | save the live workspace as this project's layout |
| `e` | open the state file · `r` refresh · `Esc` close |

The selection is held by project name rather than row position, so tagging a
project moves the selection with it into its new box instead of landing on
whatever slid into that row. Collapsed boxes are skipped.

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

Priority is the only ordering there is, and it is always on: boxes render in
the order they are stored, with no sort setting to choose. Raise or lower the
selected project with `K` and `J` — capitals, since lowercase `k` and `j` move
the selection rather than the project. Unsorted does not reorder, because that
box is derived freshest-first and would discard a hand-made order on the next
refresh; give a project a tag first.

| Tag | For |
|---|---|
| Running | being worked on right now |
| Blocked | waiting on someone or something outside your control |
| Next | queued up, starting soon |
| Paused | deliberately parked |
| Ideas | not started, someday |
| Ignore | directories you do not want to think about |

Ignore is *muted* and *collapsed*: its projects are folded away behind their
header and are left out of every count, so a tag meaning "stop showing me this"
actually stops showing it. Muted boxes sort below even Unsorted. Click any box
header to fold or unfold it.

Three ways to set a tag, all doing the same thing:

- **Right-click a row**, or click its 󰓹 button, to open an inline picker and
  click the tag you want.
- **Hover a row and press a number** — `1` for the first box through to the
  last, `0` to clear the tag. The picker shows each box's number.
- **Edit the state file** with `e` for anything bulkier: renaming tags, notes,
  or reshuffling a whole box at once.

State lives in `~/.local/state/omarchy-projects/state.json`, deliberately
outside the plugin folder so `omarchy plugin update` cannot overwrite it:

```json
{
  "version": 2,
  "liveTag": "running",
  "tags": [
    { "name": "running", "label": "Running", "glyph": "󰐊" },
    { "name": "blocked", "label": "Blocked", "glyph": "󰜺" },
    { "name": "ignore", "label": "Ignore", "glyph": "󰈉",
      "collapsed": true, "muted": true }
  ],
  "projects": {
    "running": ["my-project", "another-project"],
    "blocked": []
  },
  "notes":  { "my-project": "finish the release" },
  "hidden": ["_Archive"]
}
```

The `tags` array defines the boxes and their order, so the tag set can be
renamed, reordered or extended without touching any code. `collapsed` starts a
box folded; `muted` keeps its projects out of the counts and sorts it last. Tag glyphs are Nerd
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

### The shared default

A project with no layout of its own falls back to a shared default, and only
then to a single pane. Set it from any workspace you like the shape of:

```bash
python3 projects.py capture <project> --default
```

That writes `~/.local/state/omarchy-projects/default-layout.toml`, next to the
tags and equally safe from plugin updates. Delete the file to go back to a
single pane.

### Capturing one

The quickest way to get a layout is to stop writing one. Arrange the workspace
in Herdr however you want it — split the panes, open the tabs, start the agent
— then open the roster and click the 󰆓 button on that project's row. The live
workspace is written out as its `.herdr/layout.toml`, and that is what it
reopens with from then on. Any previous layout is kept alongside as
`layout.toml.bak`.

The same thing from a script:

```bash
omarchy-shell projects capture my-project
python3 projects.py capture my-project
```

Capture reads the split tree out of Herdr, including each split's direction and
ratio, the working directory of every pane, and whatever each pane is running.
An agent is recorded by kind together with the flags it was started with
(`agent = "claude"`, `args = ["--dangerously-skip-permissions"]`), and any
other foreground program by its command line (`cmd = "lazygit"`), so a pane
running lazygit, btop or an editor comes back on the next open. Arguments are
read from the running process, so a shell alias is captured as the command it
expands to — which is what has to be replayed, since the agent is started
directly and never sees your alias. A pane sitting at a bare shell
prompt is recorded as just a pane. It is a snapshot of that moment: a command
that has already exited is not recorded, because nothing is running in it.

### Writing one by hand

Give a project a predefined Herdr layout by adding `.herdr/layout.toml` to it.
Layouts live in the project rather than in this plugin, so they travel with the
repository to other machines and survive plugin updates. See
`layout.example.toml`.

```toml
[[tabs]]
label = "agent"
cwd = "."
agent = "claude"
args = ["--dangerously-skip-permissions"]   # optional, passed to the agent

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
previous one, off the tab's first pane with `from = "root"`, or off any earlier
pane that gave itself a `name`. That last form is what lets an arbitrary split
tree be written as a flat list, and so what lets a captured layout reproduce
the workspace exactly. A pane-level failure is collected as a warning rather
than aborting the layout, so a partial workspace still opens.

Projects without a layout file get a single pane in the project root, running
`defaultAgent` if one is configured.

## Scripting

The widget registers an IPC target, so the roster can be bound to a key:

```bash
omarchy-shell projects toggle
omarchy-shell projects open my-project
omarchy-shell projects tag my-project blocked
omarchy-shell projects cycle my-project
omarchy-shell projects move my-project up
omarchy-shell projects capture my-project
omarchy-shell projects reload
```

The backend is also usable on its own:

```bash
python3 projects.py report --root ~/data/projects   # JSON roster
python3 projects.py open my-project                     # focus or build
python3 projects.py tag my-project blocked              # move to a box
python3 projects.py move my-project up                  # up | down | top | bottom
python3 projects.py capture my-project                  # live workspace -> layout
python3 projects.py tag my-project -                    # untag
python3 projects.py tags                           # list known tags
python3 projects.py state                          # print the state file path
```

## Known limitations

- **IPC targets register only at shell start, and a hot reload leaves the old
  instance holding them.** Editing the QML reloads the widget, but the previous
  instance stays alive owning the IPC target, so `omarchy-shell projects ...`
  keeps driving the stale copy — which still refreshes its data, and so looks
  live while running old code. When a structural change does not appear, run
  `omarchy restart shell` rather than trusting the reload.
- **Nerd Font glyphs need 8-digit escapes in both languages.** These icons live
  above U+FFFF, and `\u` takes exactly four hex digits in Python *and* in
  QML/JavaScript, so `\uF0193` silently becomes U+F019 followed by `3`. Use
  `\U000F0193` in Python and the literal character in QML.
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
