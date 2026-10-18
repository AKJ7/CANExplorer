import QtQuick 2.15
import CanExplorer 1.0

// Small line with a fading fill for stat cards.
Canvas {
    id: root
    property var values: []
    property color color: Theme.accent

    onValuesChanged: requestPaint()
    onColorChanged: requestPaint()
    onWidthChanged: requestPaint()

    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        if (!values || values.length < 2)
            return
        const maximum = Math.max(...values), minimum = Math.min(...values)
        const span = maximum - minimum || 1
        const step = width / (values.length - 1)
        const y = value => height - 2 - (value - minimum) / span * (height - 4)
        ctx.beginPath()
        ctx.moveTo(0, y(values[0]))
        for (let i = 1; i < values.length; ++i)
            ctx.lineTo(i * step, y(values[i]))
        ctx.lineWidth = 2
        ctx.strokeStyle = color
        ctx.stroke()
        ctx.lineTo(width, height)
        ctx.lineTo(0, height)
        ctx.closePath()
        const gradient = ctx.createLinearGradient(0, 0, 0, height)
        gradient.addColorStop(0, Theme.alpha(color, 0.3))
        gradient.addColorStop(1, Theme.alpha(color, 0))
        ctx.fillStyle = gradient
        ctx.fill()
    }
}
