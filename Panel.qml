pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "gigerit.omahosts"
  ipcTarget: "gigerit.omahosts"

  readonly property string helper: decodeURIComponent(Qt.resolvedUrl("hosts.py").toString().replace(/^file:\/\//, ""))
  readonly property string hostsIcon: "\u{f01d6}"
  property var entries: []
  property string revision: ""
  property string error: ""
  property string notice: ""
  property bool conflict: false
  property bool editing: false
  property int editId: -1
  property string editRevision: ""
  property bool draftEnabled: true
  property bool saving: false
  property var pendingRequest: null
  property var deleteEntry: null
  property int selectedIndex: -1
  property int selectedAction: 0
  property bool restorePopup: false
  readonly property bool busy: saving || checkProcess.running
  readonly property int customCount: entries.filter(function(e) { return !e.protected }).length
  readonly property int activeCount: entries.filter(function(e) { return e.enabled && !e.protected }).length
  readonly property var filteredEntries: entries.filter(function(e) {
    var query = search.text.trim().toLowerCase()
    return !query || (e.address + " " + e.hosts.join(" ") + " " + e.comment).toLowerCase().indexOf(query) !== -1
  })

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function decode(text) {
    try { return JSON.parse(text) }
    catch (e) { return { ok: false, error: "Could not read the hosts helper response." } }
  }

  function acceptSnapshot(data) {
    entries = data.entries || []
    revision = data.revision || ""
    if (editing && editRevision !== revision) {
      conflict = true
      error = "Hosts changed. Reload to discard this draft and view the latest entries."
    }
    selectedIndex = Math.min(selectedIndex, filteredEntries.length - 1)
  }

  function refresh() {
    if (!busy && !readProcess.running) readProcess.running = true
  }

  function beginEdit(entry) {
    if (busy || !revision || (entry && entry.protected)) return
    error = ""
    notice = ""
    conflict = false
    editId = entry ? entry.id : -1
    editRevision = revision
    addressInput.text = entry ? entry.address : ""
    hostsInput.text = entry ? entry.hosts.join(" ") : ""
    commentInput.text = entry ? entry.comment : ""
    draftEnabled = entry ? entry.enabled : true
    editing = true
    Qt.callLater(function() { addressInput.forceActiveFocus() })
  }

  function endEdit() {
    editing = false
    conflict = false
    error = ""
    editId = -1
    keyCatcher.forceActiveFocus()
  }

  function reloadEntries() {
    endEdit()
    notice = ""
    refresh()
  }

  function saveDraft() {
    if (!editing || busy || conflict) return
    request({
      operation: editId < 0 ? "add" : "edit", id: editId, revision: editRevision,
      fields: {
        address: addressInput.text.trim(),
        hosts: hostsInput.text.trim() ? hostsInput.text.trim().split(/\s+/) : [],
        comment: commentInput.text, enabled: draftEnabled
      }
    })
  }

  function toggleEntry(entry) {
    if (!entry || entry.protected) return
    request({ operation: "toggle", id: entry.id, revision: revision, enabled: !entry.enabled })
  }

  function confirmDelete(entry) {
    if (busy || !entry || entry.protected) return
    // Capture the revision with the row: an external refresh must never retarget deletion.
    deleteEntry = { id: entry.id, revision: revision, hosts: entry.hosts }
    confirmation.selectedIndex = 0
    keyCatcher.forceActiveFocus()
  }

  function request(data) {
    if (busy) return
    error = ""
    notice = ""
    conflict = false
    pendingRequest = data
    checkProcess.running = true
  }

  function finishSave(exitCode) {
    saving = false
    var data = decode(saveOutput.text)
    if (exitCode === 0 && data.ok) {
      editing = false
      conflict = false
      error = ""
      notice = data.warning || ""
      acceptSnapshot(data)
    } else if (exitCode === 126) {
      error = "Authentication cancelled. Nothing was changed."
    } else if (exitCode === 127) {
      error = "Authentication failed. Nothing was changed."
    } else {
      error = data.error || "Could not save the hosts file."
      conflict = data.code === "conflict"
    }
    pendingRequest = null
    refresh()
    if (restorePopup) root.open()
    restorePopup = false
  }

  function moveSelection(dx, dy) {
    if (filteredEntries.length === 0) return
    if (dy !== 0) {
      selectedIndex = Math.max(0, Math.min(filteredEntries.length - 1, selectedIndex + dy))
      list.positionViewAtIndex(selectedIndex, ListView.Contain)
    }
    if (dx !== 0) selectedAction = Math.max(0, Math.min(2, selectedAction + dx))
  }

  function activateSelection() {
    var entry = filteredEntries[selectedIndex]
    if (!entry || entry.protected || busy) return
    if (selectedAction === 0) toggleEntry(entry)
    else if (selectedAction === 1) beginEdit(entry)
    else confirmDelete(entry)
  }

  Component.onCompleted: refresh()
  onOpenedChanged: if (opened) refresh()
  onDeleteEntryChanged: {
    if (deleteEntry) Qt.callLater(function() { dialogKeys.forceActiveFocus() })
    else keyCatcher.forceActiveFocus()
  }

  FileView {
    path: "/etc/hosts"
    watchChanges: true
    onFileChanged: { reload(); refreshTimer.restart() }
  }

  Timer {
    id: refreshTimer
    interval: 100
    onTriggered: root.refresh()
  }

  Process {
    id: readProcess
    command: ["/usr/bin/python3", "-I", "-B", root.helper, "read"]
    stdout: StdioCollector { id: readOutput; waitForEnd: true }
    onExited: function(exitCode) {
      var data = root.decode(readOutput.text)
      if (exitCode === 0 && data.ok) root.acceptSnapshot(data)
      else root.error = data.error || "Could not read the hosts file."
    }
  }

  Process {
    id: checkProcess
    command: ["/usr/bin/python3", "-I", "-B", root.helper, "check"]
    stdinEnabled: true
    onStarted: write(JSON.stringify(root.pendingRequest) + "\n")
    stdout: StdioCollector { id: checkOutput; waitForEnd: true }
    onExited: function(exitCode) {
      var data = root.decode(checkOutput.text)
      if (exitCode === 0 && data.ok) {
        root.saving = true
        root.restorePopup = root.opened
        // Release the layer-shell keyboard grab before Polkit takes focus.
        root.close()
        authDelay.start()
      } else {
        root.error = data.error || "Could not validate this change."
        root.conflict = data.code === "conflict"
        root.pendingRequest = null
        root.refresh()
      }
    }
  }

  Timer {
    id: authDelay
    interval: 180
    onTriggered: saveProcess.running = true
  }

  Process {
    id: saveProcess
    command: ["/usr/bin/pkexec", "--disable-internal-agent", "/usr/bin/python3", "-I", "-B", root.helper, "apply"]
    stdinEnabled: true
    onStarted: write(JSON.stringify(root.pendingRequest) + "\n")
    stdout: StdioCollector { id: saveOutput; waitForEnd: true }
    onExited: function(exitCode) { root.finishSave(exitCode) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.hostsIcon
    active: root.activeCount > 0
    activeColor: Color.accent
    tooltipText: "omahosts · " + root.activeCount + " active"
    onPressed: root.toggle()
  }

  component Label: Text {
    textFormat: Text.PlainText
    color: Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    wrapMode: Text.Wrap
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: root.editing ? addressInput : keyCatcher
    contentWidth: fittedContentWidth(Style.space(480))
    contentHeight: fittedContentHeight(Math.max(Style.space(250), column.implicitHeight))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editing || search.activeFocus || root.deleteEntry !== null
      Keys.onEscapePressed: {
        if (root.deleteEntry) root.deleteEntry = null
        else if (root.editing && !root.busy) root.endEdit()
        else root.close()
      }
      onMoveRequested: function(dx, dy) { root.moveSelection(dx, dy) }
      onActivateRequested: root.activateSelection()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onDeleteRequested: root.confirmDelete(root.filteredEntries[root.selectedIndex])
      onTextKey: function(text) {
        if (text === "/") search.forceActiveFocus()
        else if (text === "a") root.beginEdit(null)
        else if (text === "e") root.beginEdit(root.filteredEntries[root.selectedIndex])
        else if (text === "r") root.refresh()
      }

      ColumnLayout {
        id: column
        width: parent.width
        spacing: Style.spacing.panelGap

        PanelHero {
          Layout.fillWidth: true
          title: root.editing ? (root.editId < 0 ? "New entry" : "Edit entry") : "omahosts"
          meta: root.busy ? "Saving…" : root.activeCount + " active · " + root.customCount + (root.customCount === 1 ? " custom entry" : " custom entries")
          iconComponent: Label { text: root.hostsIcon; font.pixelSize: Style.font.display }
          trailingControl: PanelActionButton {
            iconText: "\uf00d"
            tooltipText: "Close"
            focusable: true
            onClicked: root.close()
          }
        }

        PanelSeparator { Layout.fillWidth: true }

        RowLayout {
          visible: root.error !== "" || root.notice !== ""
          Layout.fillWidth: true
          spacing: Style.spacing.controlGap
          Label {
            Layout.fillWidth: true
            text: root.error || root.notice
            color: root.error ? Color.urgent : Color.foreground
            font.pixelSize: Style.font.bodySmall
          }
          Button {
            text: "Reload"
            visible: root.conflict
            enabled: !root.busy
            focusable: true
            onClicked: root.reloadEntries()
          }
        }

        RowLayout {
          visible: !root.editing
          Layout.fillWidth: true
          spacing: Style.spacing.controlGap
          TextField {
            id: search
            Layout.fillWidth: true
            placeholderText: "Search hosts…"
            onTextChanged: root.selectedIndex = -1
            Keys.onEscapePressed: {
              if (text) text = ""
              else keyCatcher.forceActiveFocus()
            }
            Keys.onDownPressed: { root.moveSelection(0, 1); keyCatcher.forceActiveFocus() }
            Keys.onReturnPressed: { root.moveSelection(0, 1); keyCatcher.forceActiveFocus() }
          }
          Button {
            text: "Add"
            iconText: "+"
            bordered: true
            focusable: true
            enabled: !root.busy && root.revision !== ""
            onClicked: root.beginEdit(null)
          }
        }

        ListView {
          id: list
          visible: !root.editing
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(Math.max(Style.space(100), contentHeight), Math.max(Style.space(100), panel.availableCardHeight - Style.space(230)))
          clip: true
          spacing: Style.spacing.labelGap
          model: root.filteredEntries
          boundsBehavior: Flickable.StopAtBounds
          Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

          delegate: CursorSurface {
            id: row
            required property var modelData
            required property int index
            width: ListView.view.width
            implicitHeight: Math.max(labels.implicitHeight + Style.space(16), actions.implicitHeight + Style.space(12))
            hasCursor: root.selectedIndex === index

            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.spacing.rowGap

              ColumnLayout {
                id: labels
                Layout.fillWidth: true
                spacing: Style.space(3)
                opacity: row.modelData.enabled ? 1 : 0.55
                Label {
                  Layout.fillWidth: true
                  text: row.modelData.hosts.join("  ")
                  font.bold: true
                }
                Label {
                  Layout.fillWidth: true
                  text: row.modelData.address + (row.modelData.protected ? " · System" : "")
                  font.pixelSize: Style.font.bodySmall
                  color: Qt.darker(Color.foreground, 1.4)
                }
                Label {
                  Layout.fillWidth: true
                  visible: row.modelData.comment !== ""
                  text: row.modelData.comment
                  font.pixelSize: Style.font.bodySmall
                  color: Qt.darker(Color.foreground, 1.4)
                }
                Label {
                  Layout.fillWidth: true
                  visible: row.modelData.duplicates.length > 0
                  text: "Duplicate: " + row.modelData.duplicates.join(", ")
                  font.pixelSize: Style.font.bodySmall
                  color: Color.urgent
                }
              }

              Row {
                id: actions
                visible: !row.modelData.protected
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.space(2)
                ToggleSwitch {
                  checked: row.modelData.enabled
                  busy: root.busy
                  hasCursor: row.hasCursor && root.selectedAction === 0
                  onToggled: root.toggleEntry(row.modelData)
                  onHovered: function(value) { if (value) { root.selectedIndex = row.index; root.selectedAction = 0 } }
                }
                PanelActionButton {
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "\uf040"
                  tooltipText: "Edit"
                  enabled: !root.busy
                  hasCursor: row.hasCursor && root.selectedAction === 1
                  onClicked: root.beginEdit(row.modelData)
                  onHovered: function(value) { if (value) { root.selectedIndex = row.index; root.selectedAction = 1 } }
                }
                PanelActionButton {
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "\uf014"
                  tooltipText: "Delete"
                  hoverColor: Color.urgent
                  enabled: !root.busy
                  hasCursor: row.hasCursor && root.selectedAction === 2
                  onClicked: root.confirmDelete(row.modelData)
                  onHovered: function(value) { if (value) { root.selectedIndex = row.index; root.selectedAction = 2 } }
                }
              }
            }
          }

          Label {
            anchors.centerIn: parent
            visible: list.count === 0
            text: readProcess.running ? "Loading…" : search.text ? "No matching entries" : "No entries yet"
          }
        }

        Controls.ScrollView {
          visible: root.editing
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(form.implicitHeight, Math.max(Style.space(100), panel.availableCardHeight - Style.space(190)))
          contentWidth: availableWidth
          clip: true

          ColumnLayout {
            id: form
            width: parent.width
            spacing: Style.spacing.labelGap
            enabled: !root.busy

            Label { text: "Address" }
            TextField {
              id: addressInput
              Layout.fillWidth: true
              placeholderText: "127.0.0.1"
              selectByMouse: true
              onAccepted: hostsInput.forceActiveFocus()
            }
            Label { text: "Hostnames"; Layout.topMargin: Style.spacing.labelGap }
            TextField {
              id: hostsInput
              Layout.fillWidth: true
              placeholderText: "app.test api.app.test"
              selectByMouse: true
              onAccepted: commentInput.forceActiveFocus()
            }
            Label { text: "Comment"; Layout.topMargin: Style.spacing.labelGap }
            TextField {
              id: commentInput
              Layout.fillWidth: true
              placeholderText: "Optional"
              selectByMouse: true
              onAccepted: root.saveDraft()
            }
            Toggle {
              Layout.fillWidth: true
              Layout.topMargin: Style.spacing.labelGap
              label: "Enabled"
              checked: root.draftEnabled
              activeFocusOnTab: true
              Keys.onSpacePressed: root.draftEnabled = !root.draftEnabled
              Keys.onReturnPressed: root.draftEnabled = !root.draftEnabled
              onClicked: root.draftEnabled = !root.draftEnabled
            }
            RowLayout {
              Layout.fillWidth: true
              Layout.topMargin: Style.spacing.labelGap
              Item { Layout.fillWidth: true }
              Button {
                text: "Cancel"
                focusable: true
                onClicked: root.endEdit()
              }
              Button {
                text: "Save"
                focusable: true
                bordered: true
                enabled: !root.conflict
                onClicked: root.saveDraft()
              }
            }
          }
        }
      }

      ConfirmDialog {
        id: confirmation
        anchors.fill: parent
        opened: root.deleteEntry !== null
        message: root.deleteEntry ? "Delete " + root.deleteEntry.hosts.join(", ") + "?" : ""
        confirmText: "Delete"
        onCanceled: root.deleteEntry = null
        onConfirmed: {
          var entry = root.deleteEntry
          root.deleteEntry = null
          root.request({ operation: "delete", id: entry.id, revision: entry.revision })
        }
      }

      Item {
        id: dialogKeys
        Keys.onPressed: function(event) {
          if (root.deleteEntry && confirmation.handleKey(event)) event.accepted = true
        }
      }
    }
  }
}
