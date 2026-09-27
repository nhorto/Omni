import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Reads $XDG_RUNTIME_DIR/omni/status.json, which omnid rewrites on every state change.
BarWidget {
  id: root
  moduleName: "local.omni"

  property string assistantState: "idle"
  property string assistantDetail: ""
  property bool continuous: false
  property bool wake: false
  property int tokensToday: 0
  property bool menuOpen: false
  readonly property string omni: Quickshell.env("HOME") + "/.local/bin/omni"
  readonly property var labels: ({ idle: "Omni", listening: "Listening", thinking: "Thinking", working: "Working",
                                   speaking: "Speaking", awaiting_approval: "Needs OK" })

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function run(args) {
    menuOpen = false
    Quickshell.execDetached([omni].concat(args))
  }

  function readStatus(raw) {
    try {
      var value = JSON.parse(raw)
      assistantState = String(value.state || "idle")
      assistantDetail = String(value.detail || "")
      continuous = !!value.continuous
      wake = !!value.wake
      if (value.tokens_today !== undefined) tokensToday = value.tokens_today
    } catch (error) {
      assistantState = "idle"
    }
  }

  FileView {
    id: statusFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omni/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.readStatus(text())
  }

  // omnid replaces the file atomically; re-arm the watch in case the inode swap is missed.
  Timer { interval: 2000; running: true; repeat: true; onTriggered: statusFile.reload() }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "O" : (root.labels[root.assistantState] || root.assistantState) + (root.continuous && root.assistantState === "idle" ? " ·  hands-free" : "")
    active: root.assistantState !== "idle" || root.continuous
    horizontalMargin: 8
    tooltipText: (root.assistantDetail || "Omni is ready") + "\nToday: " + Math.round(root.tokensToday / 1000) + "k tokens"
      + "\nClick: menu · Middle-click: talk · Right-click: open Omni"
    onPressed: function(buttonCode) {
      if (!root.bar) return
      if (buttonCode === Qt.RightButton) root.run(["app"])
      else if (buttonCode === Qt.MiddleButton) root.run(["listen"])
      else root.menuOpen = !root.menuOpen
    }
  }

  PopupCard {
    id: actionMenu
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.menuOpen
    contentWidth: Style.space(300)
    contentHeight: Style.space(260)

    Column {
      anchors.fill: parent
      spacing: Style.space(5)

      Text {
        width: parent.width
        text: "Omni · " + (root.labels[root.assistantState] || root.assistantState)
        color: Color.popups.text
        font.family: Style.font.family
        font.bold: true
        font.pixelSize: 15
      }
      Button { width: parent.width; text: root.assistantState === "listening" ? "Finish and send" : "Talk to Omni"; onClicked: root.run(["listen"]) }
      Button { width: parent.width; text: "Quick ask"; onClicked: root.run(["popover"]) }
      Button { width: parent.width; text: root.continuous ? "Stop hands-free listening" : "Hands-free listening"; onClicked: root.run(["voice", "continuous", root.continuous ? "off" : "on"]) }
      Button { width: parent.width; text: root.wake ? "Turn wake word off" : "Turn wake word on"; onClicked: root.run(["voice", "wake", root.wake ? "off" : "on"]) }
      Button { width: parent.width; text: "Stop"; enabled: root.assistantState !== "idle"; onClicked: root.run(["stop"]) }
      Button { width: parent.width; text: "Open Omni"; onClicked: root.run(["app"]) }
    }
  }
}
