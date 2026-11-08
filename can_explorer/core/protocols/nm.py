"""Passive AUTOSAR Network Management (NM) decoder: the messages ECUs exchange to keep a bus awake, coordinate
going to sleep, and request partial networks. The same PDU is used by CAN-NM (ISO 11898-based) and UDP-NM (over
automotive Ethernet), so `parse`/`describe` work on the raw bytes of either.

An NM PDU carries, in its default layout, a Source Node Identifier (byte 0), a Control Bit Vector (byte 1) and
then user data or — when the Partial Network Information bit is set — a bitmask of the partial-network clusters
(PNC) being requested awake. NM frames are identified by their CAN identifier (project specific, usually a
dedicated NM range), so this is a passive decoder you point at NM traffic; the byte positions are configurable for
projects that place the node id or control vector elsewhere.

OSEK/VDX direct NM (the older token-ring scheme) is a different protocol and is not decoded here.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import can

# Control Bit Vector flags (AUTOSAR SWS CAN NM / UDP NM)
CBV_REPEAT_MESSAGE = 0x01  # Repeat Message Request: stay in / enter the Repeat Message state
CBV_SLEEP_READY = 0x08  # NM Coordinator: this node is ready to go to sleep
CBV_ACTIVE_WAKEUP = 0x10  # This node actively woke the network (vs. passively joining)
CBV_PN_LEARNING = 0x40  # Partial Network Learning Bit Request
CBV_PN_INFO = 0x80  # Partial Network Information: bytes after the header carry the requested PNC bitmask


@dataclass(frozen=True, slots=True)
class NmPdu:
    source_node: int
    control: int
    user_data: bytes
    pn_requests: Tuple[int, ...] = ()  # Requested partial-network cluster indices (when the PN info bit is set)

    @property
    def repeat_message(self) -> bool:
        return bool(self.control & CBV_REPEAT_MESSAGE)

    @property
    def sleep_ready(self) -> bool:
        return bool(self.control & CBV_SLEEP_READY)

    @property
    def active_wakeup(self) -> bool:
        return bool(self.control & CBV_ACTIVE_WAKEUP)

    @property
    def pn_learning(self) -> bool:
        return bool(self.control & CBV_PN_LEARNING)

    @property
    def partial_network_info(self) -> bool:
        return bool(self.control & CBV_PN_INFO)


def parse(data: bytes, source_pos: int = 0, control_pos: int = 1, pn_offset: int = 2) -> Optional[NmPdu]:
    """Decode an NM PDU. Returns None when the data is too short for the node id and control vector."""
    if len(data) <= max(source_pos, control_pos):
        return None
    control = data[control_pos]
    pn_requests: Tuple[int, ...] = ()
    if control & CBV_PN_INFO:
        pn_requests = tuple(
            index * 8 + bit for index, byte in enumerate(data[pn_offset:]) for bit in range(8) if byte & (1 << bit)
        )
    return NmPdu(data[source_pos], control, bytes(data[pn_offset:]), pn_requests)


def describe(data: bytes, source_pos: int = 0, control_pos: int = 1, pn_offset: int = 2) -> Optional[str]:
    pdu = parse(data, source_pos, control_pos, pn_offset)
    if pdu is None:
        return None
    flags = []
    if pdu.repeat_message:
        flags.append('RepeatMessage')
    if pdu.active_wakeup:
        flags.append('ActiveWakeup')
    if pdu.sleep_ready:
        flags.append('SleepReady')
    if pdu.pn_learning:
        flags.append('PN-Learning')
    text = f'NM node 0x{pdu.source_node:02X}'
    if flags:
        text += ' [' + ', '.join(flags) + ']'
    if pdu.partial_network_info:
        text += ' PN requests ' + (', '.join(f'#{pnc}' for pnc in pdu.pn_requests) if pdu.pn_requests else 'none')
    elif pdu.user_data:
        text += f', data {pdu.user_data.hex(" ").upper()}'
    return text


class NmDecoder:
    """Passive AUTOSAR NM decoder. Point it at the NM frames of a bus; the node-id and control-vector positions are
    configurable for projects that differ from the default byte 0 / byte 1 layout."""

    name = 'NM'

    def __init__(self, source_pos: int = 0, control_pos: int = 1, pn_offset: int = 2):
        self._source_pos, self._control_pos, self._pn_offset = source_pos, control_pos, pn_offset

    def reset(self) -> None:
        pass

    def describe(self, msg: can.Message) -> Optional[str]:
        data = bytes(msg.data)
        return describe(data, self._source_pos, self._control_pos, self._pn_offset) if data else None
