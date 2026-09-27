import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

// Floating card while Omni listens, works, speaks, or needs approval. Draggable; never takes focus.
Item {
  id: root
  property string assistantState: "idle"
  property string assistantDetail: ""
  property bool continuous: false
  readonly property bool shouldShow: assistantState !== "idle"
  readonly property var labels: ({ listening: "Listening…", thinking: "Thinking…", working: "Working…",
                                   speaking: "Speaking", awaiting_approval: "Omni needs your OK" })
  readonly property string omni: Quickshell.env("HOME") + "/.local/bin/omni"

  function readStatus(raw) {
    try {
      var value = JSON.parse(raw)
      assistantState = String(value.state || "idle")
      assistantDetail = String(value.detail || "")
      continuous = !!value.continuous
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

  Timer { interval: 2000; running: true; repeat: true; onTriggered: statusFile.reload() }

  PanelWindow {
    id: window
    visible: root.shouldShow
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omni-indicator"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region { item: card }

    Rectangle {
      id: card
      x: window.width - width - 24
      y: 80
      width: 340
      height: 60
      radius: 14
      color: Color.popups.background
      border.color: root.assistantState === "listening" ? Color.urgent
        : root.assistantState === "awaiting_approval" ? Color.warning || Color.urgent : Color.popups.border
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
        color: root.assistantState === "listening" ? Color.urgent : Color.accent
      }

      Column {
        x: 36
        anchors.verticalCenter: parent.verticalCenter
        width: 220
        spacing: 1
        Text {
          text: root.labels[root.assistantState] || root.assistantState
          color: Color.popups.text
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 14
        }
        Text {
          width: parent.width
          text: root.assistantDetail
          elide: Text.ElideRight
          color: Color.popups.text
          opacity: 0.65
          font.family: Style.font.family
          font.pixelSize: 11
        }
      }

      Rectangle {
        width: 60
        height: 30
        radius: 8
        color: Color.urgent
        anchors.right: parent.right
        anchors.rightMargin: 12
        anchors.verticalCenter: parent.verticalCenter
        Text {
          anchors.centerIn: parent
          text: root.assistantState === "listening" ? "Send" : root.assistantState === "awaiting_approval" ? "Open" : "Stop"
          color: Color.background
          font.family: Style.font.family
          font.bold: true
          font.pixelSize: 12
        }
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: Quickshell.execDetached([root.omni].concat(
            root.assistantState === "listening" ? ["listen"] : root.assistantState === "awaiting_approval" ? ["popover"] : ["stop"]))
        }
      }
    }
  }
}
