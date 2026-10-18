"""LIN (ISO 17987 / LIN 2.x) frame model and passive decoder.

LIN traffic is carried as CAN frames, following the convention of the Linux sllin driver
(linux-lin, CTU Prague), the de facto way to access LIN through SocketCAN:

- The CAN identifier is the LIN frame identifier (0-63), standard frame format.
- A remote (RTR) frame is a LIN header: sent, it makes the master transmit a header and wait for a
  slave response; received, it reports a header seen on the bus.
- A data frame is a LIN response: sent by the master it is transmitted as header plus response;
  received, it is the response that followed a header.
- Extended frames configure the driver (response cache) and are not LIN traffic.
"""

from typing import Dict, Optional, Tuple

import can

from can_explorer.core.frames import format_hex

MASTER_REQUEST_ID = 0x3C
SLAVE_RESPONSE_ID = 0x3D
MAX_FRAME_ID = 0x3F
LIN_BAUDRATES = (2400, 9600, 10417, 19200, 20000)
DEFAULT_BAUDRATE = 19200

# Bit times of a header (break >= 13 + delimiter + sync byte + protected identifier, nominal)
HEADER_BITS = 34
# Each response byte, including the checksum, takes 10 bit times (start + 8 data + stop)
BYTE_BITS = 10


def protected_id(frame_id: int) -> int:
    """Frame identifier with the two parity bits P0 and P1."""
    if not 0 <= frame_id <= MAX_FRAME_ID:
        raise ValueError(f'LIN frame identifier out of range: {frame_id}')
    bit = [(frame_id >> i) & 1 for i in range(6)]
    p0 = bit[0] ^ bit[1] ^ bit[2] ^ bit[4]
    p1 = 1 ^ (bit[1] ^ bit[3] ^ bit[4] ^ bit[5])
    return frame_id | (p0 << 6) | (p1 << 7)


def checksum(data: bytes, pid: Optional[int] = None) -> int:
    """Classic checksum (pid None, LIN 1.x and diagnostic frames) or enhanced checksum (LIN 2.x)."""
    total = pid or 0
    for byte in data:
        total += byte
        if total > 0xFF:
            total -= 0xFF
    return (~total) & 0xFF


def frame_duration(msg: can.Message, baudrate: int) -> float:
    """Approximate bus time of a LIN frame: header for RTR frames, response for received data frames,
    header plus response for transmitted data frames (the master sends both)."""
    if msg.is_remote_frame:
        bits = HEADER_BITS
    else:
        bits = BYTE_BITS * (len(msg.data) + 1)
        if not msg.is_rx:
            bits += HEADER_BITS
    return bits / baudrate


def is_lin_frame(msg: can.Message) -> bool:
    return not msg.is_extended_id and not msg.is_error_frame and msg.arbitration_id <= MAX_FRAME_ID


# LIN transport layer (ISO 17987-2) frames carried in the diagnostic frames 0x3C and 0x3D

NODE_CONFIGURATION_SERVICES = {
    0xB0: 'AssignNAD',
    0xB1: 'AssignFrameIdentifier',
    0xB2: 'ReadByIdentifier',
    0xB3: 'ConditionalChangeNAD',
    0xB4: 'DataDump',
    0xB5: 'AssignNAD via SNPD',
    0xB6: 'SaveConfiguration',
    0xB7: 'AssignFrameIdentifierRange',
}


class LinTpReassembler:
    """Reassembles LIN transport layer PDUs per (direction, NAD). Returns complete payloads."""

    def __init__(self):
        self._transfers: Dict[Tuple[int, int], Tuple[int, bytearray, int]] = {}

    def reset(self) -> None:
        self._transfers.clear()

    def feed(self, frame_id: int, data: bytes) -> Tuple[str, Optional[bytes]]:
        """Returns (description of the PDU, complete payload or None)."""
        if len(data) < 2:
            return 'invalid transport frame', None
        nad, pci = data[0], data[1]
        key = (frame_id, nad)
        kind = pci >> 4
        if nad == 0x00:
            return 'sleep command' if frame_id == MASTER_REQUEST_ID and data[1:2] == b'\xff' else 'NAD 0x00', None
        if kind == 0:
            length = pci & 0x0F
            if not 1 <= length <= 6 or len(data) < 2 + length:
                return f'NAD 0x{nad:02X} invalid single frame', None
            self._transfers.pop(key, None)
            return f'NAD 0x{nad:02X} SF', bytes(data[2 : 2 + length])
        if kind == 1:
            if len(data) < 3:
                return f'NAD 0x{nad:02X} invalid first frame', None
            length = ((pci & 0x0F) << 8) | data[2]
            self._transfers[key] = (length, bytearray(data[3:8]), 1)
            return f'NAD 0x{nad:02X} FF (total {length} B)', None
        if kind == 2:
            transfer = self._transfers.get(key)
            sequence = pci & 0x0F
            if transfer is None:
                return f'NAD 0x{nad:02X} CF {sequence} (no transfer)', None
            length, payload, expected = transfer
            if sequence != expected:
                del self._transfers[key]
                return f'NAD 0x{nad:02X} CF {sequence} (expected {expected}, aborted)', None
            payload.extend(data[2:8])
            if len(payload) >= length:
                del self._transfers[key]
                return f'NAD 0x{nad:02X} CF {sequence}', bytes(payload[:length])
            self._transfers[key] = (length, payload, (expected + 1) & 0x0F)
            return f'NAD 0x{nad:02X} CF {sequence} ({len(payload)}/{length} B)', None
        return f'NAD 0x{nad:02X} unknown PCI 0x{pci:02X}', None


def describe_diagnostic_payload(payload: bytes, request: bool) -> str:
    """Describe a LIN diagnostic payload: node configuration services or UDS."""
    from can_explorer.core.uds import describe_request, describe_response

    if not payload:
        return ''
    sid = (
        payload[0] if request else (payload[0] - 0x40 if payload[0] != 0x7F else payload[1] if len(payload) > 1 else 0)
    )
    if sid in NODE_CONFIGURATION_SERVICES and (request or payload[0] != 0x7F):
        name = NODE_CONFIGURATION_SERVICES[sid]
        return f'{name}{" response" if not request else ""}: {format_hex(payload[1:])}'.rstrip(': ')
    return describe_request(payload) if request else describe_response(payload)


class LinDecoder:
    """Passive LIN decoder: headers and responses with their protected identifier, diagnostic transport
    frames (0x3C master request / 0x3D slave response) with node configuration and UDS services."""

    name = 'LIN'

    def __init__(self):
        self._transport = LinTpReassembler()

    def reset(self) -> None:
        self._transport.reset()

    def describe(self, msg: can.Message) -> Optional[str]:
        if not is_lin_frame(msg):
            return None
        frame_id = msg.arbitration_id
        pid = protected_id(frame_id)
        if msg.is_remote_frame:
            return f'LIN header ID 0x{frame_id:02X} (PID 0x{pid:02X})'
        data = bytes(msg.data)
        if frame_id in (MASTER_REQUEST_ID, SLAVE_RESPONSE_ID):
            request = frame_id == MASTER_REQUEST_ID
            pdu, payload = self._transport.feed(frame_id, data)
            text = f'LIN {"master request" if request else "slave response"} {pdu}'
            if payload is not None:
                text += f': {describe_diagnostic_payload(payload, request)}'
            return text
        # The checksum is checked by the LIN driver and not passed on, so it is not shown
        return f'LIN response ID 0x{frame_id:02X} (PID 0x{pid:02X})'
