"""Playback controls for QML: the replay of a log file connection, and transmitting a log file on a bus."""

from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.filters import MessageFilter
from can_explorer.core.playback import SPEEDS, LogPlayer, PlaybackClock, speed_label
from can_explorer.core.session import SessionState
from can_explorer.core.log_formats import is_log_file, unsupported_message
from can_explorer.core.sources import FileSource


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


class PlaybackController(QObject):
    """Position, duration, speed and pausing of a playback. `refresh` is called on every view update."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._snapshot: tuple = ()

    # Implemented by the subclasses

    def _clock(self) -> Optional[PlaybackClock]:
        raise NotImplementedError

    def _duration(self) -> Optional[float]:
        raise NotImplementedError

    def _state(self) -> str:
        raise NotImplementedError

    def _frames(self) -> int:
        raise NotImplementedError

    def _path(self) -> Optional[Path]:
        raise NotImplementedError

    # Common

    def refresh(self) -> None:
        clock = self._clock()
        snapshot = (
            self._state(),
            round(clock.position, 1) if clock else 0.0,
            self._duration(),
            clock.speed if clock else None,
            self._frames(),
            self._iteration(),
        )
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self.changed.emit()

    @pyqtProperty('QVariantList', constant=True)
    def speeds(self) -> List[float]:
        return list(SPEEDS)

    @pyqtProperty('QStringList', constant=True)
    def speed_labels(self) -> List[str]:
        return [speed_label(speed) for speed in SPEEDS]

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        """'' (no playback), 'playing', 'paused', 'finished', 'stopped' or 'error'."""
        return self._state()

    @pyqtProperty(bool, notify=changed)
    def active(self) -> bool:
        return self._state() != ''

    @pyqtProperty(str, notify=changed)
    def file_name(self) -> str:
        path = self._path()
        return path.name if path else ''

    @pyqtProperty(float, notify=changed)
    def position(self) -> float:
        clock = self._clock()
        position = clock.position if clock and clock.started else 0.0
        duration = self._duration()
        return min(position, duration) if duration is not None else position

    @pyqtProperty(float, notify=changed)
    def duration(self) -> float:
        """Length of the recording in seconds; -1 while unknown."""
        duration = self._duration()
        return -1.0 if duration is None else duration

    @pyqtProperty(float, notify=changed)
    def speed(self) -> float:
        clock = self._clock()
        return clock.speed if clock else 1.0

    @pyqtProperty(bool, notify=changed)
    def paused(self) -> bool:
        clock = self._clock()
        return bool(clock and clock.paused)

    @pyqtProperty(int, notify=changed)
    def frames(self) -> int:
        return self._frames()

    def _loop(self) -> bool:
        return False

    def _iteration(self) -> int:
        return 0

    def _error(self) -> str:
        return ''

    @pyqtProperty(bool, notify=changed)
    def loop(self) -> bool:
        """Whether the playback repeats (bus replays)."""
        return self._loop()

    @pyqtProperty(int, notify=changed)
    def iteration(self) -> int:
        """Completed passes of a repeating playback."""
        return self._iteration()

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error()

    @pyqtProperty(bool, notify=changed)
    def can_seek(self) -> bool:
        return self._duration() is not None and self._state() in ('playing', 'paused', 'finished', 'stopped')

    @pyqtSlot(float)
    def seek(self, position: float):
        """Continue at `position` seconds of the recording."""
        self._seek(max(0.0, position))
        self.refresh()

    def _seek(self, position: float) -> None:
        raise NotImplementedError

    @pyqtSlot(float)
    def set_speed(self, speed: float):
        clock = self._clock()
        if clock is not None:
            clock.set_speed(speed)
            self.refresh()

    @pyqtSlot(bool)
    def set_paused(self, paused: bool):
        clock = self._clock()
        if clock is not None:
            clock.pause() if paused else clock.resume()
            self.refresh()


class ReplayPlayback(PlaybackController):
    """Playback of a connection replaying a log file into the views."""

    @property
    def source(self) -> Optional[FileSource]:
        source = self.connection.session.source
        return source if isinstance(source, FileSource) else None

    def _clock(self) -> Optional[PlaybackClock]:
        return self.source.clock if self.source else None

    def _duration(self) -> Optional[float]:
        return self.source.duration if self.source else None

    def _path(self) -> Optional[Path]:
        return self.source.path if self.source else None

    def _frames(self) -> int:
        return self.connection.session.trace.total

    def _seek(self, position: float) -> None:
        source = self.source
        if source is None:
            return
        connection = self.connection
        if not connection.session.is_open:  # Finished or stopped: replay again up to the position
            connection.clear()
            connection.open()
            if not connection.session.is_open:
                return
        elif position < source.clock.position:
            connection.clear()  # The frames are delivered again from the beginning of the file
        source.seek(position)

    def _state(self) -> str:
        if self.source is None:
            return ''
        state = self.connection.session.state
        if state is SessionState.OPEN:
            return 'paused' if self.source.clock.paused else 'playing'
        return {SessionState.FINISHED: 'finished', SessionState.ERROR: 'error'}.get(state, 'stopped')


class BusReplay(PlaybackController):
    """Transmission of a log file on the bus of a live connection."""

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(connection, parent)
        self._player: Optional[LogPlayer] = None

    @property
    def player(self) -> Optional[LogPlayer]:
        return self._player

    def _clock(self) -> Optional[PlaybackClock]:
        return self._player.clock if self._player else None

    def _duration(self) -> Optional[float]:
        return self._player.duration if self._player else None

    def _path(self) -> Optional[Path]:
        return self._player.path if self._player else None

    def _frames(self) -> int:
        return self._player.sent if self._player else 0

    def _state(self) -> str:
        return self._player.state if self._player else ''

    def _seek(self, position: float) -> None:
        if self._player is not None:
            self._player.seek(position)

    def _loop(self) -> bool:
        return bool(self._player and self._player.loop)

    def _iteration(self) -> int:
        return self._player.iteration if self._player else 0

    def _error(self) -> str:
        return self._player.error if self._player else ''

    @pyqtSlot(QUrl, float, bool, str, result=str)
    def start(self, url: QUrl, speed: float, loop: bool, filter_text: str) -> str:
        """Start transmitting the log file. Returns an error, or ''."""
        if not self.connection.session.is_open:
            return 'Connect before replaying a log file on the bus'
        path = _local_path(url)
        if not is_log_file(path):
            return unsupported_message(path)
        try:
            message_filter = MessageFilter.parse(filter_text)
        except ValueError as e:
            return f'Invalid filter: {e}'
        self.stop()
        self._player = LogPlayer(self.connection.session, path, speed, loop, message_filter, on_finished=self._finished)
        try:
            self._player.start()
        except OSError as e:
            self._player = None
            return str(e)
        self.refresh()
        return ''

    def _finished(self, player: LogPlayer) -> None:
        if player is self._player and player.state == LogPlayer.ERROR:
            self.connection.error_occurred.emit(f'Replay of {player.path.name} on the bus stopped: {player.error}')
        self.refresh()

    @pyqtSlot()
    def stop(self):
        if self._player is not None:
            self._player.stop()
            self.refresh()

    @pyqtSlot()
    def dismiss(self):
        """Stop and hide the playback bar."""
        self.stop()
        self._player = None
        self.refresh()
