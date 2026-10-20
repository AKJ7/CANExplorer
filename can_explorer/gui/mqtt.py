from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.mqtt import MqttBridge, MqttSettings
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

STATISTICS_INTERVAL_MS = 1000
DEFAULT_OPTIONS: Dict[str, Any] = {
    'broker': 'mqtt://localhost:1883',
    'username': '',
    'prefix': 'canexplorer',
    'signals': True,
    'frames': False,
    'retain': False,
    'interval': 0.1,
    'allow_send': False,
    'allowlist': '',
}


def _allowlist(text: str) -> List[str]:
    return [item for item in text.replace(',', ' ').split() if item]


class MqttController(QObject):
    """Publishes a connection to an MQTT broker (see can_explorer.core.mqtt). While enabled, the bridge runs
    whenever the connection is connected."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._bridge: Optional[MqttBridge] = None
        self._settings: Optional[MqttSettings] = None
        self._timer = QTimer(self)
        self._timer.setInterval(STATISTICS_INTERVAL_MS)
        self._timer.timeout.connect(self.changed.emit)
        connection.session.add_message_listener(self._on_message)
        connection.session.add_state_listener(self._on_session_state)

    def _app_state(self):
        return getattr(self.connection.parent(), 'app_state', None)

    @pyqtProperty('QVariantMap', notify=changed)
    def options(self) -> Dict[str, Any]:
        """The options last used (the password is not stored)."""
        state = self._app_state()
        return {**DEFAULT_OPTIONS, **(state.mqtt_options() if state is not None else {})}

    @pyqtProperty(bool, notify=changed)
    def enabled(self) -> bool:
        return self._settings is not None

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        if self._settings is None:
            return ''
        if self._bridge is None:
            return 'waiting for the connection'
        return self._bridge.state

    @pyqtProperty(str, notify=changed)
    def topic(self) -> str:
        return self._bridge.base if self._bridge is not None else ''

    @pyqtProperty(str, notify=changed)
    def statistics(self) -> str:
        bridge = self._bridge
        if bridge is None:
            return ''
        text = f'{bridge.published} published'
        if bridge.settings.allow_send:
            text += f', {bridge.sent} sent, {bridge.rejected} refused'
        if bridge.dropped:
            text += f', {bridge.dropped} frames dropped'
        return text

    @pyqtSlot('QVariantMap', result=str)
    def start(self, options: Dict[str, Any]) -> str:
        """Start publishing with the options of the MQTT dialog. Returns an error, or ''."""
        options = {**DEFAULT_OPTIONS, **options}
        allowlist = _allowlist(str(options.get('allowlist', '')))
        if options.get('allow_send') and not allowlist:
            return 'Name the messages or identifiers that may be sent (or * for all)'
        try:
            settings = MqttSettings.from_url(
                str(options['broker']).strip(),
                signals=bool(options['signals']),
                frames=bool(options['frames']),
                retain=bool(options['retain']),
                interval=max(0.01, float(options['interval'])),
                allow_send=bool(options['allow_send']),
                send_allowlist=allowlist,
            )
        except (ValueError, TypeError) as e:
            return str(e)
        if options.get('username'):
            settings.username = str(options['username'])
        if options.get('password'):
            settings.password = str(options['password'])
        if str(options.get('prefix', '')).strip('/ '):
            settings.prefix = str(options['prefix']).strip('/ ')
        state = self._app_state()
        if state is not None:
            state.set_mqtt_options(options)
        self.stop()
        self._settings = settings
        if self.connection.session.is_open:
            self._start_bridge()
        self.changed.emit()
        return ''

    @pyqtSlot()
    def stop(self) -> None:
        self._settings = None
        self._stop_bridge()
        self.changed.emit()

    def _start_bridge(self) -> None:
        session = self.connection.session
        assert self._settings is not None
        self._bridge = MqttBridge(
            self._settings,
            self.connection.name,
            lambda: session.decoder.database,
            send=session.send if session.source.can_send else None,
            on_change=self.changed.emit,
        )
        self._bridge.start()
        self._timer.start()

    def _stop_bridge(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        self._timer.stop()

    def _on_message(self, msg) -> None:
        if self._bridge is not None:
            self._bridge.on_message(msg)

    def _on_session_state(self, session) -> None:
        if session.is_open and self._settings is not None and self._bridge is None:
            self._start_bridge()
        elif not session.is_open and self._bridge is not None:
            self._stop_bridge()
        self.changed.emit()

    def shutdown(self) -> None:
        self.stop()
