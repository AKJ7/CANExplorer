import QtQuick
import QtQuick.Controls.impl

// The drop-down and fold arrow: a drawn chevron pointing down at rotation 0, tinted like text. Combo boxes, menu
// buttons and tree fold markers all use it, so every arrow looks the same. Rotate it (animated) to point elsewhere,
// e.g. 180 while a list is open or -90 for a folded tree row.
ColorImage {
    width: 14
    height: 14
    sourceSize: Qt.size(width, height)
    source: Qt.resolvedUrl("images/chevron-down.svg")
    color: Theme.muted
    Behavior on rotation { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
}
