"""The SOME/IP tool for QML: listens for SOME/IP over UDP, tracks the services announced by Service Discovery and
shows the decoded messages. It owns a SomeIpMonitor running on the event loop."""

import asyncio
import json
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QSettings, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.someip import DEFAULT_SD_MULTICAST, DEFAULT_SD_PORT, SomeIpMonitor, describe
from can_explorer.core.someip_arxml import ArxmlError, load_arxml
from can_explorer.core.someip_e2e import (
    PROFILES,
    SomeIpE2eChecker,
    SomeIpE2eConfig,
    SomeIpE2eError,
    load_configs,
    save_configs,
)
from can_explorer.core.someip_fibex import FibexError, load_fibex
from can_explorer.core.someip_serialization import SomeIpDescription
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

E2E_SETTINGS_KEY = 'someip/e2e'  # The E2E protections, as JSON

REFRESH_INTERVAL_MS = 250
MAX_ROWS = 1000  # Messages shown in the table (the monitor keeps more)


def _format(value) -> str:
    if isinstance(value, float):
        return f'{value:g}'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _parse_ports(text: str) -> List[int]:
    ports = []
    for token in text.replace(',', ' ').split():
        try:
            port = int(token)
        except ValueError as e:
            raise ValueError(f'Invalid port {token!r}') from e
        if not 1 <= port <= 65535:
            raise ValueError(f'Port {port} is out of range (1–65535)')
        ports.append(port)
    if not ports:
        raise ValueError('Enter at least one UDP port')
    return ports


class SomeIpController(QObject):
    """SOME/IP over Ethernet: service discovery and decoded messages."""

    changed = pyqtSignal()  # Open, closed or error
    data_changed = pyqtSignal()  # Services or messages
    e2e_changed = pyqtSignal()  # The E2E protections

    def __init__(self, settings: Optional[QSettings] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings
        self._e2e: List[SomeIpE2eConfig] = self._load_e2e()
        self._monitor: Optional[SomeIpMonitor] = None
        self._error = ''
        self._ports = str(DEFAULT_SD_PORT)
        self._multicast = DEFAULT_SD_MULTICAST
        self._seen = 0
        self._description: Optional[SomeIpDescription] = None
        self._description_path = ''
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)

    # State

    @pyqtProperty(bool, notify=changed)
    def is_open(self) -> bool:
        return self._monitor is not None and self._monitor.is_open

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error

    @pyqtProperty(str, constant=True)
    def default_ports(self) -> str:
        return str(DEFAULT_SD_PORT)

    @pyqtProperty(str, constant=True)
    def default_multicast(self) -> str:
        return DEFAULT_SD_MULTICAST

    @pyqtProperty(str, notify=changed)
    def ports(self) -> str:
        return self._ports

    @pyqtProperty(str, notify=changed)
    def multicast(self) -> str:
        return self._multicast

    # Control

    @pyqtSlot(str, str, result=str)
    def open(self, ports_text: str, multicast: str) -> str:
        """Listen on the UDP ports (comma or space separated), joining the SD multicast group. Returns an error or ''."""
        if self.is_open:
            return ''
        try:
            ports = _parse_ports(ports_text)
        except ValueError as e:
            return str(e)
        self._ports, self._multicast = ports_text, multicast
        self._error = ''
        self._monitor = SomeIpMonitor()
        self._monitor.e2e = self.e2e_checker()
        asyncio.ensure_future(self._open(ports, multicast.strip() or None))
        return ''

    async def _open(self, ports: List[int], multicast: Optional[str]) -> None:
        try:
            await self._monitor.open(ports, multicast)
        except OSError as e:
            self._error = f'Could not listen: {e}'
            self._monitor = None
            logger.warning('SOME/IP monitor failed to open: %s', e)
            self.changed.emit()
            return
        self._seen = 0
        self._timer.start()
        self.changed.emit()
        self.data_changed.emit()

    @pyqtSlot()
    def close(self):
        if self._monitor is not None:
            self._monitor.close()
            self._monitor = None
        self._timer.stop()
        self.changed.emit()
        self.data_changed.emit()

    @pyqtSlot()
    def clear(self):
        if self._monitor is not None:
            self._monitor.clear()
        self._seen = 0
        self.data_changed.emit()

    # Payload description (FIBEX)

    @pyqtProperty(str, notify=data_changed)
    def description_file(self) -> str:
        from pathlib import Path

        return Path(self._description_path).name if self._description_path else ''

    @pyqtSlot(QUrl, result=str)
    def load_description(self, url: QUrl) -> str:
        """Load a FIBEX or ARXML file so payloads are decoded into signals. Returns an error or ''."""
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            if path.lower().endswith('.arxml'):
                self._description = load_arxml(path)
            else:
                self._description = load_fibex(path)
        except (FibexError, ArxmlError, OSError) as e:
            return str(e)
        self._description_path = path
        logger.info('Loaded SOME/IP description %s (%d message types)', path, len(self._description))
        self.data_changed.emit()
        return ''

    @pyqtSlot()
    def unload_description(self):
        self._description = None
        self._description_path = ''
        self.data_changed.emit()

    def _signals_text(self, message) -> str:
        if self._description is None:
            return ''
        is_response = (message.message_type & ~0x20) in (0x80, 0x81)
        decoded = self._description.decode(message.service_id, message.method_id, message.payload, is_response)
        if not decoded:
            return ''
        return ', '.join(f'{name}={_format(value)}' for name, value in decoded.items())

    def shutdown(self):
        self.close()

    def _refresh(self):
        if self._monitor is not None and self._monitor.total != self._seen:
            self._seen = self._monitor.total
            self.data_changed.emit()

    # Data

    # E2E protections

    def _load_e2e(self) -> List[SomeIpE2eConfig]:
        if self._settings is None:
            return []
        try:
            data = json.loads(str(self._settings.value(E2E_SETTINGS_KEY, '[]')))
            return [SomeIpE2eConfig.from_dict(entry) for entry in data]
        except (ValueError, SomeIpE2eError) as e:
            logger.warning(f'Ignoring the saved SOME/IP E2E protections: {e}')
            return []

    def _set_e2e(self, configs: List[SomeIpE2eConfig]) -> None:
        SomeIpE2eChecker(configs)  # Raises on duplicates
        self._e2e = sorted(configs, key=lambda config: config.key)
        if self._settings is not None:
            self._settings.setValue(E2E_SETTINGS_KEY, json.dumps([config.as_dict() for config in self._e2e]))
        if self._monitor is not None:
            self._monitor.e2e = self.e2e_checker()  # From now on; messages received before keep their result
        self.e2e_changed.emit()
        self.data_changed.emit()

    def e2e_checker(self) -> Optional[SomeIpE2eChecker]:
        """A checker of the configured protections (with its own counter state), None without protections."""
        return SomeIpE2eChecker(self._e2e) if self._e2e else None

    @pyqtProperty('QVariantList', notify=e2e_changed)
    def e2e_protections(self) -> List[Dict[str, Any]]:
        return [
            {
                'id': f'{config.service_id:04X}.{config.method_id:04X}',
                'name': config.name,
                'profile': config.profile,
                'data_id': f'0x{config.data_id:X}',
                'offset': config.offset,
                'max_delta_counter': config.max_delta_counter,
            }
            for config in self._e2e
        ]

    @pyqtProperty('QVariantList', constant=True)
    def e2e_profiles(self) -> List[int]:
        return list(PROFILES)

    @pyqtSlot('QVariantMap', result=str)
    def add_e2e(self, options: Dict[str, Any]) -> str:
        """Options: service_id, method_id, data_id (hex), profile, offset (bits, default 64), max_delta_counter,
        name. A protection of the same method replaces the old one. Returns an error, or ''."""
        try:
            entry = {key: value for key, value in options.items() if value not in ('', None)}
            for key in ('service_id', 'method_id', 'data_id'):
                text = str(entry.get(key, '')).strip()
                if text and not text.lower().startswith('0x'):
                    entry[key] = '0x' + text  # Ids are typed in hex, as shown
            config = SomeIpE2eConfig.from_dict(entry)
            self._set_e2e([existing for existing in self._e2e if existing.key != config.key] + [config])
        except SomeIpE2eError as e:
            return str(e)
        return ''

    @pyqtSlot(int)
    def remove_e2e(self, index: int):
        if 0 <= index < len(self._e2e):
            self._set_e2e(self._e2e[:index] + self._e2e[index + 1 :])

    @pyqtSlot(QUrl, result=str)
    def import_e2e(self, url: QUrl) -> str:
        """Replace the protections with those of a JSON file. Returns an error, or ''."""
        try:
            self._set_e2e(load_configs(url.toLocalFile() if url.isLocalFile() else url.toString()))
        except SomeIpE2eError as e:
            return str(e)
        return ''

    @pyqtSlot(QUrl, result=str)
    def export_e2e(self, url: QUrl) -> str:
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            save_configs(path, self._e2e)
        except OSError as e:
            return f'Cannot write {path}: {e}'
        return ''

    @pyqtProperty(int, notify=data_changed)
    def message_count(self) -> int:
        return self._monitor.total if self._monitor is not None else 0

    @pyqtProperty('QVariantList', notify=data_changed)
    def services(self) -> List[Dict[str, Any]]:
        if self._monitor is None:
            return []
        rows = []
        for service in sorted(self._monitor.services.values(), key=lambda service: service.key):
            rows.append(
                {
                    'service': f'{service.service_id:04X}',
                    'instance': f'{service.instance_id:04X}',
                    'version': f'{service.major_version}.{service.minor_version}',
                    'endpoints': ', '.join(str(endpoint) for endpoint in service.endpoints.values()),
                    'offers': service.offers,
                }
            )
        return rows

    @pyqtProperty('QVariantList', notify=data_changed)
    def messages(self) -> List[Dict[str, Any]]:
        if self._monitor is None or not self._monitor.messages:
            return []
        start = self._monitor.messages[0].timestamp
        rows = []
        for received in list(self._monitor.messages)[-MAX_ROWS:]:
            message = received.message
            rows.append(
                {
                    'time': f'{received.timestamp - start:.3f}',
                    'source': received.source,
                    'id': f'{message.service_id:04X}.{message.method_id:04X}',
                    'type': message.message_type_name,
                    'length': len(message.payload),
                    'description': (self._signals_text(message) or describe(message))
                    + (f' [{received.e2e}]' if received.e2e else ''),
                    'e2e_failed': any(word in received.e2e for word in ('ERROR', 'REPEATED', 'WRONG SEQUENCE')),
                    'service_name': (
                        '' if self._description is None else self._description.service_names.get(message.service_id, '')
                    ),
                    'sd': message.is_sd,
                }
            )
        return rows
