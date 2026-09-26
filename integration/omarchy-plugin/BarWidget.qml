import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

BarWidget {
  id: root
  moduleName: "local.omi"

  property string assistantState: "idle"
  property string assistantDetail: ""
  property string selectedAgent: "codex"
  property string voiceState: "idle"
  readonly property string displayedState: voiceState === "recording" || voiceState === "transcribing"
    ? voiceState : assistantState

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function readStatus(raw) {
    try {
      var value = JSON.parse(raw)
      assistantState = String(value.state || "idle")
      assistantDetail = String(value.detail || "")
      selectedAgent = String(value.agent || "codex")
    } catch (error) {
      assistantState = "idle"
      assistantDetail = ""
    }
  }

  FileView {
    id: statusFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || (Quickshell.env("HOME") + "/.cache")) + "/omi/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.readStatus(text())
  }

  FileView {
    id: voiceFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "") + "/voxtype/state"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.voiceState = text().trim()
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    onTriggered: {
      statusFile.reload()
      voiceFile.reload()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "O" : (root.displayedState === "idle" ? "Omi · " + root.selectedAgent : "Omi · " + root.displayedState)
    active: root.displayedState !== "idle"
    horizontalMargin: 8
    tooltipText: (root.assistantDetail || ("Omi: " + root.displayedState)) + "\nClick: Omi app · Right-click: settings"
    onPressed: function(buttonCode) {
      if (!root.bar) return
      if (buttonCode === Qt.RightButton)
        Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi", "--page", "settings"])
      else Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi"])
    }
  }
}
