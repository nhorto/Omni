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
  readonly property string displayedState: voiceState === "recording" || voiceState === "transcribing"
    ? voiceState : assistantState
  readonly property bool shouldShow: displayedState === "recording" || displayedState === "transcribing"
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
      border.color: root.displayedState === "recording" ? Color.urgent : Color.popups.border
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
        color: root.displayedState === "recording" ? Color.urgent : Color.accent
      }

      Column {
        x: 36
        anchors.verticalCenter: parent.verticalCenter
        width: 210
        spacing: 1

        Text {
          text: "Omi · " + root.displayedState
          color: Color.popups.text
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 14
        }

        Text {
          width: parent.width
          text: root.displayedState === "recording" ? "Microphone active" : root.assistantDetail
          elide: Text.ElideRight
          color: Color.popups.text
          opacity: 0.65
          font.family: Style.font.family
          font.pixelSize: 11
        }
      }

      Rectangle {
        id: stopButton
        visible: root.displayedState === "recording"
        width: 56
        height: 30
        radius: 8
        color: Color.urgent
        anchors.right: parent.right
        anchors.rightMargin: 12
        anchors.verticalCenter: parent.verticalCenter

        Text {
          anchors.centerIn: parent
          text: "Stop"
          color: Color.background
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 12
        }

        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omi-voice", "stop"])
        }
      }
    }
  }
}
