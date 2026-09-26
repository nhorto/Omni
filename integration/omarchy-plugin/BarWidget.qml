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
  property bool omiRecording: false
  property bool continuous: false
  readonly property string displayedState: voiceState === "recording" || voiceState === "transcribing"
    ? (omiRecording || continuous ? "Omi " + voiceState : "Dictation " + voiceState)
    : (continuous ? "Omi listening" : assistantState)

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
    id: omiModeFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "") + "/omi/omi-recording"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.omiRecording = text().trim() === "omi"
    onLoadFailed: root.omiRecording = false
  }

  FileView {
    id: continuousFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "") + "/omi/continuous"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.continuous = text().trim() === "on"
    onLoadFailed: root.continuous = false
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
      omiModeFile.reload()
      continuousFile.reload()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "O" : (root.displayedState === "idle" ? "Omi · " + root.selectedAgent : root.displayedState)
    active: root.displayedState !== "idle"
    horizontalMargin: 8
    tooltipText: root.displayedState + "\nClick: talk to Omi / finish · Middle-click: Omi app · Right-click: settings"
    onPressed: function(buttonCode) {
      if (!root.bar) return
      if (buttonCode === Qt.RightButton)
        Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi", "--page", "settings"])
      else if (buttonCode === Qt.MiddleButton)
        Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi"])
      else if (root.continuous)
        Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi-voice", "continuous", "off"])
      else if (root.voiceState === "recording" && !root.omiRecording)
        Quickshell.execDetached(["voxtype", "record", "stop"])
      else
        Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi-voice", "toggle"])
    }
  }
}
