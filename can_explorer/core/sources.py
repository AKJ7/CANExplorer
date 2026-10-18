import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional, Protocol

import can

from can_explorer.core.log_formats import is_log_file, unsupported_message
from can_explorer.core.playback import PlaybackClock, scan_log
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

# Receive timeout of the notifier thread; bounds how long closing a bus blocks the caller
NOTIFIER_TIMEOUT = 0.1
# Upper bound of messages handed to the event loop at once, so a burst cannot stall the GUI
MAX_BATCH = 5_000
# Messages read from a log file per chunk, and chunks buffered ahead of the consumer
FILE_CHUNK = 1_000
FILE_CHUNKS_AHEAD = 8

_EOF = object()


class SourceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BusConfig:
    interface: str
    channel: str
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: Optional[int] = None
    receive_own_messages: bool = False
    # 'CAN', or 'LIN' for LIN carried as CAN frames (sllin convention, see core/lin.py)
    bus_type: str = 'CAN'
    lin_baudrate: int = 19200

    @property
    def is_lin(self) -> bool:
        return self.bus_type == 'LIN'


class MessageSource(Protocol):
    @property
    def description(self) -> str: ...

    @property
    def can_send(self) -> bool: ...

    @property
    def echoes_tx(self) -> bool:
        """True when transmitted messages are received back from the source."""
        ...

    @property
    def bitrates(self) -> Optional[tuple[int, Optional[int]]]:
        """(nominal, data) bitrate in bit/s when known, used to estimate the bus load."""
        ...

    def open(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def close(self) -> None: ...

    def batches(self) -> AsyncIterator[List[can.Message]]: ...

    def send(self, msg: can.Message) -> None: ...


class ThreadBridge:
    """Hands items from worker threads to the event loop in batches.

    Waking the loop costs a system call, so it is only woken when the consumer has drained
    everything; items arriving in the meantime are collected and delivered together.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._items: deque = deque()
        self._lock = threading.Lock()
        self._wakeup_scheduled = False
        self._event = asyncio.Event()

    def _wake(self) -> None:
        with self._lock:
            if self._wakeup_scheduled:
                return
            self._wakeup_scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:  # Loop already closed
            pass

    def put(self, item) -> None:
        """Thread-safe."""
        self._items.append(item)
        self._wake()

    async def get_batch(self) -> list:
        while True:
            with self._lock:
                self._wakeup_scheduled = False
            batch = []
            items = self._items
            while items and len(batch) < MAX_BATCH:
                batch.append(items.popleft())
            if batch:
                if items:
                    self._wake()
                return batch
            await self._event.wait()
            self._event.clear()


class _BridgeListener(can.Listener):
    """Forwards messages and errors from the python-can notifier thread to the event loop."""

    def __init__(self, bridge: ThreadBridge):
        self._bridge = bridge

    def on_message_received(self, msg: can.Message) -> None:
        self._bridge.put(msg)

    def on_error(self, exc: Exception) -> None:
        self._bridge.put(exc)

    def stop(self) -> None:
        pass


class LiveSource:
    """Messages from a python-can bus. Reception runs in the notifier thread on every platform."""

    def __init__(self, config: BusConfig):
        self.config = config
        self._bus: Optional[can.BusABC] = None
        self._notifier: Optional[can.Notifier] = None
        self._bridge: Optional[ThreadBridge] = None

    @property
    def description(self) -> str:
        config = self.config
        if config.is_lin:
            return f'{config.interface}:{config.channel} LIN @ {config.lin_baudrate} baud'
        rate = f'{config.bitrate} bit/s'
        if config.fd:
            rate += f' (FD{f", data {config.data_bitrate} bit/s" if config.data_bitrate else ""})'
        return f'{config.interface}:{config.channel} @ {rate}'

    @property
    def can_send(self) -> bool:
        return True

    @property
    def echoes_tx(self) -> bool:
        return self.config.receive_own_messages

    @property
    def bitrates(self) -> Optional[tuple[int, Optional[int]]]:
        if self.config.is_lin:
            return None
        return self.config.bitrate, self.config.data_bitrate if self.config.fd else None

    @property
    def lin_baudrate(self) -> Optional[int]:
        return self.config.lin_baudrate if self.config.is_lin else None

    @property
    def bus_state(self) -> Optional[str]:
        """'active', 'passive' or 'error' (bus off) where the interface reports it."""
        if self._bus is None:
            return None
        try:
            return self._bus.state.name.lower()
        except (NotImplementedError, AttributeError, can.CanError):
            return None

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        config = self.config
        logger.info(f'Opening bus: {config=}')
        options = dict(
            interface=config.interface,
            channel=config.channel,
            receive_own_messages=config.receive_own_messages,
        )
        if not config.is_lin:  # The LIN baud rate is a setting of the LIN driver, not of the CAN socket
            options.update(bitrate=config.bitrate, fd=config.fd)
        if config.fd and config.data_bitrate and not config.is_lin:
            options['data_bitrate'] = config.data_bitrate
        try:
            self._bus = can.Bus(**options)
        except Exception as e:
            raise SourceError(f'Could not open {self.description}: {e}') from e
        self._bridge = ThreadBridge(loop)
        self._notifier = can.Notifier(self._bus, [_BridgeListener(self._bridge)], timeout=NOTIFIER_TIMEOUT)

    def close(self) -> None:
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
        if self._bridge is not None:
            self._bridge.put(_EOF)

    async def batches(self) -> AsyncIterator[List[can.Message]]:
        bridge = self._bridge
        while True:
            batch = await bridge.get_batch()
            messages = []
            for item in batch:
                if isinstance(item, can.Message):
                    messages.append(item)
                    continue
                if messages:
                    yield messages
                if item is _EOF:
                    return
                raise SourceError(f'Bus error on {self.description}: {item}') from item
            yield messages

    def send(self, msg: can.Message) -> None:
        if self._bus is None:
            raise SourceError('Bus is not open')
        try:
            self._bus.send(msg, timeout=0.1)
        except can.CanError as e:
            raise SourceError(f'Could not send: {e}') from e

    def send_periodic(self, msg: can.Message, period: float) -> can.broadcastmanager.CyclicSendTaskABC:
        """Periodic transmission scheduled by the interface (in hardware where supported)."""
        if self._bus is None:
            raise SourceError('Bus is not open')
        try:
            return self._bus.send_periodic(msg, period)
        except can.CanError as e:
            raise SourceError(f'Could not start periodic transmission: {e}') from e


class FileSource:
    """Replays a log file with its recorded timing scaled by `speed` (0: as fast as possible).

    The playback can be paused and its speed changed through `clock`; `duration` is known once a background
    scan of the file is done.
    """

    def __init__(self, path: str | Path, realtime: bool = True, speed: Optional[float] = None):
        self.path = Path(path)
        self.clock = PlaybackClock((1.0 if realtime else 0.0) if speed is None else speed)
        self.duration: Optional[float] = None
        self._bridge: Optional[ThreadBridge] = None
        self._generation = 0  # Of the reader thread; seeking back starts a new one
        self._stop = threading.Event()
        self._slots: Optional[threading.Semaphore] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def realtime(self) -> bool:
        return self.clock.speed > 0

    @property
    def description(self) -> str:
        return f'Replay of {self.path.name}'

    @property
    def can_send(self) -> bool:
        return False

    @property
    def echoes_tx(self) -> bool:
        return False

    @property
    def bitrates(self) -> Optional[tuple[int, Optional[int]]]:
        return None

    @property
    def lin_baudrate(self) -> Optional[int]:
        return None

    @property
    def bus_state(self) -> Optional[str]:
        return None

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.path.is_file():
            raise SourceError(f'Log file not found: {self.path}')
        if not is_log_file(self.path):
            raise SourceError(unsupported_message(self.path))
        self._bridge = ThreadBridge(loop)
        self.clock.reset()  # A new replay starts at the beginning (unless seek() moves it before the first frame)
        self._start_reader()
        if self.duration is None:
            threading.Thread(target=self._scan, name=f'FileScan-{self.path.name}', daemon=True).start()

    def _start_reader(self) -> None:
        """Read the file from its beginning in a new thread; items of earlier readers are dropped."""
        self._stop.set()
        self._generation += 1
        self._stop = threading.Event()
        self._slots = threading.Semaphore(FILE_CHUNKS_AHEAD)
        self._thread = threading.Thread(
            target=self._read,
            args=(self._generation, self._stop, self._slots),
            name=f'FileSource-{self.path.name}',
            daemon=True,
        )
        self._thread.start()

    def _scan(self) -> None:
        try:
            _, self.duration = scan_log(self.path)
        except Exception as e:  # The playback reports read errors itself
            logger.debug(f'Could not scan {self.path}: {e}')

    def _read(self, generation: int, stop: threading.Event, slots: threading.Semaphore) -> None:
        def put_chunk(chunk: list) -> bool:
            # Backpressure: wait until the consumer has taken an earlier chunk
            while not slots.acquire(timeout=0.2):
                if stop.is_set():
                    return False
            self._bridge.put((generation, chunk))
            return not stop.is_set()

        try:
            with can.LogReader(self.path) as reader:
                chunk = []
                for msg in reader:
                    chunk.append(msg)
                    if len(chunk) >= FILE_CHUNK:
                        if not put_chunk(chunk):
                            return
                        chunk = []
                if chunk and not put_chunk(chunk):
                    return
        except Exception as e:
            logger.error(f'Could not read {self.path}: {e}')
            self._bridge.put((generation, e))
            return
        self._bridge.put((generation, _EOF))

    def close(self) -> None:
        self._stop.set()
        if self._bridge is not None:
            self._bridge.put((self._generation, _EOF))

    def seek(self, position: float) -> bool:
        """Continue the replay at `position` seconds of the recording.

        Forward, the frames in between are delivered at once (so statistics stay complete). Backward, the file is
        read again from its beginning up to the position; then True is returned, and the frames delivered so far
        should be cleared.
        """
        if self._bridge is None:
            raise SourceError('The replay is not running')
        position = max(0.0, position)
        restart = position < self.clock.position
        if restart:
            self._start_reader()
        self.clock.seek(position)
        return restart

    async def batches(self) -> AsyncIterator[List[can.Message]]:
        clock = self.clock
        first_timestamp = None
        while True:
            for generation, item in await self._bridge.get_batch():
                if generation != self._generation:
                    continue  # From a reader replaced by seeking back
                if item is _EOF:
                    return
                if isinstance(item, BaseException):
                    raise SourceError(f'Could not read {self.path.name}: {item}') from item
                self._slots.release()
                if first_timestamp is None and item:
                    first_timestamp = item[0].timestamp
                    if not clock.started:
                        clock.start()
                if clock.speed <= 0:
                    # Only waits while paused
                    await clock.wait_until(
                        item[-1].timestamp - first_timestamp,
                        lambda generation=generation: generation != self._generation,
                    )
                    if generation == self._generation:
                        yield item
                    continue
                # Yield the messages that are due, wait until the next one
                due = []
                for msg in item:
                    position = msg.timestamp - first_timestamp
                    if not clock.is_due(position):
                        if due:
                            yield due
                            due = []
                        if generation != self._generation:
                            break
                        await clock.wait_until(position, lambda generation=generation: generation != self._generation)
                        if generation != self._generation:
                            break
                    due.append(msg)
                if due and generation == self._generation:
                    yield due

    def send(self, msg: can.Message) -> None:
        raise SourceError('Cannot transmit on a replayed log file')
