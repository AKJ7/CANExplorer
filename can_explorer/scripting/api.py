"""The API available to simulation scripts: Node, message buffers, timers and handler decorators."""

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

import can

if TYPE_CHECKING:
    from can_explorer.scripting.runtime import ScriptRuntime

Handler = Callable[..., Any]
MessageSelector = int | str


class ScriptError(Exception):
    """Errors caused by a script, e.g. sending an unknown message."""


# Handler decorators. They only mark the functions; the runtime collects them when a node is created.


def _mark(function: Handler, kind: str, value: Any = None) -> Handler:
    marks = getattr(function, '_can_explorer_handlers', [])
    marks.append((kind, value))
    function._can_explorer_handlers = marks
    return function


def on_start(function: Handler) -> Handler:
    """Called when the simulation starts."""
    return _mark(function, 'start')


def on_stop(function: Handler) -> Handler:
    """Called when the simulation stops."""
    return _mark(function, 'stop')


def on_message(*selectors: MessageSelector) -> Callable[[Handler], Handler]:
    """Called with a ReceivedMessage for frames with the given identifiers or database message names.

    Without selectors the handler receives every frame. A node does not receive the frames it sends itself.
    """

    def decorate(function: Handler) -> Handler:
        return _mark(function, 'message', tuple(selectors))

    return decorate


def on_signal(message: str, signal: str) -> Callable[[Handler], Handler]:
    """Called with (value, message) when the decoded value of a signal changes (and when first received)."""

    def decorate(function: Handler) -> Handler:
        return _mark(function, 'signal', (message, signal))

    return decorate


def on_variable(name: str) -> Callable[[Handler], Handler]:
    """Called with the new value when a shared variable changes (set by a node or a panel)."""

    def decorate(function: Handler) -> Handler:
        return _mark(function, 'variable', name)

    return decorate


class ReceivedMessage:
    """A frame delivered to a node. `signals` decodes it with the signal database on first access."""

    def __init__(self, msg: can.Message, runtime: 'ScriptRuntime'):
        self.raw = msg
        self._runtime = runtime
        self._definition = runtime.definition_for(msg)
        self._signals: Optional[Dict[str, Any]] = None

    @property
    def id(self) -> int:
        return self.raw.arbitration_id

    @property
    def data(self) -> bytes:
        return bytes(self.raw.data)

    @property
    def dlc(self) -> int:
        return self.raw.dlc

    @property
    def is_extended(self) -> bool:
        return self.raw.is_extended_id

    @property
    def is_fd(self) -> bool:
        return self.raw.is_fd

    @property
    def is_remote(self) -> bool:
        return self.raw.is_remote_frame

    @property
    def timestamp(self) -> float:
        return self.raw.timestamp

    @property
    def name(self) -> Optional[str]:
        """Message name from the signal database, None when unknown."""
        return self._definition.name if self._definition is not None else None

    @property
    def signals(self) -> Dict[str, Any]:
        """Physical signal values (numbers; use `labels` for the text of value tables)."""
        if self._signals is None:
            self._signals = self._runtime.decode(self.raw, numeric=True) or {}
        return self._signals

    @property
    def labels(self) -> Dict[str, Any]:
        """Signal values with value table texts where defined."""
        return self._runtime.decode(self.raw, numeric=False) or {}

    def __repr__(self) -> str:
        return f'<ReceivedMessage {self.name or hex(self.id)} {self.data.hex(" ")}>'


class MessageBuffer:
    """Signal values of a database message, sent with Node.send(). Missing values start at their initial value.

    Signals are accessed as items (buffer['EngineSpeed']) or attributes (buffer.EngineSpeed).
    """

    def __init__(self, definition, frame_id: int, extended: bool, fd: bool):
        object.__setattr__(self, '_definition', definition)
        object.__setattr__(self, 'id', frame_id)
        object.__setattr__(self, 'is_extended', extended)
        object.__setattr__(self, 'is_fd', fd)
        object.__setattr__(self, 'name', definition.name)
        object.__setattr__(self, '_values', dict(_initial_values(definition)))

    @property
    def signal_names(self) -> List[str]:
        return list(self._values)

    def __getitem__(self, signal: str):
        try:
            return self._values[signal]
        except KeyError:
            raise ScriptError(f'Message {self.name!r} has no signal {signal!r}') from None

    def __setitem__(self, signal: str, value) -> None:
        if signal not in self._values:
            raise ScriptError(f'Message {self.name!r} has no signal {signal!r}')
        self._values[signal] = value

    def __getattr__(self, signal: str):
        if signal.startswith('_'):
            raise AttributeError(signal)
        return self[signal]

    def __setattr__(self, signal: str, value) -> None:
        self[signal] = value

    def update(self, values: Dict[str, Any] = None, **signals) -> 'MessageBuffer':
        for signal, value in {**(values or {}), **signals}.items():
            self[signal] = value
        return self

    def encode(self) -> bytes:
        try:
            if hasattr(self._definition, 'signals') and hasattr(self._definition, 'frame_id'):
                return self._definition.encode(self._values, strict=False)  # cantools message
            return bytes(self._definition.encode(self._values))  # LDF frame
        except Exception as e:
            raise ScriptError(f'Cannot encode {self.name!r}: {e}') from e

    def __repr__(self) -> str:
        return f'<MessageBuffer {self.name} {self._values}>'


def _initial_values(definition) -> Iterable[Tuple[str, Any]]:
    if hasattr(definition, 'signals') and hasattr(definition, 'frame_id'):  # cantools
        for signal in definition.signals:
            initial = signal.initial
            if initial is None:
                initial = signal.minimum if signal.minimum is not None and signal.minimum > 0 else 0
            yield signal.name, initial
    else:  # LDF frame
        ldf = definition._ldf
        for name in definition.signal_names:
            yield name, ldf.get_signal(name).init_value


class Timer:
    """A one-shot or periodic timer created with Node.after() or Node.every()."""

    def __init__(self, node: 'Node', period: float, callback: Handler, args: tuple, repeat: bool):
        if period <= 0:
            raise ScriptError(f'Timer period must be positive, got {period}')
        self.period = period
        self._node = node
        self._callback = callback
        self._args = args
        self._repeat = repeat
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f'{node.name} timer')

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            deadline += self.period
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            self._node._runtime.call(self._node, self._callback, *self._args)
            if not self._repeat:
                return

    @property
    def running(self) -> bool:
        return not self._task.done()

    def cancel(self) -> None:
        self._task.cancel()


class Node:
    """Base class of simulated bus nodes. Subclass it in a simulation script.

    Attributes available in handlers: `name`, `variables` (shared with other nodes and panels) and
    `time` (seconds since the simulation started).
    """

    name: str = ''

    def __init__(self):
        if not self.name:
            self.name = type(self).__name__
        self._runtime: Optional['ScriptRuntime'] = None
        self._timers: List[Timer] = []

    # Wiring by the runtime

    def _attach(self, runtime: 'ScriptRuntime') -> None:
        self._runtime = runtime

    def _handlers(self, kind: str) -> List[Tuple[Handler, Any]]:
        handlers = []
        for attribute in dir(type(self)):
            function = getattr(type(self), attribute, None)
            for marked_kind, value in getattr(function, '_can_explorer_handlers', ()):
                if marked_kind == kind:
                    handlers.append((getattr(self, attribute), value))
        return handlers

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def _require_runtime(self) -> 'ScriptRuntime':
        if self._runtime is None or not self._runtime.running:
            raise ScriptError('The simulation is not running')
        return self._runtime

    # API

    @property
    def variables(self):
        """Shared variables (`Variables`) of the simulation, also visible to the other nodes and to panels."""
        return self._runtime.variables

    @property
    def time(self) -> float:
        """Seconds since the simulation started."""
        return self._require_runtime().elapsed

    def message(self, name: str) -> MessageBuffer:
        """Create a buffer with the signals of a database message, to fill and send.

        Args:
            name: Message (or LIN frame) name in the loaded signal database.

        Raises:
            ScriptError: No database is loaded, or it has no such message.
        """
        return self._require_runtime().message(name)

    def send(
        self,
        message: MessageBuffer | str | int,
        data: bytes | Iterable[int] | None = None,
        *,
        extended: Optional[bool] = None,
        fd: bool = False,
        remote: bool = False,
        **signals: Any,
    ) -> None:
        """Send a frame.

        Examples:
            ```python
            self.send(buffer)                          # A MessageBuffer from message()
            self.send('EngineData', EngineSpeed=1000)  # Other signals use their initial value
            self.send(0x123, b'\\x01\\x02')              # Raw data
            self.send(0x10, remote=True)               # Remote frame (a header on LIN)
            ```

        Args:
            message: A MessageBuffer, a database message name or a frame identifier.
            data: Raw data bytes when `message` is an identifier.
            extended: Extended identifier; defaults to identifiers above 0x7FF.
            fd: Send a CAN FD frame (with bitrate switch).
            remote: Send a remote frame.
            **signals: Signal values when `message` is a database message name.

        Raises:
            ScriptError: The simulation is not running, the message is unknown or cannot be encoded, or the
                frame cannot be sent.
        """
        runtime = self._require_runtime()
        if isinstance(message, str):
            message = runtime.message(message).update(signals)
        elif signals:
            raise ScriptError('Signal values can only be given with a database message name')
        if isinstance(message, MessageBuffer):
            msg = can.Message(
                arbitration_id=message.id,
                is_extended_id=message.is_extended,
                is_fd=message.is_fd,
                bitrate_switch=message.is_fd,
                data=message.encode(),
            )
        else:
            payload = bytes(data or b'')
            msg = can.Message(
                arbitration_id=int(message),
                is_extended_id=int(message) > 0x7FF if extended is None else extended,
                is_fd=fd,
                bitrate_switch=fd,
                is_remote_frame=remote,
                dlc=8 if remote and not payload else None,
                data=payload,
            )
        runtime.send(self, msg)

    def every(self, period: float, callback: Handler, *args: Any) -> Timer:
        """Call `callback(*args)` periodically until the simulation stops or the timer is cancelled.

        Args:
            period: Period in seconds. The schedule does not drift.
            callback: Function or method to call.
            *args: Arguments passed to the callback.
        """
        self._require_runtime()
        timer = Timer(self, period, callback, args, repeat=True)
        self._timers.append(timer)
        return timer

    def after(self, delay: float, callback: Handler, *args: Any) -> Timer:
        """Call `callback(*args)` once.

        Args:
            delay: Delay in seconds.
            callback: Function or method to call.
            *args: Arguments passed to the callback.
        """
        self._require_runtime()
        timer = Timer(self, delay, callback, args, repeat=False)
        self._timers.append(timer)
        return timer

    def log(self, *values) -> None:
        """Write the values, separated by spaces, to the simulation console."""
        if self._runtime is not None:
            self._runtime.log(self.name, ' '.join(str(value) for value in values))

    def __repr__(self) -> str:
        return f'<Node {self.name}>'
