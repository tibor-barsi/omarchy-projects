import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Project roster in the bar. The pill counts what is open and flags anything
// blocked; the popup is the roster itself, one box per tag, and a left click
// opens the project — focusing its existing Herdr workspace when there is one,
// otherwise building a fresh workspace from the project's layout. A right
// click cycles the project's tag, moving its line to the next box.
//
// Only the tags are stored (in ~/.local/state/omarchy-projects/state.json);
// live state, git age and dirtiness are derived on every refresh by
// projects.py, which never raises so failures land here as an error pill.
// Boxes, their labels and their glyphs all come from that file, so the tag
// set can change without touching this QML.
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
  property var boxes: []
  property string liveTag: ""
  property string statePath: ""
  property string updatedAt: ""
  property string busyProject: ""
  property string pickerFor: ""
  property string hoveredProject: ""
  // The selection is held by name, not by position: tagging a project moves
  // it to another box and renumbers every row, so an index would silently
  // start pointing at a different project.
  property string selectedName: ""
  // Box names the user has folded this session, seeded from each tag's
  // `collapsed` flag the first time a report arrives.
  property var collapsed: ({})
  property bool collapseSeeded: false

  readonly property int openCount: Number(counts.open || 0)
  readonly property int blockedCount: Number(counts.blocked || 0)
  readonly property int workingCount: Number(counts.working || 0)
  readonly property int staleCount: Number(counts.stale || 0)

  readonly property string pillText: !everLoaded ? "󰉋 …"
    : !ok ? "󰉋 !"
    : blockedCount > 0 ? "󰉋 " + openCount + " !"
    : "󰉋 " + openCount

  readonly property string tooltip: {
    if (!ok) return errorText || "Projects"
    var parts = [openCount + " open", workingCount + " working"]
    if (blockedCount > 0) parts.push(blockedCount + " blocked")
    if (Number(counts.dirty || 0) > 0) parts.push(counts.dirty + " dirty")
    if (staleCount > 0) parts.push(staleCount + " stale")
    return "Projects — " + parts.join(" · ")
  }

  readonly property string updatedLabel: updatedAt !== ""
    ? Qt.formatDateTime(new Date(updatedAt), "HH:mm") : ""

  // The shell's bar-widget panel contract: `opened`, `open()` and `close()`
  // are what let `omarchy-shell shell toggle <plugin-id>` drive this widget,
  // and the shell picks the copy on the focused screen rather than whichever
  // instance happened to register an IPC target first.
  readonly property bool opened: popupOpen

  function open() {
    popupOpen = true
    if (root.selectedIndex < 0 && root.visibleRows.length > 0)
      root.selectedName = root.visibleRows[0]
  }

  function close() {
    popupOpen = false
    pickerFor = ""
  }

  // Every row the user can currently move to, in the order they are drawn.
  readonly property var visibleRows: {
    var out = []
    for (var i = 0; i < root.boxes.length; i++) {
      var box = root.boxes[i]
      if (root.isCollapsed(box.name)) continue
      var items = box.projects
      for (var j = 0; j < items.length; j++) out.push(items[j].name)
    }
    return out
  }

  readonly property int selectedIndex: root.visibleRows.indexOf(root.selectedName)

  // Keyboard acts on the selection; the mouse acts on whatever it is over.
  function targetRow() {
    return root.selectedName !== "" ? root.selectedName : root.hoveredProject
  }

  function moveSelection(delta) {
    var n = root.visibleRows.length
    if (n === 0) { root.selectedName = ""; return }
    var next = root.selectedIndex < 0 ? (delta > 0 ? 0 : n - 1)
      : root.selectedIndex + delta
    root.selectedName = root.visibleRows[next < 0 ? n - 1 : (next >= n ? 0 : next)]
    root.pickerFor = ""
  }

  // Left/right jump a whole box, which is what makes 44 rows navigable.
  function moveBox(delta) {
    var n = root.visibleRows.length
    if (n === 0) return
    var starts = []
    var seen = 0
    for (var i = 0; i < root.boxes.length; i++) {
      var box = root.boxes[i]
      if (root.isCollapsed(box.name) || box.projects.length === 0) continue
      starts.push(seen)
      seen += box.projects.length
    }
    if (starts.length === 0) return
    var current = 0
    for (var s = 0; s < starts.length; s++)
      if (starts[s] <= root.selectedIndex) current = s
    var target = current + delta
    root.selectedName = root.visibleRows[starts[target < 0 ? starts.length - 1
      : (target >= starts.length ? 0 : target)]]
    root.pickerFor = ""
  }

  function activateSelected() {
    if (root.selectedName !== "") root.openProject(root.selectedName)
  }

  // Rows sit inside nested Columns, so their own y says nothing about where
  // they are in the scrolled content; map into the content column instead.
  function ensureVisible(item) {
    if (!item || !flick.visible) return
    var pos = item.mapToItem(column, 0, 0)
    if (pos.y < flick.contentY)
      flick.contentY = Math.max(0, pos.y)
    else if (pos.y + item.height > flick.contentY + flick.height)
      flick.contentY = Math.min(
        Math.max(0, flick.contentHeight - flick.height),
        pos.y + item.height - flick.height)
  }

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
      root.boxes = data.boxes || []
      root.liveTag = data.liveTag || ""
      if (!root.collapseSeeded && root.boxes.length > 0) {
        var seed = ({})
        for (var i = 0; i < root.boxes.length; i++)
          seed[root.boxes[i].name] = root.boxes[i].collapsed === true
        root.collapsed = seed
        root.collapseSeeded = true
      }
      root.statePath = data.statePath || ""
      root.updatedAt = data.updated || ""
    } catch (e) {
      root.ok = false
      root.errorText = "Bad response from projects.py"
    }
  }

  function statusGlyph(project) {
    if (project.missing) return "✕"
    if (!project.open) return "○"
    if (project.agentStatus === "blocked") return "▲"
    if (project.agentStatus === "working") return "●"
    if (project.agentStatus === "unknown") return "◔"
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
    // Tagged as the live tag with nothing actually open in Herdr. Marked, not
    // corrected — the tag is the user's statement of intent, not a cache.
    if (project.stale) bits.push("⚠")
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

  function isCollapsed(box) {
    return root.collapsed[box] === true
  }

  function toggleCollapsed(box) {
    var next = ({})
    for (var k in root.collapsed) next[k] = root.collapsed[k]
    next[box] = !(next[box] === true)
    root.collapsed = next
  }

  function openPicker(name) {
    root.pickerFor = (root.pickerFor === name) ? "" : name
  }

  function clearTag(name) {
    if (name === "") return
    root.setTag(name, "-")
    root.pickerFor = ""
  }

  // Number keys assign by position: 1 is the first box, 0 clears the tag.
  function assignByIndex(name, index) {
    if (name === "" || index < 0 || index >= root.boxes.length) return
    var box = root.boxes[index].name
    root.setTag(name, box === "unsorted" ? "-" : box)
    root.pickerFor = ""
  }

  function captureLayout(name) {
    if (captureProc.running) return
    root.busyProject = name
    captureProc.command = ["bash", "-lc",
      "exec python3 \"$0\" capture \"$1\" --root \"$2\"",
      root.scriptPath, name, root.projectsDir]
    captureProc.running = true
  }

  function tagOf(name) {
    for (var i = 0; i < root.boxes.length; i++) {
      var items = root.boxes[i].projects
      for (var j = 0; j < items.length; j++)
        if (items[j].name === name) return root.boxes[i].name
    }
    return "unsorted"
  }

  // Right-clicking a row walks the tag list in the order the state file
  // declares it, ending at unsorted before wrapping around.
  function cycleTag(name, current) {
    var order = []
    for (var i = 0; i < root.boxes.length; i++) order.push(root.boxes[i].name)
    if (order.length === 0) return
    var idx = order.indexOf(current)
    var next = order[(idx + 1) % order.length]
    root.setTag(name, next === "unsorted" ? "-" : next)
  }

  // Position inside a box is priority, so reordering is the whole of it --
  // there is no separate priority field to drift out of step with the list.
  // Untagged rows are skipped: Unsorted is derived freshest-first and would
  // discard a hand-made order on the next refresh.
  function moveProject(name, direction) {
    if (name === "" || moveProc.running) return
    if (root.tagOf(name) === "unsorted") return
    root.busyProject = name
    moveProc.command = ["bash", "-lc",
      "exec python3 \"$0\" move \"$1\" \"$2\"",
      root.scriptPath, name, direction]
    moveProc.running = true
  }

  function setTag(name, tag) {
    if (tagProc.running) return
    root.busyProject = name
    tagProc.command = ["bash", "-lc",
      "exec python3 \"$0\" tag \"$1\" \"$2\"", root.scriptPath, name, tag]
    tagProc.running = true
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

  Process {
    id: captureProc
    command: ["true"]
    onExited: {
      root.busyProject = ""
      root.refresh()
    }
  }

  Process {
    id: tagProc
    command: ["true"]
    onExited: {
      root.busyProject = ""
      root.refresh()
    }
  }

  Process {
    id: moveProc
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
    function tag(name: string, tag: string): void { root.setTag(name, tag) }
    function cycle(name: string): void { root.cycleTag(name, root.tagOf(name)) }
    function capture(name: string): void { root.captureLayout(name) }
    function move(name: string, direction: string): void { root.moveProject(name, direction) }
    function pick(name: string): void { root.pickerFor = name; root.popupOpen = true }
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
      else if (root.popupOpen) root.close()
      else root.open()
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
      onCloseRequested: {
        if (root.pickerFor !== "") root.pickerFor = ""
        else root.close()
      }
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveSelection(dy)
        else if (dx !== 0) root.moveBox(dx)
      }
      onReturnRequested: root.activateSelected()
      onDeleteRequested: root.clearTag(root.targetRow())
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "e" || t === "E") root.editPriorities()
        else if (t === "t" || t === "T") {
          if (root.targetRow() !== "") root.openPicker(root.targetRow())
        } else if (t === "c" || t === "C") {
          if (root.targetRow() !== "") root.captureLayout(root.targetRow())
        } else if (t === "K") root.moveProject(root.targetRow(), "up")
        else if (t === "J") root.moveProject(root.targetRow(), "down")
        else if (t === "0") root.clearTag(root.targetRow())
        else if (t >= "1" && t <= "9")
          root.assignByIndex(root.targetRow(), parseInt(t) - 1)
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
                  if (root.staleCount > 0) parts.push(root.staleCount + " stale")
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
            text: !root.ok ? (root.errorText || "Loading…") : root.herdrError
            maximumLineCount: 3
            elide: Text.ElideRight
            color: root.bar.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // ---------- Boxes, one per tag ----------
          Repeater {
            model: root.boxes

            Column {
              required property var modelData
              width: column.width
              spacing: Style.space(4)
              visible: modelData.projects.length > 0

              PanelSeparator { foreground: root.bar.foreground }

              Item {
                width: column.width
                height: sectionHeader.implicitHeight

                PanelSectionHeader {
                  id: sectionHeader
                  anchors.left: parent.left
                  anchors.right: parent.right
                  text: (modelData.glyph ? modelData.glyph + "  " : "")
                    + modelData.label + "  (" + modelData.projects.length + ")"
                    + (root.isCollapsed(modelData.name) ? "   󰅂" : "")
                  foreground: root.bar.foreground
                }

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.toggleCollapsed(modelData.name)
                }
              }

              Repeater {
                model: root.isCollapsed(modelData.name) ? [] : modelData.projects

                Column {
                  id: rowItem
                  required property var modelData
                  width: column.width
                  spacing: 0

                Rectangle {
                  id: row
                  readonly property var modelData: rowItem.modelData
                  readonly property bool selected:
                    root.selectedName === rowItem.modelData.name
                  onSelectedChanged: if (selected) root.ensureVisible(rowItem)
                  width: column.width
                  height: nameText.implicitHeight + noteText.height + Style.space(10)
                  radius: Style.cornerRadius
                  color: selected
                    ? Style.selectedFillFor(root.bar.foreground, root.bar.foreground)
                    : (area.containsMouse
                      ? Style.hoverFillFor(root.bar.foreground, root.bar.foreground)
                      : "transparent")

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

                    // At rest this column carries the derived facts; on hover
                    // it becomes the actions, so rows stay narrow and quiet.
                    Item {
                      width: Style.space(58)
                      height: Style.space(22)
                      anchors.verticalCenter: parent.verticalCenter

                      Text {
                        anchors.fill: parent
                        visible: !area.containsMouse || root.busyProject === row.modelData.name
                        textFormat: Text.PlainText
                        text: root.busyProject === row.modelData.name
                          ? "…" : root.metaText(row.modelData)
                        color: Qt.darker(root.bar.foreground, 1.7)
                        font.family: root.bar.fontFamily
                        font.pixelSize: Style.font.caption
                        verticalAlignment: Text.AlignVCenter
                        horizontalAlignment: Text.AlignRight
                      }

                      Row {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(2)
                        visible: area.containsMouse && root.busyProject !== row.modelData.name

                        PanelActionButton {
                          iconText: "󰆓"
                          size: Style.space(20)
                          fontSize: Style.font.caption
                          visible: row.modelData.open === true
                          foreground: Qt.darker(root.bar.foreground, 1.4)
                          hoverColor: root.bar.foreground
                          tooltipText: "Save this workspace as the layout"
                          onClicked: root.captureLayout(row.modelData.name)
                        }

                        PanelActionButton {
                          iconText: "󰓹"
                          size: Style.space(20)
                          fontSize: Style.font.caption
                          foreground: Qt.darker(root.bar.foreground, 1.4)
                          hoverColor: root.bar.foreground
                          tooltipText: "Change tag"
                          onClicked: root.openPicker(row.modelData.name)
                        }
                      }
                    }
                  }

                  MouseArea {
                    id: area
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    enabled: !row.modelData.missing
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onEntered: root.hoveredProject = row.modelData.name
                    onExited: if (root.hoveredProject === row.modelData.name)
                      root.hoveredProject = ""
                    onClicked: function(mouse) {
                      root.selectedName = row.modelData.name
                      if (mouse.button === Qt.RightButton)
                        root.openPicker(row.modelData.name)
                      else
                        root.openProject(row.modelData.name)
                    }
                  }
                }

                // Inline tag picker. Expanding in place rather than as a
                // nested popup keeps it clear of the Flickable's clipping.
                Item {
                  width: column.width
                  visible: root.pickerFor === rowItem.modelData.name
                  height: visible ? picker.implicitHeight + Style.space(6) : 0

                  Flow {
                    id: picker
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: Style.space(20)
                    anchors.top: parent.top
                    spacing: Style.space(3)

                    Repeater {
                      model: root.boxes

                      Rectangle {
                        required property var modelData
                        required property int index
                        readonly property bool current:
                          modelData.name === rowItem.modelData.tag
                        height: chipText.implicitHeight + Style.space(5)
                        width: chipText.implicitWidth + Style.space(10)
                        radius: Style.cornerRadius
                        color: current
                          ? Style.selectedFillFor(root.bar.foreground, root.bar.foreground)
                          : (chipArea.containsMouse
                            ? Style.hoverFillFor(root.bar.foreground, root.bar.foreground)
                            : "transparent")

                        Text {
                          id: chipText
                          anchors.centerIn: parent
                          textFormat: Text.PlainText
                          text: (modelData.glyph ? modelData.glyph + " " : "")
                            + modelData.label + "  " + (index + 1)
                          color: parent.current
                            ? root.bar.foreground : Qt.darker(root.bar.foreground, 1.5)
                          font.family: root.bar.fontFamily
                          font.pixelSize: Style.font.caption
                        }

                        MouseArea {
                          id: chipArea
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.assignByIndex(rowItem.modelData.name, index)
                        }
                      }
                    }
                  }
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
            text: "↑↓ select  ·  ←→ jump box  ·  enter opens  ·  t tag  ·  1-9 set, 0/x clear"
              + "  ·  c saves the live layout  ·  e edit file  ·  r refresh  ·  esc close"
            color: Qt.darker(root.bar.foreground, 1.8)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
