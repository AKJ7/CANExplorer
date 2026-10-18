import asyncio
import enum
import time
from pathlib import Path
from typing import Callable, List, Optional

import can

from can_explorer.core.decoding import FrameDecoder
from can_explorer.core.frames import Frame, clone_message
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
    """Sends a message every `period` seconds, scheduled against the loop clock so it does not drift."""

    def __init__(self, session: 'BusSession', msg: can.Message, period: float):
        if period <= 0:
            raise ValueError(f'Period must be positive: {period=}')
        self.message = msg
        self.period = period
        self._session = session
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

    @property
    def running(self) -> bool:
        return not self._task.done()

    def stop(self) -> None:
        self._task.cancel()


class BusSession:
    """Pumps messages from a source into the trace buffer, the per-ID statistics and an optional recorder."""

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
        self.state = SessionState.CLOSED
        self.error: Optional[str] = None
        self.start_time: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._recorder: Optional[can.Listener] = None
        self.recording_path: Optional[Path] = None
        self._periodic: List[PeriodicTransmit] = []
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
            async for msg in self.source.messages():
                self._dispatch(msg)
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
        if self._recorder is not None:
            self._record(msg)

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
        for periodic in list(self._periodic):
            periodic.stop()
        self.stop_recording()
        self.source.close()

    def clear(self) -> None:
        self.trace.clear()
        self.statistics.clear()
        self.start_time = None

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

    def start_periodic(self, msg: can.Message, period: float) -> PeriodicTransmit:
        if not self.is_open:
            raise SessionError('Session is not open')
        periodic = PeriodicTransmit(self, msg, period)
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

    # Recording

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    def start_recording(self, path: str | Path) -> None:
        path = Path(path)
        if path.suffix.lower() not in LOG_FILE_SUFFIXES:
            raise SessionError(f'Unsupported log format {path.suffix!r}, use one of {", ".join(LOG_FILE_SUFFIXES)}')
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
