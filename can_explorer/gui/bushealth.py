import time
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.bushealth import BusHealth, buckets

WINDOWS = ((60, '1 min'), (600, '10 min'), (3600, '1 h'))
CHART_POINTS = 120


class BusHealthController(QObject):
    """The Bus health page of a connection: error frames, error counters, states and the load over time."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._window = 60
        connection.bus_statistics_changed.connect(self.changed)

    @property
    def health(self) -> BusHealth:
        return self._connection.health

    @pyqtProperty('QVariantList', constant=True)
    def windows(self) -> List[Dict[str, Any]]:
        return [{'value': seconds, 'name': name} for seconds, name in WINDOWS]

    @pyqtProperty(int, notify=changed)
    def window(self) -> int:
        return self._window

    @pyqtSlot(int)
    def set_window(self, seconds: int):
        self._window = max(10, seconds)
        self.changed.emit()

    @pyqtProperty(str, notify=changed)
    def window_text(self) -> str:
        return next((name for seconds, name in WINDOWS if seconds == self._window), f'{self._window} s')

    # Current values

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        """'active', 'warning', 'passive', 'bus-off' or '' while unknown."""
        return self.health.state or ''

    @pyqtProperty(int, notify=changed)
    def error_frames(self) -> int:
        return self.health.error_frames

    @pyqtProperty(bool, notify=changed)
    def has_counters(self) -> bool:
        return self.health.tec is not None

    @pyqtProperty(str, notify=changed)
    def counters_text(self) -> str:
        health = self.health
        return f'TEC {health.tec} · REC {health.rec}' if health.tec is not None else ''

    @pyqtProperty(int, notify=changed)
    def bus_off_count(self) -> int:
        return self.health.transitions.get('bus-off', 0)

    @pyqtProperty(int, notify=changed)
    def passive_count(self) -> int:
        return self.health.transitions.get('passive', 0)

    # Over the window

    def _samples(self):
        return self.health.window(self._window)

    def _series(self, values) -> List[float]:
        return buckets(values, self.bucket)[-self.points :]

    @pyqtProperty(int, notify=changed)
    def bucket(self) -> int:
        """Seconds per chart point."""
        return max(1, self._window // CHART_POINTS)

    @pyqtProperty(int, notify=changed)
    def points(self) -> int:
        """Chart points of a full window."""
        return self._window // self.bucket

    @pyqtProperty('QVariantList', notify=changed)
    def load(self) -> List[float]:
        values = [sample.load * 100 for sample in self._samples() if sample.load is not None]
        return self._series(values)

    @pyqtProperty('QVariantList', notify=changed)
    def frame_rate(self) -> List[float]:
        return self._series([sample.rx_rate + sample.tx_rate for sample in self._samples()])

    @pyqtProperty('QVariantList', notify=changed)
    def errors(self) -> List[float]:
        return self._series([float(sample.errors) for sample in self._samples()])

    @pyqtProperty('QVariantList', notify=changed)
    def tec(self) -> List[float]:
        return self._series([sample.tec for sample in self._samples()])

    @pyqtProperty('QVariantList', notify=changed)
    def rec(self) -> List[float]:
        return self._series([sample.rec for sample in self._samples()])

    @pyqtProperty('QVariantList', notify=changed)
    def timeline(self) -> List[Dict[str, Any]]:
        return self.health.timeline(self._window)

    @pyqtProperty(float, notify=changed)
    def peak_errors(self) -> float:
        return max((sample.errors for sample in self._samples()), default=0)

    @pyqtProperty(float, notify=changed)
    def peak_load(self) -> float:
        return max((sample.load * 100 for sample in self._samples() if sample.load is not None), default=-1.0)

    @pyqtProperty('QVariantList', notify=changed)
    def events(self) -> List[Dict[str, str]]:
        """The latest error events, newest first."""
        return [
            {
                'time': (
                    time.strftime('%H:%M:%S', time.localtime(event.time)) if event.time > 1e9 else f'{event.time:.3f}'
                ),
                'text': event.text,
                'state': event.state or '',
            }
            for event in reversed(list(self.health.events)[-100:])
        ]

    @pyqtSlot()
    def clear(self):
        self.health.reset()
        self.changed.emit()
