from typing import Optional

import can

from can_explorer.core.frames import format_hex

NMT_COMMANDS = {
    0x01: 'Start',
    0x02: 'Stop',
    0x80: 'Enter Pre-Operational',
    0x81: 'Reset Node',
    0x82: 'Reset Communication',
}
NMT_STATES = {0x00: 'Boot-up', 0x04: 'Stopped', 0x05: 'Operational', 0x7F: 'Pre-Operational'}
PDO_NAMES = {
    0x180: 'TPDO1',
    0x200: 'RPDO1',
    0x280: 'TPDO2',
    0x300: 'RPDO2',
    0x380: 'TPDO3',
    0x400: 'RPDO3',
    0x480: 'TPDO4',
    0x500: 'RPDO4',
}
SDO_CLIENT_COMMANDS = {
    0: 'download segment',
    1: 'write',
    2: 'read',
    3: 'upload segment',
    4: 'abort',
    5: 'block upload',
    6: 'block download',
}
SDO_SERVER_COMMANDS = {
    0: 'upload segment',
    1: 'download segment ack',
    2: 'read response',
    3: 'write ack',
    4: 'abort',
    5: 'block download',
    6: 'block upload',
}


class CanOpenDecoder:
    """Passive CANopen (CiA 301) decoder based on the predefined connection set."""

    name = 'CANopen'

    def reset(self) -> None:
        pass

    def describe(self, msg: can.Message) -> Optional[str]:
        if msg.is_extended_id:
            return None
        cob_id = msg.arbitration_id
        function, node = cob_id & 0x780, cob_id & 0x7F
        data = msg.data
        if cob_id == 0x000:
            if len(data) < 2:
                return 'NMT'
            command = NMT_COMMANDS.get(data[0], f'command {data[0]:#04x}')
            return f'NMT {command} -> {"all nodes" if data[1] == 0 else f"node {data[1]}"}'
        if cob_id == 0x080:
            return 'SYNC' + (f' counter={data[0]}' if data else '')
        if function == 0x080:
            if len(data) < 3:
                return f'EMCY node {node}'
            error_code = int.from_bytes(data[:2], 'little')
            return f'EMCY node {node}: error {error_code:#06x} register {data[2]:#04x}'
        if cob_id == 0x100:
            return 'TIME'
        if function in PDO_NAMES and node:
            return f'{PDO_NAMES[function]} node {node}: {format_hex(data)}'
        if function in (0x580, 0x600) and node:
            return self._sdo(function == 0x600, node, data)
        if function == 0x700 and node:
            if not data:
                return f'Node guarding request node {node}'
            return f'Heartbeat node {node}: {NMT_STATES.get(data[0] & 0x7F, f"state {data[0] & 0x7F:#04x}")}'
        return None

    @staticmethod
    def _sdo(is_request: bool, node: int, data: bytearray) -> str:
        direction = 'request' if is_request else 'response'
        if not data:
            return f'SDO {direction} node {node}'
        command_specifier = data[0] >> 5
        commands = SDO_CLIENT_COMMANDS if is_request else SDO_SERVER_COMMANDS
        command = commands.get(command_specifier, f'command {command_specifier}')
        text = f'SDO {direction} node {node}: {command}'
        has_index = command_specifier in ((1, 2, 4) if is_request else (2, 3, 4))
        if has_index and len(data) >= 4:
            text += f' 0x{int.from_bytes(data[1:3], "little"):04X}:{data[3]:02X}'
        if command_specifier == 4 and len(data) >= 8:
            text += f' code 0x{int.from_bytes(data[4:8], "little"):08X}'
        else:
            expedited = bool(data[0] & 0x02)
            carries_value = (command_specifier == 1) if is_request else (command_specifier == 2)
            if carries_value and expedited and len(data) >= 5:
                unused = (data[0] >> 2) & 0x3 if data[0] & 0x01 else 0
                text += f' = {format_hex(data[4 : 8 - unused])}'
        return text
