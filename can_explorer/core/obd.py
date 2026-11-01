"""OBD-II (SAE J1979 over ISO 15765-4): live data, trouble codes and vehicle information over CAN.

OBD services are UDS-like — a service byte and data, answered with the service byte + 0x40 — so the client runs on
the UDS/ISO-TP stack (UdsClient over IsoTpClient). Requests go to the functional address 0x7DF (or an ECU's
0x7E0-0x7E7); answers come from 0x7E8-0x7EF. A matching ObdServer drives the simulator for use without a vehicle.

Supported: service 01 (current data, with supported-PID discovery and the MIL/DTC status), 03 and 07 (stored and
pending trouble codes), 04 (clear) and 09 PID 02 (VIN). The current-data PIDs reuse the table in reverse/obd.py.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from can_explorer.core.reverse.obd import PIDS_BY_NUMBER
from can_explorer.core.uds import UdsClient, UdsError, UdsNegativeResponse

FUNCTIONAL_REQUEST = 0x7DF
ECU_RESPONSE = 0x7E8
PID_BLOCKS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)  # "PIDs supported" PIDs


def decode_dtc(high: int, low: int) -> str:
    """Two bytes to a diagnostic trouble code, e.g. 0x01, 0x33 -> 'P0133'."""
    category = 'PCBU'[(high >> 6) & 0x3]
    return f'{category}{(high >> 4) & 0x3}{high & 0xF:X}{low >> 4:X}{low & 0xF:X}'


@dataclass(frozen=True)
class Dtc:
    code: str
    raw: int


@dataclass
class ObdStatus:
    mil_on: bool  # Malfunction indicator lamp ("check engine")
    dtc_count: int


@dataclass
class ObdValue:
    pid: int
    name: str
    unit: str
    value: float


class ObdClient:
    """OBD-II client over a UDS/ISO-TP transport. Methods raise UdsError (or UdsNegativeResponse) on failure."""

    def __init__(self, uds: UdsClient):
        self._uds = uds

    async def _call(self, payload: bytes) -> bytes:
        return await self._uds.call(payload)

    async def supported_pids(self) -> List[int]:
        """Service 01 PID 00/20/40…: the PIDs the ECU supports, by walking the support bitmasks."""
        supported: List[int] = []
        for base in PID_BLOCKS:
            try:
                response = await self._call(bytes([0x01, base]))
            except (UdsNegativeResponse, UdsError):
                break
            if len(response) < 6:
                break
            mask = int.from_bytes(response[2:6], 'big')
            for bit in range(32):
                if mask & (1 << (31 - bit)):
                    supported.append(base + bit + 1)
            if not mask & 1:  # The last bit marks whether the next block exists
                break
        return [pid for pid in supported if pid % 0x20 != 0]  # Drop the block markers themselves

    async def read_value(self, pid: int) -> Optional[ObdValue]:
        """Read and decode one service-01 PID. Returns None when it is unknown or the answer is too short."""
        definition = PIDS_BY_NUMBER.get(pid)
        if definition is None:
            return None
        response = await self._call(bytes([0x01, pid]))
        data = response[2:]
        if len(data) < definition.length:
            return None
        return ObdValue(pid, definition.name, definition.unit, float(definition.decode(data[: definition.length])))

    async def status(self) -> ObdStatus:
        """Service 01 PID 01: the MIL state and the number of stored trouble codes."""
        response = await self._call(bytes([0x01, 0x01]))
        byte_a = response[2] if len(response) > 2 else 0
        return ObdStatus(bool(byte_a & 0x80), byte_a & 0x7F)

    async def read_dtcs(self, pending: bool = False) -> List[Dtc]:
        """Service 03 (stored) or 07 (pending) trouble codes."""
        response = await self._call(bytes([0x07 if pending else 0x03]))
        body = response[1:]
        if len(body) % 2:  # ISO 15765-4 prefixes the DTC count; drop it when present
            body = body[1:]
        dtcs = []
        for index in range(0, len(body) - 1, 2):
            raw = (body[index] << 8) | body[index + 1]
            if raw:
                dtcs.append(Dtc(decode_dtc(body[index], body[index + 1]), raw))
        return dtcs

    async def clear_dtcs(self) -> None:
        """Service 04: clear trouble codes and turn the MIL off."""
        await self._call(b'\x04')

    async def read_vin(self) -> str:
        """Service 09 PID 02: the vehicle identification number."""
        response = await self._call(bytes([0x09, 0x02]))
        body = response[3:]  # Skip 0x49, 0x02 and the number-of-data-items byte
        return bytes(byte for byte in body if 0x20 <= byte < 0x7F).decode('ascii', 'replace').strip()


class ObdServer:
    """OBD-II responder for the simulator: answers services 01, 03, 07, 04 and 09. handle() returns the responses
    to a request (the UdsEcuSimulator sends them over ISO-TP)."""

    def __init__(
        self,
        values: Optional[Dict[int, bytes]] = None,
        dtcs: Optional[List[int]] = None,
        pending: Optional[List[int]] = None,
        vin: str = '1HGCM82633A004352',
        mil: bool = True,
    ):
        # Raw data bytes per service-01 PID (as an ECU would send after 0x41 <pid>)
        self.values: Dict[int, bytes] = (
            values
            if values is not None
            else {
                0x04: bytes([0xAA]),  # Engine load ~67 %
                0x05: bytes([0x5A]),  # Coolant 50 °C
                0x0C: bytes([0x27, 0x10]),  # 2500 rpm
                0x0D: bytes([0x3C]),  # 60 km/h
                0x0F: bytes([0x46]),  # Intake air 30 °C
                0x11: bytes([0x40]),  # Throttle ~25 %
                0x2F: bytes([0x80]),  # Fuel level ~50 %
                0x42: bytes([0x30, 0xD4]),  # 12.5 V
            }
        )
        self.dtcs = list(dtcs if dtcs is not None else [0x0133, 0x0420])  # P0133, P0420
        self.pending = list(pending if pending is not None else [0x0171])  # P0171
        self.vin = vin
        self.mil = mil

    def _supported(self, base: int) -> bytes:
        mask = 0
        for offset in range(1, 33):
            pid = base + offset
            if pid in self.values or (base == 0 and pid == 0x01):
                mask |= 1 << (32 - offset)
        if any(base + 0x20 < pid <= base + 0x40 for pid in self.values):
            mask |= 1  # Mark that the next block exists
        return bytes([0x41, base]) + mask.to_bytes(4, 'big')

    @staticmethod
    def _dtc_response(service: int, codes: List[int]) -> bytes:
        return bytes([service + 0x40, len(codes)]) + b''.join(code.to_bytes(2, 'big') for code in codes)

    def handle(self, request: bytes) -> List[bytes]:
        if not request:
            return []
        service = request[0]
        if service == 0x01 and len(request) >= 2:
            pid = request[1]
            if pid in PID_BLOCKS:
                return [self._supported(pid)]
            if pid == 0x01:
                byte_a = (0x80 if self.mil else 0x00) | len(self.dtcs)
                return [bytes([0x41, 0x01, byte_a, 0x07, 0xFF, 0x00])]
            if pid in self.values:
                return [bytes([0x41, pid]) + self.values[pid]]
            return [bytes([0x7F, service, 0x12])]  # Sub-function not supported
        if service == 0x03:
            return [self._dtc_response(0x03, self.dtcs)]
        if service == 0x07:
            return [self._dtc_response(0x07, self.pending)]
        if service == 0x04:
            self.dtcs = []
            self.mil = False
            return [b'\x44']
        if service == 0x09 and len(request) >= 2 and request[1] == 0x02:
            return [bytes([0x49, 0x02, 0x01]) + self.vin.encode('ascii', 'replace')]
        return [bytes([0x7F, service, 0x11])]  # Service not supported
