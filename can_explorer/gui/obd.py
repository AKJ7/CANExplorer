"""The OBD-II (SAE J1979) page of a connection: live data, trouble codes and the VIN, over ISO-TP on CAN.

It keeps one ISO-TP transport open while the page is used and serializes all requests through a lock, so the live
polling loop and the on-demand reads (trouble codes, clear, scan) never overlap on the bus."""

import asyncio
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.obd import ECU_RESPONSE, FUNCTIONAL_REQUEST, Dtc, ObdClient, ObdValue
from can_explorer.core.reverse.obd import PIDS_BY_NUMBER
from can_explorer.core.uds import UdsClient
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

LIVE_INTERVAL = 0.2  # Seconds between polling passes of the supported values


class ObdController(QObject):
    """OBD-II diagnostics: supported-PID discovery, live values, trouble codes and the VIN."""

    changed = pyqtSignal()  # Busy, status, ids, supported set
    data_changed = pyqtSignal()  # Live values and trouble codes

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._request_id, self._response_id = FUNCTIONAL_REQUEST, ECU_RESPONSE
        self._transport: Optional[IsoTpClient] = None
        self._lock = asyncio.Lock()
        self._busy = False
        self._status = ''
        self._vin = ''
        self._mil = False
        self._dtc_count = 0
        self._supported: List[int] = []
        self._values: Dict[int, ObdValue] = {}
        self._dtcs: List[Dtc] = []
        self._pending: List[Dtc] = []
        self._live_task: Optional[asyncio.Task] = None

    @property
    def session(self):
        return self._connection.session

    # State

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(str, notify=changed)
    def status(self) -> str:
        return self._status

    @pyqtProperty(str, notify=changed)
    def vin(self) -> str:
        return self._vin

    @pyqtProperty(bool, notify=data_changed)
    def mil_on(self) -> bool:
        return self._mil

    @pyqtProperty(int, notify=data_changed)
    def dtc_count(self) -> int:
        return self._dtc_count

    @pyqtProperty(bool, notify=changed)
    def scanned(self) -> bool:
        return bool(self._supported)

    @pyqtProperty(str, notify=changed)
    def request_id(self) -> str:
        return f'{self._request_id:X}'

    @pyqtProperty(str, notify=changed)
    def response_id(self) -> str:
        return f'{self._response_id:X}'

    @pyqtProperty(bool, notify=changed)
    def live(self) -> bool:
        return self._live_task is not None and not self._live_task.done()

    @pyqtProperty('QVariantList', notify=data_changed)
    def values(self) -> List[Dict[str, Any]]:
        rows = []
        for pid in self._supported:
            definition = PIDS_BY_NUMBER[pid]
            value = self._values.get(pid)
            rows.append(
                {
                    'pid': f'{pid:02X}',
                    'name': definition.name,
                    'unit': definition.unit,
                    'value': '' if value is None else f'{value.value:g}',
                }
            )
        return rows

    @pyqtProperty('QVariantList', notify=data_changed)
    def dtcs(self) -> List[Dict[str, str]]:
        return [{'code': dtc.code} for dtc in self._dtcs]

    @pyqtProperty('QVariantList', notify=data_changed)
    def pending_dtcs(self) -> List[Dict[str, str]]:
        return [{'code': dtc.code} for dtc in self._pending]

    # Configuration

    @pyqtSlot(str, str)
    def set_ids(self, request_hex: str, response_hex: str):
        try:
            request_id, response_id = int(request_hex, 16), int(response_hex, 16)
        except ValueError:
            return
        self._request_id, self._response_id = request_id, response_id
        self._reset_transport()
        self.changed.emit()

    def _client(self) -> ObdClient:
        if self._transport is None:
            self._transport = IsoTpClient(self.session, self._request_id, self._response_id, fd=self._connection.fd)
        return ObdClient(UdsClient(self._transport))

    def _reset_transport(self) -> None:
        self._stop_live()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # Actions

    @pyqtSlot()
    def scan(self):
        """Read the VIN and status and discover the supported PIDs."""
        if not self._busy:
            asyncio.ensure_future(self._scan())

    async def _scan(self) -> None:
        if self._connection.is_lin:
            self._status = 'OBD-II runs over CAN, not LIN'
            self.changed.emit()
            return
        self._busy, self._status = True, 'Scanning the vehicle…'
        self.changed.emit()
        try:
            async with self._lock:
                client = self._client()
                try:
                    self._vin = await client.read_vin()
                except Exception:
                    self._vin = ''
                status = await client.status()
                self._mil, self._dtc_count = status.mil_on, status.dtc_count
                self._supported = [pid for pid in await client.supported_pids() if pid in PIDS_BY_NUMBER]
            self._status = (
                f'{len(self._supported)} supported values' if self._supported else 'Connected, no known values'
            )
        except Exception as e:
            logger.info('OBD scan failed: %s', e)
            self._status = f'No response — check the request/response IDs ({e})'
            self._reset_transport()
        finally:
            self._busy = False
            self.changed.emit()
            self.data_changed.emit()

    @pyqtSlot()
    def toggle_live(self):
        if self.live:
            self._stop_live()
        elif self._supported:
            self._live_task = asyncio.ensure_future(self._poll())
        self.changed.emit()

    def _stop_live(self) -> None:
        if self._live_task is not None:
            self._live_task.cancel()
            self._live_task = None

    async def _poll(self) -> None:
        try:
            while True:
                for pid in list(self._supported):
                    async with self._lock:
                        try:
                            value = await self._client().read_value(pid)
                        except Exception:
                            value = None
                    if value is not None:
                        self._values[pid] = value
                self.data_changed.emit()
                await asyncio.sleep(LIVE_INTERVAL)
        except asyncio.CancelledError:
            pass
        finally:
            self.changed.emit()

    @pyqtSlot()
    def read_dtcs(self):
        if not self._busy:
            asyncio.ensure_future(self._read_dtcs('Reading trouble codes…'))

    async def _read_dtcs(self, status: str) -> None:
        self._busy, self._status = True, status
        self.changed.emit()
        try:
            async with self._lock:
                client = self._client()
                self._dtcs = await client.read_dtcs()
                self._pending = await client.read_dtcs(pending=True)
                vehicle = await client.status()
                self._mil, self._dtc_count = vehicle.mil_on, vehicle.dtc_count
            self._status = f'{len(self._dtcs)} stored, {len(self._pending)} pending'
        except Exception as e:
            logger.info('OBD read DTCs failed: %s', e)
            self._status = f'Failed ({e})'
        finally:
            self._busy = False
            self.changed.emit()
            self.data_changed.emit()

    @pyqtSlot()
    def clear_dtcs(self):
        if not self._busy:
            asyncio.ensure_future(self._clear())

    async def _clear(self) -> None:
        self._busy, self._status = True, 'Clearing trouble codes…'
        self.changed.emit()
        try:
            async with self._lock:
                await self._client().clear_dtcs()
        except Exception as e:
            logger.info('OBD clear failed: %s', e)
            self._status = f'Failed ({e})'
            self._busy = False
            self.changed.emit()
            return
        self._busy = False
        await self._read_dtcs('Cleared — re-reading…')

    def shutdown(self) -> None:
        self._reset_transport()
