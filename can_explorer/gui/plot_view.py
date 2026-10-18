"""The time axis, cursors and chart layout shared by the signal plots of a connection."""

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

MIN_SPAN = 0.001  # Seconds; zooming in stops here
WINDOWS = (5, 10, 30, 60, 300)  # Seconds of the live window
READOUT_INTERVAL_MS = 250  # The cursor readout is recomputed at most this often while values arrive


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def _number(value: Optional[float]) -> Any:
    """For QML: None and NaN become undefined-like empty strings."""
    return '' if value is None or (isinstance(value, float) and not math.isfinite(value)) else value


class PlotView(QObject):
    """Live (following the newest values over `window` seconds) or a fixed time range after zooming or panning.

    Times are absolute frame timestamps; `time_offset` (the start of the session) makes them readable.
    Charts are lists of signal keys: a newly tracked signal gets a chart of its own, signals can share a chart.
    """

    changed = pyqtSignal()  # Time range or cursors
    layout_changed = pyqtSignal()
    readout_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._follow = True
        self._window = 10.0
        self._start = 0.0
        self._end = 0.0
        self._cursors: List[Optional[float]] = [None, None]
        self._charts: List[List[str]] = []
        self._separate_scales: Dict[int, bool] = {}
        self._readout_timer = QTimer(self)
        self._readout_timer.setSingleShot(True)
        self._readout_timer.setInterval(READOUT_INTERVAL_MS)
        self._readout_timer.timeout.connect(self.readout_changed)
        connection.signals_updated.connect(self._signals_updated)
        connection.tracked_signals_changed.connect(self._tracked_changed)

    @property
    def tracker(self):
        return self.connection.session.signals

    # Time range

    def _data_range(self) -> Optional[Tuple[float, float]]:
        firsts = [series.first[0] for series in self.tracker.series.values() if series.first]
        lasts = [series.last[0] for series in self.tracker.series.values() if series.last]
        return (min(firsts), max(lasts)) if firsts else None

    def time_range(self) -> Tuple[float, float]:
        """(start, end) of the displayed time, absolute."""
        if self._follow:
            data = self._data_range()
            end = data[1] if data else 0.0
            return end - self._window, end
        return self._start, self._end

    @pyqtProperty(float, notify=changed)
    def time_offset(self) -> float:
        start = self.connection.session.start_time
        return start if start is not None else 0.0

    @pyqtProperty(bool, notify=changed)
    def follow(self) -> bool:
        return self._follow

    @pyqtProperty('QVariantList', constant=True)
    def windows(self) -> List[int]:
        return list(WINDOWS)

    @pyqtProperty(float, notify=changed)
    def window(self) -> float:
        return self._window

    @pyqtSlot(float)
    def set_window(self, seconds: float):
        self._window = max(MIN_SPAN, seconds)
        self.follow_live()

    @pyqtSlot()
    def follow_live(self):
        self._follow = True
        self._emit()

    def _set_range(self, start: float, end: float) -> None:
        if end - start < MIN_SPAN:
            middle = (start + end) / 2
            start, end = middle - MIN_SPAN / 2, middle + MIN_SPAN / 2
        self._start, self._end, self._follow = start, end, False
        self._emit()

    @pyqtSlot(float, float)
    def zoom(self, factor: float, center: float):
        """Scale the visible time span by `factor` (< 1 zooms in) around the absolute time `center`."""
        start, end = self.time_range()
        if not math.isfinite(center):
            center = (start + end) / 2
        self._set_range(center - (center - start) * factor, center + (end - center) * factor)

    @pyqtSlot(float)
    def pan(self, seconds: float):
        """Move the visible range by `seconds` (positive: later)."""
        start, end = self.time_range()
        self._set_range(start + seconds, end + seconds)

    @pyqtSlot()
    def fit_all(self):
        """Show everything the signals recorded."""
        data = self._data_range()
        if data is not None:
            start, end = data
            margin = max((end - start) * 0.02, MIN_SPAN)
            self._set_range(start - margin, end + margin)

    @pyqtProperty(str, notify=changed)
    def range_text(self) -> str:
        start, end = self.time_range()
        offset = self.time_offset
        return f'{start - offset:.3f} s – {end - offset:.3f} s ({end - start:.3g} s)'

    # Cursors

    @pyqtSlot(int, float)
    def set_cursor(self, index: int, time: float):
        if index in (0, 1) and math.isfinite(time):
            self._cursors[index] = time
            self._emit()

    @pyqtSlot()
    def clear_cursors(self):
        self._cursors = [None, None]
        self._emit()

    def cursor(self, index: int) -> Optional[float]:
        return self._cursors[index]

    @pyqtProperty(bool, notify=changed)
    def has_cursors(self) -> bool:
        return any(cursor is not None for cursor in self._cursors)

    @pyqtProperty('QVariantMap', notify=changed)
    def cursor_times(self) -> Dict[str, Any]:
        offset = self.time_offset
        first, second = self._cursors
        return {
            'c1': '' if first is None else f'{first - offset:.6f}',
            'c2': '' if second is None else f'{second - offset:.6f}',
            'delta': '' if first is None or second is None else f'{abs(second - first) * 1000:.3f} ms',
        }

    @pyqtProperty('QVariantList', notify=readout_changed)
    def readout(self) -> List[Dict[str, Any]]:
        """Per plotted signal: value at each cursor, their difference, and statistics between the cursors (or of
        the visible range without two cursors)."""
        first, second = self._cursors
        if first is not None and second is not None:
            low, high = sorted((first, second))
        else:
            low, high = self.time_range()
        rows = []
        for key, series in self.tracker.series.items():
            value1 = series.value_at(first) if first is not None else None
            value2 = series.value_at(second) if second is not None else None
            stats = series.statistics(low, high)
            rows.append(
                {
                    'key': key,
                    'unit': series.unit,
                    'color': self.color_index(key),
                    'c1': _number(value1),
                    'c2': _number(value2),
                    'delta': _number(value2 - value1) if value1 is not None and value2 is not None else '',
                    'minimum': _number(stats[0]) if stats else '',
                    'maximum': _number(stats[1]) if stats else '',
                    'mean': _number(stats[2]) if stats else '',
                    'count': stats[3] if stats else 0,
                }
            )
        return rows

    # Charts

    @pyqtProperty('QVariantList', notify=layout_changed)
    def charts(self) -> List[List[str]]:
        return [list(chart) for chart in self._charts]

    @pyqtSlot(str, result=int)
    def color_index(self, key: str) -> int:
        """A stable colour per signal: its position in the tracked signals."""
        tracked = self.tracker.tracked
        return tracked.index(key) if key in tracked else 0

    @pyqtSlot(int, result=bool)
    def separate_scales(self, chart: int) -> bool:
        """Whether each signal of a chart has its own value scale (default: when their units differ)."""
        if chart in self._separate_scales:
            return self._separate_scales[chart]
        if not 0 <= chart < len(self._charts):
            return False
        units = {self.tracker.series[key].unit for key in self._charts[chart] if key in self.tracker.series}
        return len(units) > 1

    @pyqtSlot(int, bool)
    def set_separate_scales(self, chart: int, separate: bool):
        self._separate_scales[chart] = separate
        self.layout_changed.emit()

    @pyqtSlot(str, int)
    def move_to_chart(self, key: str, chart: int):
        """Show a signal in another chart (-1: a new chart of its own)."""
        if key not in self.tracker.series:
            return
        target = self._charts[chart] if 0 <= chart < len(self._charts) else None
        for existing in self._charts:
            if key in existing:
                existing.remove(key)
        if target is None:
            self._charts.append([key])
        else:
            target.append(key)
        self._charts = [existing for existing in self._charts if existing]
        self._separate_scales.clear()
        self.layout_changed.emit()

    def _tracked_changed(self) -> None:
        tracked = self.tracker.tracked
        charts = [[key for key in chart if key in tracked] for chart in self._charts]
        shown = {key for chart in charts for key in chart}
        charts += [[key] for key in tracked if key not in shown]
        self._charts = [chart for chart in charts if chart]
        self.layout_changed.emit()
        self.readout_changed.emit()

    # Export

    @pyqtSlot(QUrl, bool, result=str)
    def export_csv(self, url: QUrl, visible_only: bool) -> str:
        """Write the plotted signals to a CSV file: one row per time any of them changed, with the values held
        between their frames. Returns a message."""
        path = _local_path(url)
        if not self.tracker.series:
            return 'No signals are plotted'
        start, end = self.time_range() if visible_only else (-math.inf, math.inf)
        keys = list(self.tracker.series)
        points = []
        for column, key in enumerate(keys):
            series = self.tracker.series[key]
            points += [
                (time, column, value)
                for time, value in zip(series.times, series.values, strict=True)
                if start <= time <= end
            ]
        points.sort(key=lambda point: (point[0], point[1]))
        offset = self.time_offset
        try:
            with path.open('w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                units = [self.tracker.series[key].unit for key in keys]
                writer.writerow(
                    ['time_s'] + [f'{key} [{unit}]' if unit else key for key, unit in zip(keys, units, strict=True)]
                )
                current: List[Any] = [''] * len(keys)
                rows = 0
                index = 0
                while index < len(points):
                    time = points[index][0]
                    while index < len(points) and points[index][0] == time:
                        _, column, value = points[index]
                        current[column] = f'{value:.10g}'
                        index += 1
                    writer.writerow([f'{time - offset:.6f}', *current])
                    rows += 1
        except OSError as e:
            return f'Could not write {path.name}: {e}'
        return f'Exported {rows:,} rows of {len(keys)} signals to {path.name}'

    # Notifications

    def _emit(self) -> None:
        self.changed.emit()
        self.readout_changed.emit()

    def _signals_updated(self) -> None:
        if self._follow:
            self.changed.emit()
        if not self._readout_timer.isActive():
            self._readout_timer.start()
