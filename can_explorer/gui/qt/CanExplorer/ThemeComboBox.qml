import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// A ComboBox of theme names (a list of strings), each shown with swatches of its colours.
ComboBox {
    id: box

    // Select a theme by name (the first entry when it is not in the list)
    function select(name) {
        currentIndex = Math.max(0, model.indexOf(name))
    }

    implicitWidth: 260

    delegate: ItemDelegate {
        id: themeItem
        required property string modelData
        required property int index
        width: ListView.view.width
        text: modelData
        font.weight: box.currentIndex === index ? Font.DemiBold : Font.Normal
        highlighted: box.highlightedIndex === index
        hoverEnabled: box.hoverEnabled
        contentItem: RowLayout {
            spacing: 8
            Label {
                text: themeItem.text
                font: themeItem.font
                color: themeItem.highlighted ? Theme.accent : Theme.text
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            ThemeSwatches { themeName: themeItem.modelData }
        }
    }
}
