"""Bus participants for trying out and testing CANExplorer without hardware.

- UdsServer: answers UDS requests (sessions, DIDs, security access, DTCs, routines).
- UdsEcuSimulator: a UdsServer reachable over ISO-TP on CAN.
- LinSlaveSimulator: LIN slaves answering headers, optionally with a UdsServer behind the LIN transport layer.

They run on a BusSession, typically on python-can's virtual interface next to the session under test.
"""

import asyncio
from typing import Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.isotp_client import IsoTpClient, IsoTpError
from can_explorer.core.lin import MASTER_REQUEST_ID, SLAVE_RESPONSE_ID, LinTpReassembler
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.uds import NEGATIVE_RESPONSE, RESPONSE_PENDING, SUPPRESS_POSITIVE_RESPONSE


def xor_key(level: int, seed: bytes) -> bytes:
    """Demonstration seed/key algorithm of the simulator: every seed byte XOR 0xFF."""
    return bytes(byte ^ 0xFF for byte in seed)


class UdsServer:
    """UDS server logic. handle() returns the responses to send (several when answering 'pending')."""

    def __init__(
        self,
        dids: Optional[Dict[int, bytes]] = None,
        dtcs: Optional[List[Tuple[int, int]]] = None,
        seed: bytes = b'\x12\x34',
        key_function: Callable[[int, bytes], bytes] = xor_key,
        slow_routines: Tuple[int, ...] = (),
        max_block_length: int = 0x402,
        download_requires_security: bool = False,
    ):
        self.dids = dict(dids or {0xF190: b'WDB1234567890ABCD', 0xF18C: b'SN-0001'})
        self.dtcs = list(dtcs if dtcs is not None else [(0x012345, 0x09), (0xC10000, 0x08)])
        self.seed = seed
        self.key_function = key_function
        self.slow_routines = slow_routines
        self.session = 0x01
        self.unlocked_level: Optional[int] = None
        self._pending_seed_level: Optional[int] = None
        self.tester_present_count = 0
        self.requests: List[bytes] = []
        # Flashing: memory written by RequestDownload/TransferData/RequestTransferExit, erased ranges
        self.max_block_length = max_block_length
        self.download_requires_security = download_requires_security
        self.memory: Dict[int, bytes] = {}
        self.erased: List[Tuple[int, int]] = []
        self._download: Optional[Dict] = None

    @staticmethod
    def _negative(sid: int, code: int) -> List[bytes]:
        return [bytes([NEGATIVE_RESPONSE, sid, code])]

    def handle(self, request: bytes) -> List[bytes]:
        self.requests.append(request)
        if not request:
            return []
        sid = request[0]
        handler = {
            0x10: self._session_control,
            0x11: self._ecu_reset,
            0x14: self._clear_dtcs,
            0x19: self._read_dtcs,
            0x22: self._read_did,
            0x27: self._security_access,
            0x2E: self._write_did,
            0x31: self._routine_control,
            0x34: self._request_download,
            0x36: self._transfer_data,
            0x37: self._request_transfer_exit,
            0x3E: self._tester_present,
        }.get(sid)
        if handler is None:
            return self._negative(sid, 0x11)
        responses = handler(request)
        suppress = len(request) >= 2 and sid in (0x10, 0x11, 0x3E) and request[1] & SUPPRESS_POSITIVE_RESPONSE
        positive = responses and responses[-1][0] == sid + 0x40
        return [] if suppress and positive else responses

    def _session_control(self, request: bytes) -> List[bytes]:
        if len(request) != 2:
            return self._negative(0x10, 0x13)
        session = request[1] & 0x7F
        if session not in (0x01, 0x02, 0x03):
            return self._negative(0x10, 0x12)
        self.session = session
        if session == 0x01:
            self.unlocked_level = None
        return [bytes([0x50, session, 0x00, 0x32, 0x01, 0xF4])]

    def _ecu_reset(self, request: bytes) -> List[bytes]:
        self.session, self.unlocked_level = 0x01, None
        return [bytes([0x51, request[1] & 0x7F])]

    def _tester_present(self, request: bytes) -> List[bytes]:
        self.tester_present_count += 1
        return [bytes([0x7E, request[1] & 0x7F])]

    def _read_did(self, request: bytes) -> List[bytes]:
        if len(request) != 3:
            return self._negative(0x22, 0x13)
        did = int.from_bytes(request[1:3], 'big')
        if did == 0xF186:
            return [bytes([0x62]) + request[1:3] + bytes([self.session])]
        if did not in self.dids:
            return self._negative(0x22, 0x31)
        return [bytes([0x62]) + request[1:3] + self.dids[did]]

    def _write_did(self, request: bytes) -> List[bytes]:
        if len(request) < 4:
            return self._negative(0x2E, 0x13)
        if self.unlocked_level is None:
            return self._negative(0x2E, 0x33)
        self.dids[int.from_bytes(request[1:3], 'big')] = bytes(request[3:])
        return [bytes([0x6E]) + request[1:3]]

    def _security_access(self, request: bytes) -> List[bytes]:
        if len(request) < 2:
            return self._negative(0x27, 0x13)
        level = request[1]
        if self.session == 0x01:
            return self._negative(0x27, 0x7F)
        if level % 2:
            self._pending_seed_level = level
            seed = bytes(len(self.seed)) if self.unlocked_level == level else self.seed
            return [bytes([0x67, level]) + seed]
        if self._pending_seed_level != level - 1:
            return self._negative(0x27, 0x24)
        self._pending_seed_level = None
        if bytes(request[2:]) != self.key_function(level - 1, self.seed):
            return self._negative(0x27, 0x35)
        self.unlocked_level = level - 1
        return [bytes([0x67, level])]

    def _read_dtcs(self, request: bytes) -> List[bytes]:
        if len(request) != 3 or request[1] != 0x02:
            return self._negative(0x19, 0x12)
        records = b''.join(
            code.to_bytes(3, 'big') + bytes([status]) for code, status in self.dtcs if status & request[2]
        )
        return [bytes([0x59, 0x02, 0xFF]) + records]

    def _clear_dtcs(self, request: bytes) -> List[bytes]:
        self.dtcs.clear()
        return [bytes([0x54])]

    def _routine_control(self, request: bytes) -> List[bytes]:
        if len(request) < 4:
            return self._negative(0x31, 0x13)
        routine = int.from_bytes(request[2:4], 'big')
        if routine == 0xFF00 and request[1] == 0x01 and len(request) > 5:  # eraseMemory
            address, size = self._memory_range(request[4:])
            if address is None:
                return self._negative(0x31, 0x13)
            self.erased.append((address, size))
        response = bytes([0x71]) + request[1:4] + bytes([0x00])
        if routine in self.slow_routines:
            return [bytes([NEGATIVE_RESPONSE, 0x31, RESPONSE_PENDING]), response]
        return [response]

    @staticmethod
    def _memory_range(data: bytes) -> Tuple[Optional[int], int]:
        """Address and size after an addressAndLengthFormatIdentifier."""
        address_bytes, size_bytes = data[0] & 0x0F, data[0] >> 4
        if not address_bytes or not size_bytes or len(data) != 1 + address_bytes + size_bytes:
            return None, 0
        address = int.from_bytes(data[1 : 1 + address_bytes], 'big')
        return address, int.from_bytes(data[1 + address_bytes :], 'big')

    def _request_download(self, request: bytes) -> List[bytes]:
        if len(request) < 5:
            return self._negative(0x34, 0x13)
        if self.session != 0x02:
            return self._negative(0x34, 0x7F)  # Service not supported in the active session
        if self.download_requires_security and self.unlocked_level is None:
            return self._negative(0x34, 0x33)
        if self._download is not None:
            return self._negative(0x34, 0x22)
        address, size = self._memory_range(request[2:])
        if address is None:
            return self._negative(0x34, 0x13)
        if request[1] != 0x00:
            return self._negative(0x34, 0x31)  # No compression or encryption
        self._download = {'address': address, 'size': size, 'data': bytearray(), 'counter': 1}
        return [bytes([0x74, 0x20]) + self.max_block_length.to_bytes(2, 'big')]

    def _transfer_data(self, request: bytes) -> List[bytes]:
        download = self._download
        if download is None:
            return self._negative(0x36, 0x24)
        if len(request) < 2 or len(request) > self.max_block_length:
            return self._negative(0x36, 0x13)
        if request[1] != download['counter']:
            if request[1] == (download['counter'] - 1) & 0xFF:
                return [bytes([0x76, request[1]])]  # Repeated block: acknowledged, not written again
            return self._negative(0x36, 0x73)
        if len(download['data']) + len(request) - 2 > download['size']:
            return self._negative(0x36, 0x71)
        download['data'] += request[2:]
        download['counter'] = (download['counter'] + 1) & 0xFF
        return [bytes([0x76, request[1]])]

    def _request_transfer_exit(self, request: bytes) -> List[bytes]:
        download = self._download
        if download is None or len(download['data']) != download['size']:
            return self._negative(0x37, 0x24)
        self.memory[download['address']] = bytes(download['data'])
        self._download = None
        return [bytes([0x77])]


class UdsEcuSimulator:
    """A UdsServer answering over ISO-TP (normal addressing) on a session."""

    def __init__(self, session: BusSession, request_id: int = 0x7E0, response_id: int = 0x7E8, server=None):
        self.server = server or UdsServer()
        self._client = IsoTpClient(session, response_id, request_id)
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name='UDS ECU simulator')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._client.close()

    async def _run(self) -> None:
        while True:
            try:
                request = await self._client.receive(3600)
            except IsoTpError:
                continue
            for index, response in enumerate(self.server.handle(request)):
                if index:
                    await asyncio.sleep(0.05)  # After a 'response pending'
                await self._client.send(response)


class LinSlaveSimulator:
    """LIN slaves: answer headers with the configured responses (bytes, or a callable returning bytes).

    With a UdsServer, diagnostic master requests addressed to `nad` are answered over the LIN transport
    layer: the response frames are returned for the following slave response headers.
    """

    def __init__(
        self,
        session: BusSession,
        responses: Optional[Dict[int, bytes | Callable[[], bytes]]] = None,
        nad: int = 0x01,
        server: Optional[UdsServer] = None,
    ):
        self.session = session
        self.responses = dict(responses or {})
        self.nad = nad
        self.server = server
        self.headers_seen = 0
        self._reassembler = LinTpReassembler()
        self._pending_frames: List[bytes] = []
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def _respond(self, frame_id: int, data: bytes) -> None:
        try:
            self.session.send(can.Message(arbitration_id=frame_id, is_extended_id=False, data=data))
        except SessionError:
            pass

    def _queue_response(self, payload: bytes) -> None:
        if len(payload) <= 6:
            frames = [bytes([self.nad, len(payload)]) + payload]
        else:
            frames = [bytes([self.nad, 0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:5]]
            sequence = 1
            for offset in range(5, len(payload), 6):
                frames.append(bytes([self.nad, 0x20 | sequence]) + payload[offset : offset + 6])
                sequence = (sequence + 1) & 0x0F
        self._pending_frames.extend(frame.ljust(8, b'\xff') for frame in frames)

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.is_extended_id:
            return
        frame_id = msg.arbitration_id
        if msg.is_remote_frame:
            self.headers_seen += 1
            if frame_id == SLAVE_RESPONSE_ID:
                if self._pending_frames:
                    self._respond(SLAVE_RESPONSE_ID, self._pending_frames.pop(0))
                return
            response = self.responses.get(frame_id)
            if response is not None:
                self._respond(frame_id, response() if callable(response) else response)
            return
        if frame_id == MASTER_REQUEST_ID and self.server is not None and msg.data and msg.data[0] == self.nad:
            _, payload = self._reassembler.feed(MASTER_REQUEST_ID, bytes(msg.data))
            if payload is not None:
                responses = self.server.handle(payload)
                self._pending_frames.clear()
                for response in responses:
                    self._queue_response(response)
