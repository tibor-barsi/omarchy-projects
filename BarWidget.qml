import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Project roster in the bar. The pill counts what is open and flags anything
// blocked; the popup is the roster itself, grouped into priority lanes, and a
// click opens the project — focusing its existing Herdr workspace when there
// is one, otherwise building a fresh workspace from the project's layout.
//
// Only the lanes are stored (in ~/.local/state/omarchy-projects/state.json);
// live state, git age and dirtiness are derived on every refresh by
// projects.py, which never raises so failures land here as an error pill.
BarWidget {
  id: root
  moduleName: "io.github.tibor-barsi.projects"

  readonly property string home: Quickshell.env("HOME")
  readonly property string scriptPath: home + "/.config/omarchy/plugins/io.github.tibor-barsi.projects/projects.py"
  readonly property string projectsDir: String(setting("projectsDir", "~/data/projects"))
  readonly property string defaultAgent: {
    var value = String(setting("defaultAgent", "none"))
    return value === "none" ? "" : value
  }
  readonly property int refreshIntervalMs: Number(setting("refreshIntervalSec", 120)) * 1000

  property bool popupOpen: false
  property bool loading: false
  property bool ok: false
  property bool everLoaded: false
  property string errorText: ""
  property string herdrError: ""
  property var counts: ({})
  property var lanes: []
  property string statePath: ""
  property string updatedAt: ""
  property string busyProject: ""

  readonly property int openCount: Number(counts.open || 0)
  readonly property int blockedCount: Number(counts.blocked || 0)
  readonly property int workingCount: Number(counts.working || 0)

  readonly property string pillText: !everLoaded ? "󰉋 …"
    : !ok ? "󰉋 !"
    : blockedCount > 0 ? "󰉋 " + openCount + " !"
    : "󰉋 " + openCount

  readonly property string tooltip: {
    if (!ok) return errorText || "Projects"
    var parts = [openCount + " open", workingCount + " working"]
    if (blockedCount > 0) parts.push(blockedCount + " blocked")
    if (Number(counts.dirty || 0) > 0) parts.push(counts.dirty + " dirty")
    return "Projects — " + parts.join(" · ")
  }

  readonly property string updatedLabel: updatedAt !== ""
    ? Qt.formatDateTime(new Date(updatedAt), "HH:mm") : ""

  function close() { popupOpen = false }

  function refresh() {
    if (proc.running) return
    loading = true
    proc.running = true
  }

  function parse(raw) {
    loading = false
    everLoaded = true
    try {
      var data = JSON.parse(String(raw).trim())
      root.ok = data.ok === true
      root.errorText = data.error || ""
      root.herdrError = data.herdrError || ""
      root.counts = data.counts || ({})
      root.lanes = data.lanes || []
      root.statePath = data.statePath || ""
      root.updatedAt = data.updated || ""
    } catch (e) {
      root.ok = false
      root.errorText = "Bad response from projects.py"
    }
  }

  // Lane names are the storage keys; these are what the popup shows.
  function laneTitle(name) {
    if (name === "focus") return "Focus"
    if (name === "active") return "Active"
    if (name === "next") return "Next"
    if (name === "paused") return "Paused"
    return "Unsorted"
  }

  function statusGlyph(project) {
    if (project.missing) return "✕"
    if (!project.open) return "○"
    if (project.agentStatus === "blocked") return "▲"
    if (project.agentStatus === "working") return "●"
    if (project.agentStatus === "unknown") return "◐"
    return "◉"
  }

  function statusColor(project) {
    if (project.missing || project.agentStatus === "blocked") return root.bar.urgent
    if (!project.open) return Qt.darker(root.bar.foreground, 2.0)
    if (project.agentStatus === "working") return root.bar.foreground
    return Qt.darker(root.bar.foreground, 1.4)
  }

  // Right-hand column: the derived facts, cheapest summary that still says
  // something useful about a project you have not looked at in a while.
  function metaText(project) {
    if (project.missing) return "missing"
    var bits = []
    if (project.dirty !== null && project.dirty !== undefined && project.dirty > 0)
      bits.push("±" + project.dirty)
    if (project.ageDays === null || project.ageDays === undefined) bits.push("no git")
    else if (project.ageDays === 0) bits.push("today")
    else if (project.ageDays === 1) bits.push("1d")
    else if (project.ageDays < 90) bits.push(project.ageDays + "d")
    else bits.push(Math.round(project.ageDays / 30) + "mo")
    return bits.join("  ")
  }

  function openProject(name) {
    if (openProc.running) return
    root.busyProject = name
    openProc.command = ["bash", "-lc",
      "exec python3 \"$0\" open \"$1\" --root \"$2\" --default-agent \"$3\"",
      root.scriptPath, name, root.projectsDir, root.defaultAgent]
    openProc.running = true
    root.popupOpen = false
  }

  function editPriorities() {
    if (root.statePath === "") return
    Quickshell.execDetached(["bash", "-lc",
      "exec omarchy-launch-editor \"$0\"", root.statePath])
    root.popupOpen = false
  }

  visible: true
  implicitWidth: pill.implicitWidth
  implicitHeight: barSize

  Process {
    id: proc
    command: ["bash", "-lc", "exec python3 \"$0\" report --root \"$1\"",
      root.scriptPath, root.projectsDir]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parse(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0 && !root.ok) {
        root.loading = false
        root.everLoaded = true
        root.errorText = "projects.py exited with code " + exitCode
      }
    }
  }

  // Opening is fire-and-forget: building a workspace can take a moment when a
  // layout starts an agent, so the only feedback is the roster refreshing
  // once it lands.
  Process {
    id: openProc
    command: ["true"]
    onExited: {
      root.busyProject = ""
      root.refresh()
    }
  }

  Timer {
    interval: Math.max(30000, root.refreshIntervalMs)
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Lets the roster be summoned from a keybind or script:
  //   omarchy-shell projects toggle
  //   omarchy-shell projects open <project-name>
  IpcHandler {
    target: "projects"

    function toggle(): void { root.popupOpen = !root.popupOpen }
    function show(): void { root.popupOpen = true }
    function hide(): void { root.popupOpen = false }
    function reload(): void { root.refresh() }
    function open(name: string): void { root.openProject(name) }
  }

  WidgetButton {
    id: pill
    bar: root.bar
    text: root.pillText
    tooltipText: root.tooltip
    fontSize: Style.font.body
    active: root.blockedCount > 0 || (root.everLoaded && !root.ok)

    onPressed: function(button) {
      if (button === Qt.RightButton) root.refresh()
      else root.popupOpen = !root.popupOpen
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(400))
    contentHeight: popup.fittedContentHeight(flick.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "e" || t === "E") root.editPriorities()
      }

      Flickable {
        id: flick
        anchors.fill: parent
        anchors.rightMargin: Style.space(10)
        contentWidth: width
        contentHeight: column.implicitHeight
        readonly property real implicitHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(8)

          // ---------- Header ----------
          Row {
            width: parent.width
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              text: "󰉋"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.iconLarge
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              width: parent.width - Style.space(66)
              spacing: Style.space(2)

              Text {
                textFormat: Text.PlainText
                text: "Projects"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }

              Text {
                textFormat: Text.PlainText
                text: {
                  if (!root.ok) return root.updatedLabel
                  var parts = [root.openCount + " open"]
                  if (root.workingCount > 0) parts.push(root.workingCount + " working")
                  if (root.blockedCount > 0) parts.push(root.blockedCount + " blocked")
                  if (root.updatedLabel !== "") parts.push(root.updatedLabel)
                  return parts.join(" · ")
                }
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Button {
              iconText: "󰑐"
              iconSpinning: root.loading
              foreground: root.bar.foreground
              tooltipText: "Refresh (r)"
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              anchors.verticalCenter: parent.verticalCenter
              onClicked: root.refresh()
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: !root.ok || root.herdrError !== ""
            width: parent.width
            wrapMode: Text.WordWrap
            text: !root.ok ? (root.errorText || "Loading…")
              : "Herdr unavailable — " + root.herdrError
            color: root.bar.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // ---------- Lanes ----------
          Repeater {
            model: root.lanes

            Column {
              required property var modelData
              width: column.width
              spacing: Style.space(4)
              visible: modelData.projects.length > 0

              PanelSeparator { foreground: root.bar.foreground }

              PanelSectionHeader {
                text: root.laneTitle(modelData.name) + "  (" + modelData.projects.length + ")"
                foreground: root.bar.foreground
              }

              Repeater {
                model: modelData.projects

                Rectangle {
                  id: row
                  required property var modelData
                  width: column.width
                  height: nameText.implicitHeight + noteText.height + Style.space(10)
                  radius: Style.cornerRadius
                  color: area.containsMouse
                    ? Style.hoverFillFor(root.bar.foreground, root.bar.foreground)
                    : "transparent"

                  Row {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.space(4)
                    anchors.rightMargin: Style.space(4)
                    spacing: Style.space(7)

                    Text {
                      textFormat: Text.PlainText
                      text: root.statusGlyph(row.modelData)
                      color: root.statusColor(row.modelData)
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(12)
                      horizontalAlignment: Text.AlignHCenter
                    }

                    Column {
                      width: parent.width - Style.space(85)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(1)

                      Text {
                        id: nameText
                        textFormat: Text.PlainText
                        text: row.modelData.name
                        color: row.modelData.focused
                          ? root.bar.urgent : root.bar.foreground
                        font.family: root.bar.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: row.modelData.open
                        width: parent.width
                        elide: Text.ElideRight
                      }

                      Text {
                        id: noteText
                        textFormat: Text.PlainText
                        visible: String(row.modelData.note || "") !== ""
                        height: visible ? implicitHeight : 0
                        text: row.modelData.note || ""
                        color: Qt.darker(root.bar.foreground, 1.7)
                        font.family: root.bar.fontFamily
                        font.pixelSize: Style.font.caption
                        width: parent.width
                        elide: Text.ElideRight
                      }
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: root.busyProject === row.modelData.name
                        ? "…" : root.metaText(row.modelData)
                      color: Qt.darker(root.bar.foreground, 1.7)
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.caption
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(58)
                      horizontalAlignment: Text.AlignRight
                    }
                  }

                  MouseArea {
                    id: area
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    enabled: !row.modelData.missing
                    onClicked: root.openProject(row.modelData.name)
                  }
                }
              }
            }
          }

          // ---------- Footer ----------
          PanelSeparator { foreground: root.bar.foreground }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Click a project to open it in Herdr  ·  e: edit priorities  ·  r: refresh"
            color: Qt.darker(root.bar.foreground, 1.8)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
