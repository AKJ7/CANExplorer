import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// A GPS track on a Web Mercator map with the position at the replay time. Map tiles are only fetched when
// `tilesEnabled` (a preference): without them the track is drawn on a plain background with a scale bar.
Item {
    id: map
    required property var media
    property bool tilesEnabled: false
    property string tileUrl: ""
    property bool follow: true

    // View: centre in world coordinates (0..1) and a fractional zoom
    property real zoom: 2
    property real centerX: 0.5
    property real centerY: 0.5
    readonly property real worldSize: 256 * Math.pow(2, zoom)
    readonly property var current: media.current
    property bool fitPending: false  // A fit asked for while the map had no size yet (hidden until a track exists)

    clip: true

    function viewX(x) { return (x - centerX) * worldSize + width / 2 }
    function viewY(y) { return (y - centerY) * worldSize + height / 2 }

    function fit() {
        if (!media.has_track)
            return
        if (width <= 0 || height <= 0) {
            fitPending = true  // Done once the map is laid out
            return
        }
        fitPending = false
        zoom = media.fit_zoom(width, height)
        const middle = media.track_center()
        centerX = middle[0]
        centerY = middle[1]
    }

    function zoomAt(factor, px, py) {
        const newZoom = Math.max(0, Math.min(19, zoom + factor))
        // Keep the world point under the cursor in place
        const wx = centerX + (px - width / 2) / worldSize
        const wy = centerY + (py - height / 2) / worldSize
        const newSize = 256 * Math.pow(2, newZoom)
        centerX = wx - (px - width / 2) / newSize
        centerY = wy - (py - height / 2) / newSize
        zoom = newZoom
    }

    function followCurrent() {
        if (follow && current.valid) {
            centerX = current.x
            centerY = current.y
        }
    }

    onCurrentChanged: followCurrent()
    Connections {
        target: map.media
        function onTrack_changed() {
            map.follow = true
            map.fit()
            map.followCurrent()
        }
    }
    Component.onCompleted: Qt.callLater(fit)
    function fitIfPending() {
        if (fitPending && width > 0 && height > 0) {
            fit()
            followCurrent()
        }
    }
    onWidthChanged: { track.requestPaint(); fitIfPending() }
    onHeightChanged: fitIfPending()

    Rectangle {
        anchors.fill: parent
        color: Theme.cardAlt
    }

    // Tiles, laid out around an anchor; re-anchored when the view moved far or the zoom changed, so that panning
    // moves the layer instead of recreating every tile
    Item {
        id: tileLayer
        property real anchorX: 0.5
        property real anchorY: 0.5
        property real anchorZoom: -1
        property var tiles: []
        readonly property int margin: 256
        x: (anchorX - map.centerX) * map.worldSize
        y: (anchorY - map.centerY) * map.worldSize
        visible: map.tilesEnabled

        function update() {
            if (!map.tilesEnabled || map.width <= 0 || map.height <= 0) {
                tiles = []
                return
            }
            anchorX = map.centerX
            anchorY = map.centerY
            anchorZoom = map.zoom
            tiles = map.media.tiles(anchorX, anchorY, anchorZoom, map.width + 2 * margin, map.height + 2 * margin,
                                    map.tileUrl)
        }
        function check() {
            if (anchorZoom !== map.zoom || Math.abs(x) > margin / 2 || Math.abs(y) > margin / 2)
                Qt.callLater(update)
        }
        onXChanged: check()
        onYChanged: check()

        Repeater {
            model: tileLayer.tiles
            delegate: Image {
                required property var modelData
                x: modelData.left - tileLayer.margin
                y: modelData.top - tileLayer.margin
                width: modelData.size
                height: modelData.size
                source: modelData.url
                asynchronous: true
                cache: true
                smooth: true
                fillMode: Image.Stretch
            }
        }
    }
    Connections {
        target: map
        function onZoomChanged() { tileLayer.check() }
        function onWidthChanged() { Qt.callLater(tileLayer.update) }
        function onHeightChanged() { Qt.callLater(tileLayer.update) }
        function onTilesEnabledChanged() { Qt.callLater(tileLayer.update) }
        function onTileUrlChanged() { Qt.callLater(tileLayer.update) }
    }

    // The track
    Canvas {
        id: track
        anchors.fill: parent
        readonly property var coordinates: map.media.track_coordinates
        onCoordinatesChanged: requestPaint()
        Connections {
            target: map
            function onCenterXChanged() { track.requestPaint() }
            function onCenterYChanged() { track.requestPaint() }
            function onZoomChanged() { track.requestPaint() }
            function onHeightChanged() { track.requestPaint() }
        }
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const points = coordinates
            if (points.length < 4)
                return
            ctx.lineJoin = "round"
            ctx.lineCap = "round"
            ctx.beginPath()
            ctx.moveTo(map.viewX(points[0]), map.viewY(points[1]))
            for (let i = 2; i < points.length; i += 2)
                ctx.lineTo(map.viewX(points[i]), map.viewY(points[i + 1]))
            ctx.strokeStyle = String(Theme.card)
            ctx.lineWidth = 6
            ctx.stroke()
            ctx.strokeStyle = String(Theme.accent)
            ctx.lineWidth = 3.5
            ctx.stroke()
        }
    }

    // The position at the replay time, pointing in the direction of travel
    Item {
        visible: map.current.valid
        x: map.current.valid ? map.viewX(map.current.x) : 0
        y: map.current.valid ? map.viewY(map.current.y) : 0
        Rectangle {
            width: 18
            height: 18
            radius: 9
            x: -9
            y: -9
            color: Theme.danger
            border.color: "white"
            border.width: 2.5
        }
        Item {
            // Zero-sized, so it rotates around the marker's centre
            visible: map.current.valid && map.current.course >= 0
            rotation: map.current.course || 0
            Label {
                text: "▲"
                color: Theme.danger
                font.pixelSize: 14
                x: -width / 2
                y: -height / 2 - 17
            }
        }
    }

    DragHandler {
        id: drag
        target: null
        property real startX: 0
        property real startY: 0
        onActiveChanged: {
            if (active) {
                map.follow = false
                startX = map.centerX
                startY = map.centerY
            }
        }
        onTranslationChanged: {
            if (active) {
                map.centerX = startX - translation.x / map.worldSize
                map.centerY = startY - translation.y / map.worldSize
            }
        }
    }
    WheelHandler {
        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
        onWheel: event => map.zoomAt(event.angleDelta.y / 480, event.x, event.y)
    }

    // Controls
    Column {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 8
        spacing: 4
        Button { text: "+"; width: 36; onClicked: map.zoomAt(1, map.width / 2, map.height / 2) }
        Button { text: "−"; width: 36; onClicked: map.zoomAt(-1, map.width / 2, map.height / 2) }
        Button {
            text: "⤢"
            width: 36
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Show the whole track")
            onClicked: {
                map.follow = false
                map.fit()
            }
        }
        Button {
            text: "⌖"
            width: 36
            highlighted: map.follow
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Follow the position")
            onClicked: {
                map.follow = !map.follow
                map.followCurrent()
            }
        }
    }

    // Scale bar: metres per pixel at the centre's latitude
    Item {
        id: scale
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: 10
        width: barLength
        height: 24
        readonly property real latitude: Math.atan(Math.sinh(Math.PI * (1 - 2 * map.centerY)))
        readonly property real metresPerPixel: 40075016.686 * Math.cos(latitude) / map.worldSize
        readonly property real target: metresPerPixel * 100
        readonly property real niceMetres: {
            const power = Math.pow(10, Math.floor(Math.log(Math.max(target, 1e-6)) / Math.LN10))
            const steps = [1, 2, 5, 10]
            let best = power
            for (const step of steps)
                if (step * power <= target)
                    best = step * power
            return best
        }
        readonly property real barLength: niceMetres / metresPerPixel
        Rectangle { y: 18; width: scale.barLength; height: 3; color: Theme.text }
        Label {
            text: scale.niceMetres >= 1000 ? (scale.niceMetres / 1000) + " km" : scale.niceMetres + " m"
            color: Theme.text
            font.pixelSize: 11
        }
    }

    Label {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 4
        visible: map.tilesEnabled
        text: map.tileUrl.indexOf("openstreetmap.org") >= 0 ? qsTr("© OpenStreetMap contributors")
            : qsTr("Tiles: %1").arg(map.tileUrl.split("/")[2] || "")
        font.pixelSize: 10
        color: Theme.text
        padding: 3
        background: Rectangle { color: Theme.alpha(Theme.card, 0.8); radius: 3 }
    }
}
