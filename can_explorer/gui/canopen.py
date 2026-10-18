import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.canopen_tools import (
    PDO_KINDS,
    CanOpenNetwork,
    ObjectDictionary,
    OdObject,
    SdoAbort,
    SdoClient,
    data_type_name,
    decode_value,
    emcy_text,
    encode_value,
    format_value,
    nmt_message,
    pdo_mappings,
)
from can_explorer.core.frames import format_hex, parse_hex_bytes
from can_explorer.core.protocols.canopen import NMT_COMMANDS
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
REFRESH_MS = 500
PDO_CHANNELS = 4  # PDOs read from a node: the four of the predefined connection set


def _parse_hex(text: str, maximum: int, what: str) -> int:
    try:
        value = int(text.strip() or '0', 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class CanOpenController(QObject):
    """The CANopen page of a connection: NMT, SDO access to a node's object dictionary, and its PDOs."""

    changed = pyqtSignal()
    dictionary_changed = pyqtSignal()
    log_changed = pyqtSignal()
    busy_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._network = CanOpenNetwork()
        self._dictionary: Optional[ObjectDictionary] = None
        self._node_id = 1
        self._log_entries: List[Dict[str, Any]] = []
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        if self.active:
            connection.session.add_message_listener(self._network.on_message)
            self._timer.start()

    @property
    def network(self) -> CanOpenNetwork:
        return self._network

    @property
    def session(self):
        return self._connection.session

    @property
    def dictionary(self) -> Optional[ObjectDictionary]:
        return self._dictionary

    def _refresh(self):
        if self._network.changed:
            self._network.changed = False
            self.changed.emit()

    def _log(self, text: str, error: bool = False):
        self._log_entries.append({'text': text, 'error': error})
        del self._log_entries[:-MAX_LOG]
        self.log_changed.emit()

    def _update_decoder(self):
        protocol = self.session.decoder.protocol
        if protocol is not None and hasattr(protocol, 'set_dictionary'):
            protocol.set_dictionary(self._dictionary, self._node_id)

    # Properties

    @pyqtProperty(bool, constant=True)
    def active(self) -> bool:
        return self._connection.protocol == 'CANopen'

    @pyqtProperty(bool, notify=busy_changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log_entries)

    @pyqtProperty('QVariantList', constant=True)
    def nmt_commands(self) -> List[Dict[str, Any]]:
        return [{'value': value, 'name': name} for value, name in NMT_COMMANDS.items()]

    @pyqtProperty(int, notify=dictionary_changed)
    def node_id(self) -> int:
        return self._node_id

    @pyqtSlot(int)
    def set_node(self, node_id: int):
        if 1 <= node_id <= 127 and node_id != self._node_id:
            self._node_id = node_id
            self._update_decoder()
            self.dictionary_changed.emit()
            self.changed.emit()

    @pyqtProperty('QVariantList', notify=changed)
    def nodes(self) -> List[Dict[str, Any]]:
        result = []
        for node_id, node in sorted(self._network.nodes.items()):
            last = node.emcy[-1] if node.emcy else None
            result.append(
                {
                    'node_id': node_id,
                    'state': node.state_name or 'Unknown',
                    'boot_ups': node.boot_ups,
                    'emcy_count': len(node.emcy),
                    'last_emcy': f'0x{last[1]:04X} {emcy_text(last[1])}' if last else '',
                    'frames': node.frames,
                }
            )
        return result

    @pyqtProperty('QVariantList', notify=changed)
    def emergencies(self) -> List[Dict[str, Any]]:
        """Emergency messages of the selected node, newest first."""
        node = self._network.nodes.get(self._node_id)
        if node is None:
            return []
        start = self.session.start_time or 0.0
        return [
            {
                'time': f'{max(0.0, timestamp - start):.3f}',
                'code': f'0x{code:04X}',
                'text': emcy_text(code),
                'register': f'0x{register:02X}',
                'data': format_hex(data),
            }
            for timestamp, code, register, data in reversed(node.emcy)
        ]

    @pyqtProperty(str, notify=dictionary_changed)
    def dictionary_file(self) -> str:
        return str(self._dictionary.path) if self._dictionary is not None and self._dictionary.path else ''

    @pyqtProperty(str, notify=dictionary_changed)
    def device_name(self) -> str:
        if self._dictionary is None:
            return ''
        info = self._dictionary.device_info
        return ' · '.join(text for text in (info.get('VendorName', ''), info.get('ProductName', '')) if text)

    @pyqtProperty('QVariantList', notify=dictionary_changed)
    def objects(self) -> List[Dict[str, Any]]:
        if self._dictionary is None or self._dictionary.path is None:
            return []
        return [
            {
                'index': f'{entry.index:04X}',
                'subindex': f'{entry.subindex:02X}',
                'name': entry.full_name,
                'type': data_type_name(entry.data_type),
                'access': entry.access,
                'default': entry.value or entry.default,
                'mappable': entry.pdo_mappable,
                'writable': entry.writable,
                'readable': entry.readable,
            }
            for entry in self._dictionary.entries()
        ]

    @pyqtProperty('QVariantList', notify=changed)
    def pdos(self) -> List[Dict[str, Any]]:
        if self._dictionary is None:
            return []
        result = []
        for pdo in pdo_mappings(self._dictionary, self._node_id):
            last = self._network.pdo_data.get(pdo.cob_id) if pdo.valid else None
            values = dict(pdo.decode(last[1])) if last is not None and pdo.variables else {}
            result.append(
                {
                    'name': pdo.name,
                    'cob_id': f'{pdo.cob_id:03X}',
                    'valid': pdo.valid,
                    'transmission': pdo.transmission_text,
                    'length': pdo.length,
                    'received': last is not None,
                    'variables': [
                        {
                            'object': f'{variable.index:04X}:{variable.subindex:02X}',
                            'name': variable.name,
                            'bits': variable.bits,
                            'value': values.get(variable.name, ''),
                        }
                        for variable in pdo.variables
                    ],
                }
            )
        return result

    # Dictionary

    @pyqtSlot(QUrl, result=str)
    def load_dictionary(self, url: QUrl) -> str:
        """Load an EDS or DCF file of the selected node. Returns an error or ''."""
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            dictionary = ObjectDictionary.from_file(path)
        except Exception as e:
            return f'Could not load {Path(path).name}: {e}'
        self._dictionary = dictionary
        if dictionary.node_id:  # A DCF names the node it configures
            self._node_id = dictionary.node_id
        self._update_decoder()
        self.dictionary_changed.emit()
        self.changed.emit()
        logger.info(f'Loaded object dictionary {path} with {len(dictionary.entries())} entries')
        return ''

    @pyqtSlot()
    def unload_dictionary(self):
        self._dictionary = None
        self._update_decoder()
        self.dictionary_changed.emit()
        self.changed.emit()

    # Services

    def _check_send(self) -> Optional[str]:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if not self.session.is_open:
            return 'Connect first'
        return None

    @pyqtSlot(int, bool, result=str)
    def nmt(self, command: int, all_nodes: bool) -> str:
        error = self._check_send()
        if error:
            return error
        try:
            self.session.send(nmt_message(command, 0 if all_nodes else self._node_id))
        except Exception as e:
            return str(e)
        self._log(f'NMT {NMT_COMMANDS[command]} → {"all nodes" if all_nodes else f"node {self._node_id}"}')
        return ''

    @pyqtSlot(result=str)
    def send_sync(self) -> str:
        error = self._check_send()
        if error:
            return error
        self.session.send(can.Message(arbitration_id=0x080, is_extended_id=False, data=b''))
        self._log('SYNC')
        return ''

    def _start(self, operation: Callable[[SdoClient], Awaitable[str]]) -> str:
        error = self._check_send()
        if error:
            return error
        if self._busy:
            return 'An SDO transfer is in progress'
        self._busy = True
        self.busy_changed.emit()
        asyncio.ensure_future(self._run(operation))
        return ''

    async def _run(self, operation: Callable[[SdoClient], Awaitable[str]]):
        try:
            self._log(await operation(SdoClient(self.session, self._node_id)))
        except (SdoAbort, TimeoutError, ConnectionError, ValueError) as e:
            self._log(str(e), error=True)
        except Exception as e:
            logger.warning(f'SDO transfer failed: {e}')
            self._log(str(e), error=True)
        finally:
            self._busy = False
            self.busy_changed.emit()
            self.changed.emit()

    def _describe(self, index: int, subindex: int, data: bytes) -> str:
        entry = self._dictionary.entry(index, subindex) if self._dictionary is not None else None
        if entry is None:
            text = format_hex(data)
            if data and all(32 <= byte < 127 for byte in data) and len(data) > 4:
                text += f' "{data.decode("ascii")}"'
            return text
        try:
            value = decode_value(entry.data_type, data)
        except ValueError:
            return format_hex(data)
        return f'"{value}"' if isinstance(value, str) else format_value(entry.data_type, value)

    def _name(self, index: int, subindex: int) -> str:
        dictionary = self._dictionary if self._dictionary is not None else ObjectDictionary()
        name = dictionary.name(index, subindex)  # Without one: the standard objects
        return f'0x{index:04X}:{subindex:02X}' + (f' {name}' if name and not name.startswith('0x') else '')

    @pyqtSlot(str, str, result=str)
    def sdo_read(self, index_text: str, subindex_text: str) -> str:
        try:
            index = _parse_hex(index_text, 0xFFFF, 'index')
            subindex = _parse_hex(subindex_text, 0xFF, 'sub-index')
        except ValueError as e:
            return str(e)

        async def operation(client: SdoClient) -> str:
            data = await client.upload(index, subindex)
            return f'Node {self._node_id} {self._name(index, subindex)} = {self._describe(index, subindex, data)}'

        return self._start(operation)

    @pyqtSlot(str, str, str, result=str)
    def sdo_write(self, index_text: str, subindex_text: str, value: str) -> str:
        """Write a value: in the object's data type when the dictionary knows it, otherwise as hex bytes."""
        try:
            index = _parse_hex(index_text, 0xFFFF, 'index')
            subindex = _parse_hex(subindex_text, 0xFF, 'sub-index')
            entry = self._dictionary.entry(index, subindex) if self._dictionary is not None else None
            data = encode_value(entry.data_type, value) if entry is not None else parse_hex_bytes(value)
            if not data:
                raise ValueError('Enter the value to write')
        except ValueError as e:
            return str(e)

        async def operation(client: SdoClient) -> str:
            await client.download(index, subindex, data)
            return f'Node {self._node_id} {self._name(index, subindex)} ← {self._describe(index, subindex, data)}'

        return self._start(operation)

    @pyqtSlot(result=str)
    def read_pdo_configuration(self) -> str:
        """Read the communication and mapping parameters of the node's first four RPDOs and TPDOs."""

        async def operation(client: SdoClient) -> str:
            if self._dictionary is None:
                self._dictionary = ObjectDictionary()  # Only the PDO parameters
            dictionary = self._dictionary
            found = 0
            for kind, comm_base, map_base, _ in PDO_KINDS:
                for number in range(PDO_CHANNELS):
                    values = {}
                    try:
                        for subindex in (1, 2) + ((5,) if kind == 'TPDO' else ()):
                            try:
                                values[(comm_base + number, subindex)] = await client.upload(
                                    comm_base + number, subindex
                                )
                            except SdoAbort:
                                if subindex == 1:
                                    raise
                        count = (await client.upload(map_base + number, 0))[0]
                        values[(map_base + number, 0)] = bytes([count])
                        for subindex in range(1, count + 1):
                            values[(map_base + number, subindex)] = await client.upload(map_base + number, subindex)
                    except SdoAbort:
                        continue  # The node does not have this PDO
                    for (index, subindex), data in values.items():
                        dictionary.overrides[(index, subindex)] = int.from_bytes(data, 'little')
                        dictionary.objects.setdefault(index, OdObject(index, f'{kind}{number + 1} parameters', 9))
                    found += 1
            self._update_decoder()
            self.dictionary_changed.emit()
            return f'Read the configuration of {found} PDOs from node {self._node_id}'

        return self._start(operation)

    @pyqtSlot()
    def clear(self):
        self._network.clear()
        self._refresh()

    @pyqtSlot()
    def clear_log(self):
        self._log_entries.clear()
        self.log_changed.emit()
