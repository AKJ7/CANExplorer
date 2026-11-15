import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// The visual panel builder: drag widgets onto the panel, bind them to signals and variables, and save an ordinary
// QML panel (core/panel_builder.py writes it). The preview runs it with the simulation's live panel object.
Dialog {
    id: dialog
    required property var connection
    readonly property var builder: connection.panel_builder
    readonly property var widget: builder.selected_widget
    readonly property bool hasWidget: widget.kind !== undefined
    property string status: ""
    // The widget names, translated (the builder names them in English)
    readonly property var kindNames: ({
        label: qsTr("Text"), value: qsTr("Value"), gauge: qsTr("Gauge"), bar: qsTr("Bar"), lamp: qsTr("Lamp"),
        trend: qsTr("Trend"), switch: qsTr("Switch"), slider: qsTr("Slider"), button: qsTr("Button"),
    })

    title: qsTr("Panel builder") + (builder.file_name ? " — " + builder.file_name : "") + (builder.dirty ? " •" : "")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Close
    width: (Overlay.overlay ? Overlay.overlay.width : 1200) - 40
    height: (Overlay.overlay ? Overlay.overlay.height : 800) - 40

    function report(error, message) {
        status = error || message || ""
        statusLabel.color = error ? Theme.danger : Theme.muted
    }

    // The properties each kind of widget has, in the order the inspector shows them
    function fieldsFor(kind) {
        const common = {
            label: [{ key: "text", label: qsTr("Text"), type: "text" }, { key: "size", label: qsTr("Font size"), type: "number" },
                    { key: "bold", label: qsTr("Bold"), type: "bool" }],
            value: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "source", label: qsTr("Shows"), type: "source" },
                    { key: "size", label: qsTr("Font size"), type: "number" }],
            gauge: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "source", label: qsTr("Shows"), type: "source" },
                    { key: "min", label: qsTr("Minimum"), type: "number" }, { key: "max", label: qsTr("Maximum"), type: "number" },
                    { key: "warn", label: qsTr("Red from"), type: "number" }],
            bar: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "source", label: qsTr("Shows"), type: "source" },
                  { key: "min", label: qsTr("Minimum"), type: "number" }, { key: "max", label: qsTr("Maximum"), type: "number" }],
            lamp: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "source", label: qsTr("Shows"), type: "source" },
                   { key: "threshold", label: qsTr("On above"), type: "number" }, { key: "color", label: qsTr("Colour"), type: "color" }],
            trend: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "source", label: qsTr("Shows"), type: "source" },
                    { key: "min", label: qsTr("Minimum"), type: "number" }, { key: "max", label: qsTr("Maximum"), type: "number" },
                    { key: "seconds", label: qsTr("Seconds shown"), type: "number" }],
            switch: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "variable", label: qsTr("Sets variable"), type: "variable" }],
            slider: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "variable", label: qsTr("Sets variable"), type: "variable" },
                     { key: "min", label: qsTr("Minimum"), type: "number" }, { key: "max", label: qsTr("Maximum"), type: "number" },
                     { key: "step", label: qsTr("Step"), type: "number" }],
            button: [{ key: "label", label: qsTr("Label"), type: "text" }, { key: "action", label: qsTr("When clicked"), type: "action" }],
        }
        const fields = common[kind] || []
        if (kind === "button") {
            const action = widget.props.action
            if (action === "variable")
                return fields.concat([{ key: "variable", label: qsTr("Variable"), type: "variable" }, { key: "value", label: qsTr("Value"), type: "text" }])
            if (action === "message")
                return fields.concat([{ key: "message", label: qsTr("Message"), type: "text" }, { key: "signals", label: qsTr("Signals"), type: "text", hint: "Pressure=50, Mode=2" }])
            if (action === "raw")
                return fields.concat([{ key: "id", label: qsTr("Identifier"), type: "text", hint: "7DF" }, { key: "data", label: qsTr("Data"), type: "text", hint: "02 01 0D" }])
            return fields.concat([{ key: "text", label: qsTr("Log text"), type: "text" }])
        }
        return fields
    }

    function variables() {
        return dialog.builder.sources.filter(s => s.startsWith("variable:")).map(s => s.slice(9))
    }

    onAboutToShow: report("", "")

    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // The panel and its file
        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            Label { text: qsTr("Title") }
            TextField {
                id: titleField
                text: dialog.builder.title
                Layout.preferredWidth: 200
                onEditingFinished: dialog.builder.set_panel(text, widthBox.value, heightBox.value)
            }
            Label { text: qsTr("Size") }
            SpinBox {
                id: widthBox
                from: 160; to: 4000; stepSize: 40; editable: true
                value: dialog.builder.panel_width
                onValueModified: dialog.builder.set_panel(titleField.text, value, heightBox.value)
            }
            Label { text: "×" }
            SpinBox {
                id: heightBox
                from: 120; to: 4000; stepSize: 40; editable: true
                value: dialog.builder.panel_height
                onValueModified: dialog.builder.set_panel(titleField.text, widthBox.value, value)
            }
            Item { Layout.fillWidth: true }
            Button { text: qsTr("New"); onClicked: { dialog.builder.new_panel(); dialog.report("", "") } }
            Button { text: qsTr("Open…"); onClicked: openChooser.open() }
            Button {
                text: qsTr("Save")
                enabled: dialog.builder.problems.length === 0
                onClicked: dialog.builder.file_name ? dialog.report(dialog.builder.save(""), qsTr("Saved")) : saveChooser.open()
            }
            Button {
                text: qsTr("Save As…")
                enabled: dialog.builder.problems.length === 0
                onClicked: saveChooser.open()
            }
            Button {
                text: qsTr("Use in Simulation")
                highlighted: true
                enabled: dialog.builder.problems.length === 0
                onClicked: dialog.builder.file_name
                    ? dialog.report(dialog.builder.use_in_simulation(), qsTr("Added to the Simulation view's panels"))
                    : saveChooser.open()
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Save the panel and open it in the Simulation view, where it acts on the running simulation")
            }
        }

        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            Layout.fillHeight: true

            // Palette: drag a widget onto the panel, or click it to add it at the top left
            ColumnLayout {
                Layout.preferredWidth: 120
                Layout.maximumWidth: 120
                Layout.fillHeight: true
                spacing: 6
                Label { text: qsTr("Widgets"); font.bold: true }
                Repeater {
                    model: dialog.builder.kinds
                    delegate: Rectangle {
                        id: tile
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: 34
                        radius: Theme.smallRadius
                        color: tileArea.containsMouse ? Theme.cardAlt : Theme.card
                        border.color: Theme.border
                        Label { anchors.centerIn: parent; text: dialog.kindNames[tile.modelData.kind] }
                        MouseArea {
                            id: tileArea
                            anchors.fill: parent
                            hoverEnabled: true
                            // Dragged onto the panel: added where it is dropped; clicked: added at the top left
                            onPositionChanged: mouse => {
                                if (!pressed)
                                    return
                                const p = mapToItem(dialog.contentItem, mouse.x, mouse.y)
                                ghost.text = dialog.kindNames[tile.modelData.kind]
                                ghost.x = p.x - 10
                                ghost.y = p.y - 10
                                ghost.visible = true
                            }
                            onReleased: mouse => {
                                ghost.visible = false
                                const p = mapToItem(canvas, mouse.x, mouse.y)
                                if (canvas.visible && p.x >= 0 && p.y >= 0 && p.x < canvas.width && p.y < canvas.height)
                                    dialog.builder.add(tile.modelData.kind, p.x, p.y)
                            }
                            onClicked: dialog.builder.add(tile.modelData.kind, dialog.builder.grid * 2, dialog.builder.grid * 2)
                        }
                    }
                }
                Item { Layout.fillHeight: true }
            }

            // Design and preview
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0
                TabBar {
                    id: viewTabs
                    Layout.fillWidth: true
                    TabButton { text: qsTr("Design"); width: implicitWidth }
                    TabButton { text: qsTr("Preview"); width: implicitWidth }
                }
                StackLayout {
                    currentIndex: viewTabs.currentIndex
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    Flickable {
                        id: designArea
                        clip: true
                        contentWidth: canvas.width + 32
                        contentHeight: canvas.height + 32
                        ScrollBar.vertical: ScrollBar {}
                        ScrollBar.horizontal: ScrollBar {}

                        Rectangle {
                            id: canvas
                            x: 16; y: 16
                            width: dialog.builder.panel_width
                            height: dialog.builder.panel_height
                            color: Theme.card
                            border.color: Theme.border

                            // The grid the widgets snap to
                            Canvas {
                                anchors.fill: parent
                                property color dots: Theme.border
                                onDotsChanged: requestPaint()
                                onWidthChanged: requestPaint()
                                onHeightChanged: requestPaint()
                                onPaint: {
                                    const ctx = getContext("2d"), step = dialog.builder.grid * 2
                                    ctx.reset()
                                    ctx.fillStyle = dots
                                    for (let x = step; x < width; x += step)
                                        for (let y = step; y < height; y += step)
                                            ctx.fillRect(x, y, 1, 1)
                                }
                            }
                            MouseArea {
                                anchors.fill: parent
                                onClicked: mouse => dialog.builder.select_at(mouse.x, mouse.y)
                            }

                            Repeater {
                                model: dialog.builder.widgets
                                delegate: Rectangle {
                                    id: box
                                    required property var modelData
                                    readonly property bool selected: modelData.index === dialog.builder.selected
                                    x: modelData.x; y: modelData.y
                                    width: modelData.width; height: modelData.height
                                    radius: 4
                                    color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, selected ? 0.22 : 0.10)
                                    border.color: selected ? Theme.accent : Theme.border
                                    border.width: selected ? 2 : 1
                                    Column {
                                        anchors.fill: parent
                                        anchors.margins: 6
                                        spacing: 2
                                        clip: true
                                        Label {
                                            width: parent.width
                                            elide: Text.ElideRight
                                            text: "<b>" + dialog.kindNames[box.modelData.kind] + "</b>"
                                                + ((box.modelData.props.label || box.modelData.props.text)
                                                   ? " · " + (box.modelData.props.label || box.modelData.props.text) : "")
                                            textFormat: Text.StyledText
                                        }
                                        Label {
                                            visible: box.height >= 44
                                            width: parent.width
                                            elide: Text.ElideRight
                                            font.pixelSize: 11
                                            color: Theme.muted
                                            text: box.modelData.props.source || box.modelData.props.variable || ""
                                        }
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        drag.target: box
                                        drag.minimumX: 0; drag.minimumY: 0
                                        drag.maximumX: canvas.width - box.width; drag.maximumY: canvas.height - box.height
                                        onPressed: dialog.builder.select(box.modelData.index)
                                        onReleased: dialog.builder.move(box.modelData.index, box.x, box.y)
                                    }
                                    // Resize handle
                                    Rectangle {
                                        width: 12; height: 12
                                        anchors.right: parent.right; anchors.bottom: parent.bottom
                                        visible: box.selected
                                        color: Theme.accent
                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.SizeFDiagCursor
                                            property point start
                                            property size startSize
                                            onPressed: mouse => { start = mapToItem(canvas, mouse.x, mouse.y); startSize = Qt.size(box.width, box.height) }
                                            onPositionChanged: mouse => {
                                                const now = mapToItem(canvas, mouse.x, mouse.y)
                                                box.width = Math.max(16, startSize.width + now.x - start.x)
                                                box.height = Math.max(16, startSize.height + now.y - start.y)
                                            }
                                            onReleased: dialog.builder.resize(box.modelData.index, box.width, box.height)
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // The panel as it will run, on the simulation's live values
                    Item {
                        id: previewHost
                        clip: true
                        property var item: null
                        readonly property string source: viewTabs.currentIndex === 1 ? dialog.builder.qml : ""
                        onSourceChanged: rebuild()
                        function rebuild() {
                            if (item) { item.destroy(); item = null }
                            previewError.text = ""
                            if (!source)
                                return
                            // Loaded as the Simulation view loads panels: with the live panel object from the start
                            const component = Qt.createComponent(dialog.builder.preview_url())
                            if (component.status === Component.Ready)
                                item = component.createObject(previewHost, {
                                    panel: dialog.connection.simulation.panel, x: 16, y: 16,
                                    width: dialog.builder.panel_width, height: dialog.builder.panel_height,
                                })
                            if (!item)
                                previewError.text = component.errorString()
                        }
                        Label {
                            id: previewError
                            anchors.centerIn: parent
                            visible: text.length > 0 || !dialog.builder.qml
                            color: text.length > 0 ? Theme.danger : Theme.muted
                            text: ""
                            Binding on text {
                                when: !dialog.builder.qml
                                value: qsTr("Fix the problems listed on the right to preview the panel")
                            }
                        }
                    }
                }
            }

            // Inspector
            ScrollView {
                Layout.preferredWidth: 300
                Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true

                ColumnLayout {
                    width: parent.width
                    spacing: 6

                    Label {
                        text: dialog.hasWidget ? dialog.kindNames[dialog.widget.kind] : qsTr("Select a widget, or drag one onto the panel")
                        font.bold: dialog.hasWidget
                        color: dialog.hasWidget ? Theme.text : Theme.muted
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    GridLayout {
                        visible: dialog.hasWidget
                        columns: 2
                        columnSpacing: 6
                        rowSpacing: 6
                        Layout.fillWidth: true
                        Label { text: qsTr("X, Y") }
                        RowLayout {
                            TextField {
                                text: String(dialog.widget.x || 0)
                                validator: IntValidator { bottom: 0; top: 4000 }
                                Layout.fillWidth: true
                                onEditingFinished: dialog.builder.move(dialog.builder.selected, Number(text), dialog.widget.y)
                            }
                            TextField {
                                text: String(dialog.widget.y || 0)
                                validator: IntValidator { bottom: 0; top: 4000 }
                                Layout.fillWidth: true
                                onEditingFinished: dialog.builder.move(dialog.builder.selected, dialog.widget.x, Number(text))
                            }
                        }
                        Label { text: qsTr("Size") }
                        RowLayout {
                            TextField {
                                text: String(dialog.widget.width || 16)
                                validator: IntValidator { bottom: 16; top: 4000 }
                                Layout.fillWidth: true
                                onEditingFinished: dialog.builder.resize(dialog.builder.selected, Number(text), dialog.widget.height)
                            }
                            TextField {
                                text: String(dialog.widget.height || 16)
                                validator: IntValidator { bottom: 16; top: 4000 }
                                Layout.fillWidth: true
                                onEditingFinished: dialog.builder.resize(dialog.builder.selected, dialog.widget.width, Number(text))
                            }
                        }
                    }

                    Repeater {
                        model: dialog.hasWidget ? dialog.fieldsFor(dialog.widget.kind) : []
                        delegate: ColumnLayout {
                            id: field
                            required property var modelData
                            readonly property var current: dialog.widget.props[modelData.key]
                            function commit(value) {
                                const change = {}
                                change[modelData.key] = value
                                dialog.builder.update(dialog.builder.selected, change)
                            }
                            spacing: 2
                            Layout.fillWidth: true
                            Label { text: field.modelData.label; color: Theme.muted; font.pixelSize: 12 }
                            TextField {
                                visible: field.modelData.type === "text" || field.modelData.type === "number"
                                text: field.current === undefined ? "" : String(field.current)
                                placeholderText: field.modelData.hint || ""
                                Layout.fillWidth: true
                                onEditingFinished: field.commit(field.modelData.type === "number" && text.trim() !== "" && !isNaN(Number(text)) ? Number(text) : text)
                            }
                            CheckBox {
                                visible: field.modelData.type === "bool"
                                checked: field.current === true
                                onToggled: field.commit(checked)
                            }
                            RowLayout {
                                visible: field.modelData.type === "source" || field.modelData.type === "variable"
                                Layout.fillWidth: true
                                TextField {
                                    text: field.current || ""
                                    placeholderText: field.modelData.type === "source" ? "signal:Message.Signal" : qsTr("variable name")
                                    Layout.fillWidth: true
                                    onEditingFinished: field.commit(text.trim())
                                    ToolTip.visible: hovered && field.modelData.type === "source"
                                    ToolTip.text: qsTr("A signal of the connection's database (signal:Message.Signal) or a shared variable (variable:name)")
                                }
                                Button {
                                    text: "▾"
                                    leftPadding: 10
                                    rightPadding: 10
                                    enabled: choices.model.length > 0
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Choose from the database's signals and the variables")
                                    onClicked: choices.popup()
                                    Menu {
                                        id: choices
                                        property var model: field.modelData.type === "source" ? dialog.builder.sources : dialog.variables()
                                        Instantiator {
                                            model: choices.model
                                            delegate: MenuItem {
                                                required property string modelData
                                                text: modelData
                                                onTriggered: field.commit(modelData)
                                            }
                                            onObjectAdded: (index, object) => choices.insertItem(index, object)
                                            onObjectRemoved: (index, object) => choices.removeItem(object)
                                        }
                                    }
                                }
                            }
                            ComboBox {
                                visible: field.modelData.type === "color" || field.modelData.type === "action"
                                model: field.modelData.type === "color" ? dialog.builder.lamp_colors : dialog.builder.actions
                                currentIndex: Math.max(0, model.indexOf(field.current))
                                Layout.fillWidth: true
                                onActivated: field.commit(currentText)
                            }
                        }
                    }

                    Flow {
                        visible: dialog.hasWidget
                        spacing: 6
                        Layout.fillWidth: true
                        Button { text: qsTr("Duplicate"); onClicked: dialog.builder.duplicate(dialog.builder.selected) }
                        Button { text: "↑"; implicitWidth: 40; onClicked: dialog.builder.raise_widget(dialog.builder.selected, 1); ToolTip.visible: hovered; ToolTip.text: qsTr("Bring forward") }
                        Button { text: "↓"; implicitWidth: 40; onClicked: dialog.builder.raise_widget(dialog.builder.selected, -1); ToolTip.visible: hovered; ToolTip.text: qsTr("Send backward") }
                        Button { text: qsTr("Delete"); onClicked: dialog.builder.remove(dialog.builder.selected) }
                    }

                    Label {
                        visible: dialog.builder.problems.length > 0
                        text: qsTr("To fix before saving:")
                        font.bold: true
                        Layout.topMargin: 8
                    }
                    Repeater {
                        model: dialog.builder.problems
                        delegate: Label {
                            required property string modelData
                            text: modelData
                            color: Theme.warning
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }

        Label {
            id: statusLabel
            text: dialog.status
            visible: text.length > 0
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }

    // What the palette drags
    Label {
        id: ghost
        parent: dialog.contentItem
        visible: false
        z: 100
        padding: 6
        background: Rectangle { color: Theme.accent; opacity: 0.8; radius: Theme.smallRadius }
        color: Theme.onAccent
    }

    FileChooser {
        id: openChooser
        title: qsTr("Open a panel")
        nameFilters: [qsTr("QML panels (*.qml)")]
        onAccepted: dialog.report(dialog.builder.open(selectedFile), qsTr("Opened"))
    }
    FileChooser {
        id: saveChooser
        title: qsTr("Save the panel")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "qml"
        nameFilters: [qsTr("QML panels (*.qml)")]
        onAccepted: dialog.report(dialog.builder.save(selectedFile), qsTr("Saved"))
    }
}
