import math
from typing import Optional

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtQml import qmlRegisterType
from PyQt6.QtQuick import QQuickPaintedItem

MARGIN_LEFT = 70
MARGIN_RIGHT = 10
MARGIN_TOP = 20
MARGIN_BOTTOM = 18


def _format(value: float) -> str:
    return f'{value:.6g}'


class SignalPlot(QQuickPaintedItem):
    """Line plot of one tracked signal over the last `window` seconds.

    The series is read from the connection on each repaint and reduced to at most two points
    (min and max) per pixel column, so drawing cost does not grow with the number of samples.
    """

    connection_changed = pyqtSignal()
    signal_key_changed = pyqtSignal()
    window_changed = pyqtSignal()
    colors_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = None
        self._signal_key = ''
        self._window = 10.0
        self._line_color = QColor('#4e9af1')
        self._text_color = QColor('#dddddd')
        self._grid_color = QColor('#555555')
        self.setAntialiasing(True)

    def _set(self, attribute: str, value, signal) -> None:
        if getattr(self, attribute) != value:
            setattr(self, attribute, value)
            signal.emit()
            self.update()

    @pyqtProperty(QObject, notify=connection_changed)
    def connection(self):
        return self._connection

    @connection.setter
    def connection(self, connection):
        if self._connection is not None:
            self._connection.signals_updated.disconnect(self.update)
        self._set('_connection', connection, self.connection_changed)
        if connection is not None:
            connection.signals_updated.connect(self.update)

    @pyqtProperty(str, notify=signal_key_changed)
    def signal_key(self) -> str:
        return self._signal_key

    @signal_key.setter
    def signal_key(self, key: str):
        self._set('_signal_key', key, self.signal_key_changed)

    @pyqtProperty(float, notify=window_changed)
    def window(self) -> float:
        return self._window

    @window.setter
    def window(self, seconds: float):
        self._set('_window', max(0.1, seconds), self.window_changed)

    @pyqtProperty(QColor, notify=colors_changed)
    def line_color(self) -> QColor:
        return self._line_color

    @line_color.setter
    def line_color(self, color: QColor):
        self._set('_line_color', QColor(color), self.colors_changed)

    @pyqtProperty(QColor, notify=colors_changed)
    def text_color(self) -> QColor:
        return self._text_color

    @text_color.setter
    def text_color(self, color: QColor):
        self._set('_text_color', QColor(color), self.colors_changed)

    @pyqtProperty(QColor, notify=colors_changed)
    def grid_color(self) -> QColor:
        return self._grid_color

    @grid_color.setter
    def grid_color(self, color: QColor):
        self._set('_grid_color', QColor(color), self.colors_changed)

    def paint(self, painter: QPainter) -> None:
        width, height = self.width(), self.height()
        plot = QRectF(MARGIN_LEFT, MARGIN_TOP, width - MARGIN_LEFT - MARGIN_RIGHT, height - MARGIN_TOP - MARGIN_BOTTOM)
        if plot.width() < 10 or plot.height() < 10:
            return
        font = QFont(painter.font())
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QPen(self._grid_color, 1))
        painter.drawRect(plot)

        series = None
        if self._connection is not None and self._signal_key:
            series = self._connection.session.signals.series.get(self._signal_key)
        title = self._signal_key + (f' [{series.unit}]' if series is not None and series.unit else '')
        painter.setPen(self._text_color)
        painter.drawText(QRectF(MARGIN_LEFT, 0, plot.width(), MARGIN_TOP), Qt.AlignmentFlag.AlignVCenter, title)
        if series is None or series.last is None:
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, 'No data')
            return

        end = series.last[0]
        start = end - self._window
        times, values = series.window(start, end)
        finite = [value for value in values if math.isfinite(value)]
        if not finite:
            return
        low, high = min(finite), max(finite)
        if low == high:
            low, high = low - 1, high + 1
        span = high - low
        low, high = low - 0.05 * span, high + 0.05 * span

        # Grid and axis labels
        for fraction in (0.0, 0.5, 1.0):
            y = plot.bottom() - fraction * plot.height()
            painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(self._text_color)
            painter.drawText(
                QRectF(0, y - 8, MARGIN_LEFT - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _format(low + fraction * (high - low)),
            )
        painter.drawText(
            QRectF(plot.left(), plot.bottom(), plot.width(), MARGIN_BOTTOM),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f'last {self._window:g} s — current {_format(series.last[1])}',
        )

        # Reduce to min/max per pixel column
        x_scale = plot.width() / self._window
        y_scale = plot.height() / (high - low)
        path = QPainterPath()
        column, column_min, column_max, first_point = None, 0.0, 0.0, True

        def flush():
            nonlocal first_point
            x = plot.left() + column
            for value in (column_min, column_max) if column_min != column_max else (column_min,):
                point = QPointF(x, plot.bottom() - (value - low) * y_scale)
                if first_point:
                    path.moveTo(point)
                    first_point = False
                else:
                    path.lineTo(point)

        for timestamp, value in zip(times, values, strict=True):
            if not math.isfinite(value):
                continue
            pixel = max(0, int((timestamp - start) * x_scale))
            if pixel != column:
                if column is not None:
                    flush()
                column, column_min, column_max = pixel, value, value
            else:
                column_min, column_max = min(column_min, value), max(column_max, value)
        if column is not None:
            flush()
        painter.save()
        painter.setClipRect(plot)
        painter.setPen(QPen(self._line_color, 1.5))
        painter.drawPath(path)
        painter.restore()


_registered = False


def register_qml_types() -> None:
    global _registered
    if not _registered:
        qmlRegisterType(SignalPlot, 'CanExplorer.Plots', 1, 0, 'SignalPlot')
        _registered = True
