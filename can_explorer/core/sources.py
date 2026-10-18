import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol

import can

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

# File formats python-can can read back (can.LogReader) and write (can.Logger)
LOG_FILE_SUFFIXES = ('.asc', '.blf', '.csv', '.log', '.trc', '.db')

_EOF = object()

# Receive timeout of the notifier thread; bounds how long closing a bus blocks the caller
NOTIFIER_TIMEOUT = 0.1


class SourceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BusConfig:
    interface: str
    channel: str
    bitrate: int = 500_000
    fd: bool = False
    receive_own_messages: bool = False


class MessageSource(Protocol):
    @property
    def description(self) -> str: ...

    @property
    def can_send(self) -> bool: ...

    @property
    def echoes_tx(self) -> bool:
        """True when transmitted messages are received back from the source."""
        ...

    def open(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def close(self) -> None: ...

    def messages(self) -> AsyncIterator[can.Message]: ...

    def send(self, msg: can.Message) -> None: ...


class _QueueListener(can.Listener):
    """Forwards messages and errors from the python-can notifier thread into an asyncio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self._loop = loop
        self._queue = queue

    def _put(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # Loop already closed
            pass

    def on_message_received(self, msg: can.Message) -> None:
        self._put(msg)

    def on_error(self, exc: Exception) -> None:
        self._put(exc)

    def stop(self) -> None:
        pass


class LiveSource:
    """Messages from a python-can bus. Reception runs in the notifier thread on every platform."""

    def __init__(self, config: BusConfig):
        self.config = config
        self._bus: Optional[can.BusABC] = None
        self._notifier: Optional[can.Notifier] = None
        self._queue: Optional[asyncio.Queue] = None

    @property
    def description(self) -> str:
        config = self.config
        return f'{config.interface}:{config.channel} @ {config.bitrate} bit/s{" (FD)" if config.fd else ""}'

    @property
    def can_send(self) -> bool:
        return True

    @property
    def echoes_tx(self) -> bool:
        return self.config.receive_own_messages

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        config = self.config
        logger.info(f'Opening bus: {config=}')
        try:
            self._bus = can.Bus(
                interface=config.interface,
                channel=config.channel,
                bitrate=config.bitrate,
                fd=config.fd,
                receive_own_messages=config.receive_own_messages,
            )
        except Exception as e:
            raise SourceError(f'Could not open {self.description}: {e}') from e
        self._queue = asyncio.Queue()
        self._notifier = can.Notifier(self._bus, [_QueueListener(loop, self._queue)], timeout=NOTIFIER_TIMEOUT)

    def close(self) -> None:
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
        if self._queue is not None:
            self._queue.put_nowait(_EOF)

    async def messages(self) -> AsyncIterator[can.Message]:
        queue = self._queue
        while True:
            item = await queue.get()
            if item is _EOF:
                return
            if isinstance(item, BaseException):
                raise SourceError(f'Bus error on {self.description}: {item}') from item
            yield item

    def send(self, msg: can.Message) -> None:
        if self._bus is None:
            raise SourceError('Bus is not open')
        try:
            self._bus.send(msg, timeout=0.1)
        except can.CanError as e:
            raise SourceError(f'Could not send: {e}') from e


class FileSource:
    """Replays a log file. With realtime=True the original inter-frame timing is reproduced."""

    def __init__(self, path: str | Path, realtime: bool = True):
        self.path = Path(path)
        self.realtime = realtime
        self._queue: Optional[asyncio.Queue] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def description(self) -> str:
        return f'Replay of {self.path.name}{"" if self.realtime else " (as fast as possible)"}'

    @property
    def can_send(self) -> bool:
        return False

    @property
    def echoes_tx(self) -> bool:
        return False

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.path.is_file():
            raise SourceError(f'Log file not found: {self.path}')
        if self.path.suffix.lower() not in LOG_FILE_SUFFIXES:
            raise SourceError(f'Unsupported log file format: {self.path.suffix}')
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=1_000)
        self._stop.clear()
        self._thread = threading.Thread(target=self._read, name=f'FileSource-{self.path.name}', daemon=True)
        self._thread.start()

    def _put(self, item) -> bool:
        future = asyncio.run_coroutine_threadsafe(self._queue.put(item), self._loop)
        while not self._stop.is_set():
            try:
                future.result(timeout=0.2)
                return True
            except concurrent.futures.TimeoutError:
                continue
        future.cancel()
        return False

    def _read(self) -> None:
        try:
            with can.LogReader(self.path) as reader:
                for msg in reader:
                    if not self._put(msg):
                        return
        except Exception as e:
            logger.error(f'Could not read {self.path}: {e}')
            self._put(e)
        finally:
            if not self._stop.is_set():
                self._put(_EOF)

    def close(self) -> None:
        self._stop.set()

    async def messages(self) -> AsyncIterator[can.Message]:
        loop = asyncio.get_running_loop()
        first_timestamp = start = None
        while True:
            item = await self._queue.get()
            if item is _EOF:
                return
            if isinstance(item, BaseException):
                raise SourceError(f'Could not read {self.path.name}: {item}') from item
            if self.realtime:
                if first_timestamp is None:
                    first_timestamp, start = item.timestamp, loop.time()
                else:
                    delay = (item.timestamp - first_timestamp) - (loop.time() - start)
                    if delay > 0:
                        await asyncio.sleep(delay)
            yield item

    def send(self, msg: can.Message) -> None:
        raise SourceError('Cannot transmit on a replayed log file')
