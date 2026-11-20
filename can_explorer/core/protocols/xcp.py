"""Passive XCP-on-CAN and CCP decoders: label the commands, responses, errors, events and DAQ data of a
measurement and calibration session in the trace.

XCP and CCP have no fixed identifiers, so a decoder either gets the command/response identifier pair (`configure`,
done by the XCP/CCP page when it connects) or learns it from the session start: an XCP CONNECT (FF 00/01) or CCP
CONNECT (01 <counter>) answered by a positive response on another identifier. Until then only CONNECT candidates
are labelled, with a question mark.

DAQ data (DTOs) is labelled with its packet id only: which values it carries is known to the master that
configured the DAQ lists, not to a passive observer. XCP on Ethernet is not CAN traffic and is not decoded here.
"""

from typing import Optional, Tuple

import can

from can_explorer.core import ccp as ccp_protocol
from can_explorer.core import xcp_protocol as x


def _hex(data: bytes) -> str:
    return ' '.join(f'{b:02X}' for b in data)


class _PairDecoder:
    name = ''

    def __init__(self, command_id: Optional[int] = None, response_id: Optional[int] = None, extended: bool = False):
        self._configured: Optional[Tuple[int, int, bool]] = None
        if command_id is not None and response_id is not None:
            self._configured = (command_id, response_id, extended)
        self.reset()

    def configure(self, command_id: int, response_id: int, extended: bool = False) -> None:
        self._configured = (command_id, response_id, extended)
        self.pair = self._configured

    def reset(self) -> None:
        self.pair: Optional[Tuple[int, int, bool]] = self._configured
        self._candidate: Optional[Tuple[int, bool, bytes]] = None
        self._last_command: Optional[bytes] = None

    def describe(self, msg: can.Message) -> Optional[str]:
        if msg.is_error_frame or msg.is_remote_frame or not msg.data:
            return None
        data = bytes(msg.data)
        if self.pair is None:
            return self._learn(msg, data)
        command_id, response_id, extended = self.pair
        if msg.is_extended_id != extended:
            return None
        if msg.arbitration_id == command_id:
            if self.is_command(data):  # Not a STIM DTO, which is not answered
                self._last_command = data
            return f'{self.name} {self.command(data)}'
        if msg.arbitration_id == response_id:
            return f'{self.name} {self.response(data)}'
        return None

    def _learn(self, msg: can.Message, data: bytes) -> Optional[str]:
        candidate = self._candidate
        if candidate is not None and msg.arbitration_id != candidate[0] and msg.is_extended_id == candidate[1]:
            if self.confirms(candidate[2], data):
                self.pair = (candidate[0], msg.arbitration_id, candidate[1])
                self._last_command = candidate[2]
                return f'{self.name} {self.response(data)}'
        if self.is_connect(data):
            self._candidate = (msg.arbitration_id, msg.is_extended_id, data)
            return f'{self.name}? {self.command(data)}'
        return None

    # Protocol specific

    def is_command(self, data: bytes) -> bool:
        """Whether a packet from the master expects a response."""
        return True

    def is_connect(self, data: bytes) -> bool:
        raise NotImplementedError

    def confirms(self, connect: bytes, response: bytes) -> bool:
        raise NotImplementedError

    def command(self, data: bytes) -> str:
        raise NotImplementedError

    def response(self, data: bytes) -> str:
        raise NotImplementedError


class XcpDecoder(_PairDecoder):
    """XCP on CAN: commands (CTO), RES/ERR responses, events, service requests and DAQ DTOs."""

    name = 'XCP'

    def reset(self) -> None:
        super().reset()
        self.byte_order = 'little'

    def is_command(self, data: bytes) -> bool:
        return data[0] >= 0xC0  # Below are STIM DTOs (an ODT number)

    def is_connect(self, data: bytes) -> bool:
        return len(data) >= 2 and data[0] == x.CONNECT and data[1] in (0, 1)

    def confirms(self, connect: bytes, response: bytes) -> bool:
        # A CONNECT response: RES, resources, COMM_MODE_BASIC, MAX_CTO >= 8
        return len(response) >= 8 and response[0] == x.RES and response[3] >= 8

    def _address(self, data: bytes) -> str:
        return f'0x{int.from_bytes(data[:4], self.byte_order):08X}'

    def command(self, data: bytes) -> str:
        code = data[0]
        if not self.is_command(data):
            return f'STIM PID {code}: {_hex(data[1:])}'
        name = x.COMMAND_NAMES.get(code)
        if name is None:
            return f'command 0x{code:02X} {_hex(data[1:])}'
        if code in (x.PROGRAM, x.PROGRAM_NEXT) and len(data) >= 2:
            if code == x.PROGRAM and data[1] == 0:
                return 'PROGRAM end of sequence'
            return f'{name} {data[1]}: {_hex(data[2 : 2 + data[1]])}'
        if code == x.PROGRAM_CLEAR and len(data) >= 8:
            value = int.from_bytes(data[4:8], self.byte_order)
            if data[1] == x.CLEAR_FUNCTIONAL:
                areas = [name for bit, name in x.CLEAR_AREAS.items() if value & bit]
                user = [f'user 0x{value & ~0xFF:X}'] if value & ~0xFF else []
                return f'PROGRAM_CLEAR functional: {", ".join(areas + user) or "none"}'
            return f'PROGRAM_CLEAR {value} absolute'
        if code == x.PROGRAM_PREPARE and len(data) >= 4:
            return f'PROGRAM_PREPARE {int.from_bytes(data[2:4], self.byte_order)} elements'
        if code == x.PROGRAM_FORMAT and len(data) >= 5:
            return (
                f'PROGRAM_FORMAT compression 0x{data[1]:02X} encryption 0x{data[2]:02X} '
                f'programming 0x{data[3]:02X} access 0x{data[4]:02X}'
            )
        if code == x.PROGRAM_MAX:
            return f'PROGRAM_MAX: {_hex(data[1:])}'
        if code == x.PROGRAM_VERIFY and len(data) >= 8:
            mode = {x.VERIFY_INTERNAL: 'internal', x.VERIFY_VALUE: 'value'}.get(data[1], f'mode {data[1]}')
            kind = int.from_bytes(data[2:4], self.byte_order)
            value = int.from_bytes(data[4:8], self.byte_order)
            return f'PROGRAM_VERIFY {mode} type 0x{kind:04X}' + (f' value 0x{value:08X}' if data[1] else '')
        if code == x.GET_SECTOR_INFO and len(data) >= 3:
            what = {x.SECTOR_START: 'start', x.SECTOR_LENGTH: 'length', x.SECTOR_NAME_LENGTH: 'name'}
            return f'GET_SECTOR_INFO sector {data[2]} {what.get(data[1], f"mode {data[1]}")}'
        if code in (x.CLEAR_DAQ_LIST, x.GET_DAQ_LIST_INFO) and len(data) >= 4:
            return f'{name} list {int.from_bytes(data[2:4], self.byte_order)}'
        if code == x.SET_DAQ_LIST_MODE and len(data) >= 6:
            direction = 'STIM' if data[1] & x.DAQ_MODE_DIRECTION_STIM else 'DAQ'
            number, event = (int.from_bytes(data[i : i + 2], self.byte_order) for i in (2, 4))
            return f'SET_DAQ_LIST_MODE list {number} {direction} event {event} mode 0x{data[1]:02X}'
        if code == x.CONNECT:
            return f'CONNECT mode {data[1] if len(data) > 1 else 0}'
        if code == x.SET_MTA and len(data) >= 8:
            return f'SET_MTA {self._address(data[4:8])} ext {data[3]}'
        if code == x.SHORT_UPLOAD and len(data) >= 8:
            return f'SHORT_UPLOAD {data[1]} @ {self._address(data[4:8])} ext {data[3]}'
        if code == x.UPLOAD and len(data) >= 2:
            return f'UPLOAD {data[1]}'
        if code in (x.DOWNLOAD, x.DOWNLOAD_NEXT) and len(data) >= 2:
            return f'{name} {data[1]}: {_hex(data[2 : 2 + data[1]])}'
        if code == x.GET_SEED and len(data) >= 3:
            return f'GET_SEED {"first" if data[1] == 0 else "next"} part, {x.resource_names(data[2])}'
        if code == x.UNLOCK and len(data) >= 2:
            return f'UNLOCK {data[1]} bytes left: {_hex(data[2:])}'
        if code == x.BUILD_CHECKSUM and len(data) >= 8:
            return f'BUILD_CHECKSUM {int.from_bytes(data[4:8], self.byte_order)} elements'
        if code in (x.SET_CAL_PAGE, x.GET_CAL_PAGE) and len(data) >= 3:
            page = f' page {data[3]}' if code == x.SET_CAL_PAGE and len(data) > 3 else ''
            return f'{name} segment {data[2]}{page} mode 0x{data[1]:02X}'
        if code == x.WRITE_DAQ and len(data) >= 8:
            return f'WRITE_DAQ {data[2]} @ {self._address(data[4:8])} ext {data[3]}'
        if code == x.START_STOP_SYNCH and len(data) >= 2:
            return f'START_STOP_SYNCH {["stop all", "start selected", "stop selected"][min(data[1], 2)]}'
        if code == x.START_STOP_DAQ_LIST and len(data) >= 4:
            mode = ['stop', 'start', 'select'][min(data[1], 2)]
            return f'START_STOP_DAQ_LIST {mode} list {int.from_bytes(data[2:4], self.byte_order)}'
        return name if len(data) == 1 or not any(data[1:]) else f'{name} {_hex(data[1:])}'

    def response(self, data: bytes) -> str:
        pid = data[0]
        if pid == x.ERR:
            code = data[1] if len(data) > 1 else 0
            return f'ERR {x.ERROR_CODES.get(code, f"0x{code:02X}")}'
        if pid == x.EV:
            code = data[1] if len(data) > 1 else 0
            return f'EV {x.EVENT_CODES.get(code, f"0x{code:02X}")}'
        if pid == x.SERV:
            return f'SERV {_hex(data[1:])}'
        if pid != x.RES:
            return f'DAQ PID {pid}: {_hex(data[1:])}'
        last = self._last_command[0] if self._last_command else None
        if last == x.CONNECT and len(data) >= 8:
            self.byte_order = 'big' if data[2] & x.COMM_BYTE_ORDER_MOTOROLA else 'little'
            granularity = x.granularity_size((data[2] & x.COMM_ADDRESS_GRANULARITY) >> 1)
            return (
                f'RES CONNECT: {x.resource_names(data[1])}, {self.byte_order} endian, granularity {granularity}, '
                f'MAX_CTO {data[3]}, MAX_DTO {int.from_bytes(data[4:6], self.byte_order)}'
            )
        if last == x.GET_SEED and len(data) >= 2:
            return f'RES seed ({data[1]} bytes left): {_hex(data[2:])}'
        if last == x.UNLOCK and len(data) >= 2:
            return f'RES unlocked, still protected: {x.resource_names(data[1])}'
        if last in (x.UPLOAD, x.SHORT_UPLOAD) and self._last_command is not None:
            count = self._last_command[1] if len(self._last_command) > 1 else 0
            return f'RES data: {_hex(data[1 : 1 + count])}'
        name = x.COMMAND_NAMES.get(last, '') if last is not None else ''
        rest = data[1:].rstrip(b'\x00')
        return f'RES {name}'.rstrip() + (f': {_hex(data[1:])}' if rest else '')


class CcpDecoder(_PairDecoder):
    """CCP: CROs with their command counter, CRMs, event messages and DAQ DTOs."""

    name = 'CCP'

    def is_connect(self, data: bytes) -> bool:
        return len(data) >= 4 and data[0] == ccp_protocol.CONNECT

    def confirms(self, connect: bytes, response: bytes) -> bool:
        return len(response) >= 3 and response[0] == ccp_protocol.CRM and response[2] == connect[1]

    def command(self, data: bytes) -> str:
        code = data[0]
        name = ccp_protocol.COMMAND_NAMES.get(code, f'command 0x{code:02X}')
        counter = data[1] if len(data) > 1 else 0
        params = data[2:]
        if code == ccp_protocol.CONNECT and len(params) >= 2:
            detail = f' station 0x{int.from_bytes(params[:2], "little"):04X}'
        elif code == ccp_protocol.SET_MTA and len(params) >= 6:
            detail = f' MTA{params[0]} 0x{int.from_bytes(params[2:6], "big"):08X} ext {params[1]}'
        elif code == ccp_protocol.SHORT_UP and len(params) >= 6:
            detail = f' {params[0]} @ 0x{int.from_bytes(params[2:6], "big"):08X} ext {params[1]}'
        elif code in (ccp_protocol.UPLOAD, ccp_protocol.DNLOAD) and params:
            detail = f' {params[0]}' + (f': {_hex(params[1 : 1 + params[0]])}' if code == ccp_protocol.DNLOAD else '')
        elif code == ccp_protocol.DNLOAD_6:
            detail = f': {_hex(params[:6])}'
        elif code == ccp_protocol.GET_SEED and params:
            detail = ' ' + ', '.join(n for b, n in ccp_protocol.RESOURCES.items() if params[0] & b)
        else:
            detail = f' {_hex(params)}' if any(params) else ''
        return f'{name} #{counter}{detail}'

    def response(self, data: bytes) -> str:
        pid = data[0]
        if pid == ccp_protocol.CRM and len(data) >= 3:
            code = data[1]
            status = 'ok' if code == 0 else ccp_protocol.RETURN_CODES.get(code, f'error 0x{code:02X}')
            return f'CRM #{data[2]} {status}' + (f': {_hex(data[3:])}' if code == 0 and any(data[3:]) else '')
        if pid == ccp_protocol.EVENT:
            code = data[1] if len(data) > 1 else 0
            return f'event {ccp_protocol.RETURN_CODES.get(code, f"0x{code:02X}")}'
        return f'DAQ PID {pid}: {_hex(data[1:])}'
