import QtQuick
import QtQuick.Effects
import QtQuick.Templates as T
import CanExplorer

// Frosted glass background of dialogs, popups and menus: the window content behind the panel, blurred, under a
// translucent tint with a light edge, a sheen and a soft shadow. Without an ApplicationWindow to see through, it
// is a nearly opaque tinted panel.
Item {
    id: root
    property real radius: Theme.radius
    property color tint: Theme.card
    property real blur: 1.0
    property bool shadow: true

    // The window content; dialogs and popups live in the overlay above it, so they are not part of the snapshot
    readonly property Item backdrop: T.ApplicationWindow.contentItem
    // Blurring needs a GPU scene graph (OpenGL, Vulkan, Metal, Direct3D); the API is unknown until shown
    readonly property bool frosted: backdrop !== null && GraphicsInfo.api !== undefined
        && GraphicsInfo.api !== GraphicsInfo.Unknown && GraphicsInfo.api !== GraphicsInfo.Software
    readonly property real tintOpacity: frosted ? (Theme.dark ? 0.5 : 0.55) : 1.0

    RectangularShadow {
        visible: root.shadow
        anchors.fill: parent
        radius: root.radius
        blur: 36
        spread: 0
        offset.y: 10
        color: Qt.rgba(0, 0, 0, Theme.dark ? 0.45 : 0.18)
    }

    ShaderEffectSource {
        id: snapshot
        anchors.fill: parent
        visible: false
        live: true
        sourceItem: root.frosted ? root.backdrop : null
        // The part of the window behind the panel; follows the popup when it moves, resizes or animates
        sourceRect: {
            const popup = root.parent
            // Referenced so that the binding is evaluated again when they change
            const geometry = [root.width, root.height, popup ? popup.x : 0, popup ? popup.y : 0, popup ? popup.scale : 1]
            if (!root.frosted || geometry.length === 0)
                return Qt.rect(0, 0, 0, 0)
            return root.backdrop.mapFromItem(root, 0, 0, root.width, root.height)
        }
    }

    Item {
        id: mask
        anchors.fill: parent
        visible: false
        layer.enabled: true
        Rectangle {
            anchors.fill: parent
            radius: root.radius
        }
    }

    // The window paints its background colour itself, outside the snapshot: an opaque base keeps the content
    // from showing through the blur unblurred
    Rectangle {
        visible: root.frosted
        anchors.fill: parent
        radius: root.radius
        color: Theme.background
    }

    MultiEffect {
        visible: root.frosted
        anchors.fill: parent
        source: snapshot
        autoPaddingEnabled: false
        blurEnabled: true
        blur: root.blur
        blurMax: 64
        saturation: 0.15
        maskEnabled: true
        maskSource: mask
    }

    // Tint and edge
    Rectangle {
        anchors.fill: parent
        radius: root.radius
        color: Theme.alpha(root.tint, root.tintOpacity)
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, Theme.dark ? 0.14 : 0.7)
    }

    // Sheen: light falling on the upper part of the glass
    Rectangle {
        anchors.fill: parent
        anchors.margins: 1
        radius: root.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(1, 1, 1, Theme.dark ? 0.09 : 0.45) }
            GradientStop { position: 0.35; color: Qt.rgba(1, 1, 1, 0) }
        }
    }
}
