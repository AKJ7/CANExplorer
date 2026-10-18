import asyncio
import enum
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import can

from can_explorer.core.busstats import BusSnapshot, BusStatistics
from can_explorer.core.decoding import FrameDecoder
from can_explorer.core.frames import Frame, clone_message
from can_explorer.core.signals import SignalTracker
from can_explorer.core.sources import LOG_FILE_SUFFIXES, MessageSource, SourceError
from can_explorer.core.store import IdStatistics, TraceBuffer
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


class SessionError(Exception):
    pass


@enum.unique
class SessionState(enum.Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    FINISHED = 'finished'
    ERROR = 'error'


class PeriodicTransmit:
    """Base of the periodic transmissions of a session."""

    def __init__(self, session: 'BusSession', msg: can.Message, period: float):
        if period <= 0:
            raise ValueError(f'Period must be positive: {period=}')
        self.message = msg
        self.period = period
        self._session = session

    @property
    def hardware(self) -> bool:
        return False

    def stop(self) -> None:
        raise NotImplementedError


class SoftwarePeriodicTransmit(PeriodicTransmit):
    """Sends from the event loop, scheduled against the loop clock so it does not drift.

    The frames go through the session, so they appear in the trace and recordings.
    """

    def __init__(self, session: 'BusSession', msg: can.Message, period: float):
        super().__init__(session, msg, period)
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f'Periodic {msg.arbitration_id:#x}')

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while True:
                self._session.send(clone_message(self.message))
                deadline += self.period
                await asyncio.sleep(max(0.0, deadline - loop.time()))
        except SessionError as e:
            logger.warning(f'Stopping periodic transmission of {self.message.arbitration_id:#x}: {e}')
        finally:
            self._session._periodic_finished(self)

    def stop(self) -> None:
        self._task.cancel()


class HardwarePeriodicTransmit(PeriodicTransmit):
    """Sends with python-can's cyclic task: timed by the interface hardware or driver where supported.

    The frames bypass the session, so they only appear in the trace when the bus echoes own messages.
    """

    def __init__(self, session: 'BusSession', msg: can.Message, period: float):
        super().__init__(session, msg, period)
        try:
            self._task = session.source.send_periodic(msg, period)
        except (AttributeError, SourceError) as e:
            raise SessionError(f'Hardware periodic transmission not available: {e}') from e

    @property
    def hardware(self) -> bool:
        return True

    def stop(self) -> None:
        try:
            self._task.stop()
        finally:
            self._session._periodic_finished(self)


class BusSession:
    """Pumps messages from a source into the trace buffer, the statistics, the signal tracker,
    the message listeners and an optional recorder."""

    def __init__(
        self,
        source: MessageSource,
        decoder: Optional[FrameDecoder] = None,
        buffer_size: int = 100_000,
    ):
        self.source = source
        self.decoder = decoder or FrameDecoder()
        self.trace = TraceBuffer(buffer_size)
        self.statistics = IdStatistics()
        self.signals = SignalTracker()
        self.bus_statistics = BusStatistics(source.bitrates, getattr(source, 'lin_baudrate', None))
        self.state = SessionState.CLOSED
        self.error: Optional[str] = None
        self.start_time: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._recorder: Optional[can.Listener] = None
        self.recording_path: Optional[Path] = None
        self._periodic: List[PeriodicTransmit] = []
        self._message_listeners: List[Callable[[can.Message], None]] = []
        self._state_listeners: List[Callable[['BusSession'], None]] = []
        self._error_listeners: List[Callable[[str], None]] = []
        self._periodic_listeners: List[Callable[['BusSession'], None]] = []

    def add_state_listener(self, callback: Callable[['BusSession'], None]) -> None:
        self._state_listeners.append(callback)

    def add_periodic_listener(self, callback: Callable[['BusSession'], None]) -> None:
        self._periodic_listeners.append(callback)

    def add_error_listener(self, callback: Callable[[str], None]) -> None:
        """Errors that do not end the session, e.g. a failing recording."""
        self._error_listeners.append(callback)

    def add_message_listener(self, callback: Callable[[can.Message], None]) -> None:
        """Called for every received and transmitted message, in order, on the event loop."""
        self._message_listeners.append(callback)

    def remove_message_listener(self, callback: Callable[[can.Message], None]) -> None:
        if callback in self._message_listeners:
            self._message_listeners.remove(callback)

    def _report_error(self, message: str) -> None:
        logger.error(message)
        for callback in self._error_listeners:
            callback(message)

    def _set_state(self, state: SessionState, error: Optional[str] = None) -> None:
        self.state, self.error = state, error
        for callback in self._state_listeners:
            callback(self)

    @property
    def is_open(self) -> bool:
        return self.state is SessionState.OPEN

    @property
    def bus_state(self) -> Optional[str]:
        state = getattr(self.source, 'bus_state', None)
        return state if self.is_open else None

    def start(self) -> None:
        """Open the source and start receiving. Must be called with a running event loop."""
        if self.is_open:
            return
        loop = asyncio.get_running_loop()
        self.decoder.reset()
        try:
            self.source.open(loop)
        except SourceError as e:
            self._set_state(SessionState.ERROR, str(e))
            raise SessionError(str(e)) from e
        self._task = loop.create_task(self._pump(), name=f'BusSession {self.source.description}')
        self._set_state(SessionState.OPEN)

    async def _pump(self) -> None:
        try:
            async for batch in self.source.batches():
                for msg in batch:
                    self._dispatch(msg)
                # With continuous traffic a new batch is always ready, so getting it never suspends.
                # Yield explicitly so the GUI and other tasks keep running.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f'Session failed: {e}')
            self._shutdown()
            self._set_state(SessionState.ERROR, str(e))
            return
        if self.is_open:
            self._shutdown()
            self._set_state(SessionState.FINISHED)

    def _dispatch(self, msg: can.Message) -> None:
        if self.start_time is None:
            self.start_time = msg.timestamp
        frame = Frame(msg, self.decoder.describe(msg))
        self.trace.append(frame)
        self.statistics.update(frame)
        self.bus_statistics.update(msg)
        database = self.decoder.database
        if database is not None:
            self.signals.update(msg, database)
        if self._recorder is not None:
            self._record(msg)
        for callback in self._message_listeners:
            callback(msg)

    def _record(self, msg: can.Message) -> None:
        try:
            self._recorder.on_message_received(msg)
            return
        except Exception as e:
            error = e
        if msg.channel is not None:
            # Some formats store the channel as a small integer derived from the channel name (e.g. BLF),
            # which fails for names like 'vcan123456'. A session records a single bus, so drop the channel.
            anonymous = clone_message(msg)
            anonymous.channel = None
            anonymous.timestamp, anonymous.is_rx = msg.timestamp, msg.is_rx
            try:
                self._recorder.on_message_received(anonymous)
                return
            except Exception as e:
                error = e
        path = self.recording_path
        self.stop_recording()
        self._report_error(f'Recording to {path} stopped: {error}')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        was_open = self.is_open
        self._shutdown()
        if was_open:
            self._set_state(SessionState.CLOSED)

    def _shutdown(self) -> None:
        self.stop_periodic()
        self.stop_recording()
        self.source.close()

    def clear(self) -> None:
        self.trace.clear()
        self.statistics.clear()
        self.signals.clear()
        self.bus_statistics.reset()
        self.start_time = None

    def sample_bus_statistics(self) -> BusSnapshot:
        return self.bus_statistics.sample(time.monotonic())

    # Transmission

    def send(self, msg: can.Message) -> None:
        if not self.source.can_send:
            raise SessionError('This source cannot transmit')
        if not self.is_open:
            raise SessionError('Session is not open')
        msg.timestamp = time.time()
        msg.is_rx = False
        try:
            self.source.send(msg)
        except SourceError as e:
            raise SessionError(str(e)) from e
        if not self.source.echoes_tx:
            self._dispatch(msg)

    def start_periodic(self, msg: can.Message, period: float, hardware: bool = False) -> PeriodicTransmit:
        if not self.source.can_send:
            raise SessionError('This source cannot transmit')
        if not self.is_open:
            raise SessionError('Session is not open')
        periodic = (HardwarePeriodicTransmit if hardware else SoftwarePeriodicTransmit)(self, msg, period)
        self._periodic.append(periodic)
        self._notify_periodic()
        return periodic

    @property
    def periodic(self) -> List[PeriodicTransmit]:
        return list(self._periodic)

    def stop_periodic(self) -> None:
        for periodic in list(self._periodic):
            periodic.stop()

    def _periodic_finished(self, periodic: PeriodicTransmit) -> None:
        if periodic in self._periodic:
            self._periodic.remove(periodic)
            self._notify_periodic()

    def _notify_periodic(self) -> None:
        for callback in self._periodic_listeners:
            callback(self)

    # Recording and export

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    @staticmethod
    def _check_log_format(path: Path) -> None:
        if path.suffix.lower() not in LOG_FILE_SUFFIXES:
            raise SessionError(f'Unsupported log format {path.suffix!r}, use one of {", ".join(LOG_FILE_SUFFIXES)}')

    def start_recording(self, path: str | Path) -> None:
        path = Path(path)
        self._check_log_format(path)
        self.stop_recording()
        try:
            self._recorder = can.Logger(path)
        except Exception as e:
            raise SessionError(f'Could not record to {path}: {e}') from e
        self.recording_path = path
        logger.info(f'Recording to {path}')

    def stop_recording(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()
            logger.info(f'Stopped recording to {self.recording_path}')
            self._recorder = None

    @classmethod
    def export(cls, frames: Iterable[Frame], path: str | Path) -> int:
        """Write frames to a log file. Returns the number of frames written."""
        path = Path(path)
        cls._check_log_format(path)
        count = 0
        try:
            with can.Logger(path) as writer:
                for frame in frames:
                    try:
                        writer.on_message_received(frame.message)
                    except Exception:
                        anonymous = clone_message(frame.message)  # See _record()
                        anonymous.channel = None
                        anonymous.timestamp, anonymous.is_rx = frame.message.timestamp, frame.message.is_rx
                        writer.on_message_received(anonymous)
                    count += 1
        except Exception as e:
            raise SessionError(f'Could not export to {path}: {e}') from e
        return count
