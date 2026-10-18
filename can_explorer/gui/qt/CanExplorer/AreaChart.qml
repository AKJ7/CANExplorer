import QtQuick 2.15
import CanExplorer 1.0

// Smoothed line chart with a gradient fill, horizontal grid lines and value labels.
Canvas {
    id: root
    property var values: []
    property color color: Theme.accent
    property string unit: ""
    property string emptyText: qsTr("Waiting for data…")
    // Points of a full chart (the newest on the right) and the label of the oldest one
    property int capacity: 60
    property string startLabel: qsTr("-60 s")

    readonly property int labelWidth: 44

    onValuesChanged: requestPaint()
    onColorChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onCapacityChanged: requestPaint()
    onStartLabelChanged: requestPaint()
    Connections {
        target: Theme
        function onNameChanged() { root.requestPaint() }
    }

    function niceMaximum(value) {
        if (value <= 0)
            return 1
        const magnitude = Math.pow(10, Math.floor(Math.log10(value)))
        for (const factor of [1, 2, 2.5, 5, 10])
            if (factor * magnitude >= value)
                return factor * magnitude
        return 10 * magnitude
    }

    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        const left = labelWidth, top = 8, bottom = height - 20, right = width - 8
        const maximum = niceMaximum(values && values.length ? Math.max(...values) : 0)
        ctx.font = "11px sans-serif"
        ctx.textBaseline = "middle"
        for (let i = 0; i <= 4; ++i) {
            const y = bottom - (bottom - top) * i / 4
            ctx.strokeStyle = Theme.alpha(Theme.border, 0.7)
            ctx.lineWidth = 1
            ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke()
            ctx.fillStyle = Theme.muted
            ctx.textAlign = "right"
            const label = maximum * i / 4
            ctx.fillText(label >= 10 || label === 0 ? label.toFixed(0) : label.toFixed(1), left - 8, y)
        }
        ctx.textAlign = "left"
        ctx.fillStyle = Theme.muted
        ctx.fillText(startLabel, left, height - 8)
        ctx.textAlign = "right"
        ctx.fillText(qsTr("now"), right, height - 8)
        if (!values || values.length < 2) {
            ctx.textAlign = "center"
            ctx.fillText(emptyText, (left + right) / 2, (top + bottom) / 2)
            return
        }
        const step = (right - left) / Math.max(1, capacity - 1)  // The newest point on the right
        const offset = Math.max(0, capacity - values.length)
        const points = values.map((value, i) => [left + (offset + i) * step, bottom - value / maximum * (bottom - top)])
        ctx.beginPath()
        ctx.moveTo(points[0][0], points[0][1])
        for (let i = 1; i < points.length; ++i) {
            const [x0, y0] = points[i - 1], [x1, y1] = points[i]
            const mid = (x0 + x1) / 2
            ctx.bezierCurveTo(mid, y0, mid, y1, x1, y1)
        }
        ctx.lineWidth = 2.5
        ctx.strokeStyle = color
        ctx.stroke()
        ctx.lineTo(points[points.length - 1][0], bottom)
        ctx.lineTo(points[0][0], bottom)
        ctx.closePath()
        const gradient = ctx.createLinearGradient(0, top, 0, bottom)
        gradient.addColorStop(0, Theme.alpha(color, 0.35))
        gradient.addColorStop(1, Theme.alpha(color, 0.02))
        ctx.fillStyle = gradient
        ctx.fill()
        const [lastX, lastY] = points[points.length - 1]
        ctx.fillStyle = Theme.alpha(color, 0.25)
        ctx.beginPath(); ctx.arc(lastX, lastY, 8, 0, 2 * Math.PI); ctx.fill()
        ctx.fillStyle = color
        ctx.beginPath(); ctx.arc(lastX, lastY, 4, 0, 2 * Math.PI); ctx.fill()
    }
}
