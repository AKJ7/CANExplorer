import math
from typing import List, Optional

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtQml import qmlRegisterType
from PyQt6.QtQuick import QQuickPaintedItem

MARGIN_LEFT = 70
MARGIN_RIGHT = 12
MARGIN_TOP = 8
MARGIN_BOTTOM = 20
TIME_TICKS = 6


def _format(value: float) -> str:
    return f'{value:.6g}'


def _nice_step(span: float, ticks: int) -> float:
    """A round step (1, 2 or 5 times a power of ten) giving about `ticks` ticks over `span`."""
    raw = span / max(1, ticks)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for multiple in (1, 2, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


class SignalPlot(QQuickPaintedItem):
    """Line chart of one or several tracked signals over the time range of a PlotView.

    Signals share one value scale, or with `separate_scales` each is scaled to its own range (the axis then shows
    the first signal's). Series are reduced to at most two points (min and max) per pixel column, so drawing cost
    does not grow with the number of samples. Cursors of the view are drawn as vertical lines.
    """

    view_changed = pyqtSignal()
    keys_changed = pyqtSignal()
    style_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._view = None
        self._keys: List[str] = []
        self._separate_scales = False
        self._palette: List[QColor] = [QColor('#4e9af1')]
        self._text_color = QColor('#dddddd')
        self._grid_color = QColor('#555555')
        self._cursor_color = QColor('#f39c12')
        self.setAntialiasing(True)

    def _set(self, attribute: str, value, signal) -> None:
        if getattr(self, attribute) != value:
            setattr(self, attribute, value)
            signal.emit()
            self.update()

    # Properties

    @pyqtProperty(QObject, notify=view_changed)
    def view(self):
        return self._view

    @view.setter
    def view(self, view):
        if self._view is not None:
            self._view.changed.disconnect(self.update)
            self._view.connection.signals_updated.disconnect(self.update)
        self._set('_view', view, self.view_changed)
        if view is not None:
            view.changed.connect(self.update)
            view.connection.signals_updated.connect(self.update)

    @pyqtProperty('QStringList', notify=keys_changed)
    def keys(self) -> List[str]:
        return self._keys

    @keys.setter
    def keys(self, keys: List[str]):
        self._set('_keys', list(keys), self.keys_changed)

    @pyqtProperty(bool, notify=style_changed)
    def separate_scales(self) -> bool:
        return self._separate_scales

    @separate_scales.setter
    def separate_scales(self, separate: bool):
        self._set('_separate_scales', bool(separate), self.style_changed)

    @pyqtProperty('QVariantList', notify=style_changed)
    def palette(self) -> List[QColor]:
        return self._palette

    @palette.setter
    def palette(self, colors):
        self._set('_palette', [QColor(color) for color in colors] or [QColor('#4e9af1')], self.style_changed)

    @pyqtProperty(QColor, notify=style_changed)
    def text_color(self) -> QColor:
        return self._text_color

    @text_color.setter
    def text_color(self, color: QColor):
        self._set('_text_color', QColor(color), self.style_changed)

    @pyqtProperty(QColor, notify=style_changed)
    def grid_color(self) -> QColor:
        return self._grid_color

    @grid_color.setter
    def grid_color(self, color: QColor):
        self._set('_grid_color', QColor(color), self.style_changed)

    @pyqtProperty(QColor, notify=style_changed)
    def cursor_color(self) -> QColor:
        return self._cursor_color

    @cursor_color.setter
    def cursor_color(self, color: QColor):
        self._set('_cursor_color', QColor(color), self.style_changed)

    # Geometry for mouse interaction in QML

    def _plot_rect(self) -> QRectF:
        return QRectF(
            MARGIN_LEFT,
            MARGIN_TOP,
            self.width() - MARGIN_LEFT - MARGIN_RIGHT,
            self.height() - MARGIN_TOP - MARGIN_BOTTOM,
        )

    @pyqtSlot(float, result=float)
    def time_at(self, x: float) -> float:
        """The absolute time at the horizontal position `x` of the item."""
        if self._view is None:
            return math.nan
        plot = self._plot_rect()
        start, end = self._view.time_range()
        fraction = (x - plot.left()) / plot.width() if plot.width() > 0 else 0.5
        return start + min(max(fraction, 0.0), 1.0) * (end - start)

    @pyqtSlot(result=float)
    def seconds_per_pixel(self) -> float:
        if self._view is None:
            return 0.0
        start, end = self._view.time_range()
        width = self._plot_rect().width()
        return (end - start) / width if width > 0 else 0.0

    # Drawing

    def _color(self, key: str) -> QColor:
        index = self._view.color_index(key) if self._view is not None else 0
        return self._palette[index % len(self._palette)]

    def paint(self, painter: QPainter) -> None:
        plot = self._plot_rect()
        if plot.width() < 10 or plot.height() < 10:
            return
        font = QFont(painter.font())
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QPen(self._grid_color, 1))
        painter.drawRect(plot)
        if self._view is None:
            return

        tracker = self._view.tracker
        series = [(key, tracker.series[key]) for key in self._keys if key in tracker.series]
        start, end = self._view.time_range()
        if end <= start:
            end = start + 1
        windows = [(key, data, *data.window(start, end)) for key, data in series]
        ranges = {}
        for key, _, _, values in windows:
            finite = [value for value in values if math.isfinite(value)]
            if finite:
                ranges[key] = (min(finite), max(finite))
        if not ranges:
            painter.setPen(self._text_color)
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, 'No data in this time range')
            self._draw_time_axis(painter, plot, start, end)
            self._draw_cursors(painter, plot, start, end)
            return

        def padded(low: float, high: float):
            if low == high:
                low, high = low - 1, high + 1
            margin = 0.05 * (high - low)
            return low - margin, high + margin

        if self._separate_scales:
            scales = {key: padded(*bounds) for key, bounds in ranges.items()}
        else:
            common = padded(min(low for low, _ in ranges.values()), max(high for _, high in ranges.values()))
            scales = {key: common for key in ranges}

        # Value axis: the common scale, or the first signal's
        axis_key = next(key for key, _ in series if key in scales)
        low, high = scales[axis_key]
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = plot.bottom() - fraction * plot.height()
            painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(self._color(axis_key) if self._separate_scales else self._text_color)
            painter.drawText(
                QRectF(0, y - 8, MARGIN_LEFT - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _format(low + fraction * (high - low)),
            )
        self._draw_time_axis(painter, plot, start, end)

        painter.save()
        painter.setClipRect(plot)
        for key, _, times, values in windows:
            if key in scales:
                path = self._path(plot, times, values, start, end, *scales[key])
                painter.setPen(QPen(self._color(key), 1.6))
                painter.drawPath(path)
        painter.restore()
        self._draw_cursors(painter, plot, start, end)

    @staticmethod
    def _path(plot: QRectF, times, values, start: float, end: float, low: float, high: float) -> QPainterPath:
        """Min/max per pixel column."""
        x_scale = plot.width() / (end - start)
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
            pixel = int((timestamp - start) * x_scale)
            if pixel != column:
                if column is not None:
                    flush()
                column, column_min, column_max = pixel, value, value
            else:
                column_min, column_max = min(column_min, value), max(column_max, value)
        if column is not None:
            flush()
        return path

    def _draw_time_axis(self, painter: QPainter, plot: QRectF, start: float, end: float) -> None:
        offset = self._view.time_offset if self._view is not None else 0.0
        step = _nice_step(end - start, TIME_TICKS)
        tick = math.ceil((start - offset) / step) * step
        while tick + offset <= end:
            x = plot.left() + (tick + offset - start) / (end - start) * plot.width()
            painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(self._text_color)
            painter.drawText(
                QRectF(x - 40, plot.bottom() + 2, 80, MARGIN_BOTTOM - 2),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                f'{tick:.{max(0, -math.floor(math.log10(step)))}f} s',
            )
            tick += step

    def _draw_cursors(self, painter: QPainter, plot: QRectF, start: float, end: float) -> None:
        for index, label in ((0, 'C1'), (1, 'C2')):
            time = self._view.cursor(index)
            if time is None or not start <= time <= end:
                continue
            x = plot.left() + (time - start) / (end - start) * plot.width()
            pen = QPen(self._cursor_color, 1.4, Qt.PenStyle.SolidLine if index == 0 else Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.drawText(QRectF(x + 3, plot.top() + 1, 30, 14), Qt.AlignmentFlag.AlignLeft, label)


_registered = False


def register_qml_types() -> None:
    global _registered
    if not _registered:
        qmlRegisterType(SignalPlot, 'CanExplorer.Plots', 1, 0, 'SignalPlot')
        _registered = True
