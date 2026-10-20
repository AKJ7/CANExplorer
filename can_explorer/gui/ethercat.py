import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.canopen_tools import ObjectDictionary, SdoAbort, data_type_name, emcy_text
from can_explorer.core.ethercat import (
    REQUESTABLE_STATES,
    SIMULATED,
    CoePdo,
    EtherCatBackend,
    EtherCatMaster,
    SimulatedBackend,
    SoemBackend,
    al_status_text,
    ethercat_available,
    state_name,
)
from can_explorer.core.frames import format_hex
from can_explorer.gui.canopen import _parse_hex, describe_value, encode_for, object_label
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
STATE_POLL_MS = 1000
UNAVAILABLE = 'EtherCAT adapters need the pysoem package: install CANExplorer with the "ethercat" extra'


def _backend(adapter: str) -> EtherCatBackend:
    return SimulatedBackend() if adapter == SIMULATED else SoemBackend()


class EtherCatController(QObject):
    """The EtherCAT tool: the slaves of a network adapter, their states, and CoE access to their objects."""

    changed = pyqtSignal()
    adapters_changed = pyqtSignal()
    dictionary_changed = pyqtSignal()
    log_changed = pyqtSignal()
    busy_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None, backend_factory: Callable[[str], EtherCatBackend] = _backend):
        super().__init__(parent)
        self._backend_factory = backend_factory
        self._master: Optional[EtherCatMaster] = None
        self._adapters: List[Dict[str, str]] = []
        self._selected = 0  # Position of the selected slave, 0: none
        self._dictionaries: Dict[int, ObjectDictionary] = {}  # Per slave position
        self._dictionary_sources: Dict[int, str] = {}  # 'SDO information' or the EDS file
        self._pdos: Dict[int, List[CoePdo]] = {}
        self._pdo_values: Dict[int, Dict[str, str]] = {}
        self._log_entries: List[Dict[str, Any]] = []
        self._busy = False
        self._closing = False
        self._polling = False
        self._emergency_count = 0
        self._timer = QTimer(self)
        self._timer.setInterval(STATE_POLL_MS)
        self._timer.timeout.connect(self._poll)

    def _log(self, text: str, error: bool = False):
        self._log_entries.append({'text': text, 'error': error})
        del self._log_entries[:-MAX_LOG]
        self.log_changed.emit()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.busy_changed.emit()

    # Adapters

    @pyqtProperty(bool, constant=True)
    def available(self) -> bool:
        """Whether real adapters can be used (pysoem installed); the simulated network always works."""
        return ethercat_available()

    @pyqtProperty(str, constant=True)
    def unavailable_reason(self) -> str:
        return '' if ethercat_available() else UNAVAILABLE

    @pyqtProperty('QVariantList', notify=adapters_changed)
    def adapters(self) -> List[Dict[str, str]]:
        if not self._adapters:
            self._adapters = self._list_adapters()
        return list(self._adapters)

    @pyqtSlot()
    def refresh_adapters(self):
        self._adapters = self._list_adapters()
        self.adapters_changed.emit()

    @staticmethod
    def _list_adapters() -> List[Dict[str, str]]:
        adapters = []
        if ethercat_available():
            try:
                adapters = [
                    {'name': name, 'description': description} for name, description in SoemBackend().adapters()
                ]
            except Exception as e:  # Without Npcap on Windows, for example
                logger.warning(f'Could not list the network adapters: {e}')
        adapters.append({'name': SIMULATED, 'description': 'Simulated network (no hardware)'})
        return adapters

    # Network

    @pyqtProperty(bool, notify=changed)
    def is_open(self) -> bool:
        # Open until closing has finished (the master marks itself closed when closing starts)
        return self._master is not None and (self._closing or self._master.is_open)

    @pyqtProperty(str, notify=changed)
    def adapter(self) -> str:
        return self._master.adapter if self._master is not None else ''

    @pyqtProperty(str, notify=changed)
    def adapter_description(self) -> str:
        """The open adapter's description (on Windows its name is a device path such as \\Device\\NPF_{...})."""
        name = self.adapter
        return next((adapter['description'] for adapter in self.adapters if adapter['name'] == name), name)

    @pyqtProperty(bool, notify=busy_changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtSlot(str, result=str)
    def open_network(self, adapter: str) -> str:
        """Open an adapter and find its slaves. Returns an error or ''."""
        if self._busy:
            return 'An operation is in progress'
        if self._closing:
            return 'The network is still closing'
        if not adapter:
            return 'Choose a network adapter'
        if adapter != SIMULATED and not ethercat_available():
            return UNAVAILABLE
        self._set_busy(True)
        asyncio.ensure_future(self._open(adapter))
        return ''

    async def _open(self, adapter: str):
        await self._close()
        master = None
        try:
            master = EtherCatMaster(self._backend_factory(adapter))
            slaves = await master.open(adapter)
        except Exception as e:
            if master is not None:
                master.shutdown()
            self._log(str(e), error=True)
        else:
            self._master = master
            self._selected = next(
                (slave.position for slave in slaves if slave.coe), slaves[0].position if slaves else 0
            )
            self._log(f'{adapter}: {len(slaves)} slaves' if slaves else f'{adapter}: no slaves found')
            self._timer.start()
            logger.info(f'Opened EtherCAT network {adapter} with {len(slaves)} slaves')
        finally:
            self._set_busy(False)
            self.dictionary_changed.emit()
            self.changed.emit()

    @pyqtSlot()
    def close_network(self):
        if self._master is not None and not self._closing:
            self._closing = True
            asyncio.ensure_future(self._close())

    async def _close(self):
        """Close the network; it reports closed (is_open, the log) only once it is."""
        self._timer.stop()
        master = self._master
        adapter = master.adapter if master is not None else ''
        if master is not None:
            try:
                await master.close()
            except Exception as e:
                logger.warning(f'Closing the EtherCAT network failed: {e}')
            master.shutdown()
        self._master = None
        self._closing = False
        self._selected = 0
        self._dictionaries.clear()
        self._dictionary_sources.clear()
        self._pdos.clear()
        self._pdo_values.clear()
        self._emergency_count = 0
        if master is not None:
            self._log(f'{adapter} closed')
        self.dictionary_changed.emit()
        self.changed.emit()

    def shutdown(self):
        """Close the network when quitting (no event loop runs any more)."""
        self._timer.stop()
        master, self._master = self._master, None
        if master is not None:
            master.shutdown()

    @pyqtSlot(result=str)
    def scan(self) -> str:
        async def operation(master: EtherCatMaster) -> str:
            slaves = await master.scan()
            self._dictionaries.clear()
            self._dictionary_sources.clear()
            self._pdos.clear()
            self._pdo_values.clear()
            if self._selected not in [slave.position for slave in slaves]:
                self._selected = slaves[0].position if slaves else 0
            self.dictionary_changed.emit()
            return f'Found {len(slaves)} slaves'

        return self._start(operation)

    def _poll(self):
        if self._master is None or self._busy or self._polling:
            return
        self._polling = True
        asyncio.ensure_future(self._poll_states(self._master))

    async def _poll_states(self, master: EtherCatMaster):
        try:
            changed = await master.refresh_states()
        except Exception as e:
            logger.debug(f'Reading the EtherCAT states failed: {e}')
            changed = False
        finally:
            self._polling = False
        if changed or len(master.emergencies) != self._emergency_count:
            self._emergency_count = len(master.emergencies)
            self.changed.emit()

    # Slaves

    @pyqtProperty('QVariantList', notify=changed)
    def slaves(self) -> List[Dict[str, Any]]:
        if self._master is None:
            return []
        counts: Dict[int, int] = {}
        for emergency in self._master.emergencies:
            counts[emergency.position] = counts.get(emergency.position, 0) + 1
        return [
            {
                'position': slave.position,
                'name': slave.name or f'Slave {slave.position}',
                'vendor': f'0x{slave.vendor_id:08X}',
                'product': f'0x{slave.product_code:08X}',
                'revision': f'0x{slave.revision:08X}',
                'state': slave.state_name,
                'error': slave.error,
                'al_status': al_status_text(slave.al_status) if slave.al_status else '',
                'coe': slave.coe,
                'emcy_count': counts.get(slave.position, 0),
            }
            for slave in self._master.slaves
        ]

    @pyqtProperty(int, notify=changed)
    def selected(self) -> int:
        return self._selected

    @pyqtSlot(int)
    def select(self, position: int):
        if position != self._selected:
            self._selected = position
            self.dictionary_changed.emit()
            self.changed.emit()

    @pyqtProperty('QVariantList', constant=True)
    def states(self) -> List[Dict[str, Any]]:
        return [{'value': state, 'name': state_name(state)} for state in REQUESTABLE_STATES]

    @pyqtSlot(int, bool, result=str)
    def request_state(self, state: int, all_slaves: bool) -> str:
        position = 0 if all_slaves else self._selected
        if not all_slaves and not position:
            return 'Select a slave'

        async def operation(master: EtherCatMaster) -> str:
            await master.request_state(position, state)
            return f'{"All slaves" if all_slaves else f"Slave {position}"} → {state_name(state)}'

        return self._start(operation)

    @pyqtProperty('QVariantList', notify=changed)
    def emergencies(self) -> List[Dict[str, Any]]:
        """Emergency messages of the selected slave, newest first."""
        if self._master is None:
            return []
        return [
            {
                'time': time.strftime('%H:%M:%S', time.localtime(emergency.timestamp)),
                'code': f'0x{emergency.code:04X}',
                'text': emcy_text(emergency.code),
                'register': f'0x{emergency.register:02X}',
                'data': format_hex(emergency.data),
            }
            for emergency in reversed(self._master.emergencies)
            if emergency.position == self._selected
        ]

    # Object dictionary

    @property
    def dictionary(self) -> Optional[ObjectDictionary]:
        return self._dictionaries.get(self._selected)

    @pyqtProperty(str, notify=dictionary_changed)
    def dictionary_source(self) -> str:
        return self._dictionary_sources.get(self._selected, '')

    @pyqtProperty('QVariantList', notify=dictionary_changed)
    def objects(self) -> List[Dict[str, Any]]:
        dictionary = self.dictionary
        if dictionary is None:
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
            for entry in dictionary.entries()
        ]

    @pyqtSlot(result=str)
    def read_dictionary(self) -> str:
        """Read the selected slave's object dictionary with the SDO information service."""
        position = self._selected

        async def operation(master: EtherCatMaster) -> str:
            dictionary = await master.object_dictionary(position)
            self._dictionaries[position] = dictionary
            self._dictionary_sources[position] = 'SDO information'
            self.dictionary_changed.emit()
            return f'Read {len(dictionary.entries())} entries of the object dictionary of slave {position}'

        return self._start(operation)

    @pyqtSlot(QUrl, result=str)
    def load_dictionary(self, url: QUrl) -> str:
        """Load an EDS file for the selected slave. Returns an error or ''."""
        if not self._selected:
            return 'Select a slave'
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            dictionary = ObjectDictionary.from_file(path)
        except Exception as e:
            return f'Could not load {Path(path).name}: {e}'
        self._dictionaries[self._selected] = dictionary
        self._dictionary_sources[self._selected] = Path(path).name
        self.dictionary_changed.emit()
        return ''

    @pyqtSlot()
    def unload_dictionary(self):
        self._dictionaries.pop(self._selected, None)
        self._dictionary_sources.pop(self._selected, None)
        self.dictionary_changed.emit()

    # SDO transfers

    def _start(self, operation: Callable[[EtherCatMaster], Awaitable[str]]) -> str:
        if self._master is None:
            return 'Open a network first'
        if self._busy:
            return 'An operation is in progress'
        self._set_busy(True)
        asyncio.ensure_future(self._run(self._master, operation))
        return ''

    async def _run(self, master: EtherCatMaster, operation: Callable[[EtherCatMaster], Awaitable[str]]):
        try:
            self._log(await operation(master))
        except (SdoAbort, TimeoutError, ConnectionError, ValueError) as e:
            self._log(str(e), error=True)
        except Exception as e:
            logger.warning(f'EtherCAT operation failed: {e}')
            self._log(str(e), error=True)
        finally:
            self._set_busy(False)
            self.changed.emit()

    @pyqtSlot(str, str, result=str)
    def sdo_read(self, index_text: str, subindex_text: str) -> str:
        if not self._selected:
            return 'Select a slave'
        try:
            index = _parse_hex(index_text, 0xFFFF, 'index')
            subindex = _parse_hex(subindex_text, 0xFF, 'sub-index')
        except ValueError as e:
            return str(e)
        position, dictionary = self._selected, self.dictionary

        async def operation(master: EtherCatMaster) -> str:
            data = await master.sdo_read(position, index, subindex)
            label = object_label(dictionary, index, subindex)
            return f'Slave {position} {label} = {describe_value(dictionary, index, subindex, data)}'

        return self._start(operation)

    @pyqtSlot(str, str, str, result=str)
    def sdo_write(self, index_text: str, subindex_text: str, value: str) -> str:
        """Write a value: in the object's data type when the dictionary knows it, otherwise as hex bytes."""
        if not self._selected:
            return 'Select a slave'
        position, dictionary = self._selected, self.dictionary
        try:
            index = _parse_hex(index_text, 0xFFFF, 'index')
            subindex = _parse_hex(subindex_text, 0xFF, 'sub-index')
            data = encode_for(dictionary, index, subindex, value)
        except ValueError as e:
            return str(e)

        async def operation(master: EtherCatMaster) -> str:
            await master.sdo_write(position, index, subindex, data)
            label = object_label(dictionary, index, subindex)
            return f'Slave {position} {label} ← {describe_value(dictionary, index, subindex, data)}'

        return self._start(operation)

    # PDOs

    @pyqtProperty('QVariantList', notify=changed)
    def pdos(self) -> List[Dict[str, Any]]:
        values = self._pdo_values.get(self._selected, {})
        return [
            {
                'name': pdo.name,
                'kind': pdo.kind,
                'bits': pdo.bits,
                'variables': [
                    {
                        'object': f'{variable.index:04X}:{variable.subindex:02X}',
                        'name': variable.name,
                        'bits': variable.bits,
                        'value': values.get(f'{variable.index:04X}:{variable.subindex:02X}', ''),
                    }
                    for variable in pdo.variables
                ],
            }
            for pdo in self._pdos.get(self._selected, [])
        ]

    @pyqtSlot(result=str)
    def read_pdo_configuration(self) -> str:
        """Read the PDO assignment and mapping of the selected slave, and the mapped objects' values over SDO."""
        if not self._selected:
            return 'Select a slave'
        position, dictionary = self._selected, self.dictionary

        async def operation(master: EtherCatMaster) -> str:
            pdos = await master.pdo_assignment(position, dictionary)
            values = {}
            for variable in (variable for pdo in pdos for variable in pdo.variables if variable.index):
                key = f'{variable.index:04X}:{variable.subindex:02X}'
                try:
                    data = await master.sdo_read(position, variable.index, variable.subindex)
                except (SdoAbort, ConnectionError):
                    continue  # Not readable over SDO: only in the process data
                values[key] = describe_value(dictionary, variable.index, variable.subindex, data)
            self._pdos[position], self._pdo_values[position] = pdos, values
            return f'Read {len(pdos)} assigned PDOs of slave {position}'

        return self._start(operation)

    @pyqtSlot()
    def clear_log(self):
        self._log_entries.clear()
        self.log_changed.emit()

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log_entries)
