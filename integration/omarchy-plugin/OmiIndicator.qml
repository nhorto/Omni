import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

Item {
  id: root
  property string assistantState: "idle"
  property string assistantDetail: ""
  property string voiceState: "idle"
  property bool omiRecording: false
  property bool continuous: false
  readonly property bool micActive: voiceState === "recording" || voiceState === "transcribing"
  readonly property string displayedState: micActive
    ? (omiRecording || continuous ? "Omi " + voiceState : "Dictation " + voiceState)
    : (continuous ? "Omi listening" : assistantState)
  readonly property bool shouldShow: micActive || continuous
    || displayedState === "thinking" || displayedState === "working" || displayedState === "awaiting approval"

  function readStatus(raw) {
    try {
      var value = JSON.parse(raw)
      assistantState = String(value.state || "idle")
      assistantDetail = String(value.detail || "")
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
    interval: 1000
    running: true
    repeat: true
    onTriggered: {
      statusFile.reload()
      voiceFile.reload()
      omiModeFile.reload()
      continuousFile.reload()
    }
  }

  PanelWindow {
    id: window
    visible: root.shouldShow
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omi-indicator"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region { item: card }

    Rectangle {
      id: card
      x: window.width - width - 24
      y: 80
      width: 320
      height: 58
      radius: 14
      color: Color.popups.background
      border.color: root.micActive ? Color.urgent : Color.popups.border
      border.width: 2

      MouseArea {
        anchors.fill: parent
        drag.target: card
        drag.minimumX: 0
        drag.maximumX: window.width - card.width
        drag.minimumY: 0
        drag.maximumY: window.height - card.height
        cursorShape: Qt.SizeAllCursor
      }

      Rectangle {
        x: 16
        anchors.verticalCenter: parent.verticalCenter
        width: 10
        height: 10
        radius: 5
        color: root.micActive ? Color.urgent : Color.accent
      }

      Column {
        x: 36
        anchors.verticalCenter: parent.verticalCenter
        width: 210
        spacing: 1

        Text {
          text: root.displayedState
          color: Color.popups.text
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 14
        }

        Text {
          width: parent.width
          text: root.micActive ? (root.omiRecording || root.continuous ? "Sending speech to Omi" : "Typing into the focused app") : root.assistantDetail
          elide: Text.ElideRight
          color: Color.popups.text
          opacity: 0.65
          font.family: Style.font.family
          font.pixelSize: 11
        }
      }

      Rectangle {
        id: stopButton
        visible: root.voiceState === "recording" || root.continuous
        width: 56
        height: 30
        radius: 8
        color: Color.urgent
        anchors.right: parent.right
        anchors.rightMargin: 12
        anchors.verticalCenter: parent.verticalCenter

        Text {
          anchors.centerIn: parent
          text: root.continuous ? "Stop" : "Finish"
          color: Color.background
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 12
        }

        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: {
            if (root.continuous)
              Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi-voice", "continuous", "off"])
            else if (root.omiRecording)
              Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi-voice", "toggle"])
            else
              Quickshell.execDetached(["voxtype", "record", "stop"])
          }
        }
      }
    }
  }
}
