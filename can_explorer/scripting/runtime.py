"""Runs simulation scripts on a BusSession."""

import sys
import time
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.session import BusSession, SessionError
from can_explorer.scripting.api import MessageBuffer, Node, ReceivedMessage, ScriptError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

LogCallback = Callable[[str, str, str], None]  # (level, source, text)


class Variables:
    """Shared variables of a simulation (like CANoe system variables): set by nodes, panels or the user."""

    def __init__(self, on_change: Callable[[str, Any], None]):
        self._values: Dict[str, Any] = {}
        self._on_change = on_change

    def get(self, name: str, default: Any = None) -> Any:
        """Value of a variable, or `default` when it was never set."""
        return self._values.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __setitem__(self, name: str, value: Any) -> None:
        self.set(name, value)

    def __contains__(self, name: str) -> bool:
        return name in self._values

    def set(self, name: str, value: Any) -> None:
        """Set a variable; `@on_variable` handlers and panels are notified when the value changes."""
        changed = name not in self._values or self._values[name] != value
        self._values[name] = value
        if changed:
            self._on_change(name, value)

    def items(self):
        return self._values.items()

    def clear(self) -> None:
        self._values.clear()


@dataclass
class LoadedScript:
    path: Path
    nodes: List[Node] = field(default_factory=list)
    error: str = ''


def message_buffer(database, name: str) -> MessageBuffer:
    """The signal values of a database message, to send it by name."""
    if database is None:
        raise ScriptError(f'Load a signal database to send message {name!r} by name')
    if hasattr(database, 'messages'):  # cantools based databases (DBC, DBF, ARXML, KCD, SYM)
        definition = next((message for message in database.messages() if message.name == name), None)
        if definition is not None:
            if definition.is_container:
                raise ScriptError(f'Container message {name!r} cannot be sent by name')
            return MessageBuffer(definition, definition.frame_id, definition.is_extended_frame, definition.is_fd)
    elif hasattr(database, 'frame'):  # LDF
        definition = database.frame(name)
        if definition is not None:
            return MessageBuffer(definition, definition.frame_id, False, False)
    raise ScriptError(f'Unknown message {name!r} in {database.path.name}')


class ScriptRuntime:
    """Loads simulation scripts, starts and stops their nodes and dispatches bus events to them.

    Exceptions raised by script code are caught and logged, so one faulty handler does not stop the simulation.
    """

    def __init__(
        self,
        session: BusSession,
        database: Callable[[], Any],
        log: Optional[LogCallback] = None,
        buses: Optional[Callable[[], Dict[str, Tuple[BusSession, Callable[[], Any]]]]] = None,
    ):
        self.session = session
        self._database = database
        # Other connections by name, with their database, for nodes using several buses (e.g. gateways)
        self._buses = buses or (lambda: {})
        self._bus_listeners: Dict[str, Tuple[BusSession, Callable[[can.Message], None]]] = {}
        self._log = log or (lambda level, source, text: logger.info(f'[{source}] {text}'))
        self.scripts: List[LoadedScript] = []
        self.variables = Variables(self._variable_changed)
        self.running = False
        self._start_time = 0.0
        self._sending: Optional[Node] = None
        self._signal_values: Dict[Tuple[int, str, str], Any] = {}
        self._variable_listeners: List[Callable[[str, Any], None]] = []
        self.errors = 0

    # Logging and calling into scripts

    def log(self, source: str, text: str, level: str = 'info') -> None:
        self._log(level, source, text)

    def call(self, node: Optional[Node], function: Callable, *args) -> Any:
        try:
            return function(*args)
        except Exception as e:
            self.errors += 1
            source = node.name if node is not None else 'simulation'
            details = ''.join(traceback.format_exception(type(e), e, e.__traceback__)[-2:]).strip()
            self.log(source, f'{type(e).__name__}: {e}\n{details}', level='error')
            return None

    @property
    def nodes(self) -> List[Node]:
        return [node for script in self.scripts for node in script.nodes]

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_time if self.running else 0.0

    # Scripts

    def load(self, path: str | Path) -> LoadedScript:
        """Load (or reload) a script. Errors are reported in LoadedScript.error and the log."""
        path = Path(path).resolve()
        existing = next((script for script in self.scripts if script.path == path), None)
        position = self.scripts.index(existing) if existing is not None else len(self.scripts)
        if existing is not None:
            self.unload(path)
        script = LoadedScript(path)
        try:
            # Compiled from source, not through the bytecode cache: it could serve stale code when a script is
            # edited and reloaded within the same second
            module_name = f'can_explorer_script_{abs(hash(path))}'
            module = types.ModuleType(module_name)
            module.__file__ = str(path)
            sys.modules[module_name] = module  # Needed by e.g. dataclasses defined in the script
            exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
            classes = [
                value
                for value in vars(module).values()
                if isinstance(value, type)
                and issubclass(value, Node)
                and value is not Node
                and value.__module__ == module_name
            ]
            if not classes:
                raise ScriptError(f'{path.name} defines no Node subclass')
            for node_class in classes:
                node = node_class()
                node._attach(self)
                script.nodes.append(node)
        except Exception as e:
            script.nodes.clear()
            script.error = f'{type(e).__name__}: {e}'
            self.log(path.name, f'Could not load: {script.error}', level='error')
        else:
            self.log(path.name, f'Loaded nodes {", ".join(node.name for node in script.nodes)}')
        self.scripts.insert(position, script)
        if self.running:
            self._update_bus_listeners()
            for node in script.nodes:
                self._start_node(node)
        return script

    def unload(self, path: str | Path) -> None:
        path = Path(path).resolve()
        for script in [script for script in self.scripts if script.path == path]:
            if self.running:
                for node in script.nodes:
                    self._stop_node(node)
            self.scripts.remove(script)

    # Running

    def start(self) -> None:
        if self.running:
            return
        if not self.session.is_open:
            raise ScriptError('Connect before starting the simulation')
        self.running = True
        self._start_time = time.monotonic()
        self._signal_values.clear()
        self.session.add_message_listener(self._on_message)
        try:
            self._update_bus_listeners()
        except ScriptError:
            self.session.remove_message_listener(self._on_message)
            self.running = False
            raise
        self.log('simulation', 'Started')
        for node in self.nodes:
            self._start_node(node)

    def stop(self) -> None:
        if not self.running:
            return
        for node in self.nodes:
            self._stop_node(node)
        self.session.remove_message_listener(self._on_message)
        for session, listener in self._bus_listeners.values():
            session.remove_message_listener(listener)
        self._bus_listeners.clear()
        self.running = False
        self.log('simulation', 'Stopped')

    def _start_node(self, node: Node) -> None:
        for handler, _ in node._handlers('start'):
            self.call(node, handler)

    def _stop_node(self, node: Node) -> None:
        node._cancel_timers()
        for handler, _ in node._handlers('stop'):
            self.call(node, handler)

    # Buses

    def _own(self, bus: Optional[str]) -> bool:
        return not bus

    def _bus(self, bus: Optional[str]) -> Tuple[BusSession, Callable[[], Any]]:
        """Session and database of a bus: the own connection (None) or another connection by name."""
        if self._own(bus):
            return self.session, self._database
        buses = self._buses()
        if bus not in buses:
            names = ', '.join(sorted(buses)) or 'none'
            raise ScriptError(f'Unknown bus {bus!r}; the other connections are: {names}')
        return buses[bus]

    def _used_buses(self) -> set:
        used = set()
        for node in self.nodes:
            used |= {value[1] for _, value in node._handlers('message')}
            used |= {value[2] for _, value in node._handlers('signal')}
        return {bus for bus in used if not self._own(bus)}

    def _update_bus_listeners(self) -> None:
        """Receive the frames of the other buses that handlers ask for; check the buses gateways send to."""
        for node in self.nodes:
            for bus in getattr(node, 'buses', ()):
                if not self._own(bus):
                    self._bus(bus)
        for bus in self._used_buses() - set(self._bus_listeners):
            session, _ = self._bus(bus)

            def listener(msg: can.Message, bus=bus) -> None:
                self._dispatch(msg, bus)

            session.add_message_listener(listener)
            self._bus_listeners[bus] = (session, listener)

    # Database access

    @property
    def database(self):
        return self._database()

    def definition_for(self, msg: can.Message, bus: Optional[str] = None):
        database = self._bus(bus)[1]()
        if database is None or msg.is_remote_frame or msg.is_error_frame:
            return None
        return database.message_definition(msg)

    def decode(self, msg: can.Message, numeric: bool = True, bus: Optional[str] = None) -> Optional[Dict[str, Any]]:
        database = self._bus(bus)[1]()
        if database is None:
            return None
        try:
            return database.decode(msg, decode_choices=not numeric)
        except Exception:
            return None

    def message(self, name: str, bus: Optional[str] = None) -> MessageBuffer:
        return message_buffer(self._bus(bus)[1](), name)

    # Events

    def send(self, node: Node, msg: can.Message, bus: Optional[str] = None) -> None:
        session = self._bus(bus)[0]
        self._sending = node
        try:
            session.send(msg)
        except SessionError as e:
            raise ScriptError(str(e)) from e
        finally:
            self._sending = None

    @staticmethod
    def _matches(selectors: tuple, msg: can.Message, name: Optional[str]) -> bool:
        if not selectors:
            return True
        return any(
            selector == msg.arbitration_id if isinstance(selector, int) else selector == name for selector in selectors
        )

    def _on_message(self, msg: can.Message) -> None:
        self._dispatch(msg, None)

    def _dispatch(self, msg: can.Message, bus: Optional[str]) -> None:
        sender = self._sending
        received = None
        for node in self.nodes:
            if node is sender:
                continue  # Nodes do not receive their own frames
            for handler, (selectors, handler_bus) in node._handlers('message'):
                if (handler_bus or None) != bus:
                    continue
                if received is None:
                    received = ReceivedMessage(msg, self, bus)
                if self._matches(selectors, msg, received.name):
                    self.call(node, handler, received)
            for handler, (message, signal, handler_bus) in node._handlers('signal'):
                if (handler_bus or None) != bus:
                    continue
                if received is None:
                    received = ReceivedMessage(msg, self, bus)
                if received.name != message or signal not in received.signals:
                    continue
                key = (id(node), bus, message, signal)
                value = received.signals[signal]
                if key not in self._signal_values or self._signal_values[key] != value:
                    self._signal_values[key] = value
                    self.call(node, handler, value, received)

    def add_variable_listener(self, callback: Callable[[str, Any], None]) -> None:
        self._variable_listeners.append(callback)

    def _variable_changed(self, name: str, value: Any) -> None:
        for callback in self._variable_listeners:
            callback(name, value)
        if not self.running:
            return
        for node in self.nodes:
            for handler, variable in node._handlers('variable'):
                if variable == name:
                    self.call(node, handler, value)
