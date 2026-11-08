import math
from typing import List, Optional, Tuple

import numpy as np
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


def _min_max_columns(
    times: np.ndarray, values: np.ndarray, start: float, x_scale: float
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reduce a series to the min and max value of each pixel column. Returns (column, minimum, maximum) arrays,
    the columns ascending. `times` must be sorted; non-finite values are dropped. Columns are
    `int((time - start) * x_scale)`, so a point just left of the window keeps its negative column."""
    finite = np.isfinite(values)
    times, values = times[finite], values[finite]
    if times.size == 0:
        empty = np.empty(0)
        return empty.astype(np.int64), empty, empty
    columns = ((times - start) * x_scale).astype(np.int64)  # Non-decreasing, since times are sorted
    change = np.empty(columns.size, dtype=bool)
    change[0] = True
    np.not_equal(columns[1:], columns[:-1], out=change[1:])
    segments = np.nonzero(change)[0]
    return columns[segments], np.minimum.reduceat(values, segments), np.maximum.reduceat(values, segments)


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
        windows = []
        for key, data in series:
            times, values = data.window(start, end)
            windows.append((key, data, np.asarray(times, dtype=float), np.asarray(values, dtype=float)))
        ranges = {}
        for key, _, _, values in windows:
            finite = values[np.isfinite(values)]
            if finite.size:
                ranges[key] = (float(finite.min()), float(finite.max()))
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
    def _path(plot: QRectF, times: np.ndarray, values: np.ndarray, start: float, end: float, low: float, high: float):
        """Min/max per pixel column, drawn min then max."""
        x_scale = plot.width() / (end - start)
        y_scale = plot.height() / (high - low)
        columns, column_min, column_max = _min_max_columns(times, values, start, x_scale)
        path = QPainterPath()
        left, bottom = plot.left(), plot.bottom()
        xs = left + columns
        y_min = bottom - (column_min - low) * y_scale
        y_max = bottom - (column_max - low) * y_scale
        first = True
        for index in range(columns.size):
            x = xs[index]
            points = (y_min[index],) if column_min[index] == column_max[index] else (y_min[index], y_max[index])
            for y in points:
                point = QPointF(x, y)
                if first:
                    path.moveTo(point)
                    first = False
                else:
                    path.lineTo(point)
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
