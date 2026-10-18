import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtQml import qmlEngine

from can_explorer.core.frames import parse_can_id, parse_hex_bytes
from can_explorer.scripting import ScriptError, ScriptRuntime
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_CONSOLE_LINES = 1_000
PANEL_REFRESH_INTERVAL_MS = 50


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def _format(value: Any, unit: str = '') -> str:
    if value is None:
        return '–'
    text = f'{value:g}' if isinstance(value, float) else str(value)
    return f'{text} {unit}' if unit else text


class Watch(QObject):
    """Live value of a signal or variable for QML bindings: `panel.signal('EngineData.EngineSpeed').value`."""

    changed = pyqtSignal()

    def __init__(self, key: str, unit: str = '', parent: Optional[QObject] = None):
        super().__init__(parent)
        self._key = key
        self._unit = unit
        self._value = None
        self._dirty = False

    def set_value(self, value: Any) -> None:
        if value != self._value:
            self._value = value
            self._dirty = True

    def flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.changed.emit()

    @pyqtProperty(str, constant=True)
    def key(self) -> str:
        return self._key

    @pyqtProperty(str, constant=True)
    def unit(self) -> str:
        return self._unit

    @pyqtProperty('QVariant', notify=changed)
    def value(self):
        return self._value

    @pyqtProperty(float, notify=changed)
    def number(self) -> float:
        """The value as a number (0 when unknown or not numeric), convenient for gauges and sliders."""
        try:
            return float(self._value)
        except (TypeError, ValueError):
            return 0.0

    @pyqtProperty(str, notify=changed)
    def text(self) -> str:
        return _format(self._value, self._unit)


class PanelBridge(QObject):
    """The `panel` object of QML panels: live signal values, shared variables, sending frames and logging."""

    def __init__(self, controller: 'SimulationController'):
        super().__init__(controller)
        self._controller = controller
        self._signals: Dict[str, Watch] = {}
        self._variables: Dict[str, Watch] = {}
        self._watched_messages: Dict[str, List[str]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(PANEL_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    @property
    def runtime(self) -> ScriptRuntime:
        return self._controller.runtime

    def _flush(self) -> None:
        for watch in (*self._signals.values(), *self._variables.values()):
            watch.flush()

    def on_message(self, msg: can.Message) -> None:
        """Update the watched signals carried by a frame."""
        if not self._watched_messages:
            return
        definition = self.runtime.definition_for(msg)
        if definition is None or definition.name not in self._watched_messages:
            return
        values = self.runtime.decode(msg, numeric=False) or {}
        for signal in self._watched_messages[definition.name]:
            if signal in values:
                self._signals[f'{definition.name}.{signal}'].set_value(values[signal])

    def on_variable(self, name: str, value: Any) -> None:
        if name in self._variables:
            self._variables[name].set_value(value)

    @pyqtSlot(str, result=QObject)
    def signal(self, key: str) -> Watch:
        """Watch of a database signal, given as 'Message.Signal'."""
        if key not in self._signals:
            message, _, signal = key.partition('.')
            unit = ''
            database = self.runtime.database
            if database is not None:
                unit = next((info.unit for info in database.signals() if info.key == key), '')
            self._signals[key] = Watch(key, unit, parent=self)
            if signal:
                self._watched_messages.setdefault(message, []).append(signal)
        return self._signals[key]

    @pyqtSlot(str, result=QObject)
    def variable(self, name: str) -> Watch:
        if name not in self._variables:
            watch = Watch(name, parent=self)
            watch.set_value(self.runtime.variables.get(name))
            self._variables[name] = watch
        return self._variables[name]

    @pyqtSlot(str, 'QVariant')
    def set_variable(self, name: str, value: Any) -> None:
        self.runtime.variables.set(name, value)

    @pyqtSlot(str, 'QVariantMap', result=str)
    def send(self, message: str, values: Dict[str, Any]) -> str:
        """Send a database message with the given signal values. Returns an error, or ''."""
        try:
            buffer = self.runtime.message(message).update(values or {})
            frame = can.Message(
                arbitration_id=buffer.id,
                is_extended_id=buffer.is_extended,
                is_fd=buffer.is_fd,
                bitrate_switch=buffer.is_fd,
                data=buffer.encode(),
            )
            self._controller.connection.session.send(frame)
        except Exception as e:
            self._controller.log_line('error', 'panel', str(e))
            return str(e)
        return ''

    @pyqtSlot(str, str, result=str)
    def send_raw(self, can_id: str, data: str) -> str:
        """Send raw hex data, e.g. send_raw('123', '01 02'). Returns an error, or ''."""
        try:
            frame_id = parse_can_id(can_id, extended=True)
            frame = can.Message(arbitration_id=frame_id, is_extended_id=frame_id > 0x7FF, data=parse_hex_bytes(data))
            self._controller.connection.session.send(frame)
        except Exception as e:
            self._controller.log_line('error', 'panel', str(e))
            return str(e)
        return ''

    @pyqtSlot(str)
    def log(self, text: str) -> None:
        self._controller.log_line('info', 'panel', text)


class SimulationController(QObject):
    """Simulation scripts, QML panels, shared variables and the console of a connection."""

    scripts_changed = pyqtSignal()
    panels_changed = pyqtSignal()
    state_changed = pyqtSignal()
    console_changed = pyqtSignal()
    variables_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._console: List[Dict[str, str]] = []
        self._panels: List[str] = []
        self.runtime = ScriptRuntime(connection.session, lambda: connection.session.decoder.database, self.log_line)
        self._panel = PanelBridge(self)
        self.runtime.add_variable_listener(self._on_variable)
        connection.session.add_message_listener(self._panel.on_message)
        connection.session.add_state_listener(self._on_session_state)

    # Console

    def log_line(self, level: str, source: str, text: str) -> None:
        self._console.append({'time': time.strftime('%H:%M:%S'), 'level': level, 'source': source, 'text': text})
        del self._console[:-MAX_CONSOLE_LINES]
        self.console_changed.emit()

    @pyqtProperty('QVariantList', notify=console_changed)
    def console(self) -> List[Dict[str, str]]:
        return list(self._console)

    @pyqtSlot()
    def clear_console(self):
        self._console.clear()
        self.console_changed.emit()

    # Scripts

    @pyqtProperty('QVariantList', notify=scripts_changed)
    def scripts(self) -> List[Dict[str, Any]]:
        return [
            {
                'path': str(script.path),
                'name': script.path.name,
                'nodes': ', '.join(node.name for node in script.nodes),
                'error': script.error,
            }
            for script in self.runtime.scripts
        ]

    @pyqtSlot(QUrl, result=str)
    def load_script(self, url: QUrl) -> str:
        script = self.runtime.load(_local_path(url))
        self.scripts_changed.emit()
        return script.error

    @pyqtSlot(int, result=str)
    def reload_script(self, index: int) -> str:
        if not 0 <= index < len(self.runtime.scripts):
            return ''
        script = self.runtime.load(self.runtime.scripts[index].path)
        self.scripts_changed.emit()
        return script.error

    @pyqtSlot(int)
    def remove_script(self, index: int):
        if 0 <= index < len(self.runtime.scripts):
            self.runtime.unload(self.runtime.scripts[index].path)
            self.scripts_changed.emit()

    # Running

    @pyqtProperty(bool, notify=state_changed)
    def running(self) -> bool:
        return self.runtime.running

    @pyqtSlot(result=str)
    def start(self) -> str:
        try:
            self.runtime.start()
        except ScriptError as e:
            return str(e)
        finally:
            self.state_changed.emit()
        return ''

    @pyqtSlot()
    def stop(self):
        self.runtime.stop()
        self.state_changed.emit()

    def _on_session_state(self, session) -> None:
        if not session.is_open and self.runtime.running:
            self.stop()

    # Variables

    def _on_variable(self, name: str, value: Any) -> None:
        self._panel.on_variable(name, value)
        self.variables_changed.emit()

    @pyqtProperty('QVariantList', notify=variables_changed)
    def variables(self) -> List[Dict[str, str]]:
        return [{'name': name, 'value': _format(value)} for name, value in sorted(self.runtime.variables.items())]

    @pyqtSlot(str, str, result=str)
    def set_variable(self, name: str, text: str) -> str:
        """Set a variable from text: numbers (decimal or 0x hex), true/false, or a string."""
        name = name.strip()
        if not name:
            return 'Enter a variable name'
        text = text.strip()
        value: Any
        if text.lower() in ('true', 'false'):
            value = text.lower() == 'true'
        else:
            try:
                value = int(text, 0)
            except ValueError:
                try:
                    value = float(text)
                except ValueError:
                    value = text
        self.runtime.variables.set(name, value)
        return ''

    # Panels

    @pyqtProperty(QObject, constant=True)
    def panel(self) -> PanelBridge:
        return self._panel

    @pyqtProperty('QVariantList', notify=panels_changed)
    def panels(self) -> List[Dict[str, str]]:
        return [{'url': QUrl.fromLocalFile(path).toString(), 'name': Path(path).stem} for path in self._panels]

    @pyqtSlot(QUrl, result=str)
    def add_panel(self, url: QUrl) -> str:
        path = _local_path(url)
        if path.suffix.lower() != '.qml' or not path.is_file():
            return f'Not a QML file: {path.name}'
        if str(path) not in self._panels:
            self._panels.append(str(path))
            self.panels_changed.emit()
        return ''

    @pyqtSlot(int)
    def remove_panel(self, index: int):
        if 0 <= index < len(self._panels):
            del self._panels[index]
            self.panels_changed.emit()

    @pyqtSlot(QObject)
    def reload_panels(self, view: QObject):
        """Reload the panels from disk; `view` is any QML item of the engine showing them."""
        engine = qmlEngine(view)
        if engine is not None:
            engine.clearComponentCache()  # Otherwise the engine keeps serving the previously compiled files
        self.panels_changed.emit()

    @pyqtSlot(str, str)
    def panel_error(self, name: str, error: str):
        self.log_line('error', f'panel {name}', error)

    def shutdown(self):
        self.runtime.stop()
